"""C6 Context Injector — budget-aware prompt builder (deterministic, no LLM).

Policy (ARCHITECTURE.md C5/C6): always include every ``risk_flag`` + the
dependent contract surface, then fill the remaining budget with history notes
ranked by ``bm25(task, note) + recency + confidence``.
"""

from __future__ import annotations

import re
from typing import Any

TEMPLATE_WEIGHTS: dict[str, dict[str, float]] = {
    "technical": {"memories": 0.7, "skills": 1.5, "knowledge": 1.3},
    "a2e": {"memories": 1.5, "skills": 2.0, "knowledge": 1.0},
}

CONFIDENCE_WEIGHT: dict[str, float] = {"high": 1.0, "med": 0.6, "medium": 0.6, "low": 0.3}

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "the,a,an,and,or,to,of,in,on,for,with,is,are,was,were,it,this,that,be,as,at,by,from,"
    "into,add,make,fix,use,using,please,should,would,could,new".split(",")
)


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOP]


def _bm25(query: str, doc: str, k1: float = 1.5, b: float = 0.75, avg_len: float = 50.0) -> float:
    """Tiny Okapi-style scorer with uniform idf (no corpus needed for MVP)."""
    qt = _tokens(query)
    if not qt:
        return 0.0
    dt = _tokens(doc)
    if not dt:
        return 0.0
    tf: dict[str, int] = {}
    for t in dt:
        tf[t] = tf.get(t, 0) + 1
    doc_len = len(dt)
    norm = k1 * (1 - b + b * doc_len / avg_len)
    score = 0.0
    for t in set(qt):
        f = tf.get(t, 0)
        if f:
            score += (f * (k1 + 1)) / (f + norm)
    # scale ~0..len(query_terms); dampen so recency+confidence stay relevant
    return score


def _note_text(note: dict) -> str:
    parts = [
        str(note.get("summary", "")),
        str(note.get("why", "")),
        str((note.get("evidence") or {}).get("quote", "") if isinstance(note.get("evidence"), dict) else ""),
        " ".join(str(x) for x in (note.get("fix_lines") or []) if x),
    ]
    return " ".join(p for p in parts if p)


def _recency(idx: int) -> float:
    # idx 0 == newest (git log order)
    return 1.0 / (1.0 + 0.3 * max(idx, 0))


def _score_note(query: str, note: dict, idx: int) -> float:
    bm = _bm25(query, _note_text(note))
    rec = _recency(idx)
    conf = CONFIDENCE_WEIGHT.get(str(note.get("confidence", "low")).lower(), 0.3)
    return bm + rec + conf


def _format_note(note: dict) -> str:
    sha = str(note.get("sha", ""))[:7]
    kind = note.get("kind", "?")
    conf = note.get("confidence", "low")
    head = f"[{sha} {kind} conf={conf}] {note.get('summary', '')}"
    why = str(note.get("why", "") or "").strip()
    lines = [f"- {head}"]
    if why:
        lines.append(f"  why: {why[:220]}")
    risk = note.get("risk_flag")
    if isinstance(risk, dict):
        lines.append(
            f"  RISK {risk.get('lines_hint', '')}: {risk.get('text', 'DO NOT REMOVE')}"[:300]
        )
    fix_lines = note.get("fix_lines") or []
    if fix_lines:
        lines.append("  fix keeps: " + "; ".join(str(x)[:120] for x in fix_lines[:3])[:300])
    if note.get("reverted_attempt"):
        lines.append(f"  failed approach: {str(note['reverted_attempt'])[:220]}")
    return "\n".join(lines)


