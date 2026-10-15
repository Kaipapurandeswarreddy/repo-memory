"""C2 Distiller — rule-based commit → HistoryNote distillation (no API key).

HistoryNote schema:
{
  "sha": "c1a9f3",
  "kind": "BUGFIX|FEATURE|REFACTOR|REVERT|PERF|SEC",
  "summary": "<=25 words>",
  "why": "<rationale excerpt>",
  "confidence": "high|med|low",
  "evidence": {"sha": ..., "quote": "<subject excerpt>"},
  "risk_flag": {"lines_hint": ..., "text": "DO NOT REMOVE ..."}?,   # only when guard-like
  "reverted_attempt": "<what was reverted>"?,                        # only for REVERT
}
"""

from __future__ import annotations

import re
from typing import TypedDict


class RiskFlag(TypedDict):
    lines_hint: str
    text: str


class HistoryNote(TypedDict, total=False):
    sha: str
    kind: str
    summary: str
    why: str
    confidence: str
    evidence: dict
    risk_flag: RiskFlag
    reverted_attempt: str


_CONVENTIONAL_STRIP_RE = re.compile(
    r"^\s*(fix|feat|feature|refactor|perf|revert|sec|security|hotfix|docs|test|chore|build|ci|style)"
    r"(\([^)]*\))?(!)?\s*:\s*",
    re.IGNORECASE,
)

_BUG_MSG_RE = re.compile(
    r"bug|fix|regress|defect|hotfix|issue|fail|crash|exception"
    r"|prevent|avoid|self-reference|leak|corrupt|broken",
    re.IGNORECASE,
)

# Added-line guard patterns (applied to diff lines starting with '+').
_GUARD_RES = [
    re.compile(r"^\+\s*if\b.*\b(return|raise|continue|break|exit)\b"),
    re.compile(r"^\+\s*(assert|raise)\b"),
    re.compile(r"^\+\s*try\s*:"),
    re.compile(r"^\+\s*except\b"),
    re.compile(r"10000"),  # dup-refund style threshold guard (>10000)
    re.compile(r"duplicat|idempot|dedupe|retry|guard|race|atomic", re.IGNORECASE),
]

_GUARD_WORD_RE = re.compile(
    r"guard|check|validat|ensur|duplicate|idempot|retry|dedupe|assert|race", re.IGNORECASE
)

_HUNK_RE = re.compile(r"@@\s*-\d+(?:,\d+)?\s*\+(?P<start>\d+)(?:,(?P<count>\d+))?\s*@@")

_PRINCIPLE_NEVER_RE = re.compile(r"\b(never|always|must)\b", re.IGNORECASE)
_PRINCIPLE_OVER_RE = re.compile(r"\b(chose|choose|chosen|prefer)\b.*\bover\b", re.IGNORECASE)
_PRINCIPLE_INSTEAD_RE = re.compile(r"\binstead of\b", re.IGNORECASE)


def _subject_and_body(msg: str) -> tuple[str, str]:
    text = (msg or "").strip()
    if not text:
        return ("", "")
    lines = text.splitlines()
    subject = ""
    for ln in lines:
        if ln.strip():
            subject = ln.strip()
            break
    idx = text.find(subject) + len(subject) if subject else 0
    body = text[idx:].strip(" \n\t-:")
    return (subject, body)


def _summarize(msg: str, max_words: int = 25) -> str:
    subject, _ = _subject_and_body(msg)
    subject = _CONVENTIONAL_STRIP_RE.sub("", subject).strip()
    words = subject.split()
    if not words:
        return "no message"
    return " ".join(words[:max_words])


def _extract_why(msg: str, max_chars: int = 280) -> str:
    subject, body = _subject_and_body(msg)
    source = body.strip() or subject.strip()
    if not source:
        return "No rationale given in message; inferred from diff only."
    # first two sentences or first max_chars
    parts = re.split(r"(?<=[.!?])\s+|\n+", source)
    why = " ".join(p.strip() for p in parts[:2] if p.strip()).strip()
    if len(why) > max_chars:
        why = why[: max_chars - 1].rstrip() + "…"
    return why or source[:max_chars]


