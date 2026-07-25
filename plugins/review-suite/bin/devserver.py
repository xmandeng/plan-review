"""Devserver for the review-suite plugin.

Serves generated review/architecture/map HTML over HTTP, bridges a browser
xterm.js terminal to a local `claude --resume <session-id>` PTY via WebSocket,
and accepts PUT uploads of `*-layouts.json` files so the "saved named layouts"
feature of the architecture / map templates can persist to disk next to the HTML.

Shared by all skills in the bundle (plan-review, design-review,
architecture-map, code-diagram, devserver). One binary, one protocol.

Usage:
    python3 devserver.py [port]          # default port: 8765

Serves the current working directory (project root; skills launch this without
`cd`ing first so the PTY bridge spawns `claude --resume <sid>` in the same
project the transcript was recorded under).

Endpoints:
    GET  /                            — static file serving (SimpleHTTPRequestHandler)
    PUT  /*-layouts.json              — atomic write of layouts JSON, scoped to spawn cwd
    WS   /api/claude?session=<id>     — bridges browser xterm.js to `claude --resume <id>` PTY
"""

import base64
import fcntl
import fnmatch
import hashlib
import json
import os
import pty
import re
import shlex
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


# =============================================================================
# PUT handler helpers — the architecture template persists named layouts via
# `fetch(LAYOUTS_FILE, { method: 'PUT', body: JSON.stringify(layouts) })`.
# We scope PUT writes to `*-layouts.json` under the spawn cwd and cap body size
# so an opportunistic request can't clobber arbitrary files or balloon disk.
# =============================================================================

LAYOUTS_MAX_BYTES = 256 * 1024


def resolve_safe_layouts_target(raw_path: str, spawn_cwd: str) -> Path | None:
    """Return the absolute target path for a PUT to /*-layouts.json, or None if rejected.

    Rejects paths that don't match `*-layouts.json`, empty paths, and any path
    that escapes `spawn_cwd` after resolution (via `..`, absolute paths, or
    symlinks).
    """
    rel = raw_path.split("?", 1)[0].split("#", 1)[0].lstrip("/")
    if not rel or not fnmatch.fnmatch(os.path.basename(rel), "*-layouts.json"):
        return None
    base = Path(spawn_cwd).resolve()
    candidate = (base / rel).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate


# =============================================================================
# Playground session bridge — each playground HTML embeds a CLAUDE_SESSION (the
# authoring session). On first WS connect the bridge forks a live `claude` child
# from it and keeps that child alive across reloads, keyed by the playground HTML
# path. Continuity across browser reloads is that live process, and a cold start
# (first open, devserver restart, idle reap) re-forks from CLAUDE_SESSION rather
# than resuming the previous fork. The fork does write its own transcript, so the
# conversation held in a playground stays recoverable from a terminal by its own
# session id, which is what the handoff button hands over.
# =============================================================================

# HTML basename suffixes the three review-suite skills produce. We accept the
# WS playground path only if its basename matches one of these — same
# defensive posture as resolve_safe_layouts_target.
_VALID_PLAYGROUND_SUFFIXES = (
    "-design-review.html",
    "-architecture-map.html",
    "-review.html",
)


def resolve_safe_html_target(playground_rel_path: str, spawn_cwd: str) -> Path | None:
    """Return the absolute path to a playground HTML, or None if rejected.

    Same containment contract as resolve_safe_layouts_target: rejects empty
    paths, paths whose basename doesn't match a known review-suite HTML
    suffix, and any path that escapes spawn_cwd after resolution. Returns
    None on any rejection — the caller treats None as "no sticky-session
    support for this connection" and falls back to the pre-QUE-226
    always-fork path.
    """
    rel = playground_rel_path.split("?", 1)[0].split("#", 1)[0].lstrip("/")
    if not rel:
        return None
    base = Path(spawn_cwd).resolve()
    try:
        candidate = (base / rel).resolve()
        candidate.relative_to(base)
    except (ValueError, OSError):
        return None
    if not any(candidate.name.endswith(s) for s in _VALID_PLAYGROUND_SUFFIXES):
        return None
    if not candidate.is_file():
        return None
    return candidate


