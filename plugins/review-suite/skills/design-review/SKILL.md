---
name: design-review
description: Create an interactive before/after design diagram review playground. Generates a draggable SVG diagram with per-node approve/revise/question controls and a "Send to Claude" button that delivers structured node feedback directly to a live Claude Code session. Usage - /design-review [<ticket>]
allowed-tools: Read Write Edit Bash(mkdir:*) Bash(cp:*) Bash(python3:*) Bash(ls:*) Bash(cat:*) Bash(echo:*) Bash(eval:*)
argument-hint: "[<ticket>]"
---

# Design Review Skill

Creates an interactive before/after design diagram from a bundled template, pre-populated with component nodes and edges supplied by the main agent. The reviewer attaches per-node approve / revise / question marks plus free-text comments. Clicking "Send to Claude" delivers a structured feedback bundle directly into an embedded Claude Code terminal (PTY-bridged via the bundled devserver).

## Invocation Forms

| Invocation | Behavior |
|---|---|
| `/design-review` | Model infers both ticket ID and title from conversation context. Confirms with user before writing; asks if no context is recoverable. |
| `/design-review <ticket>` | User supplies the ticket ID (any tracker format: `TT-131`, `RFC-042`, `PROJ-7`, etc.). Model infers the title from context; asks if unclear. |

A two-arg form (`/design-review <id> <title>`) is **not** supported — keep the signature minimal, same as `plan-review`.

## How It Works

1. Check for a prior diagram matching the ticket. If found, offer **Resume** / **Overwrite** / **Cancel**.
2. Read the bundled template: `${CLAUDE_PLUGIN_ROOT}/assets/design-review-template.html`
3. Populate the `BEFORE_NODES`, `BEFORE_EDGES`, `AFTER_NODES`, `AFTER_EDGES` arrays with component data.
4. Set the page title, heading, and JS constants (`PLAN_NAME`, `CLAUDE_SESSION`, `LAYOUTS_FILE`).
5. Write the output HTML to the resolved output directory.
6. Start (or reuse) the bundled devserver via `${CLAUDE_PLUGIN_ROOT}/bin/devserver.py find-or-start` — project-scoped: one devserver per project root on a free port in 8765-8799.
7. Return the LAN-IP URL.

On **Resume** the flow short-circuits: hydrate the prior node/edge arrays into the agent's context, rewrite only the `CLAUDE_SESSION` constant in the existing HTML, then jump to step 6.

### Output Directory Resolution

1. **`DESIGN_REVIEW_DIR` env var** (if set) — explicit override, absolute or project-relative.
2. **`.design-review/`** — default, auto-created via `mkdir -p` if missing.

Kept distinct from `.plan-review/` so artifacts don't collide when both plugins run in one project.

## Instructions

When invoked:

1. **Parse arguments.**
   - Zero args: infer ticket + title from conversation context. Confirm the inferred values with the user before writing. If context is empty, ask the user for both.
   - One arg: treat as ticket ID. Accept any tracker format. Infer title from conversation; ask if the title is unclear.
   - Free-form slugs (e.g., `auth-rework`) passed as a single arg are **not** ticket IDs — treat as title text or ask the user to clarify.

2. **Resolve output directory.** Use `$DESIGN_REVIEW_DIR` if set, else `.design-review/`. Ensure it exists (`mkdir -p`).

2a. **Detect prior diagram for this ticket.** Before reading the template or constructing a new filename, glob the output directory for an existing HTML matching the ticket:

   ```bash
   shopt -s nullglob
   PRIOR=( "$OUT_DIR"/"$TICKET"-*-design-review.html )
   shopt -u nullglob
   ```

   Glob rather than exact match so the check still catches the prior file when the title has drifted. Skip this step entirely if no ticket was supplied — titles alone are too noisy to match prior runs reliably.

   - **Zero matches** → continue to step 3 (write-new flow unchanged).
   - **One match** → ask the user:

     > Found a prior design review for `<ticket>` at `<path>` (modified `<mtime>`).
     >
     > - **Resume** — keep the nodes/edges and your prior review marks; refresh the embedded session id so the terminal bridge works.
     > - **Overwrite** — replace with a freshly generated diagram from the current conversation. Prior review marks for this filename remain in the browser's localStorage.
     > - **Cancel** — do nothing.

     On **Resume**, jump to "Resume: Hydrate and Refresh" below. On **Overwrite**, continue to step 3. On **Cancel**, return without writing or starting the devserver.

   - **Multiple matches** → list each prior file with path + mtime; ask the user to choose which to resume, or Overwrite / Cancel.