def _diff_text(commit: dict) -> str:
    for key in ("diff", "patch", "unified_diff"):
        val = commit.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _hunk_hint(diff: str, fallback_file: str) -> str:
    heads = list(_HUNK_RE.finditer(diff or ""))
    if heads and fallback_file:
        first = heads[0]
        start = int(first.group("start"))
        count = int(first.group("count") or "1")
        end = start + max(count - 1, 0)
        span = f"L{start}" if end == start else f"L{start}-{end}"
        return f"{fallback_file}:{span}"
    if fallback_file:
        return fallback_file
    return ""


def _added_guard_lines(diff: str) -> list[str]:
    hits: list[str] = []
    for line in (diff or "").splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if any(rx.search(line) for rx in _GUARD_RES):
            hits.append(line[1:].strip()[:160])
    return hits


def _should_flag_risk(msg: str, commit: dict, diff: str) -> tuple[bool, list[str]]:
    """Risk iff bug/fix/prevent context AND guard-like additions in the diff."""
    kind = ""
    try:
        from repomemory.history_miner import classify_commit as _cc
        kind, _ = _cc(msg, commit.get("diff_stat"))
    except Exception:
        pass
    if not _BUG_MSG_RE.search(msg or "") and kind != "BUGFIX":
        return (False, [])
    guard_lines = _added_guard_lines(diff)
    if guard_lines:
        return (True, guard_lines)
    # fallback when miner gave only diff_stat (no patch text): small fix-sized
    # addition + guard vocabulary in the message suggests a guard commit.
    stats = commit.get("diff_stat") or []
    try:
        added = sum(int(d.get("added", 0) or 0) for d in stats if isinstance(d, dict))
    except (TypeError, ValueError):
        added = 0
    n_files = len(stats) if isinstance(stats, list) else 0
    if added and added <= 40 and n_files <= 2 and _GUARD_WORD_RE.search(msg or ""):
        return (True, [])
    return (False, [])


def distill_commit(commit: dict) -> HistoryNote:
    """Distil one miner commit dict into a HistoryNote (never raises on bad input)."""
    commit = commit or {}
    sha = str(commit.get("sha", "") or "")
    msg = str(commit.get("msg", "") or "")
    files = commit.get("files") or []
    first_file = str(files[0]) if files else ""

    # local import: history_miner never imports distiller (no cycle)
    from repomemory.history_miner import classify_commit

    stats = commit.get("diff_stat")
    kind, confidence = classify_commit(msg, stats)
    subject, _ = _subject_and_body(msg)
    summary = _summarize(msg)
    why = _extract_why(msg)
    diff = _diff_text(commit)

    note: HistoryNote = {
        "sha": sha,
        "kind": kind,
        "summary": summary,
        "why": why,
        "confidence": confidence,
        "evidence": {"sha": sha, "quote": subject[:200]},
    }

    flag, guard_lines = _should_flag_risk(msg, commit, diff)
    if flag:
        hint = _hunk_hint(diff, first_file or (str(stats[0].get("path")) if stats else sha[:7]))
        detail = f"guard ({'; '.join(guard_lines[:2])}) " if guard_lines else "guard "
        note["risk_flag"] = {
            "lines_hint": hint or sha[:7],
            "text": f"DO NOT REMOVE: {detail}added in {sha[:7]} to fix: {summary}",
        }

    if kind == "BUGFIX":
        # mechanism: the exact added lines so future edits can be checked
        added = [l[1:].strip()[:160] for l in (diff or "").splitlines()
                 if l.startswith("+") and not l.startswith("+++")][:4]
        if added:
            note["fix_lines"] = added

    if kind == "REVERT" or re.search(r"\brevert\b|\brollback\b", msg, re.IGNORECASE):
        note["reverted_attempt"] = (
            f"Reverted approach: {(subject or summary)[:200]} (see {sha[:7]}); "
            "do not retry without addressing the revert reason."
        )
    return note


def _principle_sentences(msg: str) -> list[str]:
    text = (msg or "").strip()
    if not text:
        return []
    out: list[str] = []
    for sent in re.split(r"(?<=[.!?])\s+|\n+", text):
        s = " ".join(sent.split()).strip(" -:;")
        if len(s.split()) < 4 or len(s) > 240:
            continue
        if _PRINCIPLE_NEVER_RE.search(s) or _PRINCIPLE_OVER_RE.search(s) or _PRINCIPLE_INSTEAD_RE.search(s):
            out.append(s)
    return out


