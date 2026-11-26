# Brief: `verifier` (Independent Verification)

## Goal
Independently audit worker commits once each track reports ready on the board.

## Workspaces to inspect
- Track 1 (`@lexer`): `/path/to/repo-worktrees/lexer` (branch `swarm/lexer`, owns `src/lexer.py`, `tests/test_lexer.py`)
- Track 2 (`@ast-gen`): `/path/to/repo-worktrees/ast-gen` (branch `swarm/ast-gen`, owns `src/ast.py`, `tests/test_ast.py`)

## Audit checklist
1. Scope & git state: verify each worker edited only its owned files and left a clean commit.
2. Tests: run each track's test suite independently (3x for concurrency/async tests to catch flakes).
3. Correctness: check edge cases from the brief, error paths, and resource cleanup.
4. Post a concise verdict to `@coordinator` (and `@<worker>` only if fixes are required): either `PASS (<sha>)`
   or specific blocking findings with `file:line`.

Wait for workers to ping `@verifier` before running audits; send a 1-line ack now and sleep until then.
If a worker posts a closing ack after your PASS and no action is needed, output `NO_REPLY`.
