# Agent & harness setup guide

How to install `swarm`, connect your harnesses (Claude Code, Codex, OMP, Gemini/Antigravity, or custom CLIs),
and wire up automatic repo-room discovery so parallel sessions stop colliding.

## 1. Install Redis and the CLI

```bash
# Start a local loopback-only Redis (macOS Homebrew or redis-server)
./examples/start-redis.sh

# Install the `swarm` CLI on PATH
uv tool install .
# or editable into a virtualenv:
pip install -e ".[dev]"

# Verify Redis, config, and available seat binaries
swarm doctor
```

## 2. Configure seats (`~/.config/swarm/config.yaml`)

Copy [examples/config.yaml](../examples/config.yaml) to `~/.config/swarm/config.yaml` (or point `SWARM_CONFIG`
at another path).

```bash
mkdir -p ~/.config/swarm
cp examples/config.yaml ~/.config/swarm/config.yaml
```

### Multi-account Claude Code seats

Run multiple Claude subscriptions side by side by setting `CLAUDE_CONFIG_DIR` in each seat's `launcher`:

```yaml
seats:
  claude-personal:
    kind: claude
    launcher: [env, CLAUDE_CONFIG_DIR=/path/to/.claude-personal, claude]
  claude-work:
    kind: claude
    launcher: [env, CLAUDE_CONFIG_DIR=/path/to/.claude-work, claude]
```

Headless Claude runs (`claude -p`) cannot answer interactive permission prompts. Adjust `claude_flags` in
`config.yaml` to match the tools you allow workers to run (for example `--permission-mode acceptEdits` plus
`--allowedTools Edit Write Read Glob Grep Bash`).

### Codex, OMP, and Gemini/Antigravity

- **Codex (`kind: codex`):** runs `codex exec --json -s workspace-write` and resumes via `codex exec resume`.
  If your Codex sandbox blocks localhost Redis, `swarm worker` automatically posts the seat's final text to
  the board (`kind=auto`).
- **OMP (`kind: omp`):** runs `omp -p --mode json --session-dir <dir>` and resumes with `-c`. Set `pool:` to
  another seat's name if `omp` spends the same subscription quota as that seat.
- **Gemini / Antigravity (`kind: agy`):** read-only reviewer (`writes: false` by default). Receives a 60 KB
  workspace snapshot (commits since `--base`, `git diff HEAD`, and untracked files) and has its stdout posted
  to the board automatically.
- **Custom CLI (`kind: cmd`):** set `command: [my-agent, --flag]` in `config.yaml` (or `SWARM_SEAT_CMD`).
  Receives the prompt on stdin and may emit JSON usage lines on stdout.

### Plugging in a quota picker

To route `swarm spawn` toward whichever subscription has the most headroom, point `picker:` at any script that
prints one JSON object mapping seat names to numeric scores (`"throttled"` skips a seat, `null` falls back to
`unknown_score`):

```yaml
picker: [python3, /path/to/picker.py]
```

See [examples/picker.py](../examples/picker.py) for a minimal working template.

## 3. Install the bundled skills

The repo ships two agent skills:

- `skills/rooms` (`swarm-rooms`) - peer-to-peer room coordination and message etiquette
- `skills/swarm` (`swarm`) - coordinator workflow for planning, worktrees, spawning, watching, and retiring

Link or copy them into your harness skills directories:

```bash
# Claude Code
mkdir -p ~/.claude/skills
ln -sfn "$PWD/skills/swarm" ~/.claude/skills/swarm
ln -sfn "$PWD/skills/rooms" ~/.claude/skills/swarm-rooms

# Gemini / Antigravity
mkdir -p ~/.gemini/skills
ln -sfn "$PWD/skills/swarm" ~/.gemini/skills/swarm
ln -sfn "$PWD/skills/rooms" ~/.gemini/skills/swarm-rooms
```

## 4. Add repo-room rules to each harness

Add the snippet for each harness you use (copy-pasteable blocks live in
[examples/global-rules.md](../examples/global-rules.md)):

| Harness | Global instructions file | Agent id convention |
|---|---|---|
| Claude Code | `~/.claude/CLAUDE.md` | `claude-<short-task>` |
| Codex | `~/.codex/AGENTS.md` | `codex-<short-task>` |
| OMP | `~/.omp/agent/AGENTS.md` | `omp-<short-task>` |
| Gemini / Antigravity | `~/.gemini/GEMINI.md` | `agy-<short-task>` |

Every session derives `<repo>` from the repository basename (e.g. `swarm`, `my-api`), runs `swarm rooms` and
`swarm --room <repo> roster`, and joins `<repo>` once. Because `swarm rooms` lists presence-only rooms even
before the first message is sent, a second session starting in the same repo sees the first immediately.

## 5. Run your first swarm

1. Write standalone briefs for each track (templates in [examples/briefs/](../examples/briefs/)).
2. Cut one git worktree per worker so parallel edits never collide:
   ```bash
   git worktree add ../repo-worktrees/track-a -b swarm/track-a main
   ```
3. Open a room, join as coordinator, and spawn workers:
   ```bash
   R=swarm-demo
   swarm --room "$R" title "Demo swarm"
   swarm --room "$R" join --as coordinator --role coordinator --harness claude
   swarm spawn --room "$R" --as track-a --role worker --group workers \
     --workspace ../repo-worktrees/track-a --brief examples/briefs/worker.md
   swarm spawn --room "$R" --as verifier --role verifier \
     --workspace ../repo-worktrees/track-a --base main --brief examples/briefs/verifier.md
   ```
4. Open the live TUI board in another terminal pane:
   ```bash
   swarm
   ```
5. End the swarm once verified:
   ```bash
   swarm --room "$R" send --from coordinator --to all --kind done "All tracks verified."
   ```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `swarm spawn` exits `4` on macOS | Running inside a Seatbelt sandbox (children inherit it and cannot drop it) | Re-run `swarm spawn` outside the sandbox |
| `swarm spawn` exits `3` | A worker with that `--as` id is already running | Check `swarm --room R roster` or stop the existing pid |
| `swarm doctor` fails on Redis | Redis is not running at `SWARM_REDIS_URL` | Run `./examples/start-redis.sh` |
| Worker crashes immediately | Seat binary missing on `PATH` or permission prompt blocked headless run | Check `~/.local/state/swarm/<room>/<agent>.log` and `swarm doctor` |
| Two seats talk back and forth with no action | Polite acks waking peers | Tell workers to output `NO_REPLY` (no `swarm send`) when no reply is needed |
