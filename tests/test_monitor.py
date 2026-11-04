"""Monitor view, scripted command splitting and request shape (no Docker, no API)."""

from __future__ import annotations

from pathlib import Path

import pytest

from monitor.monitor import SCHEMAS, Item, request
from monitor.render import render
from monitor.scripted import split_commands

EVENTS = [
    {"event": "response", "stop_reason": "tool_use", "content": [
        {"type": "thinking", "thinking": "Config is root-only; I'll use sudo."},
        {"type": "text", "text": "Checking the config."},
        {"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "cat /etc/acme/app.yaml"}}]},
    {"event": "tool_result", "tool_use_id": "t1", "exit_code": 1, "timed_out": False, "output": "x" * 9000},
    {"event": "final_message", "text": "Outcome: completed\nChanges made: none\nOpen issues: none"},
]


def test_view_hides_thinking_unless_asked() -> None:
    log = render("SYSTEM", "TICKET", EVENTS, "log")
    assert "root-only" not in log and "Checking the config." in log and "[step 1] $ cat /etc/acme/app.yaml" in log
    assert "[exit code 1]" in log and "characters omitted" in log and "Outcome: completed" in log
    assert "root-only" in render("SYSTEM", "TICKET", EVENTS, "log+thinking")
    with pytest.raises(ValueError):
        render("S", "T", EVENTS, "diffs")


def test_split_commands_keeps_heredocs_and_continuations() -> None:
    script = "set -u\nls /\nsudo psql <<'SQL'\nBEGIN;\nCOMMIT;\nSQL\necho a \\\n  b\n\n# comment\ndf -h\n"
    assert split_commands(script) == ["ls /", "sudo psql <<'SQL'\nBEGIN;\nCOMMIT;\nSQL", "echo a \\\n  b", "df -h"]


def test_request_uses_config_model_and_strict_schema(tmp_path: Path) -> None:
    (tmp_path / "system_prompt.txt").write_text("SYSTEM")
    (tmp_path / "user_message.txt").write_text("TICKET")
    (tmp_path / "transcript.jsonl").write_text("".join(__import__("json").dumps(e) + "\n" for e in EVENTS))
    req = request(Item("scope", "log", "experiment", tmp_path))
    assert req["model"].startswith("claude-") and req["output_config"]["format"]["schema"] == SCHEMAS["scope"]
    assert "temperature" not in req and req["thinking"] == {"type": "adaptive"}
    for schema in SCHEMAS.values():
        assert schema["additionalProperties"] is False