3. **Construct filename.** `<output-dir>/<ticket>-<slugified-title>-design-review.html` (lowercase, hyphens). If no ticket, use `<slugified-title>-design-review.html`.

### Recommended path: render via the generator (steps 4–8 in one command)

The preferred way to produce the HTML is **not** to inline-patch the template by hand. Build a JSON spec describing the diagram and pipe it through the bundled generator. The generator uses `json.dumps` for all data injection (guaranteeing correct JS escaping), validates the inline JS via `node --check`, and refuses to leave a broken file in place.

```bash
cat <<'JSON' | python3 "${CLAUDE_PLUGIN_ROOT}/bin/render-review-html.py"
{
  "kind": "design-review",
  "ticket": "QUE-123",
  "title": "<plan title>",
  "session_id": "<sid from review-suite-session-id hook>",
  "output_path": ".design-review/QUE-123-<slug>-design-review.html",
  "layouts_file": "QUE-123-<slug>-layouts.json",
  "before_nodes": [ { "id": "...", "x": 100, "y": 100, "layer": "orchestration", "label": "...", "type": "...", "desc": "..." } ],
  "before_edges": [ { "from": "a", "to": "b", "label": "..." } ],
  "after_nodes":  [ ... ],
  "after_edges":  [ ... ]
}
JSON
```

On success the generator prints the absolute output path on stdout. On a validator failure (broken inline JS) it exits non-zero and removes the partial file — you will not accidentally serve a blank page.

**Skip steps 4–8 below entirely if you use the generator.** Jump to step 9 (start the devserver).

### Fallback path: inline-patch the template

