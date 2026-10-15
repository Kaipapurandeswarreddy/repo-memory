# RepoMemory — Spike Specs (Closing the 4 Gaps)

This file turns ARCHITECTURE.md §7 gaps into copy-paste build specs. Sources are real `src/` + papers, not READMEs.

---

## Spike 1: src-level port specs (Python)

### 1A. Recall (`repomemory-v2/src/recall/engine.ts` + `templates.ts` + `formatter.ts`)

Port verbatim to `store.py: recall()`:

```python
def recall(agent_id, user_id, query, max_items=20, max_chars=8000,
           template="technical", collections=("memories","skills","knowledge")):
    tmpl = BUILTIN_TEMPLATES[template]  # sectionOrder, collectionWeights, preamble, extractFewShot
    fetch_limit = max(10, max_items*2)
    pool = []
    for coll, weight in [(“memories”,tmpl.memories),(“skills”,tmpl.skills),(“knowledge”,tmpl.knowledge)]:
        for r in search(coll, query, fetch_limit):  # TF-IDF + tag + decay + access (see 1B)
            pool.append((r.score*weight, r))
    pool.sort(reverse=True); selected = pool[:max_items]
    profile = get_profile(agent_id, user_id) if include_profile else None
    increment_access([r.id for _,r in selected]); flush()
    formatted = format_with_template(selected, profile, tmpl, max_chars)  # sections in tmpl.sectionOrder, truncate to max_chars
    few_shot = extract_few_shot(selected, tmpl.maxFewShot) if tmpl.extractFewShot else None
    return {memories, skills, knowledge, profile, formatted, few_shot}
```

Templates to ship (from `templates.ts`):
- `default`: profile→memories→skills→knowledge, no weights.
- `technical` (use for code): skills 1.5x, knowledge 1.3x, memories 0.7x; headers `Procedures & Patterns / Technical Reference / Context Notes`.
- `a2e` (use for injector): skills 2.0x, memories 1.5x, knowledge 1.0x, order skills→memories→knowledge→profile, `maxFewShot=7`, preamble `Output ONLY valid … Corrections [CORRECTION] = avoid`.
- Few-shot extraction: regex `\[(MCP|CALC|FETCH|A2E):\s` in skill content → `{user: derive_question(content,tags), assistant: content}`. `derive_question` = text before tag, strip `para/to/use/when`, ensure `?`.

### 1B. Scoring (`src/search/scoring.ts`) — use exact formula

```python
score = (tfidf*norm_tfidf + tag_overlap*norm_tag + emb*norm_emb) * decay * access_boost [*2.0 if correction]
# defaults: tfidf 0.7, tag 0.3, emb 0, decay_rate 0.005, maxAccessBoost 5.0, correctionBoost 2.0
# decay = exp(-0.005*days_since_update), floor 0.01; 0 disables
# access_boost = min(log2(2+access_count), 5.0)  # 0→1.0, 1→1.58, 10→3.58
# tag_overlap = Jaccard(stem(lower(tags_entity)), stem(lower(tags_query)))
```

### 1C. Search engine (`src/search/search-engine.ts` + `tfidf.ts` + `tokenizer.ts` + `stemmer.ts` + `query-expander.ts`)

- Per-scope TF-IDF index (`scope = "memories:agent:user"`), cached to `index/tfidf/<safeName>.json` (encode `_`→`%5F` to avoid collisions), LRU 100, `flush()` on dirty, atomic write.
- Indexed text = `type + content + tags + category + questions`.
- Query expansion: synonym map (`ts config → TypeScript configuration` etc.) + Porter stemming. Rank → `rankMultiScope` max-merge across scopes.
- No embeddings needed for MVP (BM25/TF-IDF only, like agentmemory keyless).

### 1D. Graph pipeline (`Understand-Anything/understand-anything-plugin/agents/*.md` + `src/*.ts`)