def _contract_line(card: dict) -> str:
    prof = card.get("profile") or {}
    fname = card.get("file", "?")
    funcs = prof.get("functions") or []
    if not funcs:
        # fall back to classes/exports so the surface is never silently empty
        classes = prof.get("classes") or []
        names = [c.get("name", "?") for c in classes if isinstance(c, dict)][:5]
        exports = prof.get("exports") or []
        extra = ", ".join([*names, *[str(e) for e in exports[:5]]])
        return f"{fname}: {extra or '(no recorded symbols)'}"
    parts: list[str] = []
    for f in funcs[:12]:
        if not isinstance(f, dict):
            continue
        params = f.get("params") or {}
        sig = ", ".join(
            f"{k}: {v}" if v else f"{k}" for k, v in list(params.items())[:8]
        )
        ret = f.get("returns")
        parts.append(f"{f.get('name', '?')}({sig})" + (f" -> {ret}" if ret else ""))
    return f"{fname}: " + "; ".join(parts)


def _truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def build_prompt(
    task: str,
    target_code: str,
    card: dict[str, Any],
    dependent_cards: list[dict[str, Any]] | None = None,
    budget_chars: int = 4000,
) -> str:
    """Build the C6 injection prompt, honouring ``budget_chars``.

    Sections: [task][target code][history+risk][decisions][dependent
    contracts][failed approaches]. Risk flags + dependent contracts are
    mandatory and are never dropped to fit the budget.
    """
    dependent_cards = list(dependent_cards or [])
    fname = str(card.get("file", "target"))
    history: list[dict] = [n for n in (card.get("history") or []) if isinstance(n, dict)]
    decisions: list[str] = [str(d) for d in (card.get("decisions") or []) if d]

    risk_notes = [n for n in history if n.get("risk_flag")]
    failed = [n for n in history if n.get("kind") == "REVERT" or n.get("reverted_attempt")]
    normal = [n for n in history if not n.get("risk_flag")]

    ranked = sorted(
        ((_score_note(task or "", n, i), i, n) for i, n in enumerate(normal)),
        key=lambda t: t[0],
        reverse=True,
    )

    risk_block = (
        "\n".join(_format_note(n) for n in risk_notes)
        or "(no DO-NOT-REMOVE guards recorded)"
    )
    decisions_block = (
        "\n".join(f"- {d}" for d in decisions[:15]) or "(no recorded decisions)"
    )
    contracts_block = (
        "\n".join(_contract_line(d) for d in dependent_cards)
        or "(no dependents)"
    )
    failed_block = (
        "\n".join(_format_note(n) for n in failed[:8])
        or "(no recorded failed approaches)"
    )

    # fixed-cost sections (mandatory); history fills whatever remains
    header_task = f"# Task\n{(task or '').strip()}\n"
    header_target = f"# Target code: {fname}\n"
    tail = (
        f"\n# History & risks (DO NOT REMOVE)\n{risk_block}\n"
        f"\n# Decisions\n{decisions_block}\n"
        f"\n# Dependent contracts\n{contracts_block}\n"
        f"\n# Failed approaches (do not retry)\n{failed_block}\n"
    )
    history_header = "\n# Relevant history\n"

    reserved = len(header_task) + len(header_target) + len(tail) + len(history_header)
    room_for_code = max(budget_chars - reserved - 400, 200)
    # keep at least a stub of target code; shrink further only if mandatory overflows
    code = _truncate(target_code or "", max(room_for_code, 200))

    def assemble(extra_notes: list[str]) -> str:
        hist = "".join(extra_notes) or "(no further history selected)\n"
        return header_task + header_target + code + "\n" + history_header + hist + tail

    # greedily add best-first history notes while inside budget
    chosen: list[str] = []
    for _, _, note in ranked:
        cand = chosen + [_format_note(note) + "\n"]
        if len(assemble(cand)) <= budget_chars:
            chosen = cand
        # else skip this note and try the next-shorter one (keeps budget, prefers relevance)
    prompt = assemble(chosen)
    if len(prompt) > budget_chars:
        # mandatory sections alone overflow: shrink target code to fit
        overflow = len(prompt) - budget_chars
        code = _truncate(code, max(len(code) - overflow - 1, 50))
        prompt = assemble(chosen)
    return prompt[:budget_chars] if len(prompt) > budget_chars else prompt


__all__ = ["TEMPLATE_WEIGHTS", "CONFIDENCE_WEIGHT", "build_prompt"]