def distill_file(commits: list[dict]) -> tuple[list[HistoryNote], list[str]]:
    """Distil a file's commit list into (notes, decisions).

    Decisions are deduped principle-like sentences: generic rule of the
    ``never/always/must`` or ``chose X over Y`` form (e.g. the Guide's
    "Decimal never float" is just one instance of this pattern).
    """
    notes: list[HistoryNote] = [distill_commit(c) for c in (commits or []) if c]
    decisions: list[str] = []
    seen: set[str] = set()
    for c in commits or []:
        for sent in _principle_sentences(str((c or {}).get("msg", ""))):
            key = sent.lower()
            if key not in seen:
                seen.add(key)
                decisions.append(sent)
    return (notes, decisions)


__all__ = ["HistoryNote", "RiskFlag", "distill_commit", "distill_file",
           "distill_with_llm", "CHRONOS_PROMPT"]


CHRONOS_PROMPT = """You are a senior engineer writing a memory card (Chronos 4-field).
Given: commit subject, body, diff excerpt (truncated).
Output JSON ONLY: {"summary": "<=25 words", "signals": "...", "motivation": "...", "solution": "...", "kind": "BUGFIX|FEATURE|REFACTOR|REVERT|PERF|SEC", "risk_flag": {"lines_hint": "", "text": ""} or null, "confidence": "high|med|low"}
Rules: quote evidence, never invent rationale; vague msg -> confidence low; guard-like additions + bug msg -> risk_flag DO NOT REMOVE.
Examples:
{"summary": "guard duplicate refunds over 10000 on gateway retry", "signals": "if amount>10000 + duplicate check", "motivation": "March outage double-charge", "solution": "added guard L80-90", "kind": "BUGFIX", "risk_flag": {"lines_hint": "L80-90", "text": "DO NOT REMOVE"}, "confidence": "high"}
{"summary": "split validate out of process_refund", "signals": "extract function", "motivation": "readability", "solution": "new validate()", "kind": "REFACTOR", "risk_flag": null, "confidence": "med"}
Now distill:
Subject: {subject}
Body: {body}
Diff: {diff}
JSON:"""


def distill_with_llm(commit: dict, gateway=None) -> HistoryNote:
    """Chronos-style LLM distillation with rule-based fallback (never raises)."""
    base = distill_commit(commit or {})
    if gateway is None:
        return base
    try:
        import json as _json
        msg = str((commit or {}).get("msg", ""))
        subject = msg.splitlines()[0][:300] if msg.strip() else ""
        body = "\n".join(msg.splitlines()[1:])[:800]
        diff = str((commit or {}).get("diff", "") or "")[:2000]
        prompt = CHRONOS_PROMPT.format(subject=subject, body=body, diff=diff)
        out = gateway.generate(prompt)
        text = out.get("text", "") if isinstance(out, dict) else str(out)
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return base
        data = _json.loads(text[start:end + 1])
        kind = str(data.get("kind", base["kind"])).upper()
        if kind not in ("BUGFIX", "FEATURE", "REFACTOR", "REVERT", "PERF", "SEC"):
            kind = base["kind"]
        note: HistoryNote = {
            "sha": base["sha"],
            "kind": kind,
            "summary": str(data.get("summary", base["summary"]))[:200] or base["summary"],
            "why": " ".join([str(data.get("motivation", "")), str(data.get("solution", ""))]).strip()[:280] or base["why"],
            "confidence": data.get("confidence", base["confidence"]) if data.get("confidence") in ("high", "med", "low") else base["confidence"],
            "evidence": base["evidence"],
        }
        rf = data.get("risk_flag")
        if isinstance(rf, dict) and rf.get("text"):
            note["risk_flag"] = {"lines_hint": str(rf.get("lines_hint", ""))[:120],
                                 "text": str(rf.get("text", ""))[:300]}
        elif "risk_flag" in base:
            note["risk_flag"] = base["risk_flag"]
        if kind == "REVERT" and "reverted_attempt" not in note:
            note["reverted_attempt"] = base.get("reverted_attempt", "")
            if not note["reverted_attempt"]:
                note.pop("reverted_attempt", None)
        return note
    except Exception:
        return base