def transcript_exists(sid: str, cwd: str) -> bool:
    """True if ``claude --resume <sid>`` would find a transcript for this project.

    Claude records per-project transcripts at
    ``~/.claude/projects/<slug>/<sid>.jsonl``, where ``<slug>`` is the project
    cwd with path separators and dots flattened to dashes. A forked session
    that never received any input, or an archived / garbage id, has no such
    file — resuming it dies with "No conversation found". The bridge checks
    this first so it can fall back to a working session rather than hand the
    user a dead terminal.
    """
    if not sid:
        return False
    slug = re.sub(r"[/.]", "-", cwd)
    return (Path.home() / ".claude" / "projects" / slug / f"{sid}.jsonl").is_file()


# =============================================================================
# WebSocket framing (RFC 6455) — minimal text/binary support for the
# /api/claude PTY bridge. Hand-rolled to avoid pulling websockets/asyncio into
# the otherwise-sync http.server.
# =============================================================================

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def ws_recv_exactly(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def ws_read_frame(sock: socket.socket) -> tuple[int, bytes] | None:
    """Read one WebSocket frame. Returns (opcode, payload) or None on close/error."""
    header = ws_recv_exactly(sock, 2)
    if header is None:
        return None
    byte1, byte2 = header[0], header[1]
    opcode = byte1 & 0x0F
    masked = (byte2 & 0x80) != 0
    length = byte2 & 0x7F
    if length == 126:
        ext = ws_recv_exactly(sock, 2)
        if ext is None:
            return None
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = ws_recv_exactly(sock, 8)
        if ext is None:
            return None
        length = struct.unpack("!Q", ext)[0]
    mask_key = b""
    if masked:
        mk = ws_recv_exactly(sock, 4)
        if mk is None:
            return None
        mask_key = mk
    payload = ws_recv_exactly(sock, length) if length else b""
    if payload is None:
        return None
    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return (opcode, payload)


def ws_send_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    """Send one unfragmented frame from server (no masking, FIN=1)."""
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < (1 << 16):
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))
    sock.sendall(bytes(header) + payload)


def resolve_lan_ip() -> str:
    """Return the host's primary LAN IPv4, or 'localhost' if unresolvable."""
    override = os.environ.get("REVIEW_SUITE_HOST")
    if override:
        return override
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip if ip and not ip.startswith("127.") else "localhost"
    except OSError:
        return "localhost"


class _StdlibPty:
    """Minimal ptyprocess.PtyProcess-compatible PTY using stdlib only.

    Used as a fallback when `ptyprocess` is not installed. Exposes the same
    `.fd`, `.setwinsize()`, `.isalive()`, `.terminate()` interface so the
    bridge code below works against either backend transparently.
    """

    def __init__(self, argv, cwd=None, env=None, dimensions=(40, 120)):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            # Child: replace process with the target command
            try:
                if cwd:
                    os.chdir(cwd)
            except OSError:
                pass
            try:
                os.execvpe(argv[0], argv, env or os.environ.copy())
            except OSError:
                os._exit(127)
        # Parent: best-effort initial window size
        try:
            self.setwinsize(*dimensions)
        except OSError:
            pass

    def setwinsize(self, rows: int, cols: int) -> None:
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def isalive(self) -> bool:
        try:
            result_pid, _ = os.waitpid(self.pid, os.WNOHANG)
            return result_pid == 0
        except ChildProcessError:
            return False

    def terminate(self, force: bool = False) -> None:
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.kill(self.pid, sig)
        except (ProcessLookupError, OSError):
            return
        try:
            os.waitpid(self.pid, 0)
        except (ChildProcessError, OSError):
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


def _pty_spawn(argv, cwd, env, dimensions=(40, 120)):
    """Spawn a PTY child. Prefers `ptyprocess`, falls back to stdlib.

    Returns an object exposing `.fd`, `.setwinsize(rows, cols)`, `.isalive()`,
    and `.terminate(force=False)` — compatible with both `ptyprocess.PtyProcess`
    and the in-house `_StdlibPty` fallback.
    """
    try:
        import ptyprocess  # type: ignore[import-untyped]
        return ptyprocess.PtyProcess.spawn(  # type: ignore[no-any-return]
            argv, cwd=cwd, env=env, dimensions=dimensions
        )
    except ImportError:
        return _StdlibPty(argv, cwd=cwd, env=env, dimensions=dimensions)


