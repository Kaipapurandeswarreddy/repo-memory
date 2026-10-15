"""C7 Verifier — deterministic patch checks (no LLM).

Findings (each ``{file, severity, type, message, sha?}``):

- ``guard-removed``    (high): deleted lines look like a recorded guard and the
  card has a ``risk_flag`` note that forbids removing them.
- ``fix-removed``      (high): deleted lines match a recorded BUGFIX note's
  ``fix_lines`` (exact added-lines of the fix) — the fix itself was undone.
- ``contract-change``  (high): a ``def name(...)`` signature was edited; the
  message names dependents that may still use the old signature.
- ``revert-retry``     (med):  added lines resemble a recorded REVERT / failed
  approach (token Jaccard over added lines vs the note).
"""

from __future__ import annotations

import re
from collections import OrderedDict

Finding = dict  # {file, severity, type, message, sha?}

_GUARD_RES = [
    re.compile(r"^\s*if\b.*\b(return|raise|continue|break|exit)\b"),
    re.compile(r"^\s*(assert|raise)\b"),
    re.compile(r"^\s*try\s*:"),
    re.compile(r"^\s*except\b"),
    re.compile(r"10000"),
    re.compile(r"duplicat|idempot|dedupe|retry|guard|race|atomic", re.IGNORECASE),
]

_DEF_RE = re.compile(
    r"^\s*(?:async\s+)?def\s+(?P<name>\w+)\s*\((?P<params>.*)\)\s*"
    r"(?:->\s*(?P<ret>[^:#]+?))?\s*:?\s*(?:#.*)?$"
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "the,a,an,and,or,to,of,in,on,for,with,is,are,was,were,it,this,that,be,as,at,by,"
    "from,into,add,make,fix,use,using,please,should,would,could,new,def,return,none".split(",")
)

REVERT_SIMILARITY_THRESHOLD = 0.15


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOP and len(t) > 1}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _is_guard_line(code_line: str) -> bool:
    return any(rx.search(code_line or "") for rx in _GUARD_RES)


def _decode_diff(data: bytes | str) -> str:
    """Decode diff bytes, sniffing BOMs (PowerShell ``>`` writes UTF-16).

    A UTF-16 diff decoded as UTF-8 becomes mojibake that parses to zero
    files — which must never report as clean (see below).
    """
    if isinstance(data, str):
        text = data
    elif isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = raw.decode("utf-16", errors="replace")
        elif raw.startswith(b"\xef\xbb\xbf"):
            text = raw.decode("utf-8-sig", errors="replace")
        else:
            text = raw.decode("utf-8", errors="replace")
    else:
        return ""
    return text.lstrip("\ufeff")


def _parse_patch(patch_diff: str) -> dict[str, dict[str, list[str]]]:
    """Parse unified diff into ``{file: {added: [...], deleted: [...]}}``."""
    files: dict[str, dict[str, list[str]]] = OrderedDict()
    current: str | None = None

    def ensure(f: str) -> dict[str, list[str]]:
        if f not in files:
            files[f] = {"added": [], "deleted": []}
        return files[f]

    for raw in (patch_diff or "").splitlines():
        if raw.startswith("diff --git "):
            parts = raw.split()
            # "diff --git a/x b/y" -> take b/ side
            b_side = parts[-1] if parts else ""
            current = b_side[2:] if b_side.startswith("b/") else b_side
            if current and current != "/dev/null":
                ensure(current)
            else:
                current = None
        elif raw.startswith("+++ "):
            path = raw[4:].strip().split("\t")[0].strip().strip('"')
            if path.startswith("b/"):
                path = path[2:]
            if path != "/dev/null":
                current = path
                ensure(current)
        elif raw.startswith("--- "):
            path = raw[4:].strip().split("\t")[0].strip().strip('"')
            if path.startswith("a/"):
                path = path[2:]
            if current is None and path != "/dev/null":
                current = path
                ensure(current)
        elif raw.startswith("+") and not raw.startswith("+++"):
            if current is None:
                current = "unknown"
                ensure(current)
            files[current]["added"].append(raw[1:])
        elif raw.startswith("-") and not raw.startswith("---"):
            if current is None:
                current = "unknown"
                ensure(current)
            files[current]["deleted"].append(raw[1:])
        # context / hunk headers / "\ No newline" ignored
    return files


