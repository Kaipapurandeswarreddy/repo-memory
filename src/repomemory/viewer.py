"""Static viewer: renders graph.json + cards to .repomemory/viewer/index.html (no server, no LLM)."""

from __future__ import annotations

import html
import json
from pathlib import Path

from .store import MemoryStore

PAGE = ("<!doctype html><html><head><meta charset=\"utf-8\"><title>RepoMemory</title>\n"
        "<style>body[[F]]font-family:system-ui;margin:24px;max-width:1000px[[G]]"
        "code[[F]]background:#f3f3f3;padding:1px 4px[[G]]\n"
        ".card[[F]]border:1px solid #ddd;border-radius:8px;padding:12px;margin:12px 0[[G]]"
        ".risk[[F]]color:#a00[[G]].node[[F]]margin:2px 0[[G]]</style>\n"
        "</head><body><h1>RepoMemory viewer</h1><p>Nodes: __NODES__ Edges: __EDGES__ Files: __FILES__</p>\n"
        "<h2>Graph</h2><div>__GRAPH__</div><h2>Files</h2><div>__CARDS__</div></body></html>")


def _render(nodes: int, edges: int, files: int, graph: str, cards: str) -> str:
    return (PAGE.replace("[[F]]", "{").replace("[[G]]", "}")
            .replace("__NODES__", str(nodes)).replace("__EDGES__", str(edges))
            .replace("__FILES__", str(files)).replace("__GRAPH__", graph)
            .replace("__CARDS__", cards))


def build_viewer(repo_root: str | Path) -> Path:
    repo = Path(repo_root).resolve()
    store = MemoryStore.open(repo / ".repomemory")
    graph = store.load_graph_json()
    nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
    edges = graph.get("edges", []) if isinstance(graph, dict) else []
    graph_html = "".join(
        f"<div class='node'><code>{html.escape(str(e.get('src') if isinstance(e, dict) else e[0] if len(e) > 0 else '?'))}</code>"
        f" → <code>{html.escape(str(e.get('dst') if isinstance(e, dict) else e[1] if len(e) > 1 else '?'))}</code></div>"
        for e in edges[:500])
    cards_html = []
    for rel in store.list_files()[:500]:
        card = store.get_card(rel)
        hist = card.get("history", []) or []
        risks = [n for n in hist if isinstance(n, dict) and n.get("risk_flag")]
        cards_html.append(
            f"<div class='card'><h3><code>{html.escape(rel)}</code></h3>"
            f"<div>dependents: {html.escape(', '.join(card.get('dependents', [])[:10])) or '—'}</div>"
            f"<div>history notes: {len(hist)}</div>"
            + "".join(f"<div class='risk'>RISK {html.escape(str((n.get('risk_flag') or {}).get('text', n.get('risk_flag')) if isinstance(n.get('risk_flag'), dict) else n.get('risk_flag'))[:200])} <small>({html.escape(str(n.get('sha', ''))[:7])})</small></div>" for n in risks[:5])
            + "</div>")
    out = repo / ".repomemory" / "viewer" / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_render(len(nodes), len(edges), len(store.list_files()),
                           graph_html or "<p>empty (run init)</p>",
                           "".join(cards_html) or "<p>no files</p>"),
                   encoding="utf-8")
    return out


__all__ = ["build_viewer"]