# =============================================================================
# Persistent PTY sessions
# =============================================================================
# Keep one live `claude` child per playground, across WebSocket reconnects, so a
# browser reload reattaches to the SAME conversation instead of killing it and
# starting over. Reattach is by live process (held here) rather than by
# `claude --resume`, which would replay a transcript instead of rejoining the
# running conversation.

_SESSION_BUFFER_MAX = 256 * 1024      # replay buffer kept per session (bytes)
_SESSION_IDLE_REAP_SECONDS = 3600     # reap a client-less session after this long

_sessions: dict[str, "PtySession"] = {}
_sessions_lock = threading.Lock()
_reaper_started = False


class PtySession:
    """A live `claude` PTY retained across WebSocket reconnections.

    A single reader thread drains the PTY for the life of the child, mirroring
    output into a bounded replay buffer and to whichever WebSocket is currently
    attached. Reload swaps the attached socket without disturbing the child; the
    replay buffer repaints the new connection so the conversation looks
    continuous. When the child exits, the reader drops the session from the
    registry and reaps the process.
    """

    def __init__(self, key: str, proc: object, active_sid: str, spawn_mode: str) -> None:
        self.key = key
        self.proc = proc
        self.fd = proc.fd  # type: ignore[attr-defined]
        self.active_sid = active_sid
        self.spawn_mode = spawn_mode
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._ws: socket.socket | None = None
        self.last_detach = time.monotonic()
        self._reader = threading.Thread(
            target=self._drain_pty, name=f"pty-reader[{active_sid[:8]}]", daemon=True
        )
        self._reader.start()

    def _drain_pty(self) -> None:
        while True:
            try:
                data = os.read(self.fd, 4096)
            except OSError:
                break
            if not data:
                break
            with self._lock:
                self._buf.extend(data)
                excess = len(self._buf) - _SESSION_BUFFER_MAX
                if excess > 0:
                    del self._buf[:excess]
                ws = self._ws
            if ws is not None:
                try:
                    ws_send_frame(ws, OP_BINARY, data)
                except OSError:
                    pass
        # Child exited: drop the session and reap so the next open cold-starts.
        with _sessions_lock:
            if _sessions.get(self.key) is self:
                del _sessions[self.key]
        self.terminate()

    def attach(self, sock: socket.socket) -> None:
        """Bind a WebSocket, evict any prior one, and replay recent output.

        The replay is flushed while holding the lock and before ``_ws`` is
        repointed, so live output can never interleave ahead of the buffered
        scrollback on the freshly connected page.
        """
        with self._lock:
            old = self._ws
            replay = bytes(self._buf)
            if replay:
                try:
                    ws_send_frame(sock, OP_BINARY, replay)
                except OSError:
                    pass
            self._ws = sock
        if old is not None and old is not sock:
            try:
                old.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def detach(self, sock: socket.socket) -> None:
        with self._lock:
            if self._ws is sock:
                self._ws = None
                self.last_detach = time.monotonic()

    def write(self, data: bytes) -> None:
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def setwinsize(self, rows: int, cols: int) -> None:
        try:
            self.proc.setwinsize(rows, cols)  # type: ignore[attr-defined]
        except (OSError, ValueError):
            pass

    def alive(self) -> bool:
        try:
            return bool(self.proc.isalive())  # type: ignore[attr-defined]
        except Exception:
            return False

    def is_idle(self, now: float, ttl: float) -> bool:
        with self._lock:
            return self._ws is None and now - self.last_detach > ttl

    def terminate(self) -> None:
        try:
            if self.proc.isalive():  # type: ignore[attr-defined]
                self.proc.terminate(force=True)  # type: ignore[attr-defined]
        except Exception:
            pass


def _reap_idle_sessions() -> None:
    """Terminate sessions with no attached client past the idle TTL."""
    while True:
        time.sleep(60)
        now = time.monotonic()
        with _sessions_lock:
            stale = [
                (k, s)
                for k, s in list(_sessions.items())
                if s.is_idle(now, _SESSION_IDLE_REAP_SECONDS)
            ]
            for k, _ in stale:
                _sessions.pop(k, None)
        for _, s in stale:
            s.terminate()


