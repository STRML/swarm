# Brief: Track 1 - `lexer` (Zero-Copy Tokenizer)

## Goal
Implement a zero-copy tokenizer in `src/lexer.py` with byte-offset spans and full UTF-8 test coverage.

## Workspace & branch
- Workspace: `/path/to/repo-worktrees/lexer`
- Branch: `swarm/lexer` (branched off `main`)
- Files you own: `src/lexer.py`, `tests/test_lexer.py` (do not edit files outside this scope)

## Requirements
1. Keep public token types in `src/tokens.py` unchanged.
2. `Token.span` uses byte offsets `(start, end)` relative to the UTF-8 buffer.
3. Handle BOM, unclosed string literals, and multi-byte CJK/emoji identifiers cleanly without panicking.

## Verification
Run:
```bash
pytest tests/test_lexer.py -v
```

## Done criteria
- All new and existing tests pass.
- Commit cleanly on `swarm/lexer` (do not push or merge).
- Post to `@verifier,@coordinator` with the commit SHA, test count, and benchmark/test summary.
- When `@verifier` posts a PASS with no requested fixes, stay silent (output `NO_REPLY`).
