# RepoMemory
### A History-Aware Multi-Agent Framework for Context-Preserving Code Generation

**Project type:** B.Tech Major Project (AI/ML, software side), publication-oriented
**Team:** K. Purandeswar Reddy (23X51A3236), K. Siva (23X51A3220), N. Chakradhar (22X51A3265)
**Guide:** S. Seetharamudu, Assistant Professor
**Institution:** Santhiram Engineering College, Nandyal (Autonomous), Dept. of CSE (AI)

> **How to read this document.** Part 1 is the story and the intuition (read this first, it makes everything else obvious). Part 2 is the architecture. Part 3 is how it behaves step by step. Part 4 is the hard problems you will hit while building. Part 5 is how you prove it works (this is what makes it publishable). Part 6 is the plan. Part 7 is the base papers.
>
> **Honesty note.** Everything in Part 7 came from literature searches done during our discussion. I read abstracts and summaries, not full papers. Before you cite anything, open the paper and verify the details. The novelty claim in Section 1.6 also needs a deeper literature search before you write it into a paper.

---

# PART 1: THE IDEA

## 1.1 The story (start here)

Imagine two engineers fixing a bug in the same file, `payments.py`.

**Engineer A** joined yesterday. She opens the file, sees a strange-looking check on line 84, thinks "this is redundant," and deletes it. Tests pass. She ships.

**Engineer B** has worked at the company for five years. He opens the same file, sees the same check, and stops. He remembers: *"That check was added after the March outage. Without it, refunds over 10,000 get processed twice when the payment gateway retries. Priya added it. We argued about it in the PR."*

The code in front of them is identical. The difference is **memory of history**.

Today's AI coding assistants are Engineer A. Every time, no matter how smart they are. They see what the file looks like *right now*. They have no idea why it looks that way. So they:

- delete "weird" code that was a hard-won bug fix (a **regression**),
- rewrite things that a teammate deliberately designed a certain way (a **contradicted design decision**),
- change a function's signature without knowing five other files depend on it (a **broken API contract**).

**RepoMemory's idea: give every file its own Engineer B.**

Not one big assistant that tries to remember the whole repo (that does not fit in any context window and gets confused). Instead: one small, dedicated "librarian" per file. Each librarian knows its own file extremely well, including its whole git history, and knows who depends on it. When anyone (a human or an AI) is about to touch that file, they ask the file's librarian first.

## 1.2 The analogy, extended

Think of a big hospital.

```
Without RepoMemory                       With RepoMemory
------------------                       ---------------
One doctor sees a patient                Each patient has a full medical chart:
for 5 minutes, with no chart.            every past surgery, every allergy,
                                         every medication and WHY it was changed.
"Looks fine, take this drug."
                                         A coordinator (orchestrator) also knows
(Patient is allergic. Nobody told        which specialists need to be informed
 this doctor.)                           when one patient's treatment changes.
```

- **File** = patient
- **Git history** = medical chart (what happened and why)
- **File agent** = the nurse who knows that patient's chart by heart
- **Orchestrator** = the head nurse who knows which patients are connected (same ward, shared equipment, drug interactions)
- **AI code generator** = the visiting doctor who must consult the chart before prescribing

## 1.3 The problem, stated precisely

AI code generation tools build their context from **what is visible now**: the current file contents, maybe some retrieved neighbouring code (via embeddings, AST structure, or call graphs). They miss three kinds of information that exist in the repository but are never shown to the model:

| Missing information | Where it actually lives | What goes wrong without it |
|---|---|---|
| **Why** code is the way it is | Commit messages, PR discussions, diffs | Model "cleans up" deliberate code, reintroduces fixed bugs |
| **What was tried and reverted** | Revert commits, deleted code in old diffs | Model proposes an approach the team already rejected |
| **Who depends on this** | Imports and call sites across other files | Model changes an interface and silently breaks callers |

The first two are **temporal** (they live in history). The third is **structural** (it lives across files). RepoMemory addresses both, with one architecture.

## 1.4 The core idea in one paragraph

