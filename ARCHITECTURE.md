# RepoMemory — Architecture (Full Build, No Phases)

> Companion to `RepoMemory_Project_Guide.md` (idea) — this file is the build contract.
> Stack: Python-only MVP. Reuse first, port second, invent last.

## 0. Principles

1. **File Agent = record on disk + templates, LLM lazy.** 2000 files = 2000 JSON records, ~zero idle cost (Guide §2.2).
2. **Deterministic first, LLM second.** Facts via parsers; interpretation via LLM with `SHA + confidence` (Guide §4.7).
3. **Single recall, not tool loops.** One `recall()` → generate saves 25–89% tokens vs per-step MCP calls (repomemory-v2 `A2E_INTEGRATION.md` measurement).
4. **Cite everything.** Every memory carries `sha, file:line, quoted evidence`. Verify at read, self-heal on contradiction (Copilot-memory + GCC pattern).

## 1. System View

```
GIT REPO (files + log --follow + PRs/issues via github-mcp-server)
  |
  v
[C1 Profiler] ----Profile----> [C3 Store]
[C2 Miner -> Distiller] --HistoryNote--> [C3]
[C4 Graph Builder] --graph.json--> [C3]
                                    |
Request / Patch / Commit --> [C5 Orchestrator: route->fetch->join->cross-check->prioritise]
                               -> [C6 Injector] -> [C8 LLM Gateway -> Patch]
                               -> [C7 Verifier/Reporter] -> per-file warnings + blast-radius
New commit --> [C9 Updater] -> C3 delta, trigger C5 if contract changed
```

## 2. Components

### C1 Profiler (deterministic, no LLM)
- In: `file_path` + source. Out: `Profile` JSON.
- Extract: funcs/classes, params+types+returns, imports/exports, call-sites, constants/config keys.
- Reuse: `codmap` (griffe+networkx+jedi) or stdlib `ast`. `archsight` parsers as fallback.
- Pattern from `Understand-Anything`: emit `importMap` once, pass to analyzers so they don't re-derive imports.

### C2 Miner + Distiller
- Miner (deterministic, `GitPython + PyDriller`): `sha, msg, author, date, diff, files, PR/issue link`. Skip whitespace-only/format commits. Follow renames (`log --follow`). Flag squash/rebase as `confidence: low`.
- Distiller (LLM, once + cached): Chronos 4-field card:
  `Summary / Signals / Motivation / Solution + kind [BUGFIX|FEATURE|REFACTOR|REVERT|PERF|SEC] + risk_flag [DO-NOT-REMOVE + lines] + reverted_attempt? + confidence [high|med|low] + evidence [sha + quote]`.
- Compact prompt style from `Augment Context Lineage` (Gemini-Flash-style: goal + files/funcs + terms).

### C3 Memory Store
- Layout:
```
.repomemory/
  profiles/<relpath>.json
  history/<relpath>.json
  graph.json
  index.sqlite  # BM25 + metadata + access counts + metrics/
  cache/llm.json
```
- Alternatively live server: `rohitg00/agentmemory` (`:3111` REST, `:3113` viewer) via `memory_smart_search`. Use file layout for MVP, server for team demo.
- `MemoryCard` schema:
```json
{
  "file": "payments.py",
  "profile": {"functions": [{"name": "process_refund", "params": {"order_id": "str", "amount": "Decimal"}, "returns": "Receipt"}], "imports": ["gateway_client"], "exported": ["process_refund"]},
  "history": [{"sha": "c1a9f3", "kind": "BUGFIX", "summary": "dup refunds >10k on retry", "why": "gateway retry", "risk_flag": "guard L80-90 DO NOT REMOVE", "confidence": "high"}],
  "decisions": ["Decimal never float", "wrap gateway errors"],
  "dependents": ["orders.py", "billing.py"],
  "dependencies": ["gateway_client.py"]
}
```
- Search: BM25 now (keyless, like agentmemory default), ChromaDB/`all-MiniLM-L6-v2` later. Recall weights (from v2 `a2e` template): skills/examples 2.0x, corrections 1.5x w/ `[CORRECTION]` prefix + 2x boost, knowledge 1.0x. Cap via `maxItems/maxChars`.
- Port from `MauricioPerera/repomemory-v2`: immutable commit chain, content-addressable objects, `saveOrUpdate` dedup, file locking, `history(id)`, `verify()`, `export/import`, TTL cleanup, 4-layer secret sanitization.

### C4 Dependency Graph Builder (deterministic)
- In: all Profiles. Out: directed `graph.json` (NetworkX): `IMPORTS`, `CALLS func`, `fan_in/out`, cycles via Tarjan SCC.
- `blast_radius(file)` = transitive dependents. `impact` for diff.
- Reuse: `archsight` (cycles, blast-radius, articulation points, temporal coupling, git churn) + `codmap` graph. Pipeline from `Understand-Anything`: `project-scanner -> file-analyzer x5 parallel (20-30/batch) -> architecture-analyzer (layers) -> graph-reviewer (referential integrity)`.
- Commit `graph.json` (like `.ua/knowledge-graph.json`); `diff-overlay.json` as scratch; git-lfs if >10MB.

### C5 Orchestrator (LangGraph, dsh-pattern)
- Graph: `router -> fetcher -> joiner -> cross-checker -> prioritiser`.
- Router: task files + 1-hop dependents/dependencies via C4.
- Fetcher: single `recall()` per file (not per-step tools).
- Joiner: merge cards, dedupe.
- Cross-checker deterministic first:
  1. `caller arg types vs callee signature` → mismatch free,
  2. `deleted lines ∩ risk_flags` → regression,
  3. `similarity to REVERT note` → already-tried,
  then LLM only for `violates decision?` + fix phrasing.
