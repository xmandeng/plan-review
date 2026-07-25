---
name: architecture-map
description: Create an interactive architecture concept-map playground for any application, seeded from chat context. Generates a draggable node graph with layered filters, per-node insights, saved named layouts, per-node feedback pins, and a Send-to-Claude button that drives a live Claude Code session via an embedded terminal. Usage - /architecture-map [<ticket>]
allowed-tools: Read Write Edit Bash(mkdir:*) Bash(cp:*) Bash(python3:*) Bash(ls:*) Bash(cat:*) Bash(echo:*) Bash(eval:*)
argument-hint: "[<ticket>]"
---

# Architecture Map Skill

Creates an interactive architecture concept-map playground from a bundled template, pre-populated with nodes, edges, layers, and insights that the main agent derives from conversation context. The reviewer attaches per-node approve / revise / question marks plus free-text comments. Clicking "Send to Claude" delivers a structured feedback bundle directly into an embedded Claude Code terminal (PTY-bridged via the bundled devserver).

Positioned alongside its sibling plugins:

| Plugin | Output | When to use |
|---|---|---|
| `plan-review` | Section-by-section plan review | Approving an implementation plan |
| `design-review` | Before/after diagram for a proposed change | Reviewing a design diff |
| **`architecture-map`** | **Single-view** concept map of a system | Mapping an existing application end-to-end |

## Invocation Forms

| Invocation | Behavior |
|---|---|
| `/architecture-map` | Infer both ticket ID and scope from recent conversation context. Generate without prompting if context is clear; ask only when context is thin. |
| `/architecture-map <ticket>` | User supplies the ticket ID (any tracker format). Infer scope from conversation; ask if unclear. |

A two-arg form is **not** supported — keep the signature minimal, same as `plan-review` and `design-review`.

## How It Works

1. Check for a prior map matching the ticket. If found, offer **Resume** / **Overwrite** / **Cancel**.
2. Read the bundled template: `${CLAUDE_PLUGIN_ROOT}/assets/map-template.html`
3. Populate the `nodes` and `edges` arrays with the system under discussion.
4. Set the page title, heading, and JS constants (`PLAN_NAME`, `CLAUDE_SESSION`, `LAYOUTS_FILE`, `SCOPE_HEADER`).
5. Write the output HTML to the resolved output directory.
6. Start (or reuse) the bundled devserver via `${CLAUDE_PLUGIN_ROOT}/bin/devserver.py find-or-start` — project-scoped: one devserver per project root on a free port in 8765-8799.
7. Return the LAN-IP URL.

On **Resume** the flow short-circuits: hydrate the prior node/edge arrays into the agent's context, rewrite only the `CLAUDE_SESSION` constant in the existing HTML, then jump to step 6.

### Output Directory Resolution

1. **`ARCHITECTURE_MAP_DIR` env var** (if set) — explicit override, absolute or project-relative.
2. **`.architecture-map/`** — default, auto-created via `mkdir -p` if missing.

Kept distinct from `.plan-review/` and `.design-review/` so artifacts don't collide when multiple plugins run in one project.

## Instructions

When invoked:

1. **Parse arguments.**
   - Zero args: infer ticket + scope from conversation context. Generate without prompting if the conversation clearly identifies the application and scope.
   - One arg: treat as ticket ID. Accept any tracker format. Infer scope from conversation; ask only if ambiguous.
   - Free-form slugs passed as a single arg are **not** ticket IDs — treat as a title hint or ask the user to clarify.

2. **Resolve output directory.** Use `$ARCHITECTURE_MAP_DIR` if set, else `.architecture-map/`. Ensure it exists (`mkdir -p`).

2a. **Detect prior map for this ticket.** Before reading the template or constructing a new filename, glob the output directory for an existing HTML matching the ticket:

   ```bash
   shopt -s nullglob
   PRIOR=( "$OUT_DIR"/"$TICKET"-*-architecture-map.html )
   shopt -u nullglob
   ```

   Glob rather than exact match so the check still catches the prior file when the title has drifted. Skip this step entirely if no ticket was supplied — titles alone are too noisy to match prior runs reliably.

   - **Zero matches** → continue to step 3 (write-new flow unchanged).
   - **One match** → ask the user:

     > Found a prior architecture map for `<ticket>` at `<path>` (modified `<mtime>`).
     >
     > - **Resume** — keep the nodes/edges/insights and your prior review marks; refresh the embedded session id so the terminal bridge works.
     > - **Overwrite** — replace with a freshly generated map from the current conversation. Prior review marks for this filename remain in the browser's localStorage.
     > - **Cancel** — do nothing.

     On **Resume**, jump to "Resume: Hydrate and Refresh" below. On **Overwrite**, continue to step 3. On **Cancel**, return without writing or starting the devserver.

   - **Multiple matches** → list each prior file with path + mtime; ask the user to choose which to resume, or Overwrite / Cancel.

