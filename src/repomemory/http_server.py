"""HTTP API (stdlib http.server, no deps).

GET  /health -> {status: ok}
GET  /tools  -> tool list
POST /tool/<name> (JSON body) -> result
Run: python -m repomemory.http_server [--repo ROOT] [--port 3210]
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .mcp_server import TOOLS, call_tool


class Handler(BaseHTTPRequestHandler):
    repo: Path = Path(".")

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._json({"status": "ok"})
        elif path == "/tools":
            self._json({"tools": [{"name": k, "description": v["description"]}
                                   for k, v in TOOLS.items()]})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not path.startswith("/tool/"):
            self._json({"error": "not found"}, 404)
            return
        name = path[len("/tool/"):]
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        try:
            args = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError as e:
            self._json({"error": f"bad json: {e}"}, 400)
            return
        try:
            self._json(call_tool(self.repo, name, args if isinstance(args, dict) else {}))
        except ValueError as e:
            self._json({"error": str(e)}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 500)

    def log_message(self, *args) -> None:  # quiet
        pass


def serve(repo: Path, port: int = 3210) -> int:
    from .store import MemoryStore as _MS
    _MS.open(repo / ".repomemory").prime()  # walk once at boot
    Handler.repo = repo
    httpd = HTTPServer(("127.0.0.1", port), Handler)
    print(f"repomemory http on http://127.0.0.1:{port} repo={repo}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--repo", default=".")
    p.add_argument("--port", type=int, default=3210)
    args = p.parse_args(argv)
    return serve(Path(args.repo).resolve(), args.port)


if __name__ == "__main__":
    raise SystemExit(main())
