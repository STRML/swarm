# Seats, role bands & quota routing

Copy [examples/config.yaml](../examples/config.yaml) to `~/.config/swarm/config.yaml` (or point `SWARM_CONFIG` at it).
Without a config file, built-in default seats (`claude`, `codex`, `omp`, `agy`, `cmd`) apply and `--harness` selects
one directly.

## Harness adapters

| Seat `kind` | First wake | Subsequent wakes | Notes |
|---|---|---|---|
| `claude` | `<launcher> -p --output-format json --session-id <uuid>` | `--resume <uuid>` | `launcher` can point at multiple accounts (`[env, CLAUDE_CONFIG_DIR=..., claude]`) |
| `codex` | `codex exec --json -s workspace-write` | `codex exec resume <thread>` | Sandbox cannot reach Redis; worker auto-posts final answer |
| `omp` | `omp -p --mode json --session-dir <dir>` | `-c` in the same dir | Parses `turn_end` usage and USD cost |
| `agy` | Prompt + workspace diff and untracked files (60 KB cap) | Fresh snapshot each wake | Read-only (`writes: false` by default); worker posts stdout |
| `cmd` | Any executable (`command:` or `SWARM_SEAT_CMD`), prompt on stdin | Same | Optional JSON usage lines on stdout |

If a resumed session fails with a missing-session error, `swarm worker` drops the stale session file and retries fresh
with a note telling the seat to read `swarm --room <R> history`.

## Automatic seat selection

When `swarm spawn` runs without `--harness`:

1. **Role band filter:** `roles.<role>: {min, max, writes}` selects models whose numeric `index` lies inside `[min, max]`.
   Roles with `writes: true` (the default) skip read-only seats (`writes: false`).
2. **Quota headroom:** If `picker` is configured, `swarm spawn` runs it (20s timeout) and reads the last JSON line, e.g.:
   ```json
   {"claude": 16.2, "codex": 4.0, "agy": null}
   ```
   Higher score wins; `"throttled"` excludes the seat; `null` uses `unknown_score` (default `0`). Ties go to the higher
   model `index`.
3. **Pool spread penalty:** Cached quota readings do not move between rapid spawns. Under a per-room Redis lock
   (`swarm:lock:spawn:<room>`), each live non-retired member in the room docks its seat's `pool` by `spread_penalty`
   (default `10`). Seats sharing one subscription set the same `pool` name so they share the penalty. Respawning a dead
   member does not count its own previous seat against itself.

Pass `--harness` (and optionally `--model` / `--effort`) to bypass the picker.
