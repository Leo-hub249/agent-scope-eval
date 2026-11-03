"""Persistent bash session inside the sandbox, backing the Anthropic-defined bash tool.

One `docker exec -i -u ops ... bash` process per run (same environment as the task
validation scenarios). Each command is evaluated in that shell, so `cd` and variables
persist, with stdin from /dev/null (interactive programs get EOF instead of eating the
protocol) and stderr merged into stdout. A random end marker carries the exit code.
On timeout the whole process tree is killed and a fresh session is started.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass

STORE_CAP_BYTES = 1_000_000  # output kept per command for the log (head + tail)


@dataclass(frozen=True)
class CommandResult:
    output: str              # combined stdout+stderr (capped at STORE_CAP_BYTES, middle elided)
    exit_code: int | None    # None if timed out or the session died
    timed_out: bool
    session_ended: bool      # the shell exited (e.g. the command ran `exit`)
    duration_s: float
    output_bytes: int        # total bytes produced, before capping


def _docker_env() -> dict[str, str]:
    env = os.environ.copy()
    env["DOCKER_CLI_HINTS"] = "false"
    return env


class BashSession:
    def __init__(self, container_id: str, user: str = "ops", workdir: str = "/home/ops", timeout_s: float = 60) -> None:
        self.container_id = container_id
        self.user = user
        self.workdir = workdir
        self.timeout_s = timeout_s
        self._proc: subprocess.Popen[bytes] | None = None
        self._chunks: queue.Queue[bytes | None] = queue.Queue()
        self.shell_pid: int | None = None
        self.restarts = 0

    # ---- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        self._chunks = queue.Queue()
        self._proc = subprocess.Popen(
            ["docker", "exec", "-i", "-u", self.user, "-w", self.workdir, self.container_id,
             "bash", "--noprofile", "--norc"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=_docker_env(),
        )
        threading.Thread(target=self._pump, args=(self._proc, self._chunks), daemon=True).start()
        pid = self.run("echo $$", timeout_s=30)
        if pid.exit_code != 0:
            raise RuntimeError(f"bash session failed to start: {pid.output!r}")
        self.shell_pid = int(pid.output.strip())

    @staticmethod
    def _pump(proc: subprocess.Popen[bytes], chunks: queue.Queue[bytes | None]) -> None:
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        while True:
            data = os.read(fd, 65536)
            if not data:
                chunks.put(None)
                return
            chunks.put(data)

    def _kill_tree(self) -> None:
        if self.shell_pid:
            script = ("kill_tree() { for c in $(pgrep -P \"$1\"); do kill_tree \"$c\"; done; kill -KILL \"$1\" 2>/dev/null; }; "
                      f"kill_tree {self.shell_pid}")
            subprocess.run(["docker", "exec", "-u", "root", self.container_id, "sh", "-c", script],
                           capture_output=True, timeout=30, env=_docker_env())
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
        if self._proc:
            self._proc.wait(timeout=10)

    def restart(self) -> None:
        self._kill_tree()
        self.restarts += 1
        self.start()

    def close(self) -> None:
        """End the session (and anything still attached to it) before the final snapshot."""
        if self._proc and self._proc.poll() is None:
            try:
                assert self._proc.stdin is not None
                self._proc.stdin.write(b"exit 0\n")
                self._proc.stdin.flush()
                self._proc.wait(timeout=5)
            except (BrokenPipeError, subprocess.TimeoutExpired, OSError):
                self._kill_tree()
        self._proc = None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ---- commands --------------------------------------------------------------
    def run(self, command: str, timeout_s: float | None = None) -> CommandResult:
        if not self.alive:
            self.start()
        assert self._proc is not None and self._proc.stdin is not None
        tok = uuid.uuid4().hex
        marker = f"__SBX_DONE_{tok}__".encode()
        wrapped = (
            f"{{ eval \"$(cat <<'__SBX_CMD_{tok}__'\n{command}\n__SBX_CMD_{tok}__\n)\" ; }} < /dev/null 2>&1\n"
            f"printf '\\n%s %s\\n' '__SBX_DONE_{tok}__' \"$?\"\n"
        )
        start = time.monotonic()
        deadline = start + (timeout_s if timeout_s is not None else self.timeout_s)
        try:
            self._proc.stdin.write(wrapped.encode())
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            return CommandResult("", None, False, True, 0.0, 0)

        buf = bytearray()
        total = 0
        elided = 0
        half = STORE_CAP_BYTES // 2
        probe = b"\n" + marker + b" "
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                partial = self._decode(buf, elided, half)
                self.restart()
                return CommandResult(partial, None, True, False, time.monotonic() - start, total)
            try:
                chunk = self._chunks.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if chunk is None:  # the shell exited
                self._proc = None
                return CommandResult(self._decode(buf, elided, half), None, False, True, time.monotonic() - start, total)
            total += len(chunk)
            buf += chunk
            idx = buf.find(probe, max(0, len(buf) - len(chunk) - len(probe) - 64))
            if idx == -1 and len(buf) <= len(chunk) + len(probe) + 64:
                idx = buf.find(probe)
            if idx != -1:
                eol = buf.find(b"\n", idx + len(probe))
                if eol != -1:
                    code = int(buf[idx + len(probe):eol].decode())
                    total -= len(buf) - idx
                    del buf[idx:]
                    return CommandResult(self._decode(buf, elided, half), code, False, False, time.monotonic() - start, total)
            keep_tail = half + len(probe) + 64
            if len(buf) > half + keep_tail + 65536:
                cut = len(buf) - keep_tail - half
                del buf[half:half + cut]  # keep head and tail, drop the middle
                elided += cut

    @staticmethod
    def _decode(buf: bytearray, elided: int, half: int) -> str:
        """Final trim to exactly head(half) + tail(half), noting how much was dropped."""
        data = bytes(buf)
        if len(data) > 2 * half:
            elided += len(data) - 2 * half
            data = data[:half] + data[-half:]
        if elided:
            data = data[:half] + f"\n[... {elided} bytes omitted from the log ...]\n".encode() + data[half:]
        return data.decode("utf-8", "replace")


def for_model(result: CommandResult, max_chars: int, timeout_s: float) -> tuple[str, bool, bool]:
    """Render a command result as the tool_result text: (content, is_error, truncated)."""
    text = result.output
    truncated = False
    if len(text) > max_chars:
        head, tail = int(max_chars * 0.6), int(max_chars * 0.4)
        text = f"{text[:head]}\n[... {len(text) - head - tail} characters omitted ...]\n{text[-tail:]}"
        truncated = True
    if result.timed_out:
        note = (f"Command timed out after {timeout_s:g} s and was killed. The shell session was restarted "
                "(working directory and environment variables were reset).")
        return (f"{text}\n{note}" if text else note), True, truncated
    if result.session_ended:
        note = "The shell session ended. A new session will be started for the next command."
        return (f"{text}\n{note}" if text else note), False, truncated
    if not text.strip():
        text = "(no output)"
    if result.exit_code not in (0, None):
        text = f"{text}\n[exit code: {result.exit_code}]"
    return text, False, truncated
