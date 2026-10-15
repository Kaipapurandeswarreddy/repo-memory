"""C4 Dependency Graph Builder — deterministic, networkx optional.

Convention: edge [src, dst, "IMPORTS"] means src depends-on dst
(src imports dst). Edge [src, dst, "CALLS"] means src *calls into* dst:
src imports dst and calls a name dst defines (best-effort static
resolution — dynamic calls are skipped, never guessed).
Dependents of F = nodes with a path -> F.
Dependencies of F = nodes reachable from F.

Public API works on plain serializable dicts so the store never
depends on networkx:
  graph_data = {"nodes": [...], "edges": [[src, dst, kind], ...]}
"""

from __future__ import annotations

from collections import defaultdict, deque


def _mod_candidates(mod: str) -> list[str]:
    """Map an imported dotted name to candidate repo-relative file paths."""
    mod = mod.strip()
    if not mod:
        return []
    parts = mod.split(".")
    cands = []
    # foo.bar -> foo/bar.py, foo.py, bar.py
    cands.append("/".join(parts) + ".py")
    cands.append("/".join(parts) + "/__init__.py")
    cands.append(parts[0] + ".py")
    cands.append(parts[-1] + ".py")
    # relative imports ("...x") — strip leading dots
    stripped = mod.lstrip(".")
    if stripped and stripped != mod:
        cands.append(stripped.replace(".", "/") + ".py")
    return cands


def build_graph_data(profiles: dict[str, dict]) -> dict:
    """Build serializable graph data from {relpath: profile}."""
    nodes = sorted(profiles.keys())
    by_stem = {}
    for n in nodes:
        stem = n[:-3] if n.endswith(".py") else n  # pkg/mod
        by_stem[stem] = n
        by_stem[stem.split("/")[-1]] = n  # basename without ext
    # index lookup: relpath -> node
    known = set(nodes)
    edges: list[list] = []
    seen: set[tuple] = set()
    for src in nodes:
        for mod in (profiles[src].get("imports") or []):
            dst = None
            for cand in _mod_candidates(str(mod)):
                if cand in known:
                    dst = cand
                    break
                # also try basename match
                base = cand.split("/")[-1]
                if base in known:
                    dst = base
                    break
            if dst is None:
                # basename fallback via by_stem
                key = str(mod).split(".")[-1]
                if key in by_stem and by_stem[key] != src:
                    dst = by_stem[key]
            if dst and dst != src and (src, dst) not in seen:
                seen.add((src, dst))
                edges.append([src, dst, "IMPORTS"])
    # -- CALLS edges: src calls a name dst defines (and src imports dst) ----
    defs: dict[str, set[str]] = {}
    for n, prof in profiles.items():
        names: set[str] = set()
        for f in prof.get("functions") or []:
            if isinstance(f, dict) and f.get("name"):
                names.add(f["name"])
        for c in prof.get("classes") or []:
            if isinstance(c, dict) and c.get("name"):
                names.add(c["name"])
                for m in c.get("methods") or []:
                    if isinstance(m, str):
                        names.add(f"{c['name']}.{m}")
                        names.add(m)
        defs[n] = names
    imported_by: dict[str, set[str]] = defaultdict(set)
    for s, d, k in edges:
        if k == "IMPORTS":
            imported_by[s].add(d)
    for src in nodes:
        targets = imported_by.get(src, set())
        if not targets:
            continue
        for call in profiles[src].get("calls") or []:
            if not isinstance(call, dict) or not call.get("name"):
                continue
            callee = str(call["name"])
            # Match the trailing segment (``ride.delete_ride_byid`` calls
            # ``delete_ride_byid``; bare ``foo()`` calls ``foo``) against
            # each imported file's definitions. Best-effort: a name defined
            # in two imported files links the first alphabetically, and
            # dynamic calls were never recorded upstream.
            short = callee.split(".")[-1]
            hit = None
            for dst in sorted(targets):
                if short in defs.get(dst, set()):
                    hit = dst
                    break
            if hit and hit != src and (src, hit, "CALLS") not in seen:
                seen.add((src, hit, "CALLS"))
                edges.append([src, hit, "CALLS"])
    return {"nodes": nodes, "edges": edges}