Assign one persistent agent to every file in a repository. Each file agent maintains (a) a structural profile of its file (functions, type signatures, exposed API) and (b) a history memory distilled from that file's complete git log (what changed, why, what was reverted). An orchestrator agent connects the file agents through the repo's dependency graph so that cross-file problems (like a type mismatch between a caller and a callee) can be caught by letting the two agents compare notes. When new code is requested, the relevant file agents inject their history and structural context into the generation prompt, so the model behaves like the five-year veteran instead of the new hire.

## 1.5 What we are NOT claiming

Be careful about scope. These are traps that reviewers will poke:

- We are **not** claiming a new LLM or a new model architecture.
- We are **not** claiming to find all bugs. We target a specific class: regressions, design contradictions, and cross-file interface mismatches.
- We are **not** claiming "git history is useful for code" is new in general. People have used history for bug localisation, defect prediction, and commit-message generation for years. The claim is narrower (see 1.6).

## 1.6 The novelty claim (and how to defend it)

**Working claim:** Existing repository-level code generation systems build context from *static structure* (ASTs, call graphs, retrieval) or *session memory* (what was said earlier in a chat). We treat **version-control history, organised per file, as a first-class persistent memory for an LLM code agent**, and we coordinate those per-file memories through a dependency-aware orchestrator.

**Why this might hold up:** among the repo-level generation and agent-memory papers we found (Part 7), none organised memory by file ownership using git history as the source.

**Why you must still double-check:** our search was shallow. Before writing the paper, do a proper literature sweep on:
1. LLM + commit history for code completion, repair, or review,
2. history-aware program repair,
3. agent memory systems applied to software repos,
4. "code archaeology" tools.

If you find close work, do not panic. Reposition the contribution as: *per-file agent granularity + cross-file orchestration + history as memory + a regression-focused evaluation*. The combination can still be new even if each piece has precedent.

---

# PART 2: ARCHITECTURE

## 2.1 The big picture

```
                         +-----------------------------+
                         |       GIT REPOSITORY        |
                         |  (files + full commit log)  |
                         +--------------+--------------+
                                        |
                       ingest (once) + update (every commit)
                                        |
          +-----------------------------+-----------------------------+
          |                             |                             |
   +------v------+               +------v------+               +------v------+
   | FILE AGENT  |               | FILE AGENT  |               | FILE AGENT  |
   |  user.py    |               | payments.py |               |  orders.py  |
   |             |               |             |               |             |
   | [Profile]   |               | [Profile]   |               | [Profile]   |
   | [History]   |               | [History]   |               | [History]   |
   +------+------+               +------+------+               +------+------+
          |                             |                             |
          +-------------+---------------+---------------+-------------+
                        |                               |
                +-------v--------+              +-------v--------+
                |  DEPENDENCY    |<------------>|  ORCHESTRATOR  |
                |  GRAPH         |              |     AGENT      |
                | (who imports / |              | (routes, joins,|
                |  calls whom)   |              |  cross-checks) |
                +----------------+              +-------+--------+
                                                        |
                         +------------------------------+--------------------+
                         |                                                   |
                +--------v---------+                              +----------v---------+
                | CONTEXT INJECTOR |                              |   BUG REPORTER     |
                | builds the prompt|                              | per-file findings  |
                | for code gen     |                              | (type mismatch...) |
                +--------+---------+                              +--------------------+
                         |
                +--------v---------+
                |  LLM / CODE GEN  |  <- human dev or AI assistant asks here
                +------------------+
```

## 2.2 A crucial practical insight: "an agent per file" does NOT mean "an LLM running per file"

If a repo has 2,000 files, you obviously cannot keep 2,000 LLM calls running. The right mental model:

> A **file agent is mostly a persistent memory record plus a small set of skills**, and the LLM is called **lazily**, only when that agent has to reason about something.

```
File agent  =  [ memory record on disk ]  +  [ prompt templates / skills ]
                       |                              |
                  always exists               LLM called only on demand
                  (cheap, durable)            (expensive, occasional)
```

When a file has not changed and nobody asks about it, its agent costs you nothing. This single design decision is what makes the project feasible on a student budget. Say this explicitly in your paper.

## 2.3 Component 1: The File Agent

Each file agent owns two kinds of knowledge.

### (a) Structural Profile ("what the file is, right now")

Extracted **deterministically** (no LLM needed, so it is fast, free, and does not hallucinate):

