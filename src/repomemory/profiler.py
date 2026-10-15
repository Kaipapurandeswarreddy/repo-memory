"""C1 Profiler — deterministic structural profile extraction (stdlib ``ast`` only).

Profiles a Python file into plain JSON-serialisable dicts::

    {
        "file": "pkg/payments.py",
        "functions": [{"name": ..., "params": {"arg": "type"},
                       "returns": "Receipt | None",
                       "lineno": int, "end_lineno": int | None,
                       "exported": bool}],
        "classes": [{"name": ..., "methods": [...],
                     "lineno": int, "end_lineno": int | None,
                     "exported": bool}],
        "imports": [...],    # raw module strings, e.g. "os", "pkg.mod"
        "exports": [...],    # __all__ if present else public top-level defs
        "constants": [...],  # public UPPER_CASE module-level names
        "calls": [{"name": ..., "lineno": int, "in": ...}],
                             # every call site: dotted name (``a.b.c``),
                             # line number, enclosing scope (function name,
                             # ``Class.method``, or ``"<module>"``).
    }

Conventions:

* Type strings come from annotations via :func:`ast.unparse`, falling back
  to :func:`str`. A missing parameter annotation becomes ``"Any"``; a
  missing return annotation becomes :data:`None`.
* ``exported`` is True for top-level definitions whose name does not start
  with ``"_"``.
* ``imports`` preserves first-appearance order, de-duplicated. For
  ``from X import Y`` the dotted ``"X.Y"`` is recorded (the graph resolves
  it through progressively shorter prefixes, so ``"pkg.mod.func"`` still
  finds ``pkg/mod.py``); star imports record the module ``"X"`` itself.
* ``exports`` uses ``__all__`` when the module defines it as a literal
  list/tuple of strings, otherwise the public top-level function/class
  names.
* ``constants`` are public (no leading underscore) ``UPPER_CASE``
  module-level assignment targets, in order of appearance.

Never raises on bad input: unreadable or unparseable files yield an empty
profile with an ``"error"`` key describing the failure.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

__all__ = [
    "DEFAULT_IGNORE_DIRS",
    "extract_profile",
    "profile_all",
    # Backward-compatible aliases.
    "profile_source",
    "profile_file",
    "profile_repo",
]

#: Directories skipped by :func:`profile_all` unless overridden.
DEFAULT_IGNORE_DIRS = frozenset({".git", ".repomemory", "__pycache__", ".venv", "node_modules"})


def _type_str(annotation: ast.expr | None) -> str:
    """Render an annotation node as a type string.

    Missing annotations become ``"Any"``. Present annotations use
    :func:`ast.unparse` with a :func:`str` fallback.
    """
    if annotation is None:
        return "Any"
    try:
        return ast.unparse(annotation)
    except Exception:
        return str(annotation)


def _returns_str(annotation: ast.expr | None) -> str | None:
    """Render a return annotation; :data:`None` when absent."""
    if annotation is None:
        return None
    try:
        return ast.unparse(annotation)
    except Exception:
        return str(annotation)


def _func_record(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict:
    """Build a function record with an ``exported`` flag (set by caller)."""
    params: dict[str, str] = {}
    for arg in (
        list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
    ):
        params[arg.arg] = _type_str(arg.annotation)
    if node.args.vararg is not None:
        params["*" + node.args.vararg.arg] = _type_str(node.args.vararg.annotation)
    if node.args.kwarg is not None:
        params["**" + node.args.kwarg.arg] = _type_str(node.args.kwarg.annotation)
    return {
        "name": node.name,
        "params": params,
        "returns": _returns_str(node.returns),
        "lineno": node.lineno,
        "end_lineno": getattr(node, "end_lineno", None),
        "exported": not node.name.startswith("_"),
    }


def _class_record(node: ast.ClassDef) -> dict:
    """Build a class record with method names and an ``exported`` flag."""
    methods = [
        item.name
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return {
        "name": node.name,
        "methods": methods,
        "lineno": node.lineno,
        "end_lineno": getattr(node, "end_lineno", None),
        "exported": not node.name.startswith("_"),
    }


def _dunder_all(tree: ast.Module) -> list[str] | None:
    """Return ``__all__`` entries when defined as a literal list/tuple."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [
                t.id
                for t in node.targets
                if isinstance(t, ast.Name) and t.id == "__all__"
            ]
            if targets:
                try:
                    value = ast.literal_eval(node.value)
                except Exception:
                    return None
                if isinstance(value, (list, tuple)) and all(
                    isinstance(x, str) for x in value
                ):
                    return list(value)
                return None
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "__all__"
            and node.value is not None
        ):
            try:
                value = ast.literal_eval(node.value)
            except Exception:
                return None
            if isinstance(value, (list, tuple)) and all(
                isinstance(x, str) for x in value
            ):
                return list(value)
            return None
    return None


