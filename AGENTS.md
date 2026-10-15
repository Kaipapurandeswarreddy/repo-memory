# AGENTS.md — RepoMemory discipline (applies to every agent session)

This repo has per-file memory. Use the `repomemory` MCP tools; do not work blind.

1. **Orient first.** Before touching any file, call `repomemory.recall` with your
   task (or `repomemory.card` for the exact file). Respect `risk` flags and
   `fix keeps` lines — they are past bugs. Never remove them without checking.
2. **Verify after.** After producing any diff, run `repomemory.verify` on the
   patch text. `[!]` findings are likely regressions — fix them before finishing.
3. **Record.** If you discover a design reason or a regression, save it so the
   next session knows (memory grows; `CLAUDE.md`-style files go stale).
