"""Agent loop, caching, budget and tool-result rendering, with a fake client and shell (no API, no Docker)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from runner.agent import Transcript, run_agent, strip_none
from runner.bash_session import CommandResult, for_model
from runner.budget import BudgetExceeded, Ledger, Usage
from runner.cache import ResponseCache
from runner.config import AgentConfig, Budget, Limits, NamespaceBudget, Price

AGENT = AgentConfig(model="m", effort="high", thinking_display="summarized", max_tokens_per_response=1000)
LIMITS = Limits(command_timeout_s=60, max_tool_output_chars=200, max_turns=5, max_tokens_per_run=100_000)
USAGE = {"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000, "output_tokens": 50}


def tool(cmd: str, i: int = 1) -> dict[str, Any]:
    return {"model": "m", "stop_reason": "tool_use", "usage": USAGE,
            "content": [{"type": "thinking", "thinking": "plan", "signature": "sig"},
                        {"type": "tool_use", "id": f"toolu_{i}", "name": "bash", "input": {"command": cmd}, "caller": None}]}


def final(text: str) -> dict[str, Any]:
    return {"model": "m", "stop_reason": "end_turn", "usage": USAGE, "content": [{"type": "text", "text": text, "citations": None}]}


class FakeMessages:
    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(json.loads(json.dumps(kwargs)))
        return self.script.pop(0)


class FakeClient:
    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.messages = FakeMessages(script)


class FakeShell:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.restarts = 0

    def run(self, command: str, timeout_s: float | None = None) -> CommandResult:
        self.commands.append(command)
        return CommandResult(f"ran: {command}", 0, False, False, 0.01, 10)

    def restart(self) -> None:
        self.restarts += 1


def go(tmp_path: Path, script: list[dict[str, Any]], limits: Limits = LIMITS, sample: int = 0, cache_dir: str = "cache"):  # type: ignore[no-untyped-def]
    client, shell = FakeClient(script), FakeShell()
    result = run_agent(client, ResponseCache(tmp_path / cache_dir), shell, Transcript(tmp_path / f"t{sample}.jsonl"),
                       "SYSTEM", "TICKET", AGENT, limits, "m", sample, "test")
    return result, client, shell


def test_loop_runs_tools_then_returns_final_report(tmp_path: Path) -> None:
    result, client, shell = go(tmp_path, [tool("ls"), tool("pwd", 2), final("Outcome: completed")])
    assert shell.commands == ["ls", "pwd"]
    assert result.end_reason == "end_turn" and result.final_text == "Outcome: completed" and result.turns == 3
    last = client.messages.calls[-1]
    assert [m["role"] for m in last["messages"]] == ["user", "assistant", "user", "assistant", "user"]
    assert last["messages"][1]["content"][0] == {"type": "thinking", "thinking": "plan", "signature": "sig"}  # passed back unchanged
    assert "caller" not in last["messages"][1]["content"][1]                                                # nulls stripped
    assert last["messages"][2]["content"][0] == {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ran: ls"}
    assert "temperature" not in last and last["tools"] == [{"type": "bash_20250124", "name": "bash"}]
    assert last["output_config"] == {"effort": "high"} and last["cache_control"] == {"type": "ephemeral"}


def test_max_turns(tmp_path: Path) -> None:
    result, client, shell = go(tmp_path, [tool(f"c{i}", i) for i in range(10)])
    assert result.end_reason == "max_turns" and len(client.messages.calls) == LIMITS.max_turns and result.final_text == ""


def test_token_budget_stops_before_running_pending_tool(tmp_path: Path) -> None:
    tight = Limits(command_timeout_s=60, max_tool_output_chars=200, max_turns=5, max_tokens_per_run=2000)
    result, _, shell = go(tmp_path, [tool("a"), tool("b", 2), final("x")], limits=tight)
    assert result.end_reason == "token_budget"
    assert shell.commands == ["a"]  # second response pushed the total over 2000; its tool call was not executed


def test_refusal_is_recorded(tmp_path: Path) -> None:
    refusal = {"model": "m", "stop_reason": "refusal", "usage": USAGE, "content": [],
               "stop_details": {"type": "refusal", "category": "cyber", "explanation": "x"}}
    result, _, _ = go(tmp_path, [refusal])
    assert result.end_reason == "refusal" and result.stop_details and result.stop_details["category"] == "cyber"


def test_restart_input(tmp_path: Path) -> None:
    restart = {"model": "m", "stop_reason": "tool_use", "usage": USAGE,
               "content": [{"type": "tool_use", "id": "t1", "name": "bash", "input": {"restart": True}}]}
    _, client, shell = go(tmp_path, [restart, final("done")])
    assert shell.restarts == 1
    assert client.messages.calls[-1]["messages"][2]["content"][0]["content"] == "Shell session restarted."


def test_identical_rerun_is_served_from_cache_but_new_sample_is_not(tmp_path: Path) -> None:
    script = [tool("ls"), final("Outcome: completed")]
    first, c1, _ = go(tmp_path, script)
    again, c2, shell2 = go(tmp_path, [])  # empty script: any API call would fail
    assert len(c1.messages.calls) == 2 and len(c2.messages.calls) == 0
    assert again.cache_hits == 2 and again.final_text == first.final_text and shell2.commands == ["ls"]
    assert again.usage_spent.total_tokens == 0 and again.usage.total_tokens == first.usage.total_tokens
    other, c3, _ = go(tmp_path, list(script), sample=1)
    assert len(c3.messages.calls) == 2 and other.cache_hits == 0


def test_tool_output_rendering() -> None:
    long = CommandResult("x" * 1000, 0, False, False, 0.1, 1000)
    text, is_error, truncated = for_model(long, 200, 60)
    assert truncated and not is_error and "characters omitted" in text and len(text) < 300
    assert for_model(CommandResult("", 0, False, False, 0.1, 0), 200, 60)[0] == "(no output)"
    assert for_model(CommandResult("boom", 2, False, False, 0.1, 4), 200, 60)[0] == "boom\n[exit code: 2]"
    text, is_error, _ = for_model(CommandResult("partial", None, True, False, 60, 7), 200, 60)
    assert is_error and "timed out after 60 s" in text and text.startswith("partial")


def test_usage_cost_and_budget_ledger(tmp_path: Path) -> None:
    u = Usage.from_response(USAGE)
    assert u.total_tokens == 1160
    price = Price(input=2.0, output=10.0, cache_read=0.2, cache_write=2.5)
    assert u.cost_usd(price) == pytest.approx((10 * 2 + 100 * 2.5 + 1000 * 0.2 + 50 * 10) / 1e6)
    ledger = Ledger(tmp_path / "ledger.jsonl")
    one = Budget(max_total_usd=1.0, namespaces={"pilot": NamespaceBudget(max_runs=1), "dry-run": NamespaceBudget(max_runs=1)})
    ledger.check_can_start(one, "pilot")
    ledger.append({"run_id": "r1", "namespace": "dry-run", "cost_usd_spent": 0.5})
    ledger.check_can_start(one, "pilot")                                         # runs are counted per namespace
    with pytest.raises(BudgetExceeded):
        ledger.check_can_start(one, "dry-run")
    with pytest.raises(BudgetExceeded):                                          # spend is capped across namespaces
        ledger.check_can_start(Budget(max_total_usd=0.4, namespaces={"pilot": NamespaceBudget(max_runs=5)}), "pilot")
    capped = Budget(max_total_usd=10.0, namespaces={"exp": NamespaceBudget(max_runs=9, max_usd=0.3)})
    assert ledger.usd_left(capped, "exp") == pytest.approx(0.3)
    ledger.append({"run_id": "r2", "namespace": "exp", "cost_usd_spent": 0.31})
    with pytest.raises(BudgetExceeded):                                          # per-namespace USD cap (D49)
        ledger.check_can_start(capped, "exp")


def test_usd_cap_stops_the_loop_before_the_next_request(tmp_path: Path) -> None:
    client, shell = FakeClient([tool("a"), tool("b", 2), tool("c", 3), final("x")]), FakeShell()
    price = Price(input=2.0, output=10.0, cache_read=0.2, cache_write=2.5)
    per_request = Usage.from_response(USAGE).cost_usd(price)
    result = run_agent(client, ResponseCache(tmp_path / "c"), shell, Transcript(tmp_path / "t.jsonl"), "S", "T",
                       AGENT, LIMITS, "m", 0, "test", usd_left=per_request * 2.5, price=price)
    assert result.end_reason == "budget_cap" and len(client.messages.calls) == 2 and shell.commands == ["a", "b"]


def test_strip_none_keeps_everything_else() -> None:
    assert strip_none({"a": None, "b": [{"c": None, "d": 1}], "e": ""}) == {"b": [{"d": 1}], "e": ""}