- Prioritiser budget e.g. 4k: always `risk_flags + contract diff`, then `BM25(task,note)+recency+confidence`.
- Mirrors `deepseek-ai/deepseek-harness`: `main` + `subagent spawn/fork` + `workflow-worker-thread` + `ralph maxRounds 64` + `todo_write` trace + checkpoint/JSONL. Host owns registries/sandbox/persistence; file agents are scoped plugins (`isolate: realm`).
- Tracing via LangSmith. Alternative `Microsoft Agent Framework 1.0` if on Azure.

### C6 Context Injector
```
[task] + [target code] + [selected history + decisions + DO-NOT-REMOVE] + [dependent contracts] + [failed approaches]  <= budget
```
- Policy v1: `all risk_flags + top-k BM25 + last-N`, ablatable later.

### C7 Verifier / Bug Reporter
- On unified diff, per-file output (Guide §2.7):
```
payments.py
  [!] guard L80-90 removed, added in c1a9f3 (dup refunds). Likely regression.
  [!] process_refund(order_id: str->int); 3 dependents still pass str: orders.py:41...
```
- Plus `ruff/pyright` sanity + fail-to-pass tests via `mini-swe-agent` runner.
- Copy `Understand-Anything /understand-diff` ripple view.

### C8 LLM Gateway
- `LiteLLM` wrapper: 1 API model + 1 open model (Ollama). Log tokens/latency per call (RQ4).

### C9 Incremental Updater
- Trigger `git diff --name-only` / post-commit hook (`--auto-update` like Understand-Anything, `hook install` like DanielGuru).
- Changed files only: re-run C1 + append C2, patch C4, if contract diff → C5 check. Fingerprint change detection (Tree-sitter style).

## 3. Workflows (all live day 1)

- **W-A Cold start:** `walk -> C1 all -> C4 -> C2 all (batched, cached) -> C3`.
- **W-B New commit:** `changed -> C1+C2 delta -> C4 patch -> if contract changed: C5+C7`.
- **W-C Generate:** `request -> C5 route+fetch -> C6 inject -> C8 patch -> C7 verify -> patch+warnings`.
- **W-D Scan:** `C4 full -> C5 all-edges check -> C7 report` (no LLM, first demo).

## 4. CLI (Guide §6.3 demo)

```
repomem init ./target
repomem card payments.py        # print MemoryCard
repomem scan                    # W-D: mismatches + cycles + blast-radius
repomem check patch.diff        # W-B/C7: guard-removed + contract-change
repomem gen "add retry limit"   # W-C: patch + warnings + tokens
```

## 5. Reuse Map (don't build)

| Need | Use | Source |
|---|---|---|
| Orchestration runtime | LangGraph | first sweep (43k stars) |
| Orchestration pattern | main+spawn/fork+workflow+ralph+todo_write | deepseek-harness |
| Memory layout + tools | `.context/` + 7 tools + `risk/sync/hook` | DanielGuru/repomemory |
| Memory engine to port | commit chain + `recall()` + dedup + verify + A2E template | MauricioPerera/repomemory-v2 |
| Memory server (alt) | `:3111/:3113`, BM25+Vector+Graph RRF, hooks | rohitg00/agentmemory (29.5k) |
| Graph pipeline | scanner→analyzer(x5)→reviewer, importMap, fingerprint, committed json | Egonex-AI/Understand-Anything (86k) |
| Graph libs | parsers + blast-radius + cycles | archsight, codmap, ast/griffe/networkx |
| Git mining | log --follow, diff, churn | GitPython, PyDriller |
| PR/issue text | issues/PRs/commits tools | github/github-mcp-server |
| Generator + eval | 100-line agent + Docker harness | mini-swe-agent + SWE-bench |
| History→card idea | episodic+semantic, +4.9% localization | Repository Memory paper |
| Card schema | Summary/Signals/Motivation/Solution + graph | Chronos |
| Session memory contrast | AST-guided (cite as B2-adjacent) | CodeMEM |
| Commit summarizer | compact diff summaries | Augment Context Lineage |
| Baselines in paper | plain / RAG / AST-graph / session-memory | B0-B3 (Guide §5.2) |

Baselines for paper stay `mem0 / Zep-Graphiti` (citable); system uses v2-pattern/agentmemory internally.

## 6. Decisions Locked

- Python-only MVP. SQLite+files now, Chroma later. BM25 now, embeddings opt-in.
- Corrections 2x boost + `[CORRECTION]` prefix. Secrets redacted before save.
- `risk_flags` + contract diff always in budget. Everything cited `SHA:file:line`.

## 7. Known Gaps (needs spike before coding)

1. `src/`-level port specs (v2 recall/scoring, UA analyzer/reviewer, dsh composition) — READMEs only so far.
2. Distiller prompt + classifier thresholds + `fix/wip` handling + rename/split policy.
3. Citation verification + injection defense + SZZ/BugsInPy benchmark + human-agreement protocol.
4. Full lit sweep to defend novelty (Guide §1.6/Part 7 was shallow).

## 8. References

- Guide: `RepoMemory_Project_Guide.md` Parts 1-8.
- `deepseek-ai/deepseek-harness`, `DanielGuru/repomemory`, `MauricioPerera/repomemory-v2` (+ `docs/A2E_INTEGRATION.md`), `rohitg00/agentmemory`, `Egonex-AI/Understand-Anything`.
- Papers: CodeMEM, Chronos, Repository Memory, GCC, MemTrace, Copilot-memory (Jan'26).
