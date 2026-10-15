"""C2 Miner — compat wrapper (canonical: history_miner + distiller).

Produces HistoryNote dicts:
{sha, kind, summary, why, risk_flag?, confidence, evidence, author, date, files}
Delegates mining to history_miner and distillation to distiller so the two
paths never diverge. Kept for CLI/back-compat; new code imports history_miner.
"""

from __future__ import annotations

import os

from .distiller import distill_commit
from .history_miner import mine_file_history as _mine_raw


def classify_kind(msg: str) -> str:
    from .history_miner import classify_commit

    kind, _ = classify_commit(msg or "")
    return kind


def _confidence(msg: str) -> str:
    from .history_miner import classify_commit

    _, conf = classify_commit(msg or "")
    return conf


def _risk_flag(msg: str, diff: str) -> str | None:
    note = distill_commit({"sha": "", "msg": msg or "", "diff": diff or "", "files": []})
    risk = note.get("risk_flag")
    if isinstance(risk, dict):
        return str(risk.get("text") or "")
    return None


def mine_file_history(
    repo_root: str | os.PathLike, relpath: str, max_commits: int = 30
) -> list[dict]:
    """git log --follow for one file -> HistoryNotes (oldest first)."""
    raw = _mine_raw(repo_root, relpath, max_commits=max_commits)
    notes: list[dict] = []
    for commit in raw:
        note = distill_commit(commit)
        note["author"] = commit.get("author", "")
        note["date"] = commit.get("date", "")
        note["files"] = commit.get("files", [relpath])
        notes.append(dict(note))
    return notes


def mine_repo(
    repo_root: str | os.PathLike, files: list[str], max_commits_per_file: int = 20
) -> dict[str, list[dict]]:
    return {
        f: mine_file_history(repo_root, f, max_commits_per_file) for f in files
    }