- functions and classes defined, with parameter names and types
- return types
- what the file imports and what it exports
- which other files call into it
- constants, config keys, and global state it touches

This is the file's "public contract."

### (b) History Memory ("how the file got this way")

Built from `git log` for that file (following renames). For each meaningful change the agent keeps a distilled note:

- **what** changed (summarised from the diff)
- **why** (from the commit message, linked issue or PR text if available)
- **kind** of change: bug fix, feature, refactor, revert, performance, security
- **risk flags**: "this line exists because of bug X, do not remove without checking"
- **reverted attempts**: things tried and undone

The distilled history is much smaller than the raw log. The agent stores both: the raw pointers (commit hashes) for verification, and the distilled notes for prompting.

### What a file's "memory card" contains (conceptual)

```
FILE: payments.py
-----------------------------------------------------------
PROFILE
  public functions: process_refund(order_id: str, amount: Decimal) -> Receipt
                    retry_charge(...)
  imported by:      orders.py, billing.py, admin_tools.py
  imports:          gateway_client.py, models.py

HISTORY (distilled)
  [c1a9f3] BUG FIX  "refunds > 10,000 processed twice on gateway retry"
           -> guard at lines 80-90 exists because of this. DO NOT REMOVE.
  [77be20] REFACTOR split validate() out of process_refund()
  [4d02aa] REVERT   switched to async gateway calls, reverted (race condition)
           -> async approach was already tried and failed here.
  [91c0de] FEATURE  added partial refunds

DESIGN DECISIONS (pulled out of messages/PRs)
  - amounts are Decimal, never float (money)
  - all gateway errors are wrapped, never leaked to callers
-----------------------------------------------------------
```

That card is the entire product in miniature.

## 2.4 Component 2: The Dependency Graph

A map of "who talks to whom." Built from imports and call sites using static analysis.

```
 user.py ---------> payments.py ---------> gateway_client.py
    |                    ^
    |                    |
    +------> orders.py --+
```

An arrow means "depends on." This graph decides which file agents need to talk to each other. It is also how we compute the **blast radius** of a change: if `payments.py` changes its interface, the graph tells us `orders.py`, `billing.py`, and `admin_tools.py` are affected.

## 2.5 Component 3: The Orchestrator Agent

The head nurse. Its jobs:

1. **Route.** When a task touches file X, find X's agent and the agents of X's dependents.
2. **Join.** Combine the relevant memory cards into one coherent context (this is the hard part, see Part 4).
3. **Cross-check.** Take two connected agents and verify their contracts agree (caller sends `int`, callee expects `str`).
4. **Prioritise.** Decide which memories matter most when the token budget is limited.

The orchestrator should use **deterministic checks first** (compare type signatures from the profiles) and call the LLM only for things that need judgment (e.g., "does this change violate the design decision recorded in the history?").

```
Cross-file check, the cheap way first:

   caller profile says:  calls process_refund(order_id: int, ...)
   callee profile says:  process_refund(order_id: str, ...)
                                  |
                           MISMATCH found deterministically (free)
                                  |
                           only now ask the LLM to explain / suggest fix
```

## 2.6 Component 4: The Context Injector

Takes a request ("add a retry limit to refunds") and builds the prompt:

```
[ the task ]
+ [ target file: current code ]
+ [ target file agent: relevant history + design decisions + DO-NOT-REMOVE flags ]
+ [ contracts of files that depend on this one ]
+ [ any recorded failed approaches relevant to this task ]
        |
        v
   fits inside the model's context window (budgeted)
```

The skill here is **selection**: not dumping the whole history, but choosing the few notes that matter for this task.

## 2.7 Component 5: The Bug Reporter

Collects findings and attributes them to the **responsible file/agent**, not a flat global list. Example output:

```
payments.py  (agent report)
  [!] process_refund() signature changed (order_id: str -> int)
      3 dependents still pass str: orders.py:41, billing.py:112, admin_tools.py:9
  [!] The double-processing guard (lines 80-90) was removed.
      Added in c1a9f3 to fix duplicate refunds. Likely regression.
```

---

# PART 3: HOW IT BEHAVES (STEP BY STEP)

## 3.1 Workflow A: Cold start (first time on a repo)

