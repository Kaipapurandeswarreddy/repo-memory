"""Eval harness: SZZ-lite mining + corpus audit + regression-benchmark runner.

- `blame_candidates(repo, file, lineno)` — R-SZZ lite: `git blame -L` latest-touch commit for a line (heuristic proxy for BIC, not guarantee).
- `szz_candidates(repo, fix_sha, files)` — deleted-line blame parents (B-SZZ core) with cosmetic filtering.
- `audit_tasks(tasks)` — flags broken-tests (truncated `test_x[w/` ids), broad (>50 fail-to-pass), contaminated (gold patch substring in problem statement). Mirrors farrellh1/agent-eval-harness audit.
- `run_regression_tasks(store, tasks, gateway)` — rewind-free MVP: for each task {file, fixed_lines_hint, tempting_task}, generate via Orchestrator, score guard-removed/contract warnings + patch-correctness heuristic. Returns metrics {regression_rate, contradiction_rate, patch_correctness, tokens}.
- Human protocol template in `human_protocol()` for contradiction spot-checks (Accuracy/Integrity/Readability/Applicability + Cohen Kappa).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .orchestrator import Orchestrator
from .verifier import check_patch

BROKEN_TEST_RE = re.compile(r"\w+\[w/$|\w+\[w$")
CONTAMINATED_MIN_LEN = 120


def _git(repo: str | Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=str(repo),
                           capture_output=True, text=True, timeout=60)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def blame_candidates(repo_root: str | Path, relpath: str, lineno: int) -> list[str]:
    """Latest-touch commit for one line (R-SZZ lite single-line proxy)."""
    out = _git(repo_root, "blame", "-l", "-L", f"{lineno},{lineno}", "--", relpath)
    if not out:
        return []
    m = re.match(r"^([0-9a-f]{7,40})\b", out.strip())
    return [m.group(1)] if m else []


def szz_candidates(repo_root: str | Path, fix_sha: str, files: list[str] | None = None) -> list[str]:
    """B-SZZ core: blame deleted lines' parents, skip cosmetic (blank/comment-only)."""
    diff = _git(repo_root, "show", "--format=", "--unified=0", fix_sha,
                "--", *(files or [])) or ""
    deleted: list[tuple[str, str]] = []  # (file, line-text)
    cur = None
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            m = re.search(r" b/(.+)$", line)
            cur = m.group(1) if m else cur
        elif line.startswith("--- ") or line.startswith("+++ "):
            continue
        elif line.startswith("-") and not line.startswith("---"):
            text = line[1:].strip()
            if not text or text.startswith("#") or text.startswith("//"):
                continue
            if cur:
                deleted.append((cur, text))
    cands: list[str] = []
    for rel, _ in deleted[:50]:
        parent = _git(repo_root, "rev-parse", f"{fix_sha}^") or ""
        parent = parent.strip().splitlines()[0] if parent.strip() else None
        if not parent:
            continue
        # blame the file at parent for lines containing similar text is expensive;
        # MVP: blame first deleted hunk line number via diff hunk header is skipped,
        # so fall back to latest-touch of the file before fix.
        out = _git(repo_root, "log", "--format=%H", "-n", "5", parent, "--", rel)
        if out:
            cands.extend(s.strip() for s in out.splitlines() if s.strip())
    # dedupe, preserve order
    seen: set[str] = set()
    out_cands = []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out_cands.append(c)
    return out_cands[:20]


def audit_tasks(tasks: list[dict]) -> dict:
    """Flag defective benchmark tasks. Returns {flags: {task_id: [reasons]}, counts}."""
    flags: dict[str, list[str]] = {}
    for t in tasks:
        tid = str(t.get("id", t.get("task", "?")))
        reasons: list[str] = []
        ftps = t.get("fail_to_pass", []) or []
        for test_id in ftps:
            if BROKEN_TEST_RE.search(str(test_id)):
                reasons.append(f"broken-tests: truncated id {test_id!r} aborts pytest run")
                break
        if len(ftps) > 50:
            reasons.append(f"broad: {len(ftps)} FAIL_TO_PASS (sweeping change, not focused fix)")
        gold = str(t.get("gold_patch", "") or "")
        problem = str(t.get("problem", t.get("statement", "")) or "")
        if len(gold) >= CONTAMINATED_MIN_LEN and gold[:CONTAMINATED_MIN_LEN] in problem:
            reasons.append("contaminated: gold solution pasted in problem statement")
        if reasons:
            flags[tid] = reasons
    return {"flags": flags, "flagged": len(flags), "total": len(tasks)}


def run_regression_tasks(store, tasks: list[dict], gateway=None) -> dict:
    """MVP regression benchmark: generate tempting edit, count reintroduced guards/contracts."""
    from .llm import LLMGateway
    gw = gateway or LLMGateway(mode="stub")
    orch = Orchestrator(store, gw, repo_root=getattr(store, "root", None))
    regs = contra = correct = 0
    tokens_in = tokens_out = 0
    details: list[dict] = []
    for t in tasks:
        rel = str(t.get("file", t.get("target", "")))
        task_text = str(t.get("tempting_task", t.get("task", "refactor the function")))
        if not rel:
            continue
        res = orch.run_generate(task_text, rel)
        warns = res.get("warnings", []) or []
        types = {w.get("type") for w in warns}
        is_reg = bool({"guard-removed", "fix-removed"} & types)
        is_contra = "contract-change" in types
        regs += 1 if is_reg else 0
        contra += 1 if is_contra else 0
        # correctness heuristic: patch non-empty and not identical to "do nothing"
        patch = res.get("patch", "")
        ok = bool(patch and len(patch.strip()) > 20)
        correct += 1 if ok else 0
        tokens_in += res.get("tokens", {}).get("tokens_in", 0)
        tokens_out += res.get("tokens", {}).get("tokens_out", 0)
        details.append({"task": rel, "regression": is_reg, "contradiction": is_contra,
                        "correct": ok, "warnings": len(warns)})
    n = len(details) or 1
    return {"tasks": len(details), "regression_rate": regs / n,
            "contradiction_rate": contra / n, "patch_correctness": correct / n,
            "tokens_in": tokens_in, "tokens_out": tokens_out, "details": details}


def human_protocol(n_sample: int = 50) -> dict:
    """Spot-check protocol for contradiction judgments."""
    return {
        "sample": n_sample,
        "metrics": ["Accuracy", "Integrity", "Readability", "Applicability"],
        "scale": "1-5 per metric + binary contradiction?",
        "raters": 2,
        "agreement": "Cohen Kappa on binary contradiction; report + adjudicate disagreements",
        "report": "contradiction_rate ± CI, Kappa, examples of disagreements",
    }


__all__ = ["blame_candidates", "szz_candidates", "audit_tasks",
           "run_regression_tasks", "human_protocol"]