def _adj(graph_data: dict) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return (forward: src->deps, reverse: dst->dependents)."""
    fwd: dict[str, set[str]] = defaultdict(set)
    rev: dict[str, set[str]] = defaultdict(set)
    for n in graph_data.get("nodes", []):
        fwd.setdefault(n, set())
        rev.setdefault(n, set())
    for e in graph_data.get("edges", []):
        if len(e) < 2:
            continue
        s, d = e[0], e[1]
        fwd.setdefault(s, set()).add(d)
        rev.setdefault(d, set()).add(s)
        fwd.setdefault(d, set())
        rev.setdefault(s, set())
    return fwd, rev


def dependencies(graph_data: dict, rel: str) -> list[str]:
    fwd, _ = _adj(graph_data)
    return sorted(fwd.get(rel, set()))


def dependents(graph_data: dict, rel: str) -> list[str]:
    _, rev = _adj(graph_data)
    return sorted(rev.get(rel, set()))


def blast_radius(graph_data: dict, rel: str) -> list[str]:
    """Transitive dependents of rel (files that would be affected)."""
    _, rev = _adj(graph_data)
    seen = {rel}
    q = deque([rel])
    out: set[str] = set()
    while q:
        cur = q.popleft()
        for dep in rev.get(cur, set()):
            if dep not in seen:
                seen.add(dep)
                out.add(dep)
                q.append(dep)
    return sorted(out)


def one_hop(graph_data: dict, rel: str) -> list[str]:
    """Target + 1-hop dependents + 1-hop dependencies."""
    fwd, rev = _adj(graph_data)
    s = {rel} | fwd.get(rel, set()) | rev.get(rel, set())
    return sorted(s)


def function_callers(graph_data: dict, profiles: dict[str, dict],
                     file: str, func: str) -> list[dict]:
    """Call sites invoking ``func`` defined in ``file``.

    Returns ``[{file, line, in}]`` sorted by file then line. A call site
    counts when the caller imports ``file`` (or a CALLS edge exists) and
    calls the bare ``func`` name. Best-effort static data: dynamic calls
    are invisible by construction.
    """
    called: set[str] = set()
    for e in graph_data.get("edges", []) or []:
        if len(e) >= 3 and e[2] == "CALLS" and e[1] == file:
            called.add(e[0])
    out: list[dict] = []
    for src in sorted(called):
        for call in (profiles.get(src, {}).get("calls") or []):
            if not isinstance(call, dict) or not call.get("name"):
                continue
            if str(call["name"]).split(".")[-1] != func:
                continue
            out.append({"file": src, "line": call.get("lineno"),
                        "in": call.get("in", "<module>")})
    out.sort(key=lambda d: (d["file"], d.get("line") or 0))
    return out


# -- cycles: networkx if present, else Tarjan SCC ------------------------

def find_cycles(graph_data: dict) -> list[list[str]]:
    """Return list of cycles (each a sorted SCC with >1 node or self-loop)."""
    try:
        import networkx as nx  # type: ignore

        g = nx.DiGraph()
        g.add_nodes_from(graph_data.get("nodes", []))
        for e in graph_data.get("edges", []):
            if len(e) >= 2:
                g.add_edge(e[0], e[1])
        return [sorted(c) for c in nx.simple_cycles(g) if c]
    except ImportError:
        return _tarjan_scc_cycles(graph_data)


def _tarjan_scc_cycles(graph_data: dict) -> list[list[str]]:
    fwd, _ = _adj(graph_data)
    index_counter = [0]
    stack: list[str] = []
    low: dict[str, int] = {}
    idx: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    sccs: list[list[str]] = []

    def strongconnect(v: str) -> None:
        idx[v] = low[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack[v] = True
        for w in fwd.get(v, ()):
            if w not in idx:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif on_stack.get(w):
                low[v] = min(low[v], idx[w])
        if low[v] == idx[v]:
            comp = []
            while True:
                w = stack.pop()
                on_stack[w] = False
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                sccs.append(sorted(comp))
            elif v in fwd.get(v, ()):  # self-loop
                sccs.append([v])

    for v in list(fwd.keys()):
        if v not in idx:
            strongconnect(v)
    return sccs


def to_networkx(graph_data: dict):
    """Optional helper; raises ImportError if networkx missing."""
    import networkx as nx  # type: ignore

    g = nx.DiGraph()
    g.add_nodes_from(graph_data.get("nodes", []))
    for e in graph_data.get("edges", []):
        if len(e) >= 3:
            g.add_edge(e[0], e[1], kind=e[2])
        elif len(e) >= 2:
            g.add_edge(e[0], e[1])
    return g
