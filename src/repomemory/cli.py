"""RepoMemory CLI — `repomem` (argparse, stdlib only).

Commands:
  repomem init <target> [--max-commits N] [--distill-llm]
  repomem card <file> [--repo ROOT]
  repomem scan [--repo ROOT] [--file F]
  repomem check <patch.diff> [--repo ROOT]
  repomem gen "<task>" --target <file> [--repo ROOT] [--mode stub|openai|gemini|groq|ollama]
  repomem update [--repo ROOT]
  repomem recall <query> [--max-items N] [--max-chars N] [--template NAME]
  repomem export <file.json> | repomem import <file.json> [--skip-existing]
  repomem snapshot [create <label>|list|restore <id>]
  repomem verify | repomem viewer | repomem hook [install|status|uninstall]
  repomem serve [--port N] | repomem eval [audit|run|protocol] --tasks tasks.json

Store location: <repo>/.repomemory (default repo = cwd).
Exit codes: 0 ok, 1 usage/data error, 2 check found high-severity warnings.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import graph_builder as _gb
from . import profiler as _prof
from . import verifier as _ver
from .llm import LLMGateway
from .orchestrator import Orchestrator
from .store import MemoryStore

try:  # richer miner when present (Group B), else Group C minimal miner
    from . import history_miner as _miner_new
except ImportError:  # pragma: no cover
    _miner_new = None
try:
    from . import miner as _miner_old
except ImportError:  # pragma: no cover
    _miner_old = None


def _resolve_repo(explicit: str | None) -> Path:
    return Path(explicit).resolve() if explicit else Path.cwd().resolve()


def _store_for(repo: Path) -> MemoryStore:
    return MemoryStore.open(repo / ".repomemory")


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


def _store_location(store) -> str:
    for attr in ("store_dir", "root"):
        v = getattr(store, attr, None)
        if v is not None:
            return str(v)
    return "?"


def _mine_one(target: Path, rel: str, max_commits: int) -> list[dict]:
    if _miner_new is not None:
        try:
            return _miner_new.mine_file_history(target, rel, max_commits=max_commits) or []
        except TypeError:
            pass
        except Exception:
            return []
    if _miner_old is not None:
        try:
            return _miner_old.mine_file_history(
                target, rel, max_commits_per_file=max_commits) or []
        except TypeError:
            try:
                return _miner_old.mine_file_history(target, rel, max_commits) or []
            except Exception:
                return []
        except Exception:
            return []
    return []


def _is_high(w: dict) -> bool:
    return str(w.get("severity", w.get("level", ""))).lower() == "high"


def _parse_files(patch: str) -> list[str]:
    parse = getattr(_ver, "parse_unified_diff", None) or getattr(_ver, "_parse_patch", None)
    if parse is None:
        return []
    try:
        return list(parse(patch or "").keys())
    except Exception:
        return []


# -- commands ---------------------------------------------------------------

def cmd_init(args) -> int:
    target = Path(args.target).resolve()
    if not target.exists():
        print(f"error: target not found: {target}", file=sys.stderr)
        return 1
    repo = _resolve_repo(getattr(args, "repo", None) or str(target))
    store = _store_for(repo)
    max_commits = int(getattr(args, "max_commits", 20) or 20)

    profile_repo = getattr(_prof, "profile_repo", None)
    if profile_repo is None:  # pragma: no cover
        print("error: profiler.profile_repo missing", file=sys.stderr)
        return 1
    profiles = profile_repo(target)
    for rel, prof in profiles.items():
        store.save_profile(rel, prof)
    graph_data = _gb.build_graph_data(profiles)
    store.save_graph(graph_data)
    # keep memory store out of the target's own commits
    try:
        gi = target / ".gitignore"
        existing = gi.read_text(encoding="utf-8", errors="replace") if gi.exists() else ""
        if ".repomemory/" not in existing:
            with open(gi, "a", encoding="utf-8") as fh:
                if existing and not existing.endswith("\n"):
                    fh.write("\n")
                fh.write(".repomemory/\n")
    except OSError:
        pass

    # mine + distill history (capped for speed); skip gracefully when not a git repo.
    # _mine_one returns raw miner commits; distill to HistoryNotes + decisions
    # (rule-based by default; --distill-llm uses OPENAI_API_KEY when set).
    from .distiller import distill_file, distill_with_llm
    use_llm = bool(getattr(args, "distill_llm", False))
    gateway = None
    if use_llm:
        import os as _os
        from .llm import LLMGateway as _GW
        if _os.environ.get("GROQ_API_KEY"):
            gateway = _GW(mode="groq")
        elif _os.environ.get("OLLAMA_API_KEY"):
            gateway = _GW(mode="ollama")
        elif _os.environ.get("GEMINI_API_KEY") or _os.environ.get("GOOGLE_API_KEY"):
            gateway = _GW(mode="gemini")
        elif _os.environ.get("OPENAI_API_KEY"):
            gateway = _GW(mode="openai")
        else:
            print("warning: --distill-llm without any LLM key, using rule-based", file=sys.stderr)
    mined = 0
    for rel in sorted(profiles):
        try:
            raw = _mine_one(target, rel, max_commits)
        except Exception:
            raw = []
        if not raw:
            continue
        try:
            if gateway is not None:
                notes = [distill_with_llm(c, gateway) for c in raw]
                decisions: list = []
                seen: set = set()
                import re as _re
                for c in raw:
                    for sent in _re.split(r"(?<=[.!?])\s+|\n+", str(c.get("msg", ""))):
                        s = " ".join(sent.split()).strip(" -:;")
                        if len(s.split()) >= 4 and _re.search(r"\b(never|always|must)\b", s, _re.I):
                            if s.lower() not in seen:
                                seen.add(s.lower())
                                decisions.append(s)
            else:
                notes, decisions = distill_file(raw)
            # keep miner author/date/files on each note for provenance
            for note, commit in zip(notes, raw):
                note.setdefault("author", commit.get("author", ""))
                note.setdefault("date", commit.get("date", ""))
                note.setdefault("files", commit.get("files", [rel]))
            store.save_history(rel, notes, decisions)
            mined += 1
        except Exception:
            continue
    print(f"init: {len(profiles)} profiles, "
          f"{len(graph_data.get('edges', []))} edges, "
          f"{mined} files with history -> {_store_location(store)}")
    try:
        cycles = _gb.find_cycles(graph_data)
    except Exception:
        cycles = []
    if cycles:
        print(f"note: {len(cycles)} cycle(s) detected (see `repomem scan`)")
    return 0


def cmd_card(args) -> int:
    repo = _resolve_repo(getattr(args, "repo", None))
    store = _store_for(repo)
    try:
        card = store.get_card(args.file, _load_graph(store))
    except TypeError:
        card = store.get_card(args.file)
    if store.load_profile(args.file) is None:
        print(f"warning: no profile for {args.file!r} (uninitialised? run init)", file=sys.stderr)
    print(json.dumps(card, indent=2))
    return 0


def cmd_scan(args) -> int:
    repo = _resolve_repo(getattr(args, "repo", None))
    store = _store_for(repo)
    graph_data = _load_graph(store)
    if not graph_data.get("nodes"):
        print("empty graph (run `repomem init` first)", file=sys.stderr)
        return 1
    cycles = _gb.find_cycles(graph_data)
    print(f"nodes={len(graph_data.get('nodes', []))} "
          f"edges={len(graph_data.get('edges', []))}")
    if cycles:
        print(f"cycles ({len(cycles)}):")
        for c in cycles[:20]:
            print("  - " + " -> ".join(c))
    else:
        print("cycles: none")
    # function-level callers: --callers FILE::FUNC (who calls this function)
    if getattr(args, "callers", None):
        spec = str(args.callers)
        if "::" not in spec:
            print("error: --callers needs FILE::FUNC, e.g. payments.py::process_refund",
                  file=sys.stderr)
            return 1
        cfile, _, cfunc = spec.partition("::")
        profiles = {}
        for n in graph_data.get("nodes", []):
            p = store.load_profile(n)
            if p:
                profiles[n] = p
        sites = _gb.function_callers(graph_data, profiles, cfile, cfunc)
        if not sites:
            print(f"no recorded call sites for {cfile}::{cfunc}")
        for s in sites[:30]:
            print(f"  - {s['file']}:{s.get('line', '?')} (in {s.get('in', '<module>')})")
        return 0
    # blast radius: specific file or top-5 by dependent count
    if getattr(args, "file", None):
        br = _gb.blast_radius(graph_data, args.file)
        print(f"blast_radius({args.file}) = {len(br)}: {', '.join(br[:20])}")
    else:
        nodes = [n if isinstance(n, str) else str(n.get("id", "?"))
                 for n in graph_data.get("nodes", [])]
        rev: dict[str, int] = {n: 0 for n in nodes}
        for e in graph_data.get("edges", []):
            dst = e.get("dst") if isinstance(e, dict) else (e[1] if len(e) >= 2 else None)
            if dst in rev:
                rev[dst] += 1
        top = sorted(rev.items(), key=lambda kv: kv[1], reverse=True)[:5]
        print("highest fan-in:")
        for n, c in top:
            br = _gb.blast_radius(graph_data, n)
            print(f"  {n}: fan_in={c} blast_radius={len(br)}")
    return 0


def cmd_check(args) -> int:
    repo = _resolve_repo(getattr(args, "repo", None))
    store = _store_for(repo)
    try:
        patch_bytes = Path(args.patch).read_bytes()
    except OSError as e:
        print(f"error: cannot read patch: {e}", file=sys.stderr)
        return 1
    from .verifier import _decode_diff as _dec
    patch = _dec(patch_bytes)
    graph_data = _load_graph(store)
    files = _parse_files(patch) or store.list_files()
    cards = {}
    for f in files:
        try:
            cards[f] = store.get_card(f, graph_data)
        except TypeError:
            cards[f] = store.get_card(f)
    orch = Orchestrator(store, LLMGateway(mode="stub"), repo_root=repo)
    warnings = orch.cross_check(patch, cards)
    print(_ver.format_report(warnings), end="" if str(_ver.format_report(warnings)).endswith("\n") else "\n")
    if any(_is_high(w) for w in warnings):
        return 2
    return 0


def cmd_gen(args) -> int:
    repo = _resolve_repo(getattr(args, "repo", None))
    store = _store_for(repo)
    if not args.target:
        print("error: gen requires --target <file>", file=sys.stderr)
        return 1
    mode = getattr(args, "mode", "stub") or "stub"
    gw = LLMGateway(mode=mode)
    orch = Orchestrator(store, gw, repo_root=repo)
    result = orch.run_generate(args.task, args.target)
    print(result["patch"])
    print("--- warnings ---")
    report = _ver.format_report(result["warnings"])
    print(report, end="" if report.endswith("\n") else "\n")
    t = result["tokens"]
    print(f"--- tokens in={t['tokens_in']} out={t['tokens_out']} "
          f"latency_ms={t['latency_ms']} scope={len(result['scope'])} ---")
    if any(_is_high(w) for w in result["warnings"]):
        return 2
    return 0


# -- parser -------------------------------------------------------------------

def cmd_update(args) -> int:
    from . import updater as _up
    repo = _resolve_repo(getattr(args, "repo", None))
    store = _store_for(repo)
    summary = _up.incremental_update(store, repo)
    print(f"updated={len(summary['updated'])} contracts_changed={summary['contracts_changed']} "
          f"nodes={summary['graph_nodes']} edges={summary['graph_edges']}")
    if summary["contracts_changed"]:
        print("note: contract changed — run `repomem scan` / `check` on dependents")
    return 0


def cmd_recall(args) -> int:
    repo = _resolve_repo(getattr(args, "repo", None))
    store = _store_for(repo)
    res = store.recall(args.query, int(getattr(args, "max_items", 20) or 20),
                       int(getattr(args, "max_chars", 8000) or 8000),
                       str(getattr(args, "template", "technical") or "technical"))
    print(res["formatted"])
    return 0


def cmd_export(args) -> int:
    import json as _json
    repo = _resolve_repo(getattr(args, "repo", None))
    store = _store_for(repo)
    Path(args.file).write_text(_json.dumps(store.export(), indent=2), encoding="utf-8")
    print(f"exported -> {args.file}")
    return 0


def cmd_import(args) -> int:
    import json as _json
    repo = _resolve_repo(getattr(args, "repo", None))
    store = _store_for(repo)
    data = _json.loads(Path(args.file).read_text(encoding="utf-8"))
    rep = store.import_data(data, skip_existing=bool(getattr(args, "skip_existing", False)))
    print(f"imported={rep['imported']} skipped={rep['skipped']}")
    return 0


def cmd_snapshot(args) -> int:
    repo = _resolve_repo(getattr(args, "repo", None))
    store = _store_for(repo)
    if getattr(args, "snap_cmd", "create") == "list":
        for s in store.list_snapshots():
            print(s)
        return 0
    if getattr(args, "snap_cmd", "create") == "restore":
        ok = store.restore(args.label)
        print("restored" if ok else "snapshot not found")
        return 0 if ok else 1
    snap = store.snapshot(getattr(args, "label", "") or "")
    print(f"snapshot {snap['id']}")
    return 0


def cmd_verify(args) -> int:
    repo = _resolve_repo(getattr(args, "repo", None))
    store = _store_for(repo)
    res = store.verify()
    print(f"valid={res['valid']} files={res['files']}")
    for e in res["errors"][:20]:
        print(f"  - {e}")
    return 0 if res["valid"] else 1


def cmd_viewer(args) -> int:
    from .viewer import build_viewer
    repo = _resolve_repo(getattr(args, "repo", None))
    out = build_viewer(repo)
    print(f"viewer -> {out}")
    return 0


def cmd_hook(args) -> int:
    from . import hooks as _hooks
    repo = _resolve_repo(getattr(args, "repo", None))
    action = getattr(args, "hook_cmd", "status")
    if action == "install":
        print(f"installed -> {_hooks.install(repo)}")
    elif action == "uninstall":
        print("removed" if _hooks.uninstall(repo) else "no hook to remove")
    else:
        print("installed" if _hooks.status(repo) else "not installed")
    return 0


def cmd_serve(args) -> int:
    from .http_server import serve
    repo = _resolve_repo(getattr(args, "repo", None))
    return serve(repo, int(getattr(args, "port", 3210) or 3210))


def cmd_eval(args) -> int:
    import json as _json
    from .eval_harness import audit_tasks, human_protocol, run_regression_tasks
    repo = _resolve_repo(getattr(args, "repo", None))
    store = _store_for(repo)
    action = getattr(args, "eval_cmd", "audit")
    if action == "protocol":
        print(_json.dumps(human_protocol(), indent=2))
        return 0
    tasks = _json.loads(Path(args.tasks).read_text(encoding="utf-8"))
    if isinstance(tasks, dict) and "tasks" in tasks:
        tasks = tasks["tasks"]
    if action == "audit":
        res = audit_tasks(tasks)
        print(f"flagged={res['flagged']}/{res['total']}")
        for tid, reasons in res["flags"].items():
            print(f"  - {tid}: " + "; ".join(reasons))
        return 0
    res = run_regression_tasks(store, tasks)
    print(_json.dumps({k: v for k, v in res.items() if k != "details"}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="repomem", description="RepoMemory CLI")
    p.add_argument("--repo", default=None, help="target repo root (default: cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="cold start: profile + graph + history")
    pi.add_argument("target", help="repo directory to index")
    pi.add_argument("--max-commits", type=int, default=20,
                    help="max git commits mined per file (default 20)")
    pi.add_argument("--distill-llm", action="store_true",
                    help="distill with LLM (needs OPENAI_API_KEY, else rule-based)")
    pi.add_argument("--repo", default=argparse.SUPPRESS,
                    help="repo root for store (default: target)")
    pi.set_defaults(func=cmd_init)

    pc = sub.add_parser("card", help="print MemoryCard JSON for a file")
    pc.add_argument("file", help="repo-relative path, e.g. payments.py")
    pc.add_argument("--repo", default=argparse.SUPPRESS,
                    help="target repo root (default: cwd)")
    pc.set_defaults(func=cmd_card)

    ps = sub.add_parser("scan", help="W-D: cycles + blast-radius + callers")
    ps.add_argument("--file", default=None, help="blast radius for one file")
    ps.add_argument("--callers", default=None,
                    help="call sites for one function, FILE::FUNC")
    ps.add_argument("--repo", default=argparse.SUPPRESS,
                    help="target repo root (default: cwd)")
    ps.set_defaults(func=cmd_scan)

    pk = sub.add_parser("check", help="verify a unified diff against memory")
    pk.add_argument("patch", help="path to .diff file")
    pk.add_argument("--repo", default=argparse.SUPPRESS,
                    help="target repo root (default: cwd)")
    pk.set_defaults(func=cmd_check)

    pg = sub.add_parser("gen", help='W-C: generate patch, e.g. gen "add retry limit"')
    pg.add_argument("task", help="task description")
    pg.add_argument("--target", required=True, help="target file (repo-relative)")
    pg.add_argument("--mode", default="stub", choices=["stub", "openai", "gemini", "groq", "ollama"])
    pg.add_argument("--repo", default=argparse.SUPPRESS,
                    help="target repo root (default: cwd)")
    pg.set_defaults(func=cmd_gen)

    pu = sub.add_parser("update", help="W-B: incremental update of changed files")
    pu.add_argument("--repo", default=argparse.SUPPRESS)
    pu.set_defaults(func=cmd_update)

    pr = sub.add_parser("recall", help="single recall() across files")
    pr.add_argument("query", help="free-text query")
    pr.add_argument("--max-items", type=int, default=20)
    pr.add_argument("--max-chars", type=int, default=8000)
    pr.add_argument("--template", default="technical")
    pr.add_argument("--repo", default=argparse.SUPPRESS)
    pr.set_defaults(func=cmd_recall)

    pe = sub.add_parser("export", help="export store to JSON")
    pe.add_argument("file", help="output .json path")
    pe.add_argument("--repo", default=argparse.SUPPRESS)
    pe.set_defaults(func=cmd_export)

    pi2 = sub.add_parser("import", help="import store JSON")
    pi2.add_argument("file", help="input .json path")
    pi2.add_argument("--skip-existing", action="store_true")
    pi2.add_argument("--repo", default=argparse.SUPPRESS)
    pi2.set_defaults(func=cmd_import)

    ps2 = sub.add_parser("snapshot", help="snapshots create/list/restore")
    ps2.add_argument("snap_cmd", nargs="?", default="create",
                     choices=["create", "list", "restore"])
    ps2.add_argument("label", nargs="?", default="")
    ps2.add_argument("--repo", default=argparse.SUPPRESS)
    ps2.set_defaults(func=cmd_snapshot)

    pv = sub.add_parser("verify", help="hash-verify store JSON")
    pv.add_argument("--repo", default=argparse.SUPPRESS)
    pv.set_defaults(func=cmd_verify)

    pvw = sub.add_parser("viewer", help="build static HTML viewer")
    pvw.add_argument("--repo", default=argparse.SUPPRESS)
    pvw.set_defaults(func=cmd_viewer)

    ph = sub.add_parser("hook", help="post-commit hook install/status/uninstall")
    ph.add_argument("hook_cmd", nargs="?", default="status",
                    choices=["install", "status", "uninstall"])
    ph.add_argument("--repo", default=argparse.SUPPRESS)
    ph.set_defaults(func=cmd_hook)

    psv = sub.add_parser("serve", help="HTTP API on 127.0.0.1:3210")
    psv.add_argument("--port", type=int, default=3210)
    psv.add_argument("--repo", default=argparse.SUPPRESS)
    psv.set_defaults(func=cmd_serve)

    pev = sub.add_parser("eval", help="benchmark audit/run/protocol")
    pev.add_argument("eval_cmd", nargs="?", default="audit",
                     choices=["audit", "run", "protocol"])
    pev.add_argument("--tasks", default="tasks.json",
                     help="tasks JSON file (audit/run)")
    pev.add_argument("--repo", default=argparse.SUPPRESS)
    pev.set_defaults(func=cmd_eval)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