3. **Construct filename.** `<output-dir>/<ticket>-<slugified-scope>-architecture-map.html` (lowercase, hyphens). If no ticket, use `<slugified-scope>-architecture-map.html`.

### Recommended path: render via the generator (steps 4–8 in one command)

The preferred way to produce the HTML is **not** to inline-patch the template by hand. Build a JSON spec describing the map and pipe it through the bundled generator. The generator uses `json.dumps` for all data injection (guaranteeing correct JS escaping), validates the inline JS via `node --check`, and refuses to leave a broken file in place.

```bash
cat <<'JSON' | python3 "${CLAUDE_PLUGIN_ROOT}/bin/render-review-html.py"
{
  "kind": "architecture-map",
  "ticket": "QUE-123",
  "title": "<map title>",
  "session_id": "<sid from review-suite-session-id hook>",
  "output_path": ".architecture-map/QUE-123-<slug>-architecture-map.html",
  "layouts_file": "QUE-123-<slug>-layouts.json",
  "scope_header": "<free-form description shown above the map>",
  "before_nodes": [],
  "before_edges": [],
  "after_nodes":  [ { "id": "...", "x": 100, "y": 100, "layer": "...", "label": "...", "type": "...", "desc": "..." } ],
  "after_edges":  [ { "from": "a", "to": "b" } ]
}
```

(Architecture-map uses a single-pane view: put everything in `after_nodes`/`after_edges` and leave the `before_*` arrays empty.)

On success the generator prints the absolute output path on stdout. On a validator failure (broken inline JS) it exits non-zero and removes the partial file — you will not accidentally serve a blank page.

**Skip steps 4–8 below entirely if you use the generator.** Jump to step 9 (start the devserver).

### Fallback path: inline-patch the template