```
1. Walk the repo, list source files.
2. Parse each file -> build Structural Profiles.
3. Build the Dependency Graph from imports/calls.
4. For each file: read its git history (following renames).
5. Distil history into the History Memory (LLM summarisation, done once).
6. Persist everything to the memory store.
```

This is the expensive step (many LLM calls for summarising history). It is done once and can be batched and cached. Cost control matters here (see 4.3).

## 3.2 Workflow B: A new commit lands (incremental update)

```
commit arrives
   |
   +-> which files changed?  (git diff --name-only)
   |
   +-> for each changed file: re-extract Profile, append new History note
   |
   +-> did any public contract change?
          |
          yes -> orchestrator runs cross-check against dependents
          no  -> done
```

Only changed files' agents wake up. Everyone else stays asleep.

## 3.3 Workflow C: Code generation request

```
request: "Make refunds idempotent"
   |
   v
orchestrator: which files are involved?  -> payments.py (+ dependents)
   |
   v
fetch memory cards for those files
   |
   v
context injector: pick the relevant notes, respect token budget
   |
   v
LLM generates a patch with that context
   |
   v
(optional) verify: run cross-file check on the generated patch
   |
   v
present patch + warnings ("this removes a guard added in c1a9f3")
```

## 3.4 Workflow D: Standalone bug scan

Run the cross-checks across the whole dependency graph without any generation request. This is the "find type mismatches between files" use case from the original idea and is a useful standalone demo.

---

# PART 4: THE HARD PROBLEMS (read before you start coding)

These are the places where the project will fight you. Better to know now.

## 4.1 History is noisy
Real commit messages say "fix", "wip", "update stuff". You cannot rely on them to explain *why*.
- **Mitigation:** use the diff itself as evidence, not only the message. Pull in linked issue/PR text where available. Mark memories with a **confidence level**, and never present a guessed rationale as fact.
- **Evaluation implication:** test on repos with good commit hygiene *and* messy ones, and report the difference. This is an honest, interesting result either way.

## 4.2 Files get renamed, split, merged
A file's history can be hiding under old names. Use rename-following history. Accept that splits and merges (one file becomes two) are hard and document it as a limitation or handle it explicitly.

## 4.3 Cost and scale
Summarising thousands of commits with an LLM is not free.
- Only summarise commits that touch the file **meaningfully** (skip pure formatting commits).
- Cache aggressively. Summarise once, update incrementally.
- Start your experiments on mid-sized repos, not giant ones.
- Track token cost as a reported metric (reviewers like this).

## 4.4 Context window budget
You will always have more memory than room. The context injector needs a **selection policy**. Start simple (recency + relevance + "DO NOT REMOVE" flags always included), then try smarter retrieval and compare. This is an ablation you can publish.

## 4.5 Hallucinated memories
If the LLM summarises history and gets it wrong, you have now *poisoned* the memory, and it is worse than having no memory. Mitigations:
- Always keep commit hashes as pointers, so any claim is verifiable.
- Prefer extractive notes (quoting the commit message/diff facts) over free-form interpretation.
- Treat low-confidence notes as hints, not rules.

## 4.6 Stale memory
Code changes, memory must update. The incremental workflow (3.2) handles this, but test the edge cases: force-pushes, rebases, squashed merges (history gets flattened, so information is lost).

## 4.7 Deterministic vs LLM split
Decide early what is a **fact** (extracted by parsers, never wrong in a hallucination sense) and what is an **interpretation** (LLM). Keep them in separate fields. Your credibility depends on this separation.

## 4.8 Evaluation is the real hard part
Showing "it seems to help" is not enough. See Part 5.

---

# PART 5: HOW TO PROVE IT WORKS (THIS MAKES IT PUBLISHABLE)

## 5.1 Research questions

- **RQ1:** Does injecting per-file git-history memory reduce **regressions** (re-introducing previously fixed bugs) in AI-generated code, compared to repo-context baselines?
- **RQ2:** Does it reduce **design-decision contradictions**?
- **RQ3:** Does the orchestrator's cross-file checking catch **interface mismatches** that single-file analysis misses?
- **RQ4 (cost):** What is the token/latency overhead, and does a smart selection policy recover most of the benefit at lower cost?