def _ensure_reaper_running() -> None:
    global _reaper_started
    if _reaper_started:
        return
    _reaper_started = True
    threading.Thread(target=_reap_idle_sessions, name="pty-reaper", daemon=True).start()


def _cold_start_spawn(
    sock: socket.socket,
    cwd: str,
    session_id: str,
) -> tuple[list[str], str, str] | None:
    """Decide the argv for a brand-new playground process: always fork.

    The live child held in ``_sessions`` is what carries the conversation across
    browser reloads. A cold start -- first open, devserver restart, or a reaped
    idle session -- re-forks from the authoring session to re-inherit the plan
    context, rather than resuming whatever fork ran before it. Re-forking keeps a
    cold start deterministic: it always begins from the plan as authored, not from
    wherever a previous playground conversation happened to end up.

    The authoring session is resumable (a normal, non-forked session flushes
    incrementally), so we fork from it and only bail with an error frame if even
    that has no transcript on disk.

    Returns ``(args, active_sid, spawn_mode)``, or ``None`` after sending an
    error frame.
    """
    if not transcript_exists(session_id, cwd):
        try:
            ws_send_frame(
                sock,
                OP_TEXT,
                json.dumps(
                    {
                        "error": (
                            f"cannot start playground: authoring session {session_id} "
                            "has no resumable transcript. Regenerate the review from a "
                            "live session so a valid session id is baked in."
                        )
                    }
                ).encode(),
            )
            ws_send_frame(sock, OP_CLOSE, b"")
        except OSError:
            pass
        return None
    new_sid = str(uuid.uuid4())
    return (
        ["claude", "--resume", session_id, "--fork-session", "--session-id", new_sid],
        new_sid,
        "fork",
    )