If the generator is unavailable (no `python3`, restricted environment, etc.), do the patching manually. Read the [Common pitfalls](#common-pitfalls-when-inline-patching) section first — this is where blank pages come from.

4. **Read the template.** `${CLAUDE_PLUGIN_ROOT}/assets/map-template.html`.

5. **Ask the main agent for node + edge data.** Node schema (mirrors the source `architecture_playground.html`):

   | Property | Required | Description |
   |---|---|---|
   | `id` | Yes | Unique identifier (used in edges). |
   | `x`, `y` | Yes | Initial position on canvas (user can drag to rearrange). |
   | `layer` | Yes | Layer name (free-form string; layer pill is auto-derived). Keep names stable across a map. |
   | `label` | Yes | Component name displayed on the node. |
   | `type` | Yes | Subtitle text (e.g., "Async Manager", "asyncio.Queue[dict]"). |
   | `file` | No | Source file path (shown in detail panel). |
   | `desc` | Yes | One-line description (shown in tooltip + detail panel). |
   | `details` | No | Multi-line body for the detail panel (`\n` separators). |
   | `code` | No | Code snippet shown in the detail panel (`\n` for newlines). |
   | `badge` | No | Pill text below label. |
   | `note` | No | Warning/callout text. |
   | `insights` | No | Array of `{author, text}` pairs — design rationale and implementation notes. `author` is a free-form string; the template picks a color per unique author. |
   | `connections` | Yes | Array of node ids this node connects to (used to highlight related nodes in the detail panel). Can be empty. |

   Edge schema:

   | Property | Required | Description |
   |---|---|---|
   | `from` | Yes | Source node id. |
   | `to` | Yes | Target node id. |
   | `label` | No | Edge label text. |
   | `style` | No | `dashed` for out-of-band flows (callbacks, async tasks); default is a solid arrow. |

5a. **Read the authoring session id.** Look for the `review-suite-session-id: <sid>` line in your own context — it is injected by the plugin's `UserPromptSubmit` hook on every turn. Extract `<sid>` for step 7. If absent, the hook did not fire (plugin not installed, or first turn of a malformed setup) — surface the error to the user rather than generating an unusable HTML.

6. **Replace the data arrays** (`nodes` and `edges`) in the template with the provided data.

7. **Update identifiers** in the HTML:
   - `<title>` tag → `Architecture Map: <ticket>: <scope>` (or `Architecture Map: <scope>` if no ticket)
   - Topbar `<h1>` → `<ticket> <scope>` (or `<scope>`)
   - `PLAN_NAME` JS constant → `<ticket>: <scope>` (or `<scope>`)
   - `CLAUDE_SESSION` JS constant → the session id from step 5a
   - `LAYOUTS_FILE` JS constant → `<ticket>-<slug>-layouts.json` (relative to the HTML; devserver scopes PUT access to `*-layouts.json` under cwd)
   - `SCOPE_HEADER` JS constant → one-line description of the scope the map captures (e.g., "Full ingestion pipeline", "Auth service only", "Event bus and direct consumers"). Shown as a sub-header in the topbar so reviewers know what was mapped.

8. **Write the file** to the resolved output directory.

9. **Start (or reuse) the devserver.** Project-scoped: the devserver self-discovers an existing instance whose `/proc/<pid>/cwd` matches the current project root, or spawns a new one in this project's cwd. Two projects on the same host get distinct devservers on distinct ports; same-project repeat invocations reuse the existing one.

   ```bash
   eval "$(python3 "${CLAUDE_PLUGIN_ROOT}/bin/devserver.py" find-or-start)"
   # $URL, $PORT, $LAN_IP are now set
   ```

10. **Return the URL.** Format: `http://<lan-ip>:$PORT/<output-dir-relative-to-cwd>/<filename>.html` (e.g., `http://192.168.1.237:8785/.architecture-map/TT-134-ingestion-architecture-map.html`).

## Resume: Hydrate and Refresh

When the user chooses **Resume** at step 2a (or picks a specific file in the multi-match case):

1. **Hydrate context.** Read the prior HTML. Extract the `nodes` and `edges` literals and internalize them. Surface a short summary:

   > Resumed architecture map: N nodes across L layers, M edges. Picking up where you left off — where do you want to focus?

2. **Refresh the session id.** Rewrite **only** the `CLAUDE_SESSION = "..."` JS constant. Do not touch `nodes`, `edges`, `PLAN_NAME`, `LAYOUTS_FILE`, `SCOPE_HEADER`, `<title>`, or `<h1>`.

3. **Validate.** If the `CLAUDE_SESSION` constant can't be found, surface the error and ask whether to regenerate. Never silently overwrite.

4. **Start (or reuse) the devserver** per step 9.

5. **Return the URL** per step 10.

`PLAN_NAME` is intentionally **not** rewritten on resume — the browser's `localStorage` keys are `map-state:<PLAN_NAME>` / `map-layout:<PLAN_NAME>` / `map-autosave:<PLAN_NAME>`, and preserving them is what keeps the reviewer's prior node feedback + saved layouts + in-progress drag positions attached to the restored file.

## Authoring Guidelines

### Scoping the map

Lean on the conversation to decide what to include. Common scope shapes:

- **End-to-end application** — every subsystem, from external inputs through persistence. Default when the user asks to "map the application".
- **One subsystem** — e.g., "auth service only", "event bus and its consumers", "ingestion path only". Draw the subsystem's inputs and outputs as boundary nodes but don't expand them.
- **Cross-cutting concern** — e.g., "how config flows", "how errors propagate". Nodes are ordered around the concern rather than by data flow.

Set `SCOPE_HEADER` to a one-line summary so reviewers can see at a glance what the map does and doesn't cover.

### Node layout

Arrange nodes as a **top-down tree**, not a circular web:

1. **Source nodes at the top** — data origins (APIs, external services, users) get the smallest `y` values.
2. **Processing nodes in the middle** — handlers, routers, services that transform or route data.
3. **Sink nodes at the bottom** — databases, message buses, terminal outputs get the largest `y` values.
4. **Fan-out horizontally** — when a node writes to multiple sinks, spread them across the `x` axis on the same row.
5. **Center the primary flow** — the main path runs down the center; secondary paths branch left/right.

Typical spacing: ~170px vertical gap between tiers, ~200px horizontal gap between siblings.

### Layers

`layer` is free-form. Use whatever names fit the system — `websocket`, `queue`, `routing`, `processor`, `persistence`, `signal`, `config`, `http`, `auth`, `ui`, etc. The template auto-derives layer pills from the unique layer names in the data and assigns a color per layer. Keep layer names stable within a map (don't mix "ws" and "websocket" for the same tier).

Aim for 3–7 distinct layers per map — fewer feels flat, more overloads the pill bar.

### Insights

The `insights` array is where the map earns its keep as a *concept* map, not just a block diagram. Each insight is `{author, text}`:

- `author` — use the codebase author's name for decisions baked into the code (e.g., `"xmandeng"`), and `"claude"` (or the current AI author) for analysis-derived observations. The template colors each unique author consistently.
- `text` — 1–3 sentences. Capture the *why*, not the *what*. Examples:
  - "Singleton is intentional — multiple connections would duplicate market data and waste bandwidth."
  - "This is the hottest path in the system — every market event passes through here."
  - "The 30s heartbeat gives a comfortable 2× safety margin over DXLink's ~60s idle timeout."

Nodes with insights get a cyan dot in the top-right corner. Nodes without are fine too — not every box needs a paragraph.

## Handling Review Feedback

When the reviewer sends feedback via the "Send to Claude" button, a structured bundle arrives in the embedded terminal:

```
Here is my architecture-map review of <ticket>: <scope>:

## Nodes flagged for revision (N)
### <label> — <layer>
File: <path>
Comment: ...

## Questions (N)
...

## Approved (N)
...
```

When you receive the bundle:

1. **Parse** the sections (revision / question / approved).
2. **For revision items:** discuss the concern conversationally. Do NOT edit the HTML until the reviewer explicitly says to update the map.
3. **When discussion on a node wraps,** ask whether to update the document (tweak the node metadata, move it to a different layer, update insights, etc.). Edits should be targeted replacements of the specific node object in the template's `nodes` array.
4. **For approved items:** note the approval; no action required unless the reviewer asks.

## Session Context Preamble

On the first Send-to-Claude click per browser session, the template prepends a one-time preamble:

> **Context switch:** you are now in the architecture-map playground. The map is at `<path>`. Discuss the feedback below conversationally. Do NOT edit the HTML or the layouts JSON until I explicitly say to update. When discussion on a node wraps, ask whether to update the document.

State is tracked via `sessionStorage` keyed off the map doc's filename.

## Session Resume

The devserver's PTY bridge forks a live `claude` child from `CLAUDE_SESSION` (the authoring session) on the first WebSocket connect, and keeps that child alive for the playground's lifetime, keyed by the HTML path. A browser refresh, "Send to Claude" click, or next-day return re-binds to the **same live process** and replays its recent output — context survives reloads without resuming anything from disk.

The forked child writes its own transcript, so the conversation held in a playground stays recoverable from a terminal by the forked session id — that is the id the handoff button copies. The bridge itself does not resume it. If the live child is gone — devserver restarted, or the session reaped after a long idle — the next open **re-forks from `CLAUDE_SESSION`**, which restarts from the plan as authored rather than from wherever the playground conversation ended. The authoring session must itself be resumable; if its transcript is missing, the bridge surfaces an error asking you to regenerate the review from a live session. There is no `ACTIVE_SESSION` constant — the fork id changes on every cold start, so nothing is baked back into the HTML.

A `claude` process that inherits the `CLAUDE_CODE_CHILD_SESSION` environment marker disables transcript saving for itself and prints a banner saying so. The devserver strips that marker before forking, so starting the devserver from inside a Claude Code tool call does not silently leave the playground conversation unwritten.

### Handoff

Each generated map includes a "Hand off to terminal" button that copies `claude --resume <sid>` to the clipboard and sends Ctrl+D to the embedded Claude child so the session is released. Paste the copied command in any local terminal to resume from there.

## Prerequisites

- Python 3.10+ on the user's machine
- `claude` CLI available in PATH
- `ptyprocess` (optional) — used by the devserver if installed; otherwise falls back to stdlib `pty.fork()`

## Environment Variable Reference

| Variable | Default | Purpose |
|---|---|---|
| `ARCHITECTURE_MAP_DIR` | `.architecture-map/` | Where generated map HTML files are written |
| `ARCHITECTURE_MAP_HOST` | auto-detected LAN IP | Override the host in the returned URL |
| `ARCHITECTURE_MAP_PORT` | `8785` | Preferred devserver port (scan starts here) |

## Common pitfalls when inline-patching

These traps are why `bin/render-review-html.py` exists. If you used the generator (recommended path above), you don't need to worry about them. If you're inline-patching the template by hand, read carefully.

1. **`re.sub` interprets backslash escapes in the replacement string.** Don't do `re.sub(pat, replacement_string, text)` when the replacement contains `\n`, `\1`, `\g<...>`, etc. — `re.sub` processes those as escape sequences and rewrites your output. Use a lambda replacement (`re.sub(pat, lambda m: replacement, text)`) or plain `str.replace()` instead.

2. **Single-quoted JS strings cannot span lines.** Multi-line `code:` or `details:` fields must use backtick template literals or `\n` escape sequences inside a single-quoted string — never a real newline inside a single-quoted JS string.

3. **Always validate inline JS before returning the URL.** Extract the `const BEFORE_NODES = ...;`, `const AFTER_NODES = ...;` (etc.) declarations and pipe through `node --check`. If parsing fails, the page renders blank.

4. **`$CLAUDE_JOB_DIR` is harness-managed and may be unset.** Wrap any usage with a fallback: `SCRATCH="${CLAUDE_JOB_DIR:-$(mktemp -d -t claude-job-XXXXXX)}"`.

## Saved Layouts

The template persists named layouts via `PUT <ticket>-<slug>-layouts.json`. The bundled devserver has a narrowly-scoped PUT handler that accepts only `*-layouts.json` under its spawn cwd, validates JSON, caps body size at 256 KB, and writes atomically. If PUT fails for any reason, the template gracefully falls back to localStorage + a download prompt on save.
