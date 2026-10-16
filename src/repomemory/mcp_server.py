"""MCP server (JSON-RPC 2.0 over stdio, stdlib only).

Tools: recall, card, search, risk, verify(check patch), stats, export.
Run: python -m repomemory.mcp_server [--repo ROOT]
Wire: {"mcpServers": {"repomemory": {"command": "python", "args": ["-m", "repomemory.mcp_server", "--repo", "/path"]}}}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .store import MemoryStore
from .verifier import check_patch


def _store(repo: Path) -> MemoryStore:
    return MemoryStore.open(repo / ".repomemory")


TOOLS = {
    "recall": {
        "description": "Single recall() across files (query, max_items, max_chars, template)",
        "inputSchema": {"type": "object",
                        "properties": {"query": {"type": "string"},
                                       "max_items": {"type": "integer"},
                                       "max_chars": {"type": "integer"},
                                       "template": {"type": "string"}},
                        "required": ["query"]},
    },
    "card": {
        "description": "MemoryCard JSON for a file",
        "inputSchema": {"type": "object",
                        "properties": {"file": {"type": "string"}},
                        "required": ["file"]},
    },
    "search": {
        "description": "BM25 file ranking for a query",
        "inputSchema": {"type": "object",
                        "properties": {"query": {"type": "string"},
                                       "limit": {"type": "integer"}},
                        "required": ["query"]},
    },
    "risk": {
        "description": "Risk flags + dependents for a file",
        "inputSchema": {"type": "object",
                        "properties": {"file": {"type": "string"}},
                        "required": ["file"]},
    },
    "verify": {
        "description": "Deterministic patch check (patch diff text)",
        "inputSchema": {"type": "object",
                        "properties": {"patch": {"type": "string"},
                                       "file": {"type": "string"}},
                        "required": ["patch"]},
    },
    "stats": {
        "description": "Store statistics",
        "inputSchema": {"type": "object", "properties": {}},
    },
}


def call_tool(repo: Path, name: str, args: dict) -> object:
    store = _store(repo)
    if name == "recall":
        return store.recall(str(args.get("query", "")),
                            int(args.get("max_items", 20)),
                            int(args.get("max_chars", 8000)),
                            str(args.get("template", "technical")))
    if name == "card":
        return store.get_card(str(args.get("file", "")))
    if name == "search":
        return store.search_bm25(str(args.get("query", "")), int(args.get("limit", 10)))
    if name == "risk":
        card = store.get_card(str(args.get("file", "")))
        risks = [n for n in card.get("history", []) if isinstance(n, dict) and n.get("risk_flag")]
        return {"file": card["file"], "risks": risks,
                "dependents": card.get("dependents", []),
                "blast_radius_note": "use scan for transitive set"}
    if name == "verify":
        patch = str(args.get("patch", ""))
        files = [str(args.get("file", ""))] if args.get("file") else store.list_files()[:20]
        cards = {f: store.get_card(f) for f in files}
        out = []
        for rel in sorted(cards):
            out.extend(check_patch(patch, cards[rel],
                                   [c for k, c in cards.items() if k != rel]))
        seen = set()
        uniq = []
        for f in out:
            key = (f.get("file"), f.get("type"), f.get("message"))
            if key not in seen:
                seen.add(key)
                uniq.append(f)
        return uniq
    if name == "stats":
        data = store.stats()
        data.update(store.verify())
        return data
    if name == "export":
        return store.export()
    raise ValueError(f"unknown tool: {name}")


def _repo_from_root_uri(root_uri: object) -> Path | None:
    """Resolve the project repo from MCP initialize ``rootUri``.

    The host tells us the workspace it opened (e.g. ``file:///D:/Ambigo/server``).
    Binding to it beats guessing from the child's working directory — which is
    undefined when a host spawns duplicate children. Returns None when the
    URI is missing or does not look like a local project root.
    """
    if not isinstance(root_uri, str) or not root_uri.startswith("file://"):
        return None
    path = root_uri[len("file://"):]
    # file:///D:/x -> D:/x ; file://host/D:/x (UNC-ish) -> //host/D:/x
    if path.startswith("/") and len(path) > 2 and path[2] == ":":
        path = path[1:]
    try:
        repo = Path(path)
    except Exception:
        return None
    if not repo.is_absolute():
        return None
    return repo


def _respond(req_id, result=None, error=None) -> None:
    msg: dict = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        msg["error"] = {"code": -32603, "message": str(error)}
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _iter_messages():
    """Yield JSON-RPC messages from stdin.

    Handles both newline-delimited JSON and Content-Length framing
    (both are seen in the wild across MCP hosts).
    """
    buf = sys.stdin.buffer
    while True:
        line = buf.readline()
        if not line:
            return
        try:
            text = line.decode("utf-8", errors="replace").strip()
        except Exception:
            continue
        if not text:
            continue
        if text.lower().startswith("content-length:"):
            try:
                length = int(text.split(":", 1)[1].strip())
            except ValueError:
                continue
            # consume remaining header lines until blank
            while True:
                h = buf.readline()
                if not h or h.strip() in (b"",):
                    break
            body = b""
            while len(body) < length:
                chunk = buf.read(length - len(body))
                if not chunk:
                    break
                body += chunk
            try:
                yield json.loads(body.decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, ValueError):
                continue
        elif text.startswith("{"):
            try:
                yield json.loads(text)
            except json.JSONDecodeError:
                continue
        # ignore any other framing noise (never respond to it)


def _debug_log(repo: Path, event: dict) -> None:
    """Append one JSON line to .repomemory/mcp-debug.log (cap 200 lines).

    The only channel that survives a broken host route: the assistant can
    read this file with its own file tools and see exactly what the host
    sent (rootUri present? which cwd? which repo got bound?).
    """
    import time
    try:
        log = repo / ".repomemory" / "mcp-debug.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        event = {"ts": time.time(), **event}
        lines: list[str] = []
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            pass
        lines.append(json.dumps(event))
        log.write_text("\n".join(lines[-200:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def serve(repo: Path) -> int:
    bound = {"repo": repo}
    # Pre-warm in background: the walk must NEVER delay initialize/tools-list
    # past the host's fetch timeout (default 5s). First real query may walk,
    # but the handshake is always instant.
    import threading

    def _prime() -> None:
        try:
            MemoryStore.open(bound["repo"] / ".repomemory").prime()
        except Exception:
            pass

    threading.Thread(target=_prime, daemon=True).start()
    _debug_log(bound["repo"], {"event": "startup", "pid": os.getpid(),
                               "cwd": os.getcwd(), "bound": str(bound["repo"])})
    for req in _iter_messages():
        if not isinstance(req, dict):
            continue
        method = req.get("method", "")
        params = req.get("params", {}) or {}
        # notifications carry no id and must NOT get a response
        if "id" not in req or method.startswith("notifications/"):
            continue
        req_id = req.get("id")
        try:
            if method == "initialize":
                # echo the client's protocol version (strict hosts drop mismatches)
                client_ver = (params.get("protocolVersion")
                              if isinstance(params, dict) else None)
                # bind to the workspace the host actually opened: with duplicate
                # children in unknown cwds, rootUri is the only reliable repo.
                if isinstance(params, dict):
                    rooted = _repo_from_root_uri(params.get("rootUri"))
                    if rooted is not None:
                        bound["repo"] = rooted
                import os as _os
                _debug_log(bound["repo"], {
                    "event": "initialize",
                    "pid": _os.getpid(),
                    "cwd": os.getcwd(),
                    "rootUri": (params.get("rootUri")
                                if isinstance(params, dict) else None),
                    "bound": str(bound["repo"]),
                })
                _respond(req_id, {"protocolVersion": client_ver or "2024-11-05",
                                  "capabilities": {"tools": {}, "roots": {}},
                                  "serverInfo": {"name": "repomemory", "version": "0.1.0"}})
            elif method == "tools/list":
                import time as _t
                _t0 = _t.perf_counter()
                _respond(req_id, {"tools": [{"name": k, "description": v["description"],
                                             "inputSchema": v["inputSchema"]}
                                            for k, v in TOOLS.items()]})
                _debug_log(bound["repo"], {
                    "event": "tools/list", "latency_ms": round((_t.perf_counter() - _t0) * 1000, 1),
                    "bound": str(bound["repo"]),
                })
            elif method == "tools/call":
                import time as _t
                _t0 = _t.perf_counter()
                _respond(req_id, call_tool(bound["repo"], params.get("name", ""),
                                           params.get("arguments", {}) or {}))
                _debug_log(bound["repo"], {
                    "event": "tools/call",
                    "tool": params.get("name", "?"),
                    "latency_ms": round((_t.perf_counter() - _t0) * 1000, 1),
                    "bound": str(bound["repo"]),
                })
            elif method in ("shutdown", "exit"):
                _debug_log(bound["repo"], {"event": method, "bound": str(bound["repo"])})
                _respond(req_id, "ok")
                return 0
            elif method == "ping":
                _respond(req_id, {})
            else:
                _respond(req_id, error=f"unknown method: {method}")
        except Exception as e:  # never kill the stdio loop
            _respond(req_id, error=e)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--repo", default=".")
    args = p.parse_args(argv)
    return serve(Path(args.repo).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
