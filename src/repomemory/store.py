"""C3 Memory Store â€” file-layout persistence (no server, no embeddings).

Layout under ``<repo>/.repomemory/``::

    profiles/<relpath>.json   # C1 profiler output per file
    history/<relpath>.json    # {"history": [...], "decisions": [...]}
    graph.json                # C4 dependency graph (see depgraph.to_json)
    index.sqlite              # lightweight full-text mirror for inspection

A ``MemoryCard`` assembles everything known about one file::

    {"file": ..., "profile": {...}, "history": [...], "decisions": [...],
     "dependents": [...], "dependencies": [...]}

All JSON is UTF-8 and written atomically (temp file in the same directory
+ :func:`os.replace`). ``search_bm25`` is dependency-free plain-Python TF
scoring over history notes + decisions.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path

__all__ = ["MemoryStore", "compute_score", "DEFAULT_SCORING_WEIGHTS"]

_TOKEN_RE = re.compile(r"[a-z0-9]+")

DEFAULT_SCORING_WEIGHTS = {
    "tfidfWeight": 0.7,
    "tagWeight": 0.3,
    "decayRate": 0.005,
    "maxAccessBoost": 5.0,
    "correctionBoost": 2.0,
}

RECALL_TEMPLATES = {
    "default": {"order": ["profile", "memories", "skills", "knowledge"], "weights": {}},
    "technical": {"order": ["profile", "skills", "knowledge", "memories"],
                  "weights": {"memories": 0.7, "skills": 1.5, "knowledge": 1.3}},
    "a2e": {"order": ["skills", "memories", "knowledge", "profile"],
            "weights": {"memories": 1.5, "skills": 2.0, "knowledge": 1.0}},
}


def compute_score(tfidf=0.0, tag_overlap=0.0, days_since_update=0.0,
                  access_count=0, is_correction=False, weights=None) -> float:
    """SPIKES 1B: relevance*decay*access[*correctionBoost]."""
    w = dict(DEFAULT_SCORING_WEIGHTS)
    if weights:
        w.update(weights)
    relevance = tfidf * w["tfidfWeight"] + tag_overlap * w["tagWeight"]
    decay = 1.0 if w["decayRate"] == 0 else max(0.01, 2.718281828 ** (-w["decayRate"] * days_since_update))
    import math
    access_boost = min(math.log2(2 + max(access_count, 0)), w["maxAccessBoost"])
    base = relevance * decay * access_boost
    return base * w["correctionBoost"] if is_correction else base


def _safe_rel(rel: str | os.PathLike) -> str:
    """Normalise to a ``/``-separated relative path, rejecting escapes."""
    text = os.fspath(rel).replace("\\", "/")
    if not text or "\x00" in text:
        raise ValueError(f"bad relpath: {rel!r}")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise ValueError(f"absolute path not allowed: {rel!r}")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ValueError(f"path escape not allowed: {rel!r}")
    return "/".join(parts)


def _atomic_write_json(path: Path, data: object) -> None:
    """Write JSON (UTF-8) atomically via temp file + :func:`os.replace`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokeniser shared by search and indexing."""
    return _TOKEN_RE.findall(text.lower())


def _doc_text(history: list, decisions: list) -> str:
    """Flatten history notes + decisions to searchable text."""
    try:
        return json.dumps({"h": history, "d": decisions}, ensure_ascii=False)
    except (TypeError, ValueError):
        return f"{history} {decisions}"


class MemoryStore:
    """Persistence for profiles, history, cards, graph and search.

    Args:
        root: Repository root. State lives in ``<root>/.repomemory/``.
            For convenience, passing the ``.repomemory`` directory itself
            is also accepted (it is then used directly as the store dir).
    """

    def __init__(self, root: str | os.PathLike) -> None:
        given = Path(root)
        if given.name == ".repomemory":
            self.store_dir = given
            self.root = given.parent
        else:
            self.root = given
            self.store_dir = given / ".repomemory"
        self.profiles_dir = self.store_dir / "profiles"
        self.history_dir = self.store_dir / "history"
        self.graph_path = self.store_dir / "graph.json"
        self.index_db = self.store_dir / "index.sqlite"
        # Listing cache: directory mtimes -> relpath lists. Re-scans (rglob
        # over thousands of files) happen only when something changed, so
        # per-call paths (recall/search/stats/verify) stay O(1) syscalls.
        self._listing_cache: dict = {"key": None, "profiles": [], "histories": []}
        self._graph_cache: dict = {"mtime": -1.0, "data": {"nodes": [], "edges": []}}
        self.ensure()

    # -- lifecycle ------------------------------------------------------

    def ensure(self) -> MemoryStore:
        """Create store directories and the sqlite index (idempotent)."""
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_db()
        return self

    @classmethod
    def open(cls, root: str | os.PathLike) -> MemoryStore:
        """Open (creating as needed) the store for ``root``."""
        return cls(root)

    def _ensure_db(self) -> None:
        try:
            with sqlite3.connect(self.index_db, timeout=5.0) as conn:
                # WAL + busy timeout: safe when two servers (e.g. a duplicate
                # host spawn) share one store; readers never block writers.
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA busy_timeout=5000")
                except sqlite3.Error:
                    pass
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS documents"
                    " (path TEXT PRIMARY KEY, content TEXT, updated REAL)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS access_counts"
                    " (path TEXT PRIMARY KEY, count INTEGER DEFAULT 0, updated REAL)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS metrics"
                    " (day TEXT, agent TEXT, calls INTEGER, hits INTEGER)"
                )
        except sqlite3.Error:
            pass

    def _lock_path(self) -> Path:
        return self.store_dir / ".lock"

    def _dir_key(self) -> tuple:
        """Cheap freshness key: (mtime, entry-count) per directory, recursive.

        Touches directories only â€” O(dirs), not O(files) â€” so it stays fast
        at 3,374 files while still catching external writers (a second
        server process, a git checkout, etc.).
        """
        sigs = []
        for base in (self.profiles_dir, self.history_dir):
            stack = [os.fspath(base)]
            seen = 0
            try:
                while stack and seen < 4096:
                    d = stack.pop()
                    try:
                        st = os.stat(d)
                        entries = list(os.scandir(d))
                    except OSError:
                        continue
                    sigs.append((st.st_mtime_ns, len(entries)))
                    seen += 1
                    for e in entries:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                stack.append(e.path)
                        except OSError:
                            pass
            except OSError:
                sigs.append((-1, -1))
        return tuple(sigs)

    @staticmethod
    def _rel_json_files(base: Path) -> list[str]:
        out = []
        if not base.exists():
            return out
        for path in sorted(base.rglob("*.json")):
            rel = path.relative_to(base).as_posix()
            out.append(rel[: -len(".json")])
        return out

    def _listings(self) -> tuple[list[str], list[str]]:
        """Cached (profiles, histories) relpaths; rescans only on change."""
        key = self._dir_key()
        if key != self._listing_cache["key"]:
            self._listing_cache = {
                "key": key,
                "profiles": self._rel_json_files(self.profiles_dir),
                "histories": self._rel_json_files(self.history_dir),
            }
        return self._listing_cache["profiles"], self._listing_cache["histories"]

    def _invalidate_listings(self) -> None:
        self._listing_cache["key"] = None

    def _index_doc(self, rel: str, history: list, decisions: list) -> None:
        """Mirror a history document into sqlite (best effort)."""
        try:
            import time

            with sqlite3.connect(self.index_db, timeout=5.0) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO documents(path, content, updated)"
                    " VALUES (?, ?, ?)",
                    (rel, _doc_text(history, decisions), time.time()),
                )
        except (sqlite3.Error, OSError):
            pass

    # -- paths ----------------------------------------------------------

    def _profile_path(self, rel: str) -> Path:
        return self.profiles_dir / (_safe_rel(rel) + ".json")

    def _history_path(self, rel: str) -> Path:
        return self.history_dir / (_safe_rel(rel) + ".json")

    def _read_history_raw(self, rel: str) -> tuple[list, list]:
        """Return ``(history, decisions)`` tolerating legacy list files."""
        path = self._history_path(rel)
        if not path.exists():
            return [], []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return [], []
        if isinstance(data, list):
            return data, []
        if isinstance(data, dict):
            history = data.get("history", data.get("notes", []))
            decisions = data.get("decisions", [])
            return (
                history if isinstance(history, list) else [],
                decisions if isinstance(decisions, list) else [],
            )
        return [], []

    def _write_history(self, rel: str, history: list, decisions: list) -> None:
        _atomic_write_json(
            self._history_path(rel), {"history": history, "decisions": decisions}
        )
        self._index_doc(_safe_rel(rel), history, decisions)
        self._invalidate_listings()

    def _iter_history_rels(self) -> list[str]:
        """All relpaths with a history file (cached, rescans only on change)."""
        return self._listings()[1]

    # -- profiles -------------------------------------------------------

    def save_profile(self, rel: str, profile: dict) -> None:
        """Persist the C1 profile for ``rel`` (UTF-8, atomic)."""
        _atomic_write_json(self._profile_path(rel), profile)
        self._invalidate_listings()

    def load_profile(self, rel: str) -> dict | None:
        """Load the C1 profile for ``rel``; :data:`None` when missing."""
        path = self._profile_path(rel)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def list_files(self) -> list[str]:
        """Sorted relpaths having a stored profile (cached, rescans only on change)."""
        return list(self._listings()[0])

    # -- history --------------------------------------------------------

    def save_history(self, rel: str, notes: list[dict], decisions: list | None = None) -> None:
        """Persist history notes; when decisions is None preserve stored ones."""
        if decisions is None:
            _, decisions = self._read_history_raw(rel)
        self._write_history(rel, list(notes), list(decisions or []))

    def load_history(self, rel: str) -> list[dict]:
        """Load history notes for ``rel``; ``[]`` when missing."""
        history, _ = self._read_history_raw(rel)
        return history

    def append_history_note(self, rel: str, note: dict) -> list[dict]:
        """Append ``note`` unless its ``(sha, summary)`` duplicates one."""
        history, decisions = self._read_history_raw(rel)
        key = (note.get("sha"), note.get("summary"))
        if not any((n.get("sha"), n.get("summary")) == key for n in history if isinstance(n, dict)):
            history.append(note)
            self._write_history(rel, history, decisions)
        return history

    # -- cards ----------------------------------------------------------

    def save_card(self, rel: str | dict, card: dict | None = None) -> None:
        """Persist a memory card, fanning out to profile + history files.

        Accepts either ``save_card(rel, card)`` or ``save_card(card)`` where
        ``card["file"]`` gives the relpath. ``dependents``/``dependencies``
        entries are derived from the graph at read time and ignored here.
        """
        if card is None and isinstance(rel, dict):
            card, rel = rel, rel.get("file")
        if rel is None or card is None:
            raise ValueError("save_card requires a relpath and a card dict")
        rel = _safe_rel(str(rel))
        profile = card.get("profile")
        if isinstance(profile, dict):
            self.save_profile(rel, profile)
        history = card.get("history", [])
        decisions = card.get("decisions", [])
        self._write_history(
            rel,
            list(history) if isinstance(history, list) else [],
            list(decisions) if isinstance(decisions, list) else [],
        )

    def load_card(self, rel: str) -> dict:
        """Assemble the ``MemoryCard`` dict for ``rel``.

        Missing pieces default to empty (``profile`` â†’ ``{}``,
        ``history``/``decisions``/``dependents``/``dependencies`` â†’ ``[]``).
        ``dependents``/``dependencies`` come from ``IMPORTS`` edges in the
        stored ``graph.json`` (both current dict-edge and legacy list-edge
        shapes are understood).
        """
        rel = _safe_rel(rel)
        profile = self.load_profile(rel) or {}
        history, decisions = self._read_history_raw(rel)
        dependents, dependencies = self._card_links(rel)
        return {
            "file": rel,
            "profile": profile,
            "history": history,
            "decisions": decisions,
            "dependents": dependents,
            "dependencies": dependencies,
        }

    def get_card(self, rel: str, graph_data: dict | None = None) -> dict:
        """Assemble a card, optionally using an explicit graph dict.

        Backward-compatible helper: when ``graph_data`` is given it is
        used for link resolution instead of the stored ``graph.json``.
        """
        card = self.load_card(rel)
        if graph_data is not None:
            dependents, dependencies = _links_from_graph_data(graph_data, _safe_rel(rel))
            card["dependents"] = dependents
            card["dependencies"] = dependencies
        return card

    def _card_links(self, rel: str) -> tuple[list[str], list[str]]:
        data = self.load_graph_json()
        return _links_from_graph_data(data, rel)

    # -- graph ----------------------------------------------------------

    def save_graph_json(self, data: dict) -> None:
        """Persist dependency-graph JSON to ``graph.json`` (UTF-8, atomic)."""
        self.ensure()
        _atomic_write_json(self.graph_path, data)
        self._graph_cache["mtime"] = -1.0

    # Legacy alias (graph is plain JSON in this store).
    save_graph = save_graph_json

    def load_graph_json(self) -> dict:
        """Load ``graph.json`` (mtime-cached; ``{"nodes": [], "edges": []}`` when missing)."""
        try:
            mtime = os.stat(self.graph_path).st_mtime_ns
        except OSError:
            return {"nodes": [], "edges": []}
        if mtime != self._graph_cache["mtime"]:
            try:
                data = json.loads(self.graph_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                data = {"nodes": [], "edges": []}
            if not isinstance(data, dict):
                data = {"nodes": [], "edges": []}
            self._graph_cache = {"mtime": mtime, "data": data}
        return self._graph_cache["data"]

    # Legacy alias.
    load_graph = load_graph_json

    # -- search ---------------------------------------------------------

    def search_bm25(self, query: str, limit: int = 10) -> list[str]:
        """Rank files by plain-Python TF scoring over history + decisions.

        Args:
            query: Free-text query; tokenised like indexed documents.
            limit: Maximum relpaths to return.

        Returns:
            Relpaths with a positive term-frequency score, sorted by
            score (desc) then path (asc) for determinism.
        """
        terms = _tokenize(query or "")
        if not terms:
            return []
        # Fast path: the sqlite documents mirror (one SELECT, no file reads).
        # Falls back to the file walk only for legacy stores with an empty index.
        rows: list[tuple[str, str]] | None = None
        try:
            with sqlite3.connect(self.index_db, timeout=5.0) as conn:
                rows = [(str(p), str(c or ""))
                        for p, c in conn.execute("SELECT path, content FROM documents")]
        except sqlite3.Error:
            rows = None
        scored: list[tuple[int, str]] = []
        if rows:
            for rel, content in rows:
                counts = Counter(_tokenize(content))
                score = sum(counts[t] for t in terms)
                if score > 0:
                    scored.append((score, rel))
        else:
            for rel in self._iter_history_rels():
                history, decisions = self._read_history_raw(rel)
                counts = Counter(_tokenize(_doc_text(history, decisions)))
                score = sum(counts[t] for t in terms)
                if score > 0:
                    scored.append((score, rel))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [rel for _, rel in scored[: max(0, limit)]]

    # -- recall (single-call, A2E-style) ----------------------------------

    def _access_counts(self) -> dict[str, int]:
        """All access counts in one query (recall calls this once, not per file)."""
        try:
            with sqlite3.connect(self.index_db, timeout=5.0) as conn:
                return {str(p): int(c)
                        for p, c in conn.execute("SELECT path, count FROM access_counts")}
        except sqlite3.Error:
            return {}

    def _access_count(self, rel: str) -> int:
        try:
            with sqlite3.connect(self.index_db, timeout=5.0) as conn:
                row = conn.execute(
                    "SELECT count FROM access_counts WHERE path=?", (rel,)).fetchone()
                return int(row[0]) if row else 0
        except sqlite3.Error:
            return 0

    def _bump_access(self, rels: list[str]) -> None:
        import time
        try:
            with sqlite3.connect(self.index_db, timeout=5.0) as conn:
                for rel in rels:
                    conn.execute(
                        "INSERT INTO access_counts(path,count,updated) VALUES(?,1,?)"
                        " ON CONFLICT(path) DO UPDATE SET count=count+1, updated=?",
                        (rel, time.time(), time.time()))
        except sqlite3.Error:
            pass

    @staticmethod
    def _note_text(n: dict) -> str:
        """Searchable text for one note: summary + why + evidence + fix lines."""
        parts = [str(n.get("summary", "")), str(n.get("why", ""))]
        ev = n.get("evidence")
        if isinstance(ev, dict) and ev.get("quote"):
            parts.append(str(ev["quote"]))
        parts.extend(str(x) for x in (n.get("fix_lines") or []) if x)
        return " ".join(parts)

    def recall(self, query: str, max_items: int = 20, max_chars: int = 8000,
               template: str = "technical") -> dict:
        """Single recall(): per-NOTE best-match scoring, file ranked by its best note.

        Fixes the junior-librarian problem: a file with one exact note now
        outranks a file with ten vague ones. Notes carrying ``risk_flag``
        get a 1.5x boost (they are past bugs); corrections 2x (via
        :func:`compute_score`). Formats the best note(s) per file, not the
        first five in storage order.
        """
        tmpl = RECALL_TEMPLATES.get(template, RECALL_TEMPLATES["technical"])
        weights = tmpl["weights"]
        terms = set(_tokenize(query or ""))
        if not terms:
            return {"items": [], "formatted": "(empty query)",
                    "template": template, "estimated_chars": 15}
        cands: list[tuple[float, str, dict, dict]] = []  # (score, rel, best_note, data)
        access_map = self._access_counts()
        # Prefilter via the sqlite mirror (one SELECT): any file whose best
        # note can match must share a term with its indexed doc, so this
        # loses nothing while bounding JSON reads to ~5x the request.
        pre = set(self.search_bm25(query, max(50, max_items * 5)))
        rels = [r for r in self._iter_history_rels() if not pre or r in pre]
        if pre and not rels:
            rels = list(self._iter_history_rels())
        for rel in rels:
            history, decisions = self._read_history_raw(rel)
            notes = [n for n in history if isinstance(n, dict)]
            if not notes and not decisions:
                continue
            access = access_map.get(rel, 0)
            best: tuple[float, dict] | None = None
            for n in notes:
                counts = Counter(_tokenize(self._note_text(n)))
                tf = sum(counts.get(t, 0) for t in terms)
                if tf <= 0:
                    continue
                is_corr = (n.get("kind") == "REVERT"
                           or n.get("category") == "correction")
                score = compute_score(tfidf=float(tf), tag_overlap=0.0,
                                      days_since_update=0.0,
                                      access_count=access,
                                      is_correction=is_corr)
                if isinstance(n.get("risk_flag"), dict):
                    score *= 1.5
                if best is None or score > best[0]:
                    best = (score, n)
            if best is None:
                continue
            w = weights.get("memories", 1.0)
            cands.append((best[0] * w, rel, best[1],
                          {"history": notes, "decisions": decisions}))
        cands.sort(key=lambda x: (-x[0], x[1]))
        chosen = cands[: max(0, max_items)]
        self._bump_access([r for _, r, _, _ in chosen])
        # format: best note first per file, then further matching notes
        lines: list[str] = []
        total = 0

        def _emit(text: str) -> bool:
            nonlocal total
            if total + len(text) > max_chars:
                return False
            lines.append(text)
            total += len(text)
            return True

        for _, rel, best_note, data in chosen:
            head = (f"[{rel} {str(best_note.get('sha', ''))[:7]} "
                    f"{best_note.get('kind', '?')}] {best_note.get('summary', '')}")
            rf = best_note.get("risk_flag")
            if isinstance(rf, dict):
                head += f" RISK {rf.get('lines_hint', '')}: {str(rf.get('text', ''))[:160]}"
            fl = best_note.get("fix_lines") or []
            if fl:
                head += " fix keeps: " + "; ".join(str(x)[:100] for x in fl[:2])
            if not _emit(head):
                break
            # one more matching note from the same file, if budget allows
            for n in data["history"]:
                if n is best_note or not isinstance(n, dict):
                    continue
                counts = Counter(_tokenize(self._note_text(n)))
                if sum(counts.get(t, 0) for t in terms) <= 0:
                    continue
                extra = (f"  + [{rel} {str(n.get('sha', ''))[:7]} "
                         f"{n.get('kind', '?')}] {n.get('summary', '')}")
                if not _emit(extra):
                    break
        formatted = "\n".join(lines) if lines else "(no relevant memories)"
        return {"items": [{"file": r, "score": round(s, 3),
                           "sha": str(bn.get("sha", ""))[:7]} for s, r, bn, _ in chosen],
                "formatted": formatted,
                "template": template, "estimated_chars": len(formatted)}

    def prime(self) -> MemoryStore:
        """Pre-warm listing + graph caches (server boot, not per-CLI-call).

        Pays the one full directory walk up front so the first tool call
        never eats it under a host timeout.
        """
        self._listings()
        self.load_graph_json()
        return self

    # -- portability ------------------------------------------------------

    def export(self) -> dict:
        """Portable JSON of all entities + access counts."""
        import time
        entities: dict[str, list] = {"profiles": [], "histories": []}
        for rel in self.list_files():
            p = self.load_profile(rel)
            if p is not None:
                entities["profiles"].append({"file": rel, "profile": p})
        for rel in self._iter_history_rels():
            h, d = self._read_history_raw(rel)
            entities["histories"].append({"file": rel, "history": h, "decisions": d})
        counts: dict[str, int] = {}
        try:
            with sqlite3.connect(self.index_db, timeout=5.0) as conn:
                for path, count in conn.execute("SELECT path,count FROM access_counts"):
                    counts[str(path)] = int(count)
        except sqlite3.Error:
            pass
        return {"version": 1, "exportedAt": time.time(),
                "entities": entities, "accessCounts": counts,
                "graph": self.load_graph_json()}

    def import_data(self, data: dict, skip_existing: bool = False) -> dict:
        """Import export() payload. Returns {imported, skipped}."""
        imported = skipped = 0
        ents = (data or {}).get("entities", {})
        for entry in ents.get("profiles", []):
            rel = entry.get("file")
            if not rel:
                continue
            if skip_existing and self.load_profile(rel) is not None:
                skipped += 1
                continue
            self.save_profile(rel, entry.get("profile", {}))
            imported += 1
        for entry in ents.get("histories", []):
            rel = entry.get("file")
            if not rel:
                continue
            if skip_existing and self.load_history(rel):
                skipped += 1
                continue
            self._write_history(rel, entry.get("history", []), entry.get("decisions", []))
            imported += 1
        graph = (data or {}).get("graph")
        if isinstance(graph, dict) and graph.get("nodes"):
            self.save_graph_json(graph)
        return {"imported": imported, "skipped": skipped}

    def snapshot(self, label: str = "") -> dict:
        """Full point-in-time copy under .repomemory/snapshots/<ts>-<label>."""
        import shutil
        import time
        ts = time.strftime("%Y%m%d-%H%M%S")
        name = f"{ts}-{label}" if label else ts
        dest = self.store_dir / "snapshots" / name
        dest.mkdir(parents=True, exist_ok=True)
        for sub in ("profiles", "history"):
            src = self.store_dir / sub
            if src.exists():
                shutil.copytree(src, dest / sub, dirs_exist_ok=True)
        if self.graph_path.exists():
            shutil.copy2(self.graph_path, dest / "graph.json")
        return {"id": name, "path": dest.as_posix()}

    def list_snapshots(self) -> list[str]:
        snap = self.store_dir / "snapshots"
        if not snap.exists():
            return []
        return sorted(p.name for p in snap.iterdir() if p.is_dir())

    def restore(self, snap_id: str) -> bool:
        """Restore from snapshot id. Returns True on success."""
        import shutil
        src = self.store_dir / "snapshots" / snap_id
        if not src.exists():
            return False
        for sub in ("profiles", "history"):
            s = src / sub
            if s.exists():
                target = self.store_dir / sub
                shutil.rmtree(target, ignore_errors=True)
                shutil.copytree(s, target)
        g = src / "graph.json"
        if g.exists():
            shutil.copy2(g, self.graph_path)
        return True

    def cleanup(self, max_age_days: int = 90, dry_run: bool = False) -> dict:
        """Remove history files older than max_age_days (by mtime)."""
        import time
        cutoff = time.time() - max_age_days * 86400
        removed: list[str] = []
        for rel in self._iter_history_rels():
            p = self._history_path(rel)
            try:
                if p.stat().st_mtime < cutoff:
                    removed.append(rel)
                    if not dry_run:
                        p.unlink()
            except OSError:
                pass
        return {"removed": removed, "dry_run": dry_run}

    def verify(self) -> dict:
        """Hash-validate JSON files. Returns {valid, files, errors}."""
        errors: list[str] = []
        count = 0
        for base in (self.profiles_dir, self.history_dir):
            if not base.exists():
                continue
            for p in base.rglob("*.json"):
                count += 1
                try:
                    json.loads(p.read_text(encoding="utf-8"))
                except Exception as e:
                    errors.append(f"{p.name}: {e}")
        return {"valid": not errors, "files": count, "errors": errors}

    # -- stats ----------------------------------------------------------

    def stats(self) -> dict:
        """Return store statistics (cached listings + graph; one indexed COUNT)."""
        profiles, histories = self._listings()
        data = self.load_graph_json()
        nodes, edges = data.get("nodes", []), data.get("edges", [])
        graph_nodes = len(nodes) if isinstance(nodes, list) else 0
        graph_edges = len(edges) if isinstance(edges, list) else 0
        indexed = 0
        try:
            if self.index_db.exists():
                with sqlite3.connect(self.index_db, timeout=5.0) as conn:
                    row = conn.execute("SELECT COUNT(*) FROM documents").fetchone()
                    indexed = int(row[0]) if row else 0
        except (sqlite3.Error, OSError, ValueError):
            indexed = 0
        return {
            "profiles": len(profiles),
            "histories": len(histories),
            "graph_nodes": graph_nodes,
            "graph_edges": graph_edges,
            "indexed_docs": indexed,
            "index_db": self.index_db.exists(),
            "store_dir": self.store_dir.as_posix(),
        }


def _links_from_graph_data(data: dict, rel: str) -> tuple[list[str], list[str]]:
    """Resolve ``(dependents, dependencies)`` for ``rel`` from graph JSON."""
    if not isinstance(data, dict):
        return [], []
    node_types: dict[str, str] = {}
    for entry in data.get("nodes", []) or []:
        if isinstance(entry, dict) and entry.get("id") is not None:
            node_types[str(entry["id"])] = str(entry.get("type", "file"))
        elif isinstance(entry, str):
            node_types[entry] = "file"

    def _is_file(node: str) -> bool:
        return node_types.get(node, "file") == "file" and "::" not in node

    dependents: set[str] = set()
    dependencies: set[str] = set()
    for entry in data.get("edges", []) or []:
        if isinstance(entry, dict):
            src, dst = entry.get("src"), entry.get("dst")
            kind = entry.get("kind")
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            src, dst = entry[0], entry[1]
            kind = entry[2] if len(entry) >= 3 else None
        else:
            continue
        if src is None or dst is None:
            continue
        src, dst = str(src), str(dst)
        if kind is not None and kind != "IMPORTS":
            continue
        if src == rel and _is_file(dst):
            dependencies.add(dst)
        if dst == rel and _is_file(src):
            dependents.add(src)
    return sorted(dependents), sorted(dependencies)
