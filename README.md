# RepoMemory — quickstart

> Full idea + build contract: `RepoMemory_Project_Guide.md`, `ARCHITECTURE.md`, `SPIKES.md`.

```bash
pip install -e .            # or: pip install -r requirements.txt
repomem init ./target --max-commits 20
repomem card payments.py
repomem scan
repomem check patch.diff
repomem gen "add retry limit" --target payments.py   # stub, zero API keys
```

- Stub gateway is default (deterministic, no network). `--mode openai` needs `OPENAI_API_KEY`.
- Store lives in `<target>/.repomemory/` (profiles, history, graph.json).
- `repomem check` exits 2 on high-severity (guard-removed) warnings.
- **Nested git repos:** run `init`/`check`/`scan` with cwd (or `--repo`) set to the
  nested root that owns the files. A diff taken from a parent repo parses to
  zero files there — `check` warns (`unparsed-diff`) instead of reporting clean.
- **Windows diffs:** PowerShell `>` redirection writes UTF-16; `check` sniffs
  BOMs and still parses, but prefer `Out-File -Encoding utf8` for clean files.
