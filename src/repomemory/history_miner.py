"""C2 Miner — rename-following per-file history + commit classifier (stdlib only).

Miner output per commit:
{
  "sha": "c1a9f3...",
  "msg": "<subject>\\n\\n<body>",
  "author": "...",
  "date": "iso-8601",
  "diff_stat": [{"path": "pkg/payments.py", "added": 12, "deleted": 3}],
  "files": ["pkg/payments.py"]
}

Skip policy (Guide §4.1 / SPIKES 2): skip whitespace-only and pure merge
commits unless the message mentions a conflict (kept as conflict resolution).
Renames are followed via ``git log --follow``; ``prev_paths`` are folded into
``files``/``diff_stat`` (split handling beyond ``--follow`` is out of scope).

Backend: GitPython (``git.Repo``) when importable, else ``subprocess git``.
Both paths share the same numstat parser + skip rules.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

COMMIT_KINDS = ("BUGFIX", "FEATURE", "REFACTOR", "REVERT", "PERF", "SEC")
CONFIDENCES = ("high", "med", "low")

# -- classifier patterns ----------------------------------------------------

_CONVENTIONAL_RE = re.compile(
    r"^\s*(fix|feat|feature|refactor|perf|revert|sec|security|hotfix"
    r"|docs|test|tests|chore|build|ci|style)"
    r"(\([^)]*\))?(!)?\s*:\s*",
    re.IGNORECASE,
)

_PREFIX_TO_KIND = {
    "fix": "BUGFIX",
    "hotfix": "BUGFIX",
    "feat": "FEATURE",
    "feature": "FEATURE",
    "refactor": "REFACTOR",
    "perf": "PERF",
    "revert": "REVERT",
    "sec": "SEC",
    "security": "SEC",
}

_REVERT_RE = re.compile(r"\brevert\b|\breverted\b|\brollback\b|^\s*revert\s+\"", re.IGNORECASE)
_SEC_RE = re.compile(r"secur|cve\b|\bvuln|\bxss\b|\bexploit\b|\binjection\b", re.IGNORECASE)
_PERF_RE = re.compile(r"\bperf\b|performance|optimiz|speed[\s-]?up|\bfaster\b|cach(e|ing)\b", re.IGNORECASE)
_BUGFIX_RE = re.compile(
    r"\bfix\b|\bfixed\b|\bfixes\b|\bfixing\b|\bhotfix\b|\bbug\b|\bdefect\b|"
    r"\bregress\w*\b|\bissue\s*#?\d+|\bpatch\b|\berror\b|\bexception\b|\bcrash\b|\bflak\w*\b",
    re.IGNORECASE,
)
_REFACTOR_RE = re.compile(
    r"\brefactor\b|\brename\b|\bcleanup\b|\bclean[\s-]?up\b|\brestructur\w*\b|"
    r"\bsplit\b.*\b(out|function|method)\b|\bextract\b.*\b(function|method)\b",
    re.IGNORECASE,
)
_FEATURE_STRONG_RE = re.compile(r"\bfeat\b|\bfeature\b", re.IGNORECASE)
_FEATURE_WEAK_RE = re.compile(
    r"\badd(s|ed|ing)?\s+(support|feature|command|option|endpoint|method|function)"
    r"|\bimplement(s|ed|ing)?\b|\bintroduce[sd]?\b|\bsupport\s+for\b",
    re.IGNORECASE,
)

_VAGUE_WORDS = {
    "fix", "wip", "update", "updates", "updated", "minor", "stuff", "things",
    "code", "tweak", "tweaks", "change", "changes", "edit", "edits", "patch",
    "misc", "temp",
}


def _is_vague_message(msg: str) -> bool:
    text = (msg or "").strip().lower()
    if not text:
        return True
    # strip one conventional prefix before vagueness test ("fix: stuff")
    text = _CONVENTIONAL_RE.sub("", text, count=1).strip(" :.-\n\t")
    if not text or len(text) < 3:
        return True
    words = re.findall(r"[a-z]+", text)
    if not words:
        return True
    if len(words) <= 4 and all(w in _VAGUE_WORDS for w in words):
        return True
    return False


def _diff_totals(diff_stat) -> tuple[int, int, int, bool]:
    """Return (n_files, added, deleted, has_rename) for list/str/None input."""
    if not diff_stat:
        return (0, 0, 0, False)
    if isinstance(diff_stat, str):
        # raw numstat text fallback
        entries = _parse_numstat(diff_stat)
    elif isinstance(diff_stat, (list, tuple)):
        entries = list(diff_stat)
    else:
        return (0, 0, 0, False)
    added = deleted = 0
    has_rename = False
    for e in entries:
        if isinstance(e, dict):
            try:
                added += int(e.get("added", 0) or 0)
            except (TypeError, ValueError):
                pass
            try:
                deleted += int(e.get("deleted", 0) or 0)
            except (TypeError, ValueError):
                pass
            p = str(e.get("path", ""))
            if "=>" in p:
                has_rename = True
        elif isinstance(e, (list, tuple)) and len(e) >= 3:
            try:
                added += int(e[0] or 0)
            except (TypeError, ValueError):
                pass
            try:
                deleted += int(e[1] or 0)
            except (TypeError, ValueError):
                pass
    return (len(entries), added, deleted, has_rename)


def classify_commit(msg: str, diff_stat=None) -> tuple[str, str]:
    """Classify a commit message (+ optional diff_stat) into (kind, confidence).

    Priority (SPIKES 2, few-shot rule): an explicit conventional prefix
    (``fix:``/``feat:``/...) beats keyword inference. Vague messages
    (``fix``/``wip``/``update`` alone) keep the keyword guess but drop to
    ``low`` confidence; with no signal the diff size decides the fallback.
    """
    raw = (msg or "").strip()
    # 1. explicit conventional prefix wins (only for kinds we track)
    m = _CONVENTIONAL_RE.match(raw)
    unknown_prefix = False
    if m:
        kind = _PREFIX_TO_KIND.get(m.group(1).lower())
        if kind is not None:
            return (kind, "high")
        # docs/test/chore/build/ci/style have no RepoMemory kind: remember and
        # fall through to keyword inference; pure-chore falls back to REFACTOR.
        unknown_prefix = True

    low = raw.lower()
    vague = _is_vague_message(raw)

    # 2. keyword inference, fixed priority: REVERT > SEC > PERF > BUGFIX > REFACTOR > FEATURE
    if _REVERT_RE.search(raw):
        return ("REVERT", "low" if vague else "high")
    if _SEC_RE.search(raw):
        return ("SEC", "low" if vague else "high")
    if _PERF_RE.search(raw):
        return ("PERF", "low" if vague else "high")
    if _BUGFIX_RE.search(raw):
        return ("BUGFIX", "low" if vague else "high")
    if _REFACTOR_RE.search(raw):
        return ("REFACTOR", "low" if vague else "high")
    if _FEATURE_STRONG_RE.search(raw):
        return ("FEATURE", "low" if vague else "high")
    if _FEATURE_WEAK_RE.search(raw):
        return ("FEATURE", "low" if vague else "med")

    # 3. vague-message fallback keeps a plausible guess at low confidence
    if vague:
        if "fix" in low:
            return ("BUGFIX", "low")
        if "wip" in low:
            return ("REFACTOR", "low")
        if "update" in low or "upgrade" in low:
            return ("FEATURE", "low")

    # 4. diff-feature fallback (Levin & Yehudai idea, size-only for MVP).
    # Chore/docs/test/style commits carry no RepoMemory kind -> REFACTOR low.
    if unknown_prefix:
        return ("REFACTOR", "low")
    n_files, added, deleted, has_rename = _diff_totals(diff_stat)
    total = added + deleted
    if has_rename:
        return ("REFACTOR", "med" if not vague else "low")
    if n_files == 0 and not raw:
        return ("REFACTOR", "low")
    if total <= 30 and n_files <= 2:
        return ("BUGFIX", "low")
    if added >= 80 or n_files >= 4:
        return ("FEATURE", "low")
    return ("REFACTOR", "low")


# -- git helpers ------------------------------------------------------------


def _normalize_rel(relpath: str) -> str:
    rel = (relpath or "").replace("\\", "/").strip()
    rel = re.sub(r"^\./", "", rel)
    rel = rel.lstrip("/")
    return rel


def _try_gitpython():  # type: ignore[no-untyped-def]
    try:
        import git  # type: ignore[import-not-found]

        return git
    except Exception:
        return None


# fix the brace regex typo above at import time (kept simple on purpose)
_RESOLVE_BRACE_RE = re.compile(r"^(?P<pre>.*)\{(?P<old>.*?)\s*=>\s*(?P<new>.*?)\}(?P<post>.*)$")


def _resolve_new_path(path: str) -> str:
    p = (path or "").strip().strip('"').strip("'")
    if "=>" not in p:
        return p
    mb = _RESOLVE_BRACE_RE.match(p)
    if mb:
        return (mb.group("pre") + mb.group("new") + mb.group("post")).strip()
    right = p.split("=>", 1)[1].strip().strip("{} ").strip()
    return right


def _parse_numstat(text: str) -> list[dict]:
    """Parse ``git --numstat`` output into [{path, added, deleted}]."""
    out: list[dict] = []
    for line in (text or "").splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            # numstat -z or short form fallback: "added deleted path"
            sp = line.split(None, 2)
            if len(sp) < 3:
                continue
            parts = sp
        a_raw, d_raw = parts[0].strip(), parts[1].strip()
        raw_path = "\t".join(parts[2:]).strip()
        try:
            added = int(a_raw) if a_raw != "-" else 0
        except ValueError:
            added = 0
        try:
            deleted = int(d_raw) if d_raw != "-" else 0
        except ValueError:
            deleted = 0
        path = _resolve_new_path(raw_path)
        out.append({"path": path, "added": added, "deleted": deleted})
    return out


def _patch_excerpt_subprocess(root: Path, sha: str, rel: str,
                               max_chars: int = 3000) -> str:
    """Unified diff of ``sha`` filtered to ``rel`` (truncated, never raises)."""
    code, out, _ = _run_git(
        ["git", "show", "--format=", "--unified=2", sha, "--", rel], root)
    if code != 0 or not out or not out.strip():
        return ""
    text = out.strip()
    return text[:max_chars] + "\n... [truncated]" if len(text) > max_chars else text


def _patch_excerpt_gitpython(gitmod, root: Path, sha: str, rel: str,
                             max_chars: int = 3000) -> str:  # type: ignore[no-untyped-def]
    try:
        repo = gitmod.Repo(str(root))
        out = repo.git.show("--format=", "--unified=2", sha, "--", rel)
    except Exception:
        return ""
    text = str(out or "").strip()
    if not text:
        return ""
    return text[:max_chars] + "\n... [truncated]" if len(text) > max_chars else text


def _run_git(args: list[str], cwd: str | os.PathLike) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        return (proc.returncode, proc.stdout or "", proc.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return (1, "", str(exc))


def _parse_show_header(out: str) -> tuple[str, str, str, str, str]:
    """Parse ``%an%x00%ad%x00%P%x00%s%x00%b%x1e`` output."""
    core = out.split("\x1e", 1)[0]
    parts = core.split("\x00")
    # show -s adds no extra lines when --format is exact; be defensive anyway
    while len(parts) < 5:
        parts.append("")
    author = parts[0].splitlines()[-1].strip() if parts[0] else ""
    date = parts[1].strip()
    parents = parts[2].strip()
    subject = parts[3].strip()
    body = parts[4].strip()
    return (author, date, parents, subject, body)


def _commit_message(subject: str, body: str) -> str:
    subject = (subject or "").strip()
    body = (body or "").strip()
    if subject and body:
        return f"{subject}\n\n{body}"
    return subject or body


def _should_skip(msg: str, parents: str, diff_stat: list[dict], whitespace_empty: bool) -> bool:
    has_conflict = "conflict" in (msg or "").lower()
    if not diff_stat:
        # pure merge / empty commit: keep only conflict resolutions
        n_parents = len((parents or "").split())
        if n_parents > 1:
            return not has_conflict
        return True
    if whitespace_empty and not has_conflict:
        return True
    return False


# -- subprocess backend -----------------------------------------------------


def _mine_subprocess(repo_root: str | os.PathLike, rel: str, max_commits: int) -> list[dict]:
    root = Path(repo_root)
    code, shas_out, _ = _run_git(
        ["git", "log", "--follow", f"--max-count={max_commits}", "--format=%H", "--", rel],
        root,
    )
    if code != 0:
        return []
    shas = [s.strip() for s in shas_out.splitlines() if s.strip()]
    commits: list[dict] = []
    fmt = "%an%x00%ad%x00%P%x00%s%x00%b%x1e"
    for sha in shas[:max_commits]:
        c1, meta_out, _ = _run_git(
            ["git", "show", "-s", "--date=iso-strict", f"--format={fmt}", sha], root
        )
        if c1 != 0:
            continue
        author, date, parents, subject, body = _parse_show_header(meta_out)
        msg = _commit_message(subject, body)
        c2, stat_out, _ = _run_git(
            ["git", "diff-tree", "--no-commit-id", "--numstat", "-m", "--root", sha], root
        )
        diff_stat = _parse_numstat(stat_out) if c2 == 0 else []
        whitespace_empty = False
        if diff_stat:
            c3, w_out, _ = _run_git(
                ["git", "diff-tree", "--no-commit-id", "--numstat", "-m", "-w", "--root", sha],
                root,
            )
            if c3 == 0:
                whitespace_empty = len(_parse_numstat(w_out)) == 0
        if _should_skip(msg, parents, diff_stat, whitespace_empty):
            continue
        files = [d["path"] for d in diff_stat]
        commits.append(
            {
                "sha": sha,
                "msg": msg,
                "author": author,
                "date": date,
                "diff_stat": diff_stat,
                "files": files,
                "patch": _patch_excerpt_subprocess(root, sha, rel),
            }
        )
    return commits


# -- GitPython backend ------------------------------------------------------


def _mine_gitpython(gitmod, repo_root: str | os.PathLike, rel: str, max_commits: int) -> list[dict]:  # type: ignore[no-untyped-def]
    try:
        repo = gitmod.Repo(str(repo_root))
    except Exception:
        return []
    try:
        shas_out = repo.git.log(
            "--follow", f"--max-count={max_commits}", "--format=%H", "--", rel
        )
    except Exception:
        return []
    shas = [s.strip() for s in str(shas_out or "").splitlines() if s.strip()]
    commits: list[dict] = []
    fmt = "%an%x00%ad%x00%P%x00%s%x00%b%x1e"
    for sha in shas[:max_commits]:
        try:
            meta_out = repo.git.show("-s", "--date=iso-strict", f"--format={fmt}", sha)
        except Exception:
            continue
        author, date, parents, subject, body = _parse_show_header(str(meta_out or ""))
        msg = _commit_message(subject, body)
        try:
            stat_out = repo.git.diff_tree("--no-commit-id", "--numstat", "-m", "--root", sha)
        except Exception:
            stat_out = ""
        diff_stat = _parse_numstat(str(stat_out or ""))
        whitespace_empty = False
        if diff_stat:
            try:
                w_out = repo.git.diff_tree(
                    "--no-commit-id", "--numstat", "-m", "-w", "--root", sha
                )
                whitespace_empty = len(_parse_numstat(str(w_out or ""))) == 0
            except Exception:
                whitespace_empty = False
        if _should_skip(msg, parents, diff_stat, whitespace_empty):
            continue
        commits.append(
            {
                "sha": sha,
                "msg": msg,
                "author": author,
                "date": date,
                "diff_stat": diff_stat,
                "files": [d["path"] for d in diff_stat],
                "patch": _patch_excerpt_gitpython(gitmod, root, sha, rel),
            }
        )
    return commits


# -- public API -------------------------------------------------------------


def mine_file_history(
    repo_root: str | os.PathLike, relpath: str, max_commits: int = 50
) -> list[dict]:
    """Return newest-first per-file history for ``relpath`` (follows renames).

    Uses GitPython when installed, else ``subprocess git``. Returns ``[]``
    for non-repos, unknown paths, or ``max_commits <= 0`` (never raises for
    git failures).
    """
    if max_commits is None or max_commits <= 0:
        return []
    rel = _normalize_rel(relpath)
    if not rel:
        return []
    root = Path(repo_root)
    if not root.exists():
        return []
    gitmod = _try_gitpython()
    if gitmod is not None:
        try:
            got = _mine_gitpython(gitmod, root, rel, int(max_commits))
            # empty can mean "no history" OR gitpython quirk; confirm via subprocess
            # only when repo truly lacks git metadata (avoid double cost otherwise).
            if got:
                return got
        except Exception:
            pass
        # fall through to subprocess (covers odd GitPython failures)
    try:
        return _mine_subprocess(root, rel, int(max_commits))
    except Exception:
        return []


__all__ = ["COMMIT_KINDS", "CONFIDENCES", "classify_commit", "mine_file_history"]
