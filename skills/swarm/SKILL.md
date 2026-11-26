---
name: swarm
description: >-
  Run a multi-harness agent swarm on a shared Redis Streams room: one coordinator (you), headless workers
  across Claude Code, Codex, OMP, Gemini, or custom commands, and a verifier, watched live on `swarm board`.
  Use when fanning work out to parallel agents.
---

# Running a swarm

You are the coordinator. You plan the tracks, open the room, brief the members, answer questions on the board,
and retire the swarm when verified.

```
plan ─▶ room + title ─▶ worktrees ─▶ spawn members ─▶ watch + answer ─▶ verify ─▶ done
```

## 1. Plan

Split the work into 2-6 independent tracks touching different files. Give each worker its own worktree and a
standalone brief file (goal, owned files, acceptance criteria, commit/PR expectations, who to report to). Add
one verifier for every 2-4 workers.

## 2. Open the room

Use one room per swarm, named for the work:

```bash
R=swarm-auth-refactor
swarm --room "$R" title "Auth refactor: sessions + tests"
swarm --room "$R" join --as coordinator --role coordinator --harness claude --model claude-sonnet-5-5
swarm --room "$R" state --as coordinator waiting --doing "briefing the swarm"
```

## 3. Cut worktrees

One git worktree per worker off the base branch:

```bash
git worktree add ../repo-worktrees/auth-db -b swarm/auth-db main
```

Give the verifier the worktree path (and `--base main` if it uses a read-only seat).

## 4. Spawn members

```bash
swarm spawn --room "$R" --as auth-db --role worker --group workers \
  --workspace ../repo-worktrees/auth-db --brief briefs/auth-db.md
swarm spawn --room "$R" --as verifier --role verifier \
  --workspace ../repo-worktrees/auth-db --base main --brief briefs/verifier.md
```

Each call prints JSON with `agent`, `harness`, `model`, `pid`, and `log`. Pass `--harness` / `--model` only
when overriding config selection. On macOS, run `swarm spawn` outside any Seatbelt sandbox (inside a sandbox
it exits `4` before joining anyone, because children inherit the sandbox).

## 5. Watch and answer

Arm a watcher for `coordinator` before waiting on workers (`swarm --room "$R" wait --as coordinator --timeout 300`
or `skills/rooms/scripts/watch.sh coordinator "$R"`). Tell the user they can monitor live with:

```bash
swarm board --room "$R"
```

Check `swarm --room "$R" roster` periodically. A `system` post from a worker means its seat exited non-zero;
check its log and respawn (respawning preserves the member's cursor) or reassign.

## 6. Verify and finish

Once every track is verified and proof (commit SHAs, test output, PR URLs) is on the board, end the swarm:

```bash
swarm --room "$R" send --from coordinator --to all --kind done "Auth refactor landed and verified."
```

Every worker retires (`state=retired`) and exits cleanly. `swarm board` marks the room `✓ finished`.
Remove merged worktrees when done.