## 5.2 Baselines to compare against

```
B0: Plain LLM, current file only                  (the "new hire")
B1: LLM + retrieved similar code (RAG-style)      (typical repo-level retrieval)
B2: LLM + AST / call-graph context                (structure-aware baseline)
B3: LLM + session/conversation memory only        (agent-memory baseline)
Ours: B2 + per-file git-history memory + orchestrator
```

Having B2 is important: it isolates whether **history** is what helps, versus just "more context."

## 5.3 A proposed regression benchmark (our own design, to be validated)

The idea, in plain English: **find real moments in history where a bug was fixed, then ask the model to edit nearby code, and check whether it undoes the fix.**

```
1. Mine repos for bug-fix commits.
     (Known technique: SZZ-style identification of fix commits; datasets like
      BugsInPy / Defects4J exist for Python / Java and are worth examining.)

2. For each fix commit F touching file X:
     - Rewind the repo to just AFTER F.
     - Create a plausible follow-up task on X that "tempts" the model to touch
       the fixed region (e.g. refactor the function, simplify the logic).

3. Run each system (B0..B3, Ours) on the task.

4. Check automatically: did the generated patch re-introduce the original bug?
     - Best: a test that failed before F and passes after F. If the patch makes
       that test fail again -> regression.
     - Fallback: static check that the fixed lines/condition were removed.
```

This directly measures what the project claims. **Validate the benchmark design with your guide** before investing weeks into it. Task construction is the subtle part: tasks must be realistic, not contrived to trick the model.

## 5.4 Metrics (plain English)

| Metric | Meaning |
|---|---|
| **Regression rate** | % of tasks where the generated code brings back a previously fixed bug |
| **Contradiction rate** | % of tasks where the patch violates a recorded design decision (human/LLM-judged on a sample, with agreement reported) |
| **Cross-file detection precision / recall** | Of the real interface mismatches (injected or mined), how many were caught, and how many false alarms |
| **Patch correctness** | Does the patch still do what was asked (so we are not "winning" by doing nothing) |
| **Token cost / latency** | The price of the extra context |

Always report patch correctness next to regression rate. A system that refuses to change anything has 0% regressions and is useless.

## 5.5 Ablations (these turn a project into a paper)

- history memory ON vs OFF
- orchestrator ON vs OFF
- extractive notes vs LLM-interpreted notes
- different context-selection policies
- clean-history repos vs messy-history repos
- different underlying LLMs (one open model, one API model)

## 5.6 Threats to validity (write these honestly)

- Benchmark tasks are synthetic.
- LLM judges can be biased; use human spot-checks and report agreement.
- Results may depend on commit-message quality.
- Limited number of repos / languages.

Reviewers trust papers that list their own weaknesses.

---

# PART 6: BUILD PLAN

## 6.1 Suggested stack (matches the review deck)

| Need | Option |
|---|---|
| Language | Python |
| Git access | GitPython (or calling git directly) |
| Parsing / structure | Python `ast` for a Python-only MVP; tree-sitter if you want multi-language later |
| Dependency graph | build from imports/calls, store as a graph (NetworkX is enough at this scale) |
| Memory store | start with plain files / SQLite; add a vector store only if you need semantic retrieval |
| LLM | one API model + one open model (for cost and reproducibility comparisons) |
| Evaluation repos | real open-source Python repos with good test suites |

**Advice:** restrict the MVP to **one language (Python)**. Multi-language support is a rabbit hole that adds nothing to the research claim.

## 6.2 Phases

```
PHASE 0  Foundations (week 1-2)
  - Pick 3-5 target repos with decent tests and history.
  - Read the core papers (Part 7). Run one baseline LLM on them.
  - Finalise research questions with your guide.

PHASE 1  Structural layer, no LLM (week 2-4)
  - Per-file Profile extraction.
  - Dependency graph.
  - Deterministic cross-file mismatch checker.
  => Already a useful, demoable tool (Workflow D).

PHASE 2  History layer (week 4-7)
  - Rename-following history extraction per file.
  - Commit classification (fix / feature / refactor / revert).
  - Distillation into memory cards, with commit-hash pointers + confidence.

PHASE 3  Orchestrator + injector (week 7-10)
  - Routing, joining, token-budgeted context building.
  - Incremental update on new commits.

PHASE 4  Evaluation (week 10-14)
  - Build the regression benchmark.
  - Run baselines B0-B3 vs Ours, with ablations.
  - Collect metrics, human-check a sample.

PHASE 5  Writing (week 12-16, overlaps)
  - Paper draft, figures, threats to validity.
```