def _run_ws_input(sock: socket.socket, sess: "PtySession") -> None:
    """Pump client->PTY input for one WebSocket; returns when the WS closes.

    The PTY->client direction is handled by the session's own reader thread, so
    this loop only forwards keystrokes, resize, and ping. It does not own the
    process lifecycle -- returning here just detaches this connection.
    """
    while True:
        frame = ws_read_frame(sock)
        if frame is None:
            break
        opcode, payload = frame
        if opcode == OP_CLOSE:
            break
        if opcode == OP_PING:
            try:
                ws_send_frame(sock, OP_PONG, payload)
            except OSError:
                break
            continue
        if opcode == OP_TEXT:
            try:
                msg = json.loads(payload.decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(msg, dict) and msg.get("type") == "resize":
                try:
                    sess.setwinsize(int(msg["rows"]), int(msg["cols"]))
                except (KeyError, ValueError):
                    pass
            continue
        if opcode == OP_BINARY:
            sess.write(payload)


def bridge_ws_to_claude_pty(
    sock: socket.socket,
    cwd: str,
    session_id: str,
    playground_html: Path | None = None,
) -> None:
    """Bridge a browser WebSocket to the playground's live `claude` PTY.

    Persistent-session behavior: one `claude` child is kept alive per
    playground (keyed by its HTML path) for the life of the devserver, in
    ``_sessions``. A browser reload drops the WebSocket but NOT the child --
    the next connection re-binds to the same process and replays the recent
    output buffer, so the conversation survives reloads unbroken. The child is
    forked from the authoring session only on the FIRST open (to inherit the
    plan context); reloads never re-fork or `--resume`.

    Wire protocol:
      - BINARY frames in both directions carry raw PTY bytes (terminal I/O).
      - TEXT frames carry JSON control messages from the client. Currently
        only ``{"type":"resize","rows":N,"cols":N}`` is recognised.
      - Server sends one TEXT frame on connect with the active session info:
        ``{"type":"active_session","sid":"<sid>","mode":"attach"|"fork"}``.

    Unix-only (Windows would need `pywinpty`; out of scope).
    """
    if not session_id:
        try:
            ws_send_frame(
                sock,
                OP_TEXT,
                json.dumps(
                    {"error": "WS /api/claude requires ?session=<id> query parameter"}
                ).encode(),
            )
            ws_send_frame(sock, OP_CLOSE, b"")
        except OSError:
            pass
        return

    key = str(playground_html) if playground_html is not None else f"session:{session_id}"

    with _sessions_lock:
        sess = _sessions.get(key)
        if sess is not None and not sess.alive():
            _sessions.pop(key, None)
            sess = None
        reused = sess is not None
        if sess is None:
            # Cold start: no live process for this playground yet. Decide how to
            # spawn one, then keep it alive across reconnects.
            spawn = _cold_start_spawn(sock, cwd, session_id)
            if spawn is None:
                return  # error frame already sent
            args, active_sid, spawn_mode = spawn
            env = os.environ.copy()
            env.setdefault("TERM", "xterm-256color")
            # A claude process that sees CLAUDE_CODE_CHILD_SESSION in its
            # environment disables transcript saving for itself and prints a
            # banner saying so. The devserver inherits that marker whenever it
            # was launched from inside a Claude Code tool call, and passing it
            # down would leave the playground conversation unwritten to disk.
            # Drop it so the child flushes a transcript like any other session.
            env.pop("CLAUDE_CODE_CHILD_SESSION", None)
            try:
                proc = _pty_spawn(args, cwd=cwd, env=env)
            except Exception as exc:
                try:
                    ws_send_frame(
                        sock,
                        OP_TEXT,
                        json.dumps({"error": f"failed to spawn claude: {exc}"}).encode(),
                    )
                    ws_send_frame(sock, OP_CLOSE, b"")
                except OSError:
                    pass
                return
            sess = PtySession(key, proc, active_sid, spawn_mode)
            _sessions[key] = sess
            _ensure_reaper_running()

    # Announce the active session so the UI can show the right SID. A reused
    # session is reported as "attach" -- the client treats it as a reconnect.
    # `handoff` is the resumable id the terminal-handoff button must copy. The
    # forked child flushes its own transcript, so handing off its id resumes the
    # conversation held in the playground rather than restarting from the plan as
    # the authoring session was left. Fall back to the authoring session if the
    # fork has not written a transcript yet, which is the case before its first
    # turn completes.
    handoff_sid = sess.active_sid if transcript_exists(sess.active_sid, cwd) else session_id
    try:
        ws_send_frame(
            sock,
            OP_TEXT,
            json.dumps(
                {
                    "type": "active_session",
                    "sid": sess.active_sid,
                    "mode": "attach" if reused else sess.spawn_mode,
                    "handoff": handoff_sid,
                }
            ).encode(),
        )
    except OSError:
        pass

    # Bind this WebSocket to the live process (evicting any prior one) and
    # replay recent output so a reloaded page repaints the conversation.
    sess.attach(sock)
    try:
        _run_ws_input(sock, sess)
    finally:
        # A dropped WebSocket (reload, tab close) detaches but DOES NOT kill the
        # child -- that is the whole point. The process lingers for the next
        # connection; the idle reaper collects it if no one returns.
        sess.detach(sock)
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class DevHandler(SimpleHTTPRequestHandler):
    """Extends SimpleHTTPRequestHandler with a WebSocket endpoint for the PTY bridge."""

    spawn_cwd: str = os.getcwd()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/claude" and (
            self.headers.get("Upgrade", "").lower() == "websocket"
        ):
            qs = parse_qs(parsed.query)
            session_values = qs.get("session", [])
            session_id = session_values[0] if session_values else ""
            # Optional `playground` param identifies which playground HTML
            # triggered this WS. The devserver uses it to key the live held
            # `claude` child so reloads of the same page reattach to the same
            # process. Absent or unrecognized → keyed by session id instead.
            playground_values = qs.get("playground", [])
            playground_path = playground_values[0] if playground_values else ""
            self.handle_claude_upgrade(session_id, playground_path)
            return
        super().do_GET()

    def handle_claude_upgrade(self, session_id: str, playground_path: str = "") -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        version = self.headers.get("Sec-WebSocket-Version", "")
        if not key or version != "13":
            self.send_error(400, "Bad WebSocket upgrade")
            return
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()
        ).decode()
        self.close_connection = True
        self.wfile.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                "\r\n"
            ).encode()
        )
        self.wfile.flush()
        playground_html = (
            resolve_safe_html_target(playground_path, self.spawn_cwd)
            if playground_path
            else None
        )
        try:
            bridge_ws_to_claude_pty(
                self.connection, self.spawn_cwd, session_id, playground_html
            )
        except Exception as exc:
            self.log_message("WS /api/claude bridge error: %s", exc)

    def do_PUT(self) -> None:
        """Accept `PUT /*-layouts.json` to persist named layouts next to the HTML.

        Narrowly scoped: only filenames matching `*-layouts.json` within the
        spawn cwd are accepted. Everything else is a 403. Body must be JSON and
        within `LAYOUTS_MAX_BYTES`. Writes atomically via tmp + rename.
        """
        parsed = urlparse(self.path)
        target = resolve_safe_layouts_target(parsed.path, self.spawn_cwd)
        if target is None:
            self.send_error(403, "PUT only permitted on *-layouts.json under server root")
            return
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("application/json"):
            self.send_error(415, "Content-Type must be application/json")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400, "Invalid Content-Length")
            return
        if length <= 0:
            self.send_error(400, "Empty body")
            return
        if length > LAYOUTS_MAX_BYTES:
            self.send_error(413, f"Body exceeds {LAYOUTS_MAX_BYTES} bytes")
            return
        body = self.rfile.read(length)
        try:
            json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_error(400, "Body is not valid JSON")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            tmp.write_bytes(body)
            os.replace(tmp, target)
        except OSError as exc:
            self.send_error(500, f"Write failed: {exc}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return
        self.send_response(204)
        self.end_headers()

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        # Quieter logging — skip 200/304 GETs
        if len(args) >= 2 and str(args[1]) in ("200", "304"):
            return
        super().log_message(format, *args)


# =============================================================================
# Project-scoped discovery (TT-150) — devserver instances are intrinsically
# tied to their spawn cwd (static root, PUT containment base, PTY-bridge
# `claude --resume` cwd). When the skill kickoff runs from project A, we must
# only reuse devservers whose `/proc/<pid>/cwd` resolves to project A's root.
# Sharing across projects would serve the wrong files and would attach the
# PTY bridge to the wrong transcript.
# =============================================================================

PORT_SCAN_RANGE = range(8765, 8800)
SPAWN_PORT_WAIT_TIMEOUT = 5.0
SPAWN_PORT_WAIT_INTERVAL = 0.05


def lsof_listening_pids(port: int) -> list[int]:
    """Return PIDs holding a LISTEN socket on `port`, or [] if none / lsof missing."""
    try:
        out = subprocess.run(
            ["lsof", "-t", "-i", f":{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def pid_cwd(pid: int) -> Path | None:
    """Return realpath of /proc/<pid>/cwd, or None if not introspectable."""
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd")).resolve()
    except OSError:
        return None


def devserver_on_port_matches_cwd(port: int, project_root: Path) -> bool:
    """True iff any LISTEN-er on `port` has cwd resolving to `project_root`."""
    for pid in lsof_listening_pids(port):
        cwd = pid_cwd(pid)
        if cwd is not None and cwd == project_root:
            return True
    return False


def pgrep_review_suite_devservers() -> list[int]:
    """Return PIDs whose command line matches `review-suite.*devserver\\.py`."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", r"review-suite.*devserver\.py"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def socket_inodes_for_pid(pid: int) -> set[str]:
    """Return the set of socket inodes referenced by `pid`'s open fds.

    Each fd in `/proc/<pid>/fd/` whose readlink starts with `socket:[<inode>]`
    contributes its inode. Returns empty set on any access error (pid gone,
    permission denied, etc.).
    """
    inodes: set[str] = set()
    try:
        entries = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return inodes
    for entry in entries:
        try:
            target = os.readlink(f"/proc/{pid}/fd/{entry}")
        except OSError:
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            inodes.add(target[len("socket:[") : -1])
    return inodes


def listening_port_for_pid(pid: int) -> int | None:
    """Return the first TCP LISTEN port held by `pid`, or None.

    Pure-`/proc` implementation — avoids `lsof -p <pid>` which can take many
    seconds on hosts with filesystem mounts that stall lsof's initial walk
    (CIFS, FUSE, Docker overlays). Reads `/proc/<pid>/fd/*` for socket
    inodes, then matches them against LISTEN rows in `/proc/net/tcp` and
    `/proc/net/tcp6`. State `0A` is TCP_LISTEN.

    Columns in /proc/net/tcp:
        sl  local_address  rem  st  tx/rx  tr/tm  retrnsmt  uid  timeout  inode  ...
        0   1              2    3   4      5      6         7    8        9
    `local_address` is `<hex-addr>:<hex-port>`; we want the hex port.
    """
    inodes = socket_inodes_for_pid(pid)
    if not inodes:
        return None
    for proc_path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(proc_path) as fh:
                next(fh, None)  # header
                for line in fh:
                    parts = line.split()
                    if len(parts) < 10:
                        continue
                    if parts[3] != "0A":  # TCP_LISTEN
                        continue
                    if parts[9] not in inodes:
                        continue
                    local = parts[1]
                    colon = local.rfind(":")
                    if colon < 0:
                        continue
                    try:
                        return int(local[colon + 1 :], 16)
                    except ValueError:
                        continue
        except OSError:
            continue
    return None


def find_review_suite_devserver_for_cwd(project_root: Path) -> int | None:
    """Return a LISTEN port for a review-suite devserver whose cwd matches.

    Filters pgrep matches by `/proc/<pid>/cwd == project_root`. Returns the
    first matching pid's listening port, or None if no match.
    """
    for pid in pgrep_review_suite_devservers():
        cwd = pid_cwd(pid)
        if cwd is None or cwd != project_root:
            continue
        port = listening_port_for_pid(pid)
        if port is not None:
            return port
    return None


def port_is_free(port: int) -> bool:
    """True iff `port` is not currently held by any LISTEN socket."""
    return not lsof_listening_pids(port)


def pick_free_port(requested: int | None) -> int:
    """Resolve a port to bind. Honors `requested` if free, otherwise scans 8765-8799.

    Raises RuntimeError on a requested-port collision or if the scan exhausts.
    """
    if requested is not None:
        if not port_is_free(requested):
            raise RuntimeError(f"port {requested} is already in use")
        return requested
    for candidate in PORT_SCAN_RANGE:
        if port_is_free(candidate):
            return candidate
    raise RuntimeError(f"no free port in {PORT_SCAN_RANGE.start}-{PORT_SCAN_RANGE.stop - 1}")


def wait_for_listen(port: int, timeout: float = SPAWN_PORT_WAIT_TIMEOUT) -> bool:
    """Poll until `port` has a LISTEN-er, or `timeout` elapses. True on success."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lsof_listening_pids(port):
            return True
        time.sleep(SPAWN_PORT_WAIT_INTERVAL)
    return False


def spawn_background_devserver(port: int, project_root: Path) -> subprocess.Popen[bytes]:
    """Spawn `python3 devserver.py <port>` detached, with stdio captured to a log.

    `project_root` is the cwd passed to the child — the devserver reads it
    via os.getcwd() at startup and uses it as the static root, PUT containment
    base, and PTY-bridge spawn cwd. start_new_session detaches the child so
    it survives the find-or-start parent exit.
    """
    log_dir = project_root / ".plan-review"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / ".devserver.log"
    # Open in append mode so successive spawns accumulate. The log file is
    # not part of the public contract; it exists for human debugging.
    log_fh = log_path.open("ab")
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), str(port)],
        cwd=str(project_root),
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
    )


