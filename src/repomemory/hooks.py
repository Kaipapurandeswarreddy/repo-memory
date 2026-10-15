"""Git hooks: post-commit incremental update (fingerprint: changed files only)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

HOOK = """#!/bin/sh
# repomemory post-commit hook (incremental: changed files only)
repomem update --repo "{repo}" >/dev/null 2>&1 || python -m repomemory.cli update --repo "{repo}" >/dev/null 2>&1 || true
"""


def hook_path(repo_root: str | Path) -> Path:
    return Path(repo_root).resolve() / ".git" / "hooks" / "post-commit"


def install(repo_root: str | Path) -> Path:
    repo = Path(repo_root).resolve()
    hook = hook_path(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(HOOK.format(repo=repo.as_posix()), encoding="utf-8")
    try:
        hook.chmod(hook.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass
    return hook


def uninstall(repo_root: str | Path) -> bool:
    hook = hook_path(repo_root)
    try:
        if hook.exists() and "repomemory" in hook.read_text(encoding="utf-8", errors="replace").lower():
            hook.unlink()
            return True
    except OSError:
        pass
    return False


def status(repo_root: str | Path) -> bool:
    hook = hook_path(repo_root)
    try:
        return hook.exists() and "repomem" in hook.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


__all__ = ["install", "uninstall", "status", "hook_path"]
