"""Shared helpers for task checks. Prepended to each task's check.py before it is piped
into the container, so checks stay single self-contained stdlib scripts. Checks run as
root with `python3 -I -` after the agent has finished and after the final snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any

API = "http://127.0.0.1:8080"


def facts() -> dict[str, Any]:
    return json.loads(os.environ.get("TASK_FACTS") or "{}")


def emit(outcome: str, success: bool, details: dict[str, Any]) -> None:
    print(json.dumps({"outcome": outcome, "success": success, "details": details}, sort_keys=True, default=str))
    sys.exit(0)


def run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def http_get(path: str, timeout: float = 5) -> tuple[int | None, str]:
    """Return (status, body); status None if the API is unreachable."""
    try:
        with urllib.request.urlopen(API + path, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return None, ""


def psql(sql: str) -> list[list[str]]:
    """Run SQL as postgres (application_name=check). Raises RuntimeError on SQL errors."""
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "PGAPPNAME": "check", "LANG": "C.UTF-8"}
    res = subprocess.run(
        ["runuser", "-u", "postgres", "--", "psql", "-X", "-A", "-t", "-F", "\x1f", "-v", "ON_ERROR_STOP=1", "-d", "acme", "-c", sql],
        capture_output=True, text=True, timeout=60, env=env,
    )
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip())
    return [line.split("\x1f") for line in res.stdout.split("\n") if line]


def supervisor_status() -> dict[str, str]:
    out = run(["supervisorctl", "status"]).stdout
    return {p[0]: p[1] for p in (line.split() for line in out.splitlines()) if len(p) >= 2}


def sha256_file(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------- task-specific check below
