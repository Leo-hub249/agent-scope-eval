"""The persistent bash session against a real sandbox container."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from runner.bash_session import STORE_CAP_BYTES, BashSession
from runner.image_lock import verify as verify_locked_image
from runner.sandbox import Sandbox, new_project_name

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def box() -> Iterator[Sandbox]:
    with Sandbox(new_project_name("sbx-bash"), expected_image_id=verify_locked_image()) as b:
        yield b


@pytest.fixture()
def sh(box: Sandbox) -> Iterator[BashSession]:
    session = BashSession(box.container_id, timeout_s=10)
    session.start()
    yield session
    session.close()


def test_runs_as_ops_with_state_persisting(sh: BashSession) -> None:
    assert sh.run("whoami").output.strip() == "ops"
    sh.run("cd /etc && export FOO=bar")
    assert sh.run("pwd; echo $FOO").output.split() == ["/etc", "bar"]


def test_exit_code_and_stderr_are_captured(sh: BashSession) -> None:
    res = sh.run("ls /does-not-exist")
    assert res.exit_code == 2 and "No such file" in res.output
    assert sh.run("false").exit_code == 1


def test_multiline_and_heredoc(sh: BashSession) -> None:
    res = sh.run("cat <<EOF\nline one\nline two\nEOF\necho after")
    assert res.output.splitlines() == ["line one", "line two", "after"]


def test_unbalanced_quote_fails_fast_instead_of_hanging(sh: BashSession) -> None:
    res = sh.run("echo 'oops")
    assert res.exit_code not in (0, None) and not res.timed_out and res.duration_s < 5


def test_programs_reading_stdin_get_eof(sh: BashSession) -> None:
    assert sh.run("cat").exit_code == 0
    res = sh.run("sudo -u postgres psql acme")  # interactive psql would otherwise eat the protocol
    assert not res.timed_out


def test_output_without_trailing_newline(sh: BashSession) -> None:
    assert sh.run("printf abc").output == "abc"


def test_timeout_kills_and_restarts_the_session(sh: BashSession) -> None:
    sh.run("cd /tmp")
    res = sh.run("echo started; sleep 30", timeout_s=2)
    assert res.timed_out and res.exit_code is None and "started" in res.output
    after = sh.run("pwd")
    assert after.exit_code == 0 and after.output.strip() == "/home/ops" and sh.restarts == 1
    assert sh.run("pgrep -f 'sleep 30' || echo gone").output.strip() == "gone"


def test_exit_ends_session_and_next_command_starts_a_new_one(sh: BashSession) -> None:
    res = sh.run("exit 3")
    assert res.session_ended
    assert sh.run("echo back").output.strip() == "back"


def test_large_output_is_capped_in_the_log(sh: BashSession) -> None:
    res = sh.run("head -c 3000000 /dev/zero | tr '\\0' 'a'; echo; echo END")
    assert res.exit_code == 0 and res.output_bytes >= 3_000_000
    assert len(res.output) < STORE_CAP_BYTES + 200 and "bytes omitted from the log" in res.output
    assert res.output.rstrip().endswith("END")
