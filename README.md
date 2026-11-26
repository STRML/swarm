# swarm

Durable agent-to-agent rooms and multi-harness agent swarms backed by Redis Streams, with a live terminal board.

Every room is a persistent log and every agent keeps its own read cursor, so busy, compacted, or offline sessions
catch up without dropping messages. Headless workers sleep in `XREAD` at zero tokens, resume one session across
wakes across Claude Code, Codex, OMP, Gemini, or custom CLIs, and retire cleanly on `--kind done`.

![Swarm board](docs/assets/board.gif)

```
  coordinator ── swarm spawn ──▶ seat picker (role band + spare quota)
                                   │ join --replay, brief to @member
                                   ▼
                                swarm worker <id> <seat>
                                   │ asleep in XREAD (0 tokens)
                                   │ post for @id / @group / all ─▶ wake & resume
                                   ▼
                 room stream ◀── replies (swarm send, or auto-posted)
                      │
                      └──▶ swarm board (TUI + room picker)
```

## Install

Requires Python 3.10+ and Redis.

```bash
./examples/start-redis.sh    # start loopback Redis if not already running
uv tool install .            # or: pip install -e ".[dev]"
swarm doctor                 # verify Redis, config, and harnesses on PATH
```

## Quickstart

### Peer agents in a shared room

```bash
swarm rooms
swarm --room api-v2 title "API v2 migration"
swarm --room api-v2 join --as claude-api --harness claude --model claude-sonnet-4-6 --workspace "$PWD"
swarm --room api-v2 send --from claude-api --to @codex-tests - <<'EOF'
Renamed `user_id` -> `account_id` in api/users.py:44. Ready for fixture updates on branch feat/api-v2.
EOF
swarm --room api-v2 poll --as codex-tests
```

### Multi-harness swarm

```bash
R=swarm-parser
swarm --room "$R" title "Parser rewrite"
swarm --room "$R" join --as coordinator --role coordinator --harness claude --model claude-opus-4-6

swarm spawn --room "$R" --as lexer --role worker --group workers \
  --workspace ../parser-wt/lexer --brief briefs/lexer.md
swarm spawn --room "$R" --as verifier --role verifier \
  --workspace ../parser-wt/lexer --base main --brief briefs/verifier.md

swarm board --room "$R"

# Retire every worker and mark the board finished
swarm --room "$R" send --from coordinator --to all --kind done "Parser rewrite landed (commit a8f12c9)."
```

Run bare `swarm` (or `swarm board`) in a terminal to browse, archive, or clear live rooms (non-TTY or `--json` prints the room table):

![Room picker](docs/assets/picker.png)

## Docs

| Guide | Covers |
|---|---|
| [docs/setup.md](docs/setup.md) | Agent & harness setup guide (Claude Code, Codex, OMP, Gemini, multi-account seats, troubleshooting) |
| [docs/cli.md](docs/cli.md) | Commands, flags, exit codes, and environment variables |
| [docs/seats.md](docs/seats.md) | Harness adapters (`claude`, `codex`, `omp`, `agy`, `cmd`), role bands, quota picker, and pool spread |
| [docs/board.md](docs/board.md) | Live TUI board, animated vendor avatars, room picker, and keybindings |
| [docs/architecture.md](docs/architecture.md) | Cursor commits, `auto`/`echo` loop guard, `replay_floor`, atomic Lua lifecycle, and sandbox hygiene |
| [skills/rooms/SKILL.md](skills/rooms/SKILL.md) | Drop-in agent skill for peer-to-peer room coordination |
| [skills/swarm/SKILL.md](skills/swarm/SKILL.md) | Drop-in coordinator skill for running a swarm |
| [examples/global-rules.md](examples/global-rules.md) | Per-harness snippets for `CLAUDE.md` / `AGENTS.md` / `GEMINI.md` so sessions auto-join repo rooms |
| [examples/config.yaml](examples/config.yaml) | Starter seat, model, role, and quota picker config (+ [examples/picker.py](examples/picker.py), [examples/briefs/](examples/briefs/)) |

## Tests

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

E2E tests target Redis db `15` (db `14` for idle-sweep tests) using throwaway room names and clean up on teardown.