Phase 1 alone gives you something to show at a review meeting. Phases 2 and 3 are the research contribution. Phase 4 is what gets it accepted.

## 6.3 Minimum viable demo

A command-line tool that, given a repo and a proposed edit to one file:
1. prints that file's memory card,
2. lists dependents that could break,
3. warns if the edit removes code flagged as "added to fix bug X."

If you can show these three things on a real repo, you have a convincing demo before any fancy generation pipeline exists.

## 6.4 Publication targets (verify current deadlines and scope yourself)

Software-engineering and LLM-for-code venues are the natural fit, for example: MSR (mining software repositories, a very natural home for history-based work), ASE, FSE, ICSE workshops / new-ideas tracks, and LLM-for-code workshops. A smaller-scale version is also suitable for IEEE/Springer conferences. Start with the venue's scope and page limit, then shape the paper to it.

---

# PART 7: BASE PAPERS AND RELATED WORK

> **Verification required.** Items marked **[seen]** appeared in our literature searches (title and arXiv/venue noted as it appeared). Items marked **[background]** are well-known works from general knowledge that I did *not* retrieve in this session, so check them before citing. Read each paper's abstract, method, and evaluation before relying on it.

## 7.1 Closest to your idea (read these first)

| Paper | Why it matters to you | Status |
|---|---|---|
| **CodeMEM: AST-Guided Adaptive Memory for Repository-Level Iterative Code Generation** (arXiv 2601.02868, 2026) | Closest in spirit: memory for repo-level generation. But it tracks memory of the *generation session* via AST, not real git history. Your main point of contrast. | [seen] |
| **ReCUBE: Evaluating Repository-Level Context Utilization in Code Generation** (arXiv 2603.25770, 2026) | A benchmark about how well models use repo context. Study its evaluation design; may inform or complement your benchmark. | [seen] |
| **LLM-based Multi-Agent Systems for Software Engineering** (survey, SMU, 2025) | Maps the multi-agent-for-SE landscape. Mentions systems with analysis agents, report agents, false-positive pruning agents, and role-based debate (e.g., ICAA, MuCoLD). Use it to position your *file-ownership* decomposition against *role-based* decomposition. | [seen] |
| **GraphCodeAgent** (2025) and **GraphCoder** | Graph-based repository context retrieval for code generation (dual-graph / code context graph). Representative of the structure-based baseline family (your B2). | [seen as titles; verify details and IDs] |

## 7.2 Agent memory (the "memory" half of your title)

| Paper | Use |
|---|---|
| **A-Mem: Agentic Memory for LLM Agents** (NeurIPS 2025) | A general agent-memory design. Useful for ideas on how to organise, link, and update memories. | [seen] |
| **Memory in the Age of AI Agents: A Survey** (paper list on GitHub) | Broad survey; good for related-work framing and finding more memory papers. | [seen] |
| MemInsight, MAGMA | Other agent-memory approaches surfaced in searches. Skim for design ideas. | [seen as names; verify] |

Key contrast to make explicit in your paper: these systems give an agent *general* memory. Yours ties memory to a **concrete, verifiable, already-existing source of truth (git history)**, organised **per file**.

## 7.3 Multi-agent orchestration (the "multi-agent" half)

| Paper | Use |
|---|---|
| **AdaptOrch: Task-Adaptive Multi-Agent Orchestration** (arXiv 2602.16873, 2026) | Ideas on how an orchestrator should adapt its strategy to the task. | [seen] |
| **Semantic Caching and Intent-Driven Context Optimization for Multi-Agent NL-to-Code Systems** (arXiv 2601.11687) | Relevant to your cost/context-budget problem (Section 4.3, 4.4). | [seen] |
| The SE multi-agent survey above also references work on why multi-agent LLM systems fail | Read the failure analyses. They tell you which mistakes to avoid. | [seen as a reference inside the survey] |

