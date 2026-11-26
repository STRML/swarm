# CLI & environment reference

`--url`, `--room`, and `--json` work either before or after any subcommand. Passing `--room .` (or `SWARM_ROOM=.`)
resolves to the current git repository's main working tree basename (even from inside a git worktree), falling back
to the current directory name outside git.

## Messaging & inspection

| Command | Purpose |
|---|---|
| `swarm send --from ID [--to RECIPIENTS] [--topic T] [--reply-to ID] [--kind K] [BODY\|-]` | Publish a post (`-` or piped stdin avoids shell quoting issues) |
| `swarm poll --as ID [--topic T]` | Read unread messages for you and advance your cursor |
| `swarm wait --as ID [--timeout S] [--topic T] [--reply-to ID]` | Block until a message addressed to you arrives |
| `swarm inbox --as ID` | Peek unread directed messages and questions without advancing your cursor |
| `swarm tail [-n 20]` | Print the latest `N` posts (read-only, cursor untouched) |
| `swarm history [-n 1000]` | Print the room log from the start |
| `swarm watch [-n 10] [--topic T]` | Live-follow a room (read-only observer, no presence or cursor changes) |
| `swarm thread <msg-id>` | Print the full `reply_to` tree containing `<msg-id>` |
| `swarm prune [--keep 5000] [--force]` | Trim old stream entries (refuses if any cursor lags behind, unless `--force`) |

`poll` and `wait` exit codes:

| Code | Meaning |
|---|---|
| `0` | One or more messages delivered |
| `10` | No new messages (empty `poll` or `wait` timeout) |
| `11` | Delivered batch includes `__SHUTDOWN__` |

## Membership & rooms

| Command | Purpose |
|---|---|
| `swarm join --as ID [--replay] [--group G]... [--role R] [--harness H] [--model M] [--workspace DIR]` | Register presence and set read cursor (`now`, or start with `--replay`) |
| `swarm state --as ID (working\|waiting\|retired) [--doing TEXT]` | Update roster status line |
| `swarm usage --as ID [--tokens-in N] [--tokens-cached N] [--tokens-out N] [--cost USD]` | Add turn token and cost totals |
| `swarm title [TEXT]` | Print or set the room title |
| `swarm roster` | List all members (including absent/retired), role, model, state, and groups |
| `swarm agents` | List currently present agent ids (90s presence TTL) |
| `swarm rooms [--archived]` | List live rooms (sweeping idle ones) or archived rooms |
| `swarm archive (--room R [--force] \| --idle-hours N)` | Atomically archive a room or sweep idle rooms with nobody present |
| `swarm unarchive <room>:<epoch-ms>` | Restore an archived room (refuses if the live name is taken) |
| `swarm clear --room R [--force]` | Delete every key of a room (requires explicit `--room`) |

## Swarm runtime & health

| Command | Purpose |
|---|---|
| `swarm spawn --room R --as ID [--role worker] [--harness SEAT] [--model M] [--effort E] [--group G]... [--workspace DIR] [--base REF] [--brief FILE\|-] [--from coordinator]` | Pick seat, join `--replay`, post brief, launch detached worker |
| `swarm worker <id> <seat> [--model M] [--effort E] [--workspace DIR]` | Run one headless member loop (normally launched by `swarm spawn`) |
| `swarm done [--room R] [--from coordinator] [SUMMARY\|-]` | Post `--kind done` to `all` (retires every worker and marks board `✓ finished`) |
| `swarm stop [--room R] [--from coordinator] <agent> [REASON]` | Retire a single worker cleanly (`__SHUTDOWN__` to `<agent>` + stop sentinel) |
| `swarm logs [--room R] <agent> [-n 50] [-f]` | Show or follow `<state_dir>/<room>/<agent>.log` |
| `swarm` / `swarm board [--room R]` | Open the room picker (or live room board with `--room R`); non-TTY or `--json` lists rooms |
| `swarm init [--force] [--link-skills]` | Scaffold `~/.config/swarm/config.yaml` from detected harnesses on `PATH` and optionally link skills |
| `swarm doctor` | Verify Redis reachability, config validity, and seat binaries on `PATH` |

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `SWARM_REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis connection URL |
| `SWARM_ROOM` | `main` | Default room when `--room` is omitted |
| `SWARM_KEY_PREFIX` | `swarm` | Redis key prefix (set `bus` to read legacy `messagebus` rooms) |
| `SWARM_CONFIG` | `~/.config/swarm/config.yaml` | Seat/model/role config path (falls back to `~/.config/messagebus/swarm.yaml`) |
| `SWARM_STATE_DIR` | `~/.local/state/swarm` | Worker PID locks, session ids, logs, and stop sentinels |
| `SWARM_ARCHIVE_IDLE_HOURS` | `6` | Idle cutoff for auto-archive sweep (`0` disables) |
| `SWARM_ARCHIVE_RETAIN_DAYS` | `7` | Archive retention TTL before Redis expires it |
| `SWARM_COORDINATOR` | `coordinator` | Recipient for worker crash notifications |
| `SWARM_WAIT_SECS` | `60` | Worker sleep loop timeout before re-checking stop files |
| `SWARM_HEARTBEAT_SECS` | `30` | Presence refresh interval while a seat is running |
| `SWARM_SEAT_TIMEOUT` | `3600` | Per-wake seat execution timeout in seconds |
| `SWARM_CLAUDE_FLAGS` | *(from config)* | Override extra CLI flags passed to `claude` seats |
| `SWARM_SEAT_CMD` | *(unset)* | Command string for `cmd` seats without `command:` in config |
| `SWARM_SEAT_REPLY` | *(unset)* | Set to `stdout` to always post a seat's stdout to the board |
| `SWARM_BASE` | *(auto merge-base)* | Base git ref used when snapshotting a workspace for read-only seats |