def write_port_file(project_root: Path, port: int) -> None:
    """Persist the active port under `<project_root>/.plan-review/.devserver-port`."""
    port_dir = project_root / ".plan-review"
    port_dir.mkdir(parents=True, exist_ok=True)
    (port_dir / ".devserver-port").write_text(f"{port}\n")


def read_port_file(project_root: Path) -> int | None:
    """Return the port recorded under `.plan-review/.devserver-port`, or None."""
    port_file = project_root / ".plan-review" / ".devserver-port"
    try:
        raw = port_file.read_text().strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def emit_kv(url: str, port: int, lan_ip: str) -> None:
    """Print the find-or-start key=value envelope expected by SKILL.md kickoffs.

    Three single-quoted shell-safe assignments so SKILL.md can `eval` the
    output. `shlex.quote` defends against pathological LAN-IP values (e.g. a
    REVIEW_SUITE_HOST override containing whitespace) — the port is an int
    so it's always safe, but quoting all three keeps the contract uniform.
    """
    print(f"URL={shlex.quote(url)}")
    print(f"PORT={shlex.quote(str(port))}")
    print(f"LAN_IP={shlex.quote(lan_ip)}")


def find_or_start(port_arg: int | None) -> int:
    """Subcommand entry: discover or spawn a project-scoped devserver, print URL=/PORT=/LAN_IP=.

    Returns the process exit code (0 on success, non-zero on failure).

    Order of operations:
      1. `<project_root>/.plan-review/.devserver-port` → verify the listener's
         cwd matches project_root. Reuse if it does.
      2. If no port arg was given, scan `pgrep -f "review-suite.*devserver\\.py"`
         matches and reuse the first whose cwd matches project_root.
      3. Otherwise spawn a fresh devserver in project_root, write the port file,
         and emit the URL once the port is listening.

    An explicit `port_arg` skips the pgrep step (the user asked for that specific
    port). It still consults the port file fast path because if a server is
    already running on the requested port AND it belongs to this project, we
    should reuse rather than fail.
    """
    project_root = Path.cwd().resolve()
    lan_ip = resolve_lan_ip()

    # Step 1: port-file fast path. Reuse only if the listener is ours.
    saved = read_port_file(project_root)
    if saved is not None and devserver_on_port_matches_cwd(saved, project_root):
        if port_arg is None or port_arg == saved:
            emit_kv(f"http://{lan_ip}:{saved}/", saved, lan_ip)
            return 0

    # Step 2: pgrep fallback, scoped to this project's cwd. Skipped if the
    # user gave an explicit port arg — they want that port specifically, not
    # whatever happens to be running.
    if port_arg is None:
        found = find_review_suite_devserver_for_cwd(project_root)
        if found is not None:
            write_port_file(project_root, found)
            emit_kv(f"http://{lan_ip}:{found}/", found, lan_ip)
            return 0

    # Step 3: spawn fresh.
    try:
        port = pick_free_port(port_arg)
    except RuntimeError as exc:
        print(f"devserver find-or-start: {exc}", file=sys.stderr)
        return 1
    spawn_background_devserver(port, project_root)
    if not wait_for_listen(port):
        print(
            f"devserver find-or-start: spawned child on port {port} did not "
            "begin listening within timeout; check .plan-review/.devserver.log",
            file=sys.stderr,
        )
        return 1
    write_port_file(project_root, port)
    emit_kv(f"http://{lan_ip}:{port}/", port, lan_ip)
    return 0


