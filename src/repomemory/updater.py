"""C9 Incremental Updater — changed-files-only refresh (W-B).

changed_files(repo_root, base="HEAD~1") -> list[relpath] via
  `git diff --name-only <base>` (+ untracked files).
Falls back to GitPython if the git binary is missing but GitPython exists.

incremental_update(store, repo_root, files=None) -> summary dict:
  for each changed *.py file: re-run C1 profiler, append a stub HistoryNote,
  then patch the graph (rebuild from all stored profiles — cheap at MVP scale).
If a public contract (functions/classes/exports) changed, the summary flags
it so callers can trigger C5 cross-check.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from . import graph_builder as _gb
from . import profiler as _prof


def _run_git(args: list[str], cwd: str | os.PathLike) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd),
                           capture_output=True, text=True, timeout=60)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def changed_files(repo_root: str | os.PathLike, base: str = "HEAD~1") -> list[str]:
    """Tracked-modified + untracked files, repo-relative posix paths."""
    root = Path(repo_root)
    # 1. tracked changes vs base (falls back to worktree vs index)
    out = _run_git(["diff", "--name-only", base], root)
    if out is None:
        out = _run_git(["diff", "--name-only"], root) or ""
    files = [l.strip().replace("\\", "/") for l in out.splitlines() if l.strip()]
    # 2. untracked (new files git diff misses)
    untracked = _run_git(["ls-files", "--others", "--exclude-standard"], root) or ""
    for l in untracked.splitlines():
        l = l.strip().replace("\\", "/")
        if l and l not in files:
            files.append(l)
    # 3. GitPython fallback when no binary
    if not files:
        try:
            import git  # type: ignore

            repo = git.Repo(str(root))
            for item in repo.index.diff(None):
                p = item.a_path or item.b_path
                if p and p not in files:
                    files.append(p)
            for p in repo.untracked_files:
                if p not in files:
                    files.append(p)
        except Exception:
            pass
    return sorted(files)


def _contract_of(profile: dict) -> dict:
    try:
        fns = {}
        for f in profile.get("functions", []) or []:
            if isinstance(f, dict) and f.get("name"):
                params = f.get("params", {}) or {}
                fns[f["name"]] = sorted(params.keys()) if isinstance(params, dict) else []
        cls = {}
        for c in profile.get("classes", []) or []:
            if isinstance(c, dict) and c.get("name"):
                meths = c.get("methods", []) or []
                cls[c["name"]] = sorted(m for m in meths if isinstance(m, str))
        return {"functions": fns, "classes": cls,
                "exports": sorted(profile.get("exports", []) or [])}
    except Exception:
        return {"functions": {}, "classes": {}, "exports": []}


def _save_graph(store, graph_data: dict) -> None:
    for name in ("save_graph", "save_graph_json"):
        fn = getattr(store, name, None)
        if callable(fn):
            try:
                fn(graph_data)
                return
            except TypeError:
                continue
    raise AttributeError("store has no save_graph/save_graph_json")


def _has_uncommitted(root: Path, rel: str) -> bool:
    """True when ``rel`` has staged/unstaged/untracked working-tree changes."""
    out = _run_git(["status", "--porcelain", "--", rel], root)
    return bool(out and out.strip())


def incremental_update(
    store, repo_root: str | os.PathLike, files: list[str] | None = None,
    base: str = "HEAD~1",
) -> dict:
    """Re-profile changed files, mine real new commits, patch graph. Returns summary."""
    from .distiller import distill_file as _distill_file

    try:
        from .history_miner import mine_file_history as _mine
    except ImportError:
        _mine = None  # type: ignore
    root = Path(repo_root)
    if files is None:
        files = changed_files(root, base)
    py_files = [f for f in files if f.endswith(".py")]
    updated: list[str] = []
    contracts_changed: list[str] = []
    notes_added = 0
    for rel in py_files:
        abs_p = root / rel
        if not abs_p.exists():
            continue
        old = store.load_profile(rel)
        new = _prof.profile_file(abs_p, root)
        store.save_profile(rel, new)
        updated.append(rel)
        if old is not None and _contract_of(old) != _contract_of(new):
            contracts_changed.append(rel)
        # mine real history, merge only SHAs not already stored (distilled)
        added_here = 0
        if _mine is not None:
            try:
                raw = _mine(root, rel, max_commits=5)
                if raw:
                    notes, _decisions = _distill_file(raw)
                    existing = {n.get("sha") for n in store.load_history(rel)
                                if isinstance(n, dict)}
                    fresh = []
                    for note, commit in zip(notes, raw):
                        if note.get("sha") not in existing:
                            note.setdefault("author", commit.get("author", ""))
                            note.setdefault("date", commit.get("date", ""))
                            note.setdefault("files", commit.get("files", [rel]))
                            fresh.append(note)
                    for note in fresh:
                        store.append_history_note(rel, note)
                    added_here = len(fresh)
            except Exception:
                added_here = 0
        if added_here == 0 and _has_uncommitted(root, rel):
            # uncommitted working-tree change (mine found nothing new): low-confidence
            # stub, clearly marked so it never poses as a real commit
            note = {
                "sha": "WORKING",
                "kind": "FEATURE",
                "summary": f"working-tree update of {rel}",
                "why": "incremental updater re-profile (uncommitted change)",
                "risk_flag": None,
                "confidence": "low",
                "evidence": f"re-profiled at {time.strftime('%Y-%m-%d')}; loc={new.get('loc')}",
                "author": "",
                "date": time.strftime("%Y-%m-%d"),
                "files": [rel],
            }
            store.append_history_note(rel, note)
        notes_added += added_here
    # patch graph: rebuild from all stored profiles (deterministic, cheap <10k files)
    profiles = {}
    for rel in store.list_files():
        p = store.load_profile(rel)
        if p:
            profiles[rel] = p
    graph_data = _gb.build_graph_data(profiles)
    _save_graph(store, graph_data)
    return {"updated": sorted(updated), "contracts_changed": sorted(contracts_changed),
            "notes_added": notes_added,
            "graph_nodes": len(graph_data.get("nodes", [])),
            "graph_edges": len(graph_data.get("edges", []))}
