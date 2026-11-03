"""Host-side control of sandbox containers.

Every run gets its own compose project (fresh container from the same image),
destroyed afterwards. Nothing here executes commands on the host other than the
docker CLI itself; everything the agent does runs via `docker exec`.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

REPO = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO / "sandbox" / "compose.yaml"
PROBE_SCRIPT = REPO / "sandbox" / "probe" / "snapshot.py"
IMAGE = "agent-scope-eval/sandbox:latest"
SERVICE = "server"


class SandboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecResult:
    returncode: int
    stdout: str
    stderr: str
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["DOCKER_CLI_HINTS"] = "false"  # keep CLI hint text out of captured output
    return env


def docker(*args: str, stdin: str | None = None, timeout: float = 120) -> ExecResult:
    start = time.monotonic()
    proc = subprocess.run(
        ["docker", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_env(),
    )
    return ExecResult(proc.returncode, proc.stdout, proc.stderr, time.monotonic() - start)


def compose(project: str, *args: str, timeout: float = 180) -> ExecResult:
    return docker("compose", "-f", str(COMPOSE_FILE), "-p", project, *args, timeout=timeout)


def image_id(image: str = IMAGE) -> str:
    res = docker("image", "inspect", "--format", "{{.Id}}", image)
    if not res.ok:
        raise SandboxError(f"image {image} not found; run `make sandbox` first")
    return res.stdout.strip()


def new_project_name(prefix: str = "sbx") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Sandbox:
    """One fresh sandbox container. Use as a context manager so it is always destroyed."""

    def __init__(self, project: str, expected_image_id: str | None = None) -> None:
        self.project = project
        self.expected_image_id = expected_image_id
        self.container_id = ""
        self.image_id = ""  # id of the image this container was actually created from

    # ---- lifecycle -------------------------------------------------------
    def start(self, ready_timeout: float = 90) -> Self:
        res = compose(self.project, "up", "-d", "--wait", "--no-build", timeout=ready_timeout + 30)
        if not res.ok:
            self.stop()
            raise SandboxError(f"compose up failed: {res.stderr.strip()}")
        cid = compose(self.project, "ps", "-q", SERVICE).stdout.strip()
        if not cid:
            self.stop()
            raise SandboxError("container id not found after compose up")
        self.container_id = cid
        self.image_id = docker("inspect", "--format", "{{.Image}}", cid).stdout.strip()
        if self.expected_image_id and self.image_id != self.expected_image_id:
            self.stop()
            raise SandboxError(f"container image {self.image_id} != locked image {self.expected_image_id}")
        self.wait_ready(ready_timeout)
        return self

    def wait_ready(self, timeout: float = 90) -> None:
        """Healthy per compose AND every supervisord program RUNNING (acme-api has startsecs=3)."""
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            res = self.exec(["supervisorctl", "status"], user="root", timeout=10)
            last = res.stdout
            lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
            if lines and all(ln.split()[1] == "RUNNING" for ln in lines if len(ln.split()) > 1):
                return
            time.sleep(0.5)
        raise SandboxError(f"services not RUNNING after {timeout}s:\n{last}")

    def stop(self) -> None:
        compose(self.project, "down", "-v", "--remove-orphans", "--timeout", "5")
        self.container_id = ""

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    # ---- execution -------------------------------------------------------
    def _require(self) -> str:
        if not self.container_id:
            raise SandboxError("sandbox is not running")
        return self.container_id

    def exec(
        self,
        cmd: list[str],
        user: str = "ops",
        stdin: str | None = None,
        timeout: float = 120,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        args = ["exec"]
        if stdin is not None:
            args.append("-i")
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        args += ["-u", user, self._require(), *cmd]
        return docker(*args, stdin=stdin, timeout=timeout)

    def run_script(
        self, script: str, user: str = "root", timeout: float = 120, env: dict[str, str] | None = None
    ) -> ExecResult:
        """Pipe a bash script in via stdin; it never exists as a file inside the container."""
        return self.exec(["bash", "-s"], user=user, stdin=script, timeout=timeout, env=env)

    def run_python(
        self, script: str, user: str = "root", timeout: float = 120, env: dict[str, str] | None = None,
        args: list[str] | None = None,
    ) -> ExecResult:
        """Pipe a stdlib-only Python script in via stdin, run with `python3 -I -B -` (isolated mode, and
        no bytecode written: a fresh .pyc in the system Python would date the instrumentation, D42)."""
        return self.exec(["/usr/local/bin/python3", "-I", "-B", "-", *(args or [])], user=user, stdin=script,
                         timeout=timeout, env=env)

    # ---- observation -----------------------------------------------------
    def snapshot(self, baseline: dict[str, Any] | None = None, timeout: float = 120) -> dict[str, Any]:
        """Run the probe. With a baseline, log files also get a hash of their baseline-length prefix."""
        env: dict[str, str] = {}
        if baseline is not None:
            sizes = {
                path: entry["size"]
                for path, entry in baseline["volatile"]["logs"].items()
                if entry.get("type") == "f" and "size" in entry
            }
            env["PROBE_PREFIX_SIZES"] = json.dumps(sizes, separators=(",", ":"))
        res = self.run_python(PROBE_SCRIPT.read_text(encoding="utf-8"), timeout=timeout, env=env)
        if not res.ok:
            raise SandboxError(f"probe failed (exit {res.returncode}): {res.stderr.strip()}")
        snap: dict[str, Any] = json.loads(res.stdout)
        return snap

    def changed_paths(self) -> list[tuple[str, str]]:
        """`docker diff`: every path written since the container was created (A/C/D, path)."""
        res = docker("diff", self._require())
        if not res.ok:
            raise SandboxError(f"docker diff failed: {res.stderr.strip()}")
        out: list[tuple[str, str]] = []
        for line in res.stdout.splitlines():
            kind, _, path = line.partition(" ")
            out.append((kind, path))
        return out

    def inspect(self) -> dict[str, Any]:
        res = docker("inspect", self._require())
        data: list[dict[str, Any]] = json.loads(res.stdout)
        return data[0]


def state_json(snapshot: dict[str, Any]) -> str:
    """Canonical serialization of the deterministic part of a snapshot."""
    return json.dumps(snapshot["state"], sort_keys=True, separators=(",", ":"))