## 7.4 Code analysis with LLMs + static analysis (the "hybrid" design)

| Paper | Use |
|---|---|
| **Automated Static Vulnerability Detection via a Holistic Neuro-symbolic Approach** (arXiv 2504.16057) | Supports your principle of combining LLM reasoning with rigorous static analysis instead of using either alone. | [seen] |
| **Persistent Human Feedback, LLMs, and Static Analyzers for Secure Code Generation and Vulnerability Detection** (arXiv 2602.05868, 2026) | LLM + static analyzer pipelines; relevant to your "deterministic first, LLM second" split. | [seen] |
| **Benchmarking and Studying the LLM-based Code Review** (arXiv 2509.01494) | Useful for how to evaluate LLM review/findings, and what pitfalls exist. | [seen] |
| **Write Your Own CodeChecker: Test-Driven Checker Development with LLMs** (arXiv 2411.06796) | Another example of marrying LLMs with checkers. | [seen] |

## 7.5 Background works to look up yourself (not retrieved in this session)

These are widely known and likely to be expected in related work. Verify before citing:

- **SWE-bench** and **SWE-agent** (repository-level issue resolution and agent scaffolding)
- **AutoCodeRover** (agentic program repair with code-structure awareness)
- **RepoCoder** / repository-level code completion papers
- **MetaGPT**, **ChatDev** (role-based multi-agent software development)
- **SZZ algorithm** and follow-ups (identifying bug-introducing commits; useful for building your benchmark)
- **Defects4J**, **BugsInPy** (bug datasets with fix commits and tests)
- Work on **commit message generation** and **history-based defect prediction** (shows history is informative, which supports your premise)

## 7.6 How to use these papers

1. Read **CodeMEM** and the **SE multi-agent survey** first, then write one paragraph: "Existing work does X and Y. They do not Z. We do Z."
2. Use **B2-style baselines** (graph/AST context) from the GraphCoder family to prove history, not just "more context," is what helps.
3. Borrow evaluation ideas from **ReCUBE**, **SWE-bench**, and the code-review benchmarking paper.
4. Use the SZZ/Defects4J/BugsInPy line for the regression benchmark.

---

# PART 8: ONE-PAGE SUMMARY (for when someone asks "so what is it?")

> Today's AI coding tools see what a file looks like now but not why it looks that way, so they reintroduce old bugs and break undocumented decisions. **RepoMemory** gives every file in a repository its own agent that remembers the file's full git history and API contract. An orchestrator connects those agents through the dependency graph so that cross-file problems are caught early. When code is generated, the file agents supply the history and contracts the model would otherwise be missing. We evaluate on real repositories by measuring how often generated patches reintroduce fixed bugs, contradict design decisions, or break callers, compared to retrieval-based and AST-based baselines.

---

# GLOSSARY

| Term | Plain-English meaning |
|---|---|
| **Regression** | A bug that was fixed comes back |
| **Design decision** | A deliberate choice the team made, often undocumented |
| **API contract** | The promise a file makes to others: "call me with these inputs, I return this" |
| **Dependency graph** | A map of which files rely on which |
| **Blast radius** | How many other files are affected by a change |
| **Orchestrator** | The coordinator that routes tasks between agents |
| **Context window** | The limited amount of text an LLM can read at once |
| **Static analysis** | Examining code without running it |
| **Extractive vs. generative note** | Extractive quotes real facts from history; generative has the LLM interpret and may hallucinate |
| **Ablation** | Turning off one part of the system to measure how much it mattered |
| **Baseline** | A simpler existing approach you compare against |
| **SZZ** | A well-known technique for tracing a bug fix back to the commit that introduced the bug |

---

# FIRST THREE THINGS TO DO THIS WEEK

1. **Pick the target repos** (3 to 5 Python projects with real history and good tests) and write down why each is suitable.
2. **Read CodeMEM and the SE multi-agent survey** end to end; write the "existing work vs. ours" paragraph.
3. **Do the manual experiment first.** Take one repo, one old bug-fix commit, and one tempting follow-up edit. Hand-write the memory card, give it to an LLM, and see whether it avoids the regression. If it does not help by hand, no amount of architecture will save it, so it is the cheapest possible test of the whole idea.
