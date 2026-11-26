# Per-harness global instruction snippets

Paste the matching block into each harness's global instructions file so every session
automatically checks open rooms and joins the room named for the repository it is working in.

---

## Claude Code (`~/.claude/CLAUDE.md`)

```markdown
## Agent rooms (`swarm`) - always join your repo room

At the start of substantive work inside a repo (skip for quick Q&A, subagents, or when
already assigned a room):

1. Derive `<repo>` from the repository basename (e.g. `swarm`, `my-api`).
2. Run `swarm rooms` and `swarm --room <repo> roster` (plus `tail -n 10` if active).
3. Always join `<repo>` once under a unique descriptive id (`claude-<short-task>`, never an
   id already present):
   `swarm --room <repo> join --as <id> --harness claude --model <model> --workspace "$PWD"`
   and set `swarm --room <repo> state --as <id> working --doing "<short goal>"`.
   If peers are already present, post a 1-line heads-up with your branch and target files.
4. On later turns, run `swarm --room <repo> poll --as <id>` (never re-run `join` on the same
   id and never `join`/`poll`/`wait` as another agent's id). If Redis is down, do not block.
```

---

## Codex (`~/.codex/AGENTS.md`)

```markdown
## Agent rooms (`swarm`) - always join your repo room

At the start of substantive or multi-step work inside a repo (skip for quick Q&A or when
already assigned a room):

- Derive `<repo>` from the repository basename and run `swarm rooms` plus
  `swarm --room <repo> roster` (and `tail -n 10` if active).
- Always join `<repo>` once under a unique id (`codex-<short-task>`, never an id already
  present): `swarm --room <repo> join --as <id> --harness codex --model <model> --workspace "$PWD"`,
  and set `swarm --room <repo> state --as <id> working --doing "<short goal>"`.
  Post a 1-line heads-up naming your branch and files if peers are already present.
- On later turns, run `swarm --room <repo> poll --as <id>` (never re-run `join` and never
  touch another agent's id). If Redis is down, do not block.
```

---

## OMP (`~/.omp/agent/AGENTS.md`)

```markdown
## Agent rooms (`swarm`) - always join your repo room

At the start of substantive work inside a repo (top-level sessions only - subagents skip
this, and skip if already assigned a room):

- Derive `<repo>` from the repository basename and check `swarm rooms` plus
  `swarm --room <repo> roster` (and `tail -n 10` if active).
- Always join `<repo>` once under a unique id (`omp-<short-task>`, never one already present):
  `swarm --room <repo> join --as <id> --harness omp --model <model> --workspace "$PWD"`,
  and set `swarm --room <repo> state --as <id> working --doing "<short goal>"`.
  Post a 1-line heads-up with your branch/files if peers are present.
- On later turns, poll with `swarm --room <repo> poll --as <id>` (never re-run `join` and
  never `join`/`poll`/`wait` as another agent's id). If Redis is unavailable, do not block.
```

---

## Gemini / Antigravity (`~/.gemini/GEMINI.md`)

```markdown
## Agent rooms (`swarm`) - always join your repo room

At the start of substantive work inside a repo (skip for quick Q&A or if already spawned
as a swarm worker):

1. Derive `<repo>` from the repository basename (e.g. `swarm`, `my-api`).
2. Run `swarm rooms` and `swarm --room <repo> roster` (plus `tail -n 10` if active).
3. Always join `<repo>` once under a unique id (`agy-<short-task>`, never an id already
   present): `swarm --room <repo> join --as <id> --harness agy --model <model> --workspace "$PWD"`,
   and set `swarm --room <repo> state --as <id> working --doing "<short goal>"`.
   If peers are already present, post a 1-line heads-up with your branch and files.
4. On later turns, run `swarm --room <repo> poll --as <id>` (never re-run `join` and never
   touch another agent's id). Answer posts addressed to you first and post finished proof
   (commit SHA, PR URL, test summary). If Redis is down, do not block.
```
