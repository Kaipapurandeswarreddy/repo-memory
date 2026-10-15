"""C5 Orchestrator — lightweight dsh-pattern without langgraph dep.

Pipeline (mirrors deepseek-harness turn flow, single pass):
  router -> fetcher -> joiner/injector -> cross-checker -> prioritiser(report)

class Orchestrator(store, gateway):
    route(task_files, graph)  -> target + 1-hop dependents/dependencies
    fetch(files)              -> {rel: MemoryCard}
    cross_check(patch, cards) -> findings (delegates to verifier)
    run_generate(task, target_rel) -> {patch, warnings, tokens}

run_generate = fetch -> inject -> generate -> verify.
Canonical companion APIs (see injector/verifier):
  injector.build_prompt(task, target_code, card, dependent_cards, budget_chars)
  verifier.check_patch(patch_diff, card, dependent_cards)
Legacy dict-of-cards shapes are still accepted via adapters.
Optional langgraph: if installed, `as_graph()` exposes the same nodes;
core path never requires it (zero-key CLI works).
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from . import graph_builder as _gb
from . import injector as _inj
from . import verifier as _ver


def _load_graph(store) -> dict:
    for name in ("load_graph", "load_graph_json"):
        fn = getattr(store, name, None)
        if callable(fn):
            try:
                data = fn()
                if isinstance(data, dict):
                    return data
            except TypeError:
                pass
    return {"nodes": [], "edges": []}


def _get_card(store, rel: str, graph: dict | None) -> dict:
    for name in ("get_card", "load_card"):
        fn = getattr(store, name, None)
        if not callable(fn):
            continue
        try:
            sig = inspect.signature(fn)
            if name == "get_card" and len(sig.parameters) >= 2:
                try:
                    card = fn(rel, graph)
                except TypeError:
                    card = fn(rel)
            else:
                card = fn(rel)
            if isinstance(card, dict):
                return card
        except (TypeError, ValueError):
            continue
    return {"file": rel, "profile": {}, "history": [],
            "decisions": [], "dependents": [], "dependencies": []}


def _split_cards(target_rel: str, cards: dict[str, dict]):
    """Return (primary_card, dependent_cards) for canonical APIs."""
    primary = cards.get(target_rel)
    if primary is None:
        # fall back to first card (deterministic order)
        first = sorted(cards)[0] if cards else target_rel
        primary = cards.get(first, {"file": first})
        target_rel = first
    dependents = [c for k, c in sorted(cards.items()) if k != target_rel]
    return primary, dependents


def _build_prompt(task: str, target_code: str, primary: dict,
                  dependents: list[dict], budget_chars: int) -> str:
    fn = _inj.build_prompt
    try:
        return fn(task, target_code, primary, dependents,
                  budget_chars=budget_chars)
    except TypeError:
        pass
    # legacy: build_prompt(task, target_rel, target_code, cards_dict, ...)
    cards = {primary.get("file", "target"): primary}
    for d in dependents:
        if isinstance(d, dict) and d.get("file"):
            cards[d["file"]] = d
    try:
        return fn(task, primary.get("file", "target"), target_code,
                  cards, budget_chars=budget_chars)
    except TypeError:
        return fn(task, target_code, primary, dependents)


def _check_cards(patch: str, cards: dict[str, dict]) -> list[dict]:
    """Delegate to verifier, supporting canonical + legacy signatures."""
    if not cards:
        return []
    fn = _ver.check_patch
    try:
        n_params = len(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        n_params = 3
    if n_params >= 3:
        # canonical: one call per card-as-primary so every file's own
        # risk notes are evaluated (cheap, deterministic).
        seen: set[str] = set()
        out: list[dict] = []
        for rel in sorted(cards):
            primary = cards[rel]
            dependents = [c for k, c in sorted(cards.items()) if k != rel]
            for f in fn(patch, primary, dependents) or []:
                key = (f.get("file"), f.get("type"), f.get("message"))
                if key not in seen:
                    seen.add(key)
                    out.append(f)
        return out
    # legacy: check_patch(patch, cards_dict)
    return fn(patch, cards) or []


class Orchestrator:
    def __init__(self, store, gateway, repo_root: str | Path | None = None,
                 budget_chars: int = 12000) -> None:
        self.store = store
        self.gateway = gateway
        self.repo_root = Path(repo_root) if repo_root else None
        self.budget_chars = budget_chars
        self.trace: list[dict] = []  # ralph-style todo_write trace

    # -- nodes -------------------------------------------------------------
    def route(self, task_files: list[str], graph: dict | None = None) -> list[str]:
        """Router: task files + 1-hop dependents/dependencies."""
        graph = graph if graph is not None else _load_graph(self.store)
        scope: set[str] = set()
        for f in task_files:
            scope.add(f)
            try:
                scope.update(_gb.one_hop(graph, f))
            except Exception:
                scope.add(f)
        ordered = sorted(scope)
        self.trace.append({"node": "router", "in": list(task_files), "out": ordered})
        return ordered

    def fetch(self, files: list[str]) -> dict[str, dict]:
        """Fetcher: single recall() per file (no per-step tool loops)."""
        graph = _load_graph(self.store)
        cards = {f: _get_card(self.store, f, graph) for f in files}
        self.trace.append({"node": "fetcher", "files": list(files), "cards": len(cards)})
        return cards

    def cross_check(self, patch: str, cards: dict[str, dict] | None = None,
                    card: dict | None = None,
                    dependent_cards: list[dict] | None = None) -> list[dict]:
        """Cross-checker: deterministic first (verifier), LLM only for phrasing.

        Accepts either the legacy ``cards`` dict or canonical
        ``card + dependent_cards``.
        """
        if cards is None:
            cards = {}
            if card is not None:
                key = card.get("file", "target") if isinstance(card, dict) else "target"
                cards[key] = card
            for d in dependent_cards or []:
                if isinstance(d, dict) and d.get("file"):
                    cards[d["file"]] = d
        warnings = _check_cards(patch or "", cards or {})
        self.trace.append({"node": "cross-checker", "warnings": len(warnings)})
        return warnings

    # -- end-to-end ---------------------------------------------------------
    def run_generate(self, task: str, target_rel: str) -> dict[str, Any]:
        """W-C Generate: route -> fetch -> inject -> generate -> verify."""
        graph = _load_graph(self.store)
        scope = self.route([target_rel], graph)
        cards = self.fetch(scope)
        # ensure the target card exists even when the store has no profile yet
        if target_rel not in cards:
            cards[target_rel] = _get_card(self.store, target_rel, graph)
            scope = sorted(set(scope) | {target_rel})
        primary, dependents = _split_cards(target_rel, cards)
        target_code = self._read_target(target_rel)
        prompt = _build_prompt(task, target_code, primary, dependents,
                               self.budget_chars)
        gen = self.gateway.generate(prompt)
        patch = gen["text"]
        warnings = self.cross_check(patch, cards)
        tokens = {"tokens_in": gen["tokens_in"], "tokens_out": gen["tokens_out"],
                  "latency_ms": gen["latency_ms"]}
        self.trace.append({"node": "prioritiser",
                           "warnings": len(warnings), "tokens": tokens})
        return {"patch": patch, "warnings": warnings, "tokens": tokens,
                "prompt": prompt, "scope": scope}

    def run_generate_with_retry(self, task: str, target_rel: str,
                                max_rounds: int = 3) -> dict[str, Any]:
        """Ralph loop: regenerate while high-severity warnings persist (max_rounds)."""
        best: dict[str, Any] | None = None
        for round_no in range(1, max(1, max_rounds) + 1):
            res = self.run_generate(task, target_rel)
            highs = [w for w in res["warnings"]
                     if str(w.get("severity", "")).lower() == "high"]
            res["rounds"] = round_no
            self.trace.append({"node": "ralph", "round": round_no,
                               "highs": len(highs)})
            if best is None or len(highs) < len(
                    [w for w in best["warnings"] if str(w.get("severity", "")).lower() == "high"]):
                best = res
            if not highs:
                break
            task = (task + f"\n\nPrevious attempt raised: "
                    + "; ".join(w.get("message", "")[:160] for w in highs[:3])
                    + "\nAvoid removing guards/contracts flagged above.")
        assert best is not None
        return best

    # -- helpers -------------------------------------------------------------
    def _read_target(self, rel: str) -> str:
        if self.repo_root is not None:
            p = self.repo_root / rel
            try:
                return p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""
        return ""

    # -- optional langgraph ----------------------------------------------------
    def as_graph(self):
        """Return a langgraph graph if langgraph is installed, else None."""
        try:
            from langgraph.graph import StateGraph  # type: ignore
        except ImportError:
            return None

        orch = self

        def router(s):  # noqa: ANN001, ANN202
            return {"scope": orch.route(s.get("task_files", []), s.get("graph"))}

        def fetcher(s):  # noqa: ANN001, ANN202
            return {"cards": orch.fetch(s.get("scope", []))}

        def checker(s):  # noqa: ANN001, ANN202
            return {"warnings": orch.cross_check(s.get("patch", ""), s.get("cards", {}))}

        g = StateGraph(dict)
        g.add_node("router", router)
        g.add_node("fetcher", fetcher)
        g.add_node("cross-checker", checker)
        g.set_entry_point("router")
        g.add_edge("router", "fetcher")
        g.add_edge("fetcher", "cross-checker")
        g.set_finish_point("cross-checker")
        return g.compile()