Two-phase file-analyzer (port to Python `ast`+`tree-sitter`):
1. **Deterministic script** (`extract-structure.mjs` → `profiler.py`): functions/classes/exports/callGraph/metrics + `batchImportData[file] = [resolved internal paths]` from `project-scanner`. Never re-resolve in LLM.
2. **LLM semantic**: 1–2 sentence summary (never filename restatement), 3–5 hyphenated tags, `complexity simple<50 / moderate<200 / complex`, `languageNotes` only if educational.
- Significance filter: emit `function:` only if `lines≥10 or exported`; `class:` only if `methods≥2 or lines≥20 or exported`. Exception: `previousSymbols` (incremental) must be re-emitted even if now below threshold; report deletions explicitly, never silent-drop.
- Node IDs strict: `file:<path>`, `function:<path>:<name>`, `class:<path>:<name>`, `config:`, `document:`, `service:`, `table:`, `endpoint:`, `pipeline:`, `schema:`, `resource:`. No project prefix.
- Edges with fixed weights: `contains 1.0, inherits/implements 0.9, calls/exports 0.8, imports 0.7, depends_on/configures 0.6, documents/tested_by 0.5`. `imports` count must equal `len(batchImportData[file])` — self-check before write.
- Batching: 5 workers, 20–30 files/batch, `neighborMap` for cross-batch `calls/inherits` confidence. Output `intermediate/batch-<i>.json` (split if nodes>60 or edges>120 → `batch-<i>-part-<k>.json`, regex `batch-(\d+)(?:-part-(\d+))?\.json` or dropped).
- Graph-reviewer (deterministic script before LLM judge): Checks 1–9 — schema, referential integrity (no dangling), completeness (≥1 node/edge/layer/tour), layer coverage (every file-level node in exactly one layer), uniqueness, tour 5–15 steps sequential, no empty/generic summaries, no self-edges, warn orphans + missing `documents/deploys/triggers/defines_schema`, type↔prefix match. `approved = (issues==[])`.
- Diff/impact (`src/diff-analyzer.ts` + `context-builder.ts`): `blast_radius` + ripple list for verifier.

### 1E. Orchestrator turn flow (`deepseek-harness/docs/architecture.md` + `agent-lifecycle.md`)

Map dsh to LangGraph nodes:
```
turn/start → claim input → assemble prompt (systemPrompt + tool schemas)
  → agent/pre-step (rewrite/reject; empty first claim closes turn, no step)
  → step/start → agent/request + prepareCall (cancellation commits neither)
  → freeze history from log → stream → assistant/message|attempt
  → tool/call → pre-execute → execute → post-execute → tool/result
  → step/end → if tools owe request or inbox non-empty → next step else agent/turn-stopping → turn/end
```
Rules: **model-visible means logged** (`SessionEvent` JSONL `session.vN.jsonl.zstd`); prompt travels as `system/message` nodes; `subagent spawn` = fresh, `fork` = inherits history one-shot; `isolate: realm` per agent preset; capability seams (Service Definition/Provider/Consumer) for fs/tools/sandbox/subprocess so one provider swap moves Bash+PTY+LSP together.

---

## Spike 2: Distiller prompt + classifier + rename policy

### Classifier (few-shot wins)
Per `arXiv 2605.02033` (3,200 InfluxDB commits, Mistral-7B/LLaMA-3-8B/DeepSeek-R1-32B): **few-shot > zero-shot > CoT** for commit classification; scale helps (R1-32B best). Use few-shot with decision-priority rules (explicit `fix:/feat:` keyword beats inferred meaning).
- Labels: Conventional Commits `fix/feat/BREAKING/refactor/perf/test/docs/chore` → collapse to RepoMemory `BUGFIX/FEATURE/REFACTOR/REVERT/PERF/SEC` + `BREAKING?`.
- Backup signal when msg is `fix/wip/update`: Levin & Yehudai features — keywords (19 binary: fix,bug,refactor,implement…) + AST change types (46 binary: STATEMENT_DELETE, METHOD_RENAMING, ADDITIONAL_CLASS…). Combined RF ~80–85% acc (`gitFahim/Github-Commit-Classification` repro). Fuse: `if msg confidence low → use diff features`.
- Filter: skip whitespace-only, `>80% comment/blank`, pure merges (unless conflict), binary.

### Distiller prompt (final template)

```
You are a senior engineer writing a memory card. Given: commit msg, diff (truncated), linked issue/PR.
Output JSON: {summary<=25w, why, kind, risk_flag {lines, text}?, reverted_attempt?, confidence}
Rules: quote evidence, never invent rationale; if msg is "fix/wip" set confidence=low and rely on diff; mark DO-NOT-REMOVE only if diff adds guard + msg/issue mentions bug/regression.
Few-shot: 2x BUGFIX (guard + revert), 1x REFACTOR, 1x FEATURE.
```

Prompt style from CommitBench RAG work: `<=12-word` imperative + retrieval of top-3 similar historical (diff,msg) via TF-IDF+MiniLM hybrid as exemplars (TF-IDF stable for short diffs, hybrid for refactors).

### Rename / split / history policy (`GitPython + PyDriller`)
- `git log --follow -- <file>` always; store `prev_paths[]`.
- Splits: if similarity < threshold or 1→N, keep parent card + link children with `split_from: sha`, confidence med.
- Squash/rebase: mark `history_flattened: true`, confidence low.
- PyDriller `Commit.modified_files` gives `old_path/new_path`, diff, complexity; `traverse_commits` for incremental updater fingerprint.