def _norm_sig(params: str, ret: str | None) -> str:
    p = re.sub(r"\s+", "", params or "").strip()
    r = re.sub(r"\s+", "", (ret or "").strip())
    return f"({p})->{r}" if r else f"({p})"


def _card_for(cards: dict[str, dict], path: str) -> dict | None:
    if path in cards:
        return cards[path]
    # tolerate a/ b/ prefixes and ./ prefixes
    norm = path.replace("\\", "/").lstrip("./")
    norm = re.sub(r"^[ab]/", "", norm)
    return cards.get(norm)


def check_patch(
    patch_diff: str | bytes, card: dict | None, dependent_cards: list[dict] | None = None
) -> list[Finding]:
    """Run deterministic checks; never raises on malformed input.

    A non-empty diff that parses to zero files (UTF-16 mojibake, wrong repo
    root such as a parent of a nested repo) yields an ``unparsed-diff``
    warning — it must never report as clean.
    """
    card = card or {}
    dependent_cards = list(dependent_cards or [])
    text = _decode_diff(patch_diff or "")
    file_patches = _parse_patch(text)
    if not file_patches:
        if text.strip():
            return [{
                "file": "?",
                "severity": "low",
                "type": "unparsed-diff",
                "message": ("diff parsed to zero files — nothing was checked. "
                            "Wrong repo root (nested git repo?) or non-UTF-8 "
                            "encoding (PowerShell `>` writes UTF-16; use "
                            "`Out-File -Encoding utf8` instead)."),
            }]
        return []

    cards: dict[str, dict] = {}
    if card.get("file"):
        cards[str(card["file"])] = card
    for d in dependent_cards:
        if isinstance(d, dict) and d.get("file"):
            cards[str(d["file"])] = d

    findings: list[Finding] = []
    for path, change in file_patches.items():
        added = change.get("added", [])
        deleted = change.get("deleted", [])
        doc = _card_for(cards, path) or (card if path == card.get("file") else None) or {}
        history = doc.get("history") or []
        risk_notes = [n for n in history if isinstance(n, dict) and n.get("risk_flag")]

        # -- guard-removed ------------------------------------------------
        guard_deleted = [ln for ln in deleted if _is_guard_line(ln)]
        if guard_deleted and risk_notes:
            for rn in risk_notes:
                sha = str(rn.get("sha", ""))
                hint = ""
                risk = rn.get("risk_flag") or {}
                if isinstance(risk, dict):
                    hint = str(risk.get("lines_hint", ""))
                summary = str(rn.get("summary", ""))[:120]
                sample = guard_deleted[0].strip()[:100]
                msg = (
                    f"guard {hint + ' ' if hint else ''}removed "
                    f"({len(guard_deleted)} guard-like line(s), e.g. `{sample}`), "
                    f"added in {sha[:7]} ({summary}). Likely regression."
                )
                findings.append(
                    {
                        "file": path,
                        "severity": "high",
                        "type": "guard-removed",
                        "message": " ".join(msg.split()),
                        "sha": sha,
                    }
                )

        # -- fix-removed ----------------------------------------------------
        fix_deleted = []
        for ln in deleted:
            norm = " ".join(ln.split())
            if not norm:
                continue
            for rn in history:
                if not isinstance(rn, dict) or rn.get("kind") != "BUGFIX":
                    continue
                for fl in (rn.get("fix_lines") or []):
                    fl_norm = " ".join(str(fl).split())
                    if fl_norm and (fl_norm == norm or fl_norm in norm or norm in fl_norm):
                        fix_deleted.append((ln, rn))
                        break
        for ln, rn in fix_deleted[:3]:
            sha = str(rn.get("sha", ""))
            findings.append(
                {
                    "file": path,
                    "severity": "high",
                    "type": "fix-removed",
                    "message": (
                        f"fix line `{ln.strip()[:100]}` removed, added in "
                        f"{sha[:7]} ({str(rn.get('summary', ''))[:110]}). Likely regression."
                    ),
                    "sha": sha,
                }
            )

        # -- contract-change ----------------------------------------------
        del_defs = [(m.group("name"), _norm_sig(m.group("params"), m.group("ret")))
                     for ln in deleted for m in [_DEF_RE.match(ln)] if m]
        add_defs = {m.group("name"): _norm_sig(m.group("params"), m.group("ret"))
                    for ln in added for m in [_DEF_RE.match(ln)] if m}
        for name, old_sig in del_defs:
            new_sig = add_defs.get(name)
            if new_sig is not None and new_sig != old_sig:
                dependents = list(doc.get("dependents") or card.get("dependents") or [])
                dep_txt = (
                    f"; {len(dependents)} dependent(s) may still use old signature: "
                    + ", ".join(dependents[:5])
                    if dependents
                    else ""
                )
                # concrete call sites from dependents' recorded calls
                sites: list[str] = []
                for d in dependent_cards:
                    if not isinstance(d, dict):
                        continue
                    df = str(d.get("file", "?"))
                    for c in ((d.get("profile") or {}).get("calls") or []):
                        if (isinstance(c, dict) and c.get("name")
                                and str(c["name"]).split(".")[-1] == name):
                            sites.append(f"{df}:{c.get('lineno', '?')}")
                            break
                if sites:
                    dep_txt += f"; call sites: " + ", ".join(sites[:5])
                findings.append(
                    {
                        "file": path,
                        "severity": "high",
                        "type": "contract-change",
                        "message": f"{name}{old_sig} changed to {name}{new_sig}{dep_txt}".strip(),
                    }
                )

        # -- revert-retry --------------------------------------------------
        revert_notes = [
            n for n in history
            if isinstance(n, dict)
            and (n.get("kind") == "REVERT" or n.get("reverted_attempt"))
        ]
        if added and revert_notes:
            added_toks = _tokens("\n".join(added))
            for rn in revert_notes:
                note_text = " ".join(
                    str(rn.get(k, "")) for k in ("summary", "why", "reverted_attempt")
                )
                sim = _jaccard(added_toks, _tokens(note_text))
                if sim >= REVERT_SIMILARITY_THRESHOLD:
                    sha = str(rn.get("sha", ""))
                    findings.append(
                        {
                            "file": path,
                            "severity": "med",
                            "type": "revert-retry",
                            "message": (
                                f"patch resembles reverted attempt in {sha[:7]} "
                                f"({str(rn.get('summary', ''))[:110]}; sim={sim:.2f}). "
                                "Already tried - verify the revert reason first."
                            ),
                            "sha": sha,
                        }
                    )
    return findings


def format_report(findings: list[dict]) -> str:
    """Render per-file findings à la Guide §2.7 (grouped, file-first)."""
    if not findings:
        return "No issues found.\n"
    by_file: dict[str, list[dict]] = OrderedDict()
    for f in findings or []:
        by_file.setdefault(str(f.get("file", "unknown")), []).append(f)
    lines: list[str] = []
    for path, items in by_file.items():
        lines.append(path)
        for f in items:
            sev = str(f.get("severity", "med")).lower()
            mark = "[!]" if sev == "high" else "[~]"
            lines.append(f"  {mark} {f.get('type', '?')}: {f.get('message', '')}")
    return "\n".join(lines) + "\n"


__all__ = ["check_patch", "format_report", "REVERT_SIMILARITY_THRESHOLD",
           "_decode_diff"]