def _call_name(func: ast.expr) -> str | None:
    """Render a call target as a dotted name (``a.b.c``), else None.

    Handles ``Name`` and ``Attribute`` chains (``ride.delete_ride_byid``).
    Anything dynamic (``f()()``, ``getattr(...)()``, subscripts, lambdas)
    returns None and is honestly skipped — best-effort, never guessed.
    """
    parts: list[str] = []
    node = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _collect_calls(tree: ast.Module) -> list[dict]:
    """Collect every call site with its enclosing scope and line number."""
    calls: list[dict] = []

    def _walk(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = child.name if scope in ("<module>",) else f"{scope}.{child.name}"
                _walk(child, inner)
            elif isinstance(child, ast.ClassDef):
                _walk(child, child.name)
            elif isinstance(child, ast.Call):
                name = _call_name(child.func)
                if name:
                    calls.append({"name": name, "lineno": child.lineno, "in": scope})
                _walk(child, scope)
            else:
                _walk(child, scope)

    _walk(tree, "<module>")
    return calls


def _collect(tree: ast.Module) -> dict:
    """Collect the profile body (functions/classes/imports/exports/constants)."""
    functions: list[dict] = []
    classes: list[dict] = []
    imports: list[str] = []
    constants: list[str] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(_func_record(node))
        elif isinstance(node, ast.ClassDef):
            classes.append(_class_record(node))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id.isupper()
                    and not target.id.startswith("_")
                    and target.id not in constants
                ):
                    constants.append(target.id)
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if (
                isinstance(target, ast.Name)
                and target.id.isupper()
                and not target.id.startswith("_")
                and target.id not in constants
            ):
                constants.append(target.id)

    # Imports from the whole module (including nested scopes) so that
    # function-level imports still create dependency edges.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in imports:
                    imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            prefix = "." * level + (node.module or "")
            recorded = False
            for alias in node.names:
                if alias.name == "*":
                    continue
                if node.module:
                    full = f"{prefix}.{alias.name}"
                else:
                    full = f"{prefix}{alias.name}"
                if full not in imports:
                    imports.append(full)
                recorded = True
            if not recorded and prefix and prefix not in imports:
                # Star import (``from x import *``): keep the module itself.
                imports.append(prefix)

    dunder = _dunder_all(tree)
    if dunder is not None:
        exports = dunder
    else:
        exports = [f["name"] for f in functions if f["exported"]]
        exports += [c["name"] for c in classes if c["exported"]]

    return {
        "functions": functions,
        "classes": classes,
        "imports": imports,
        "exports": exports,
        "constants": constants,
        "calls": _collect_calls(tree),
    }


def _empty_profile(rel: str, error: str) -> dict:
    """Return an empty (failed) profile that never raises downstream."""
    return {
        "file": rel,
        "functions": [],
        "classes": [],
        "imports": [],
        "exports": [],
        "constants": [],
        "calls": [],
        "error": error,
    }


def _relpath_of(file_path: str | os.PathLike, repo_root: str | os.PathLike | None) -> str:
    """Compute the ``/``-separated repo-relative path for a file."""
    text = os.fspath(file_path).replace("\\", "/")
    if repo_root is not None:
        try:
            rel = Path(file_path).relative_to(Path(repo_root)).as_posix()
            return rel
        except ValueError:
            pass
    # Already relative, or unrelated absolute path: use as-is / basename.
    candidate = Path(text)
    if not candidate.is_absolute():
        cleaned = text
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        return cleaned
    return candidate.name


def extract_profile(
    file_path: str | os.PathLike, repo_root: str | os.PathLike | None = None
) -> dict:
    """Extract the structural profile of one Python file.

    Args:
        file_path: Absolute or repo-relative path of the ``.py`` file.
        repo_root: Repository root used to compute the ``"file"`` key.
            When omitted or unrelated, the path itself (or its basename)
            is used.

    Returns:
        Profile dict with ``file/functions/classes/imports/exports/
        constants`` keys. Never raises; failures are reported via an
        ``"error"`` key on an otherwise empty profile.
    """
    rel = _relpath_of(file_path, repo_root)
    try:
        source = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _empty_profile(rel, f"read_error: {exc}")
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        return _empty_profile(rel, f"parse_error: {exc}")
    return {"file": rel, **_collect(tree)}


def profile_all(
    repo_root: str | os.PathLike,
    ignore_dirs: set[str] | frozenset[str] | tuple[str, ...] | list[str] | None = None,
) -> dict[str, dict]:
    """Profile every ``*.py`` file under ``repo_root``.

    Args:
        repo_root: Repository root to walk.
        ignore_dirs: Directory names to prune. Defaults to
            ``{.git, .repomemory, __pycache__, .venv, node_modules}``.

    Returns:
        Mapping of ``/``-separated relative path to profile dict.
    """
    ignored = set(DEFAULT_IGNORE_DIRS) if ignore_dirs is None else set(ignore_dirs)
    root = Path(repo_root)
    out: dict[str, dict] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in ignored)
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            full = Path(dirpath) / filename
            try:
                rel = full.relative_to(root).as_posix()
            except ValueError:
                continue
            out[rel] = extract_profile(full, root)
    return out


# -- Backward-compatible aliases -------------------------------------------


def profile_source(source: str, relpath: str) -> dict:
    """Profile Python source text (legacy name; same shape as extract)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        return _empty_profile(relpath, f"parse_error: {exc}")
    return {"file": relpath, **_collect(tree)}


def profile_file(
    abs_path: str | os.PathLike, repo_root: str | os.PathLike | None = None
) -> dict:
    """Profile a file on disk (legacy name for :func:`extract_profile`)."""
    return extract_profile(abs_path, repo_root)


def profile_repo(
    repo_root: str | os.PathLike,
    skip_dirs: set[str] | frozenset[str] | tuple[str, ...] | list[str] | None = None,
) -> dict[str, dict]:
    """Walk and profile every ``*.py`` file (legacy name for profile_all)."""
    return profile_all(repo_root, ignore_dirs=skip_dirs)