---

## Spike 3: Verification + injection defense + benchmark protocol

### Citation verification (read-time, from Copilot-memory + GCC)
Before injecting any note: check `sha exists && file:line still matches quoted evidence` via `git show sha:path` + current read (2 cheap reads). If contradict → mark stale, store corrected version with new timestamp (self-heal, as Copilot adversarial tests). Never present guessed rationale as fact.

### Secret + injection defense (port `v2/src/a2e/sanitize.ts` 4 layers)
1. Known secrets `value→{{VAR}}` (length-desc, ≥4 chars).
2. URL params `apikey,token,secret,password,appid,client_secret,auth…` → `{{PARAM}}` (string split, not URL-encode, to preserve `{{}}`).
3. JSON keys `authorization,api_key,bearer,private_key…` → `{{KEY}}`.
4. Prefixes `Bearer/Basic/sk-/ghp_/xoxb-/sk-ant-/eyJ` + high-entropy → `{{REDACTED_*}}`.
Plus: v2 ID validation (reject `/,\,.., :, \0`, tags ≤50, content ≤1MB), lockdown mode for public repos (only push-users' content), `X-MCP-Readonly` equivalent for scan mode.

### Benchmark (SZZ + BugsInPy + SWE-bench + audit)
- Mining: SZZ family — start `R-SZZ` (most precise ~70% w/ issue-date filter, Rosa et al.): annotation graph + ignore cosmetic/meta + latest-touch only. Note limits: only deleted lines (use A-SZZ block expansion for added lines), revert handling. Upgrade path `LLM4SZZ` (+6.9–16% F1, handles added-lines + full patch context + commit msg) and `Fonte` (FL-weighted ranking, +45.8% MRR, top-5 87% on SAP HANA).
- Dataset: `BugsInPy` (493 bugs, 17 Python projects, Defects4J-for-Python) for RQ1; `SWE-bench Verified` slice (25 tasks like `farrellh1/agent-eval-harness`) for generation. Rewind to post-fix commit F, craft tempting refactor task touching fixed region, run B0–B3 vs Ours, check `FAIL_TO_PASS` flips to fail = regression. Always report `patch correctness` alongside regression rate.
- Audit corpus first (`cli.py audit` pattern): flag `broken-tests` (truncated `test_x[w/` ids abort pytest → false 0), `broad` (438 tests flip = sweeping, not fix), `contaminated` (gold patch in prompt). Quarantine corrupted ids.
- Human judgment: sample contradiction-rate checks with `Accuracy/Integrity/Readability/Applicability` (TSE 2024) + Cohen Kappa; report agreement; LLM-judge only with spot-check.

---

## Spike 4: Lit sweep (novelty defense, one paragraph each)

- **HAFixAgent (arXiv 2511.01047, Nov 2025)** — closest threat. Blame-derived history (last-touch commit + coevolving files) injected into repair agent loop. 854 Defects4J + 501 BugsInPy. +4.4% Defects4J / +38.6% BugsInPy (single-file multi-hunk), robustness under +1/+3/+5 FL shift (40–56% vs 0%), no extra steps/tokens. Contrast: single blame commit per bug vs your per-file persistent memory + orchestrator across dependents + regression-focused eval.
- **HAFix (Shi et al. 2026)** — single-prompt blame context for single-line bugs. Yours generalizes to multi-hunk + cross-file via agents.
- **Repository Memory (Wang et al. 2026b)** — episodic (BM25 commits+issues) + semantic (hot-file summaries) for localization (+4.9% LocAgent). No per-file ownership, no orchestrator, no generation/regression eval.
- **Chronos** — PR experience cards + typed evolution graph + multi-hop retrieval (69.2→72.9% SWE-Agent). Test-time retrieval, not persistent per-file agents.
- **CodeMEM (2601.02868)** — AST-guided session memory (not git history). Your §7.1 contrast.
- **CommitChronicle (ASE 2023, 10.7M commits)** + **ERICommiter / CommitBench** — history improves generation/completion; supports premise, not the architecture.
- **LLM4SZZ / Fonte / SZZ variants** — BIC identification methods; you use them as benchmark miners, not the contribution.
- **Mem0 / Zep-Graphiti / A-MEM / GCC / MemTrace** — general agent memory; yours is tied to verifiable git history per file.

**Defense line:** *No prior work organises version-control history as persistent per-file agent memory coordinated through a dependency-aware orchestrator and evaluated on regression reintroduction. If a close match surfaces, claim the combination (granularity + orchestration + history-as-memory + regression eval).*