If the generator is unavailable (no `python3`, restricted environment, etc.), do the patching manually. Read the [Common pitfalls](#common-pitfalls-when-inline-patching) section first — this is where blank pages come from.

4. **Read the template.** `${CLAUDE_PLUGIN_ROOT}/assets/design-review-template.html`.

5. **Ask the main agent for node + edge data.** Each node needs:

   | Property | Required | Description |
   |---|---|---|
   | `id` | Yes | Unique identifier (used in edges). |
   | `x`, `y` | Yes | Initial position on canvas (user can drag to rearrange). |
   | `layer` | Yes | Color group: `orchestration`, `routing`, `queue`, `signal`, or `websocket`. |
   | `label` | Yes | Component name displayed on the node. |
   | `type` | Yes | Subtitle text (e.g., "Async Manager"). |
   | `file` | No | Source file path (shown in detail panel). |
   | `desc` | Yes | Tooltip description. |
   | `code` | No | Code snippet shown in detail panel (use `\n` for newlines). |
   | `badge` | No | Pill text below label. |
   | `note` | No | Warning/callout text. |
   | `change` | No | **After pane only**: `new`, `modified`, or `removed`. |

   Each edge needs:

   | Property | Required | Description |
   |---|---|---|
   | `from` | Yes | Source node id. |
   | `to` | Yes | Target node id. |
   | `label` | No | Edge label text. |
   | `style` | No | `new` (green) or `callback` (dashed red). |

5a. **Read the authoring session id.** Look for the `review-suite-session-id: <sid>` line in your own context — it is injected by the plugin's `UserPromptSubmit` hook on every turn. Extract `<sid>` for step 7. If absent, the hook did not fire (plugin not installed, or first turn of a malformed setup) — surface the error to the user rather than generating an unusable HTML.

6. **Replace the four data arrays** (`BEFORE_NODES`, `BEFORE_EDGES`, `AFTER_NODES`, `AFTER_EDGES`) in the template with the provided data.

7. **Update identifiers** in the HTML:
   - `<title>` tag → `Design Review: <ticket>: <title>` (or `Design Review: <title>`)
   - Topbar `<h1>` → `<ticket> <title>` (or `<title>`)
   - `PLAN_NAME` JS constant → `<ticket>: <title>` (or `<title>`)
   - `CLAUDE_SESSION` JS constant → the session id from step 5a
   - `LAYOUTS_FILE` JS constant → `<ticket>-<slug>-layouts.json` (relative to the HTML; devserver scopes PUT access to `*-layouts.json` under cwd)

8. **Write the file** to the resolved output directory.

9. **Start (or reuse) the devserver.** Project-scoped: the devserver self-discovers an existing instance whose `/proc/<pid>/cwd` matches the current project root, or spawns a new one in this project's cwd. Two projects on the same host get distinct devservers on distinct ports; same-project repeat invocations reuse the existing one.

   ```bash
   eval "$(python3 "${CLAUDE_PLUGIN_ROOT}/bin/devserver.py" find-or-start)"
   # $URL, $PORT, $LAN_IP are now set
   ```

10. **Return the URL.** Format: `http://<lan-ip>:$PORT/<output-dir-relative-to-cwd>/<filename>.html` (e.g., `http://192.168.1.237:8775/.design-review/TT-131-foo-design-review.html`).

## Resume: Hydrate and Refresh

When the user chooses **Resume** at step 2a (or picks a specific file in the multi-match case):

1. **Hydrate context.** Read the prior HTML. Extract the `BEFORE_NODES`, `BEFORE_EDGES`, `AFTER_NODES`, `AFTER_EDGES` literals and internalize them. Surface a short summary to the user:

   > Resumed design review: N before-nodes, M after-nodes (J new / K modified / L removed). Picking up where you left off — where do you want to focus?

2. **Refresh the session id.** Rewrite **only** the `CLAUDE_SESSION = "..."` JS constant. Do not touch the node/edge arrays, `PLAN_NAME`, `LAYOUTS_FILE`, `<title>`, or `<h1>`.

3. **Validate.** If the `CLAUDE_SESSION` constant can't be found, surface the error and ask whether to regenerate. Never silently overwrite.

4. **Start (or reuse) the devserver** per step 9.

5. **Return the URL** per step 10.

`PLAN_NAME` is intentionally **not** rewritten on resume — the browser's `localStorage` keys are `arch-state:<PLAN_NAME>` / `arch-layout:<PLAN_NAME>` / `arch-autosave:<PLAN_NAME>`, and preserving them is what keeps the reviewer's prior node feedback + saved layouts + in-progress drag positions attached to the restored file.

## Node Layout Guidelines

Arrange nodes as a **top-down tree**, not a circular web. The reviewer expects a clear directional flow:

1. **Source nodes at the top** — data origins (e.g., Brokerage WS, API) get the smallest `y` values.
2. **Processing nodes in the middle** — orchestrators, services that transform or route data.
3. **Sink nodes at the bottom** — databases, message buses, terminal outputs get the largest `y` values.
4. **Fan-out horizontally** — when a node writes to multiple sinks, spread them across the `x` axis on the same row.
5. **Center the primary flow** — the main path runs down the center; secondary paths branch left/right.

Typical spacing: ~170px vertical gap between tiers, ~200px horizontal gap between siblings. Avoid circular layouts where edges loop back up.

### Layer Colors

| Layer | Use for |
|---|---|
| `orchestration` | Managers, coordinators, databases |
| `routing` | Routers, handlers, middleware |
| `queue` | Processors, queues, buffers |
| `signal` | Signals, events, sinks |
| `websocket` | WS bridges, external streaming connections |

## Handling Review Feedback

When the reviewer sends feedback via the "Send to Claude" button, a structured bundle arrives in the embedded terminal:

```
Here is my design-review of <ticket>: <title>:

## Nodes flagged for revision (N)
### <label> — <pane> pane (<change>)
File: <path>
Comment: ...

## Questions (N)
...

## Approved (N)
...
```

When you receive the bundle:

1. **Parse** the sections (revision / question / approved).
2. **For revision items:** discuss the concern conversationally. Do NOT edit the HTML until the reviewer explicitly says to update the diagram.
3. **When discussion on a node wraps,** ask whether to update the document (tweak the node metadata, move it to a different layer, mark it removed, etc.). Edits should be a targeted replacement of the specific node object in the template's `BEFORE_NODES` / `AFTER_NODES` arrays.
4. **For approved items:** note the approval; no action required unless the reviewer asks.

## Session Context Preamble

On the first Send-to-Claude click per browser session, the template prepends a one-time preamble:

> **Context switch:** you are now in the design-review playground. The diagram is at `<path>`. Discuss the feedback below conversationally. Do NOT edit the HTML or the layouts JSON until I explicitly say to update. When discussion on a node wraps, ask whether to update the document.

State is tracked via `sessionStorage` keyed off the review doc's filename.

## Session Resume

The devserver's PTY bridge forks a live `claude` child from `CLAUDE_SESSION` (the authoring session) on the first WebSocket connect, and keeps that child alive for the playground's lifetime, keyed by the HTML path. A browser refresh, "Send to Claude" click, or next-day return re-binds to the **same live process** and replays its recent output — context survives reloads without resuming anything from disk.

The forked child writes its own transcript, so the conversation held in a playground stays recoverable from a terminal by the forked session id — that is the id the handoff button copies. The bridge itself does not resume it. If the live child is gone — devserver restarted, or the session reaped after a long idle — the next open **re-forks from `CLAUDE_SESSION`**, which restarts from the plan as authored rather than from wherever the playground conversation ended. The authoring session must itself be resumable; if its transcript is missing, the bridge surfaces an error asking you to regenerate the review from a live session. There is no `ACTIVE_SESSION` constant — the fork id changes on every cold start, so nothing is baked back into the HTML.

A `claude` process that inherits the `CLAUDE_CODE_CHILD_SESSION` environment marker disables transcript saving for itself and prints a banner saying so. The devserver strips that marker before forking, so starting the devserver from inside a Claude Code tool call does not silently leave the playground conversation unwritten.

### Handoff

Each generated review includes a "Hand off to terminal" button that copies `claude --resume <sid>` to the clipboard and sends Ctrl+D to the embedded Claude child so the session is released. Paste the copied command in any local terminal to resume from there.

## Prerequisites

- Python 3.10+ on the user's machine
- `claude` CLI available in PATH
- `ptyprocess` (optional) — used by the devserver if installed; otherwise falls back to stdlib `pty.fork()`

## Environment Variable Reference

| Variable | Default | Purpose |
|---|---|---|
| `DESIGN_REVIEW_DIR` | `.design-review/` | Where generated review HTML files are written |
| `DESIGN_REVIEW_HOST` | auto-detected LAN IP | Override the host in the returned URL |
| `DESIGN_REVIEW_PORT` | `8775` | Devserver port |

## Common pitfalls when inline-patching

These traps are why `bin/render-review-html.py` exists. If you used the generator (recommended path above), you don't need to worry about them. If you're inline-patching the template by hand, read carefully.

1. **`re.sub` interprets backslash escapes in the replacement string.** Don't do `re.sub(pat, replacement_string, text)` when the replacement contains `\n`, `\1`, `\g<...>`, etc. — `re.sub` processes those as escape sequences and rewrites your output. The classic symptom: a `code:` field that contained literal `\n` escapes ends up with real newlines, breaking the single-quoted JS string and rendering the page blank. Use a lambda replacement (`re.sub(pat, lambda m: replacement, text)`) or plain `str.replace()` instead.

2. **Single-quoted JS strings cannot span lines.** If a `code:` field has multiple lines, either (a) use a backtick template literal — `code: \`line1\nline2\`` — or (b) write all newlines as `\n` escape sequences inside a single-quoted string. Don't put a literal newline in the JS source between the opening and closing quotes.

3. **Always validate inline JS before returning the URL.** Extract every `const X = ...;` declaration from the rendered HTML and pipe through `node --check`. If parsing fails, the page renders blank — don't hand the user a link to broken JS.

4. **`$CLAUDE_JOB_DIR` is harness-managed and may be unset.** When you need a scratch directory for the patching script, wrap any usage with a fallback: `SCRATCH="${CLAUDE_JOB_DIR:-$(mktemp -d -t claude-job-XXXXXX)}"`. Bare `$CLAUDE_JOB_DIR` resolves to empty and your script will write to `/` or fail.

## Saved Layouts

The template persists named layouts (node positions across both panes) via `PUT <ticket>-<slug>-layouts.json`. The bundled devserver has a narrowly-scoped PUT handler that accepts only `*-layouts.json` under its spawn cwd, validates JSON, caps body size at 256 KB, and writes atomically. If PUT fails for any reason, the template gracefully falls back to localStorage + a download prompt on save.