def main() -> None:
    # Subcommand dispatch: `find-or-start [PORT]` runs the project-scoped
    # discovery logic and exits. Everything else falls through to the legacy
    # foreground server mode for backward compatibility (skills that haven't
    # been updated yet, direct `python3 devserver.py 8765` invocations).
    if len(sys.argv) > 1 and sys.argv[1] == "find-or-start":
        port_arg: int | None = None
        if len(sys.argv) > 2 and sys.argv[2]:
            try:
                port_arg = int(sys.argv[2])
            except ValueError:
                print(
                    f"devserver find-or-start: invalid port arg {sys.argv[2]!r}",
                    file=sys.stderr,
                )
                sys.exit(2)
        sys.exit(find_or_start(port_arg))

    port = int(os.environ.get("REVIEW_SUITE_PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8765))

    # Spawn cwd = current working directory at startup. The skill invokes the
    # devserver from the user's project root (no `cd` first), so this is
    # naturally the right place for `claude --resume <sid>` to find the transcript.
    DevHandler.spawn_cwd = os.getcwd()

    class ReusableThreadingHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = True
        allow_reuse_port = True

        def server_bind(self) -> None:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass
            super().server_bind()

    server = ReusableThreadingHTTPServer(("0.0.0.0", port), DevHandler)
    lan_ip = resolve_lan_ip()
    print(f"review-suite devserver: http://{lan_ip}:{port}/")
    print(f"Serving: {Path.cwd()}")
    print(f"PTY bridge spawns `claude --resume <sid>` from: {DevHandler.spawn_cwd}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
