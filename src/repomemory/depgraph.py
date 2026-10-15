"""C4 Dependency Graph Builder (deterministic, networkx).

Builds a directed graph from C1 :mod:`repomemory.profiler` profiles:

* one ``type="file"`` node per profile key (repo-relative ``.py`` path);
* one ``IMPORTS`` edge (``weight=0.7``) from importer to dependency for each
  ``import x`` / ``from x import ...`` entry that resolves to a profiled
  file — absolute and relative imports are both resolved, trying progressively
  shorter dotted prefixes so ``from pkg.mod import func`` finds
  ``pkg/mod.py``;
* one ``CONTAINS`` edge (``weight=1.0``) from each file node to its
  function/class member nodes (``"<file>::<name>"``).

Edge direction is importer → dependency, so a file's *dependents* are its
graph ancestors and its *dependencies* its successors. :func:`blast_radius`
therefore returns transitive dependents (sorted); it is implemented with
:func:`networkx.descendants` over the reversed graph.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import networkx as nx

__all__ = [
    "IMPORT_WEIGHT",
    "CONTAINS_WEIGHT",
    "build_graph",
    "blast_radius",
    "detect_cycles",
    "to_json",
    "from_json",
    "save_graph",
    "load_graph",
]

#: Edge weight for ``IMPORTS`` (file depends on file); cf. SPIKES.md §1D.
IMPORT_WEIGHT = 0.7
#: Edge weight for ``CONTAINS`` (file contains member); cf. SPIKES.md §1D.
CONTAINS_WEIGHT = 1.0


def _resolve_import(importer: str, imp: str, file_set: set[str]) -> str | None:
    """Resolve one raw import string to a repo-relative ``.py`` path.

    Returns :data:`None` when the import does not map onto a profiled file
    (stdlib, third-party, or missing).
    """
    if not imp:
        return None
    stripped = imp.strip()
    if not stripped:
        return None
    level = len(stripped) - len(stripped.lstrip("."))
    rest = stripped.lstrip(".")

    def _first(candidates: list[str]) -> str | None:
        for cand in candidates:
            if cand in file_set:
                return cand
        return None

    def _prefix_hits(parts: list[str], minimum: int) -> str | None:
        for i in range(len(parts), minimum - 1, -1):
            prefix = parts[:i]
            if not prefix:
                continue
            hit = _first(["/".join(prefix) + ".py", "/".join(prefix) + "/__init__.py"])
            if hit is not None:
                return hit
        return None

    if level:
        # Relative import: anchor at the importer's package.
        directory = importer.rpartition("/")[0]
        base = directory.split("/") if directory else []
        if len(base) < level - 1:
            return None  # escapes the repository top
        base = base[: len(base) - (level - 1)] if level > 1 else base
        if not rest:
            # ``from . import x`` with a bare "." entry: the package itself.
            if not base:
                return None
            return _first(["/".join(base) + "/__init__.py"])
        full = base + rest.split(".")
        # Longest prefix first; may fall back to the package __init__ when
        # the imported name lives there (``from . import helper``).
        return _prefix_hits(full, len(base))
    return _prefix_hits(rest.split("."), 1)


def build_graph(profiles: dict[str, dict]) -> nx.DiGraph:
    """Build the dependency digraph from ``{relpath: profile}``.

    Args:
        profiles: Mapping of repo-relative ``.py`` path to a profiler
            profile dict (only ``"imports"``, ``"functions"`` and
            ``"classes"`` are read; missing keys are tolerated).

    Returns:
        A :class:`networkx.DiGraph` with file nodes plus ``IMPORTS`` and
        ``CONTAINS`` edges as described in the module docstring.
    """
    graph = nx.DiGraph()
    for rel in profiles:
        graph.add_node(rel, type="file")
    file_set = set(profiles)
    for src, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        for imp in profile.get("imports") or []:
            dst = _resolve_import(src, str(imp), file_set)
            if dst is not None and dst in file_set and dst != src:
                if graph.has_edge(src, dst):
                    data = graph[src][dst]
                    data["kind"] = "IMPORTS"
                    data["weight"] = IMPORT_WEIGHT
                else:
                    graph.add_edge(src, dst, kind="IMPORTS", weight=IMPORT_WEIGHT)
        for kind, members in (
            ("function", profile.get("functions") or []),
            ("class", profile.get("classes") or []),
        ):
            for member in members:
                name = member.get("name") if isinstance(member, dict) else None
                if not name:
                    continue
                node_id = f"{src}::{name}"
                if not graph.has_node(node_id):
                    graph.add_node(node_id, type=kind, file=src)
                if not graph.has_edge(src, node_id):
                    graph.add_edge(src, node_id, kind="CONTAINS", weight=CONTAINS_WEIGHT)
    return graph


def _is_file_node(graph: nx.DiGraph, node: str) -> bool:
    """True for file nodes (missing ``type`` attr counts as file)."""
    try:
        return graph.nodes[node].get("type", "file") == "file"
    except KeyError:
        return False


def blast_radius(graph: nx.DiGraph, file: str) -> list[str]:
    """Return the sorted transitive *dependents* of ``file``.

    With ``IMPORTS`` edges oriented importer → dependency, dependents are
    the ancestors of ``file`` — equivalently the descendants of ``file``
    on the reversed graph. Member (``CONTAINS``) nodes are excluded, so
    only file paths are returned. Unknown files yield ``[]``.
    """
    if file not in graph:
        return []
    reversed_view = graph.reverse(copy=False)
    try:
        reachable = nx.descendants(reversed_view, file)
    except nx.NetworkXError:
        return []
    return sorted(n for n in reachable if _is_file_node(graph, n))


def detect_cycles(graph: nx.DiGraph) -> list[list[str]]:
    """Return import cycles between file nodes.

    Each cycle is normalised to start with its smallest member and the
    outer list is sorted, so output is deterministic. Member nodes can
    never be part of a cycle and are ignored.
    """
    file_nodes = [n for n in graph.nodes if _is_file_node(graph, n)]
    subgraph = graph.subgraph(file_nodes)
    cycles: list[list[str]] = []
    for cycle in nx.simple_cycles(subgraph):
        if not cycle:
            continue
        start = min(range(len(cycle)), key=lambda k: cycle[k])
        cycles.append(cycle[start:] + cycle[:start])
    cycles.sort()
    return cycles


def to_json(graph: nx.DiGraph) -> dict:
    """Serialise ``graph`` to a JSON-compatible dict."""
    return {
        "nodes": [
            {"id": node, **dict(graph.nodes[node])} for node in sorted(graph.nodes)
        ],
        "edges": [
            {"src": src, "dst": dst, **dict(graph[src][dst])}
            for src, dst in sorted(graph.edges)
        ],
    }


def from_json(data: dict) -> nx.DiGraph:
    """Rebuild a digraph from :func:`to_json` output.

    Also accepts the legacy compact shape ``{"nodes": [id, ...], "edges":
    [[src, dst, kind?], ...]}`` so graphs persisted by older tooling load.
    """
    graph = nx.DiGraph()
    for entry in data.get("nodes", []):
        if isinstance(entry, dict):
            node_id = entry.get("id")
            if node_id is None:
                continue
            attrs = {k: v for k, v in entry.items() if k != "id"}
            graph.add_node(node_id, **attrs)
        else:
            graph.add_node(entry, type="file")
    for entry in data.get("edges", []):
        if isinstance(entry, dict):
            src, dst = entry.get("src"), entry.get("dst")
            if src is None or dst is None:
                continue
            graph.add_edge(src, dst, **{k: v for k, v in entry.items() if k not in ("src", "dst")})
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            src, dst = entry[0], entry[1]
            attrs: dict = {}
            if len(entry) >= 3 and entry[2]:
                attrs["kind"] = entry[2]
            graph.add_edge(src, dst, **attrs)
    return graph


def save_graph(graph: nx.DiGraph, path: str | os.PathLike) -> None:
    """Persist ``graph`` as UTF-8 JSON, atomically (temp file + replace)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(target.parent), prefix=target.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(to_json(graph), handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_graph(path: str | os.PathLike) -> nx.DiGraph:
    """Load a graph previously written by :func:`save_graph`."""
    with open(path, encoding="utf-8") as handle:
        return from_json(json.load(handle))
