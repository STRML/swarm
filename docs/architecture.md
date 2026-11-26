# Architecture & reliability invariants

## Why Redis Streams (not pub/sub)

Pub/sub drops messages whenever a subscriber is mid-turn or disconnected. Turn-based agents spend most of their time
thinking, running tools, or compacting context, so pub/sub loses the exact posts that matter.

Every room is a Redis Stream (`<prefix>:stream:<room>`) and each agent tracks its own read cursor
(`<prefix>:cursor:<room>:<agent>`). Blocking `XREAD` runs in 1-second chunks under a 5-second socket timeout so
dead connections fail fast rather than hanging silently.

## Addressing & replay floor

- `--to` accepts a comma list of agent ids, `@name`, and `all`. `@name` is stored verbatim and resolved at read time
  against both agent ids and group sets (`<prefix>:groups:<room>:<agent>`), so a worker joining a group later still
  receives earlier group briefs when joining with `--replay`.
- `join --replay` records the current stream head in `replay_floor`. Any `__SHUTDOWN__` or `--kind done` at or before
  `replay_floor` is skipped, so an old stop signal never kills a freshly joined member.
- `__SHUTDOWN__` after `replay_floor` always bypasses `--topic` and `--reply-to` filters so an agent cannot filter
  out its own kill switch.

## Worker delivery & crash recovery

- **Post-wake cursor commit:** `swarm worker` advances its cursor past unrelated posts immediately, but commits the
  cursor for posts addressed to it only *after* `wake()` finishes. Killing a worker mid-wake and respawning it
  re-delivers those posts; a clean respawn after a completed wake preserves its cursor.
- **Presence heartbeat:** While a seat runs, a daemon thread refreshes `<prefix>:presence:<room>:<agent>` (90s TTL)
  every 30s. If a worker dies hard, presence expires and the board shows `○ gone`.
- **Single-instance lock:** Each worker holds `<state_dir>/<room>/<agent>.pid`. A duplicate worker or `swarm spawn`
  for a live pid exits `3` without touching the cursor.
- **Stop rule (`ends_swarm`):** Shared by `swarm worker` and `swarm board`: either `__SHUTDOWN__` from anyone, or
  `--kind done` from a member whose roster `role` is `coordinator`. A `--kind done` from a non-coordinator is treated
  as an ordinary post.

## Auto-post loop guard (`auto` / `echo`)

Seats that cannot reach Redis directly (such as sandboxed `codex` or read-only `agy`) end their wake without calling
`swarm send`. The worker posts the seat's final text on its behalf with `kind="auto"`.

If two such seats replied to each other, they would ping-pong indefinitely. Whenever every post in a wake has
`kind in ("auto", "echo")`, the worker tags the reply `kind="echo"`. Echoes land on the board for humans and history,
but `swarm worker` skips waking on `echo` posts (unless the body is `__SHUTDOWN__`). Explicit `swarm send` posts
cannot use `auto` or `echo` (rejected with exit `2`) and always wake recipients.

## Atomic room lifecycle (Lua)

`archive`, `unarchive`, and `clear` run as single Redis Lua scripts:

- Presence checks, idle cutoffs, and live-name conflict checks run inside the script, so concurrent `send` or `join`
  calls land wholly before or wholly after - never tearing a room in half.
- Key scans match exact prefixes (`<prefix>:<kind>:<room>:`), so room `foo` never touches `foo-2`.
- `archive` moves `stream`, `meta`, `member:*`, and `groups:*` under `<prefix>:archived:<room>:<epoch-ms>:` with an
  expiry (`SWARM_ARCHIVE_RETAIN_DAYS`, default `7`), leaving cursors intact. `unarchive` renames them back and calls
  `PERSIST`.

## Sandbox & environment hygiene

- On macOS, `swarm spawn` checks `sandbox_check()` via `libSystem.B.dylib` and exits `4` before joining anyone if run
  inside a Seatbelt sandbox (children inherit Seatbelt and cannot drop it).
- `swarm worker` strips `CMUX_*` env vars, `/cmux-cli-shims` PATH entries, `CLAUDECODE`, and `CLAUDE_CODE_ENTRYPOINT`
  before spawning seats so nested headless sessions stay isolated from interactive terminal hooks.
