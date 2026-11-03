"""Provenance helpers: which code produced a result."""

from __future__ import annotations

import subprocess

from runner.sandbox import REPO


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False).stdout.strip()


def git_commit() -> str:
    return _git("rev-parse", "HEAD")


def git_dirty() -> bool:
    return bool(_git("status", "--porcelain"))
