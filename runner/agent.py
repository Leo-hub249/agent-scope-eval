"""Manual tool-use loop: one Anthropic-defined bash tool, executed in the sandbox.

Append-only history (thinking blocks are passed back unchanged). Stops when the model
ends its turn without tool calls, refuses, hits a per-response limit, or when the run
reaches max_turns or max_tokens_per_run. Every request, response, tool call, tool
result and per-step observation goes to the transcript (JSONL).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from runner.bash_session import CommandResult, for_model
from runner.budget import Usage
from runner.cache import ResponseCache
from runner.config import AgentConfig, Limits, Price

BASH_TOOL = {"type": "bash_20250124", "name": "bash"}


class MessagesAPI(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class Client(Protocol):
    messages: MessagesAPI


class Shell(Protocol):
    def run(self, command: str, timeout_s: float | None = None) -> CommandResult: ...
    def restart(self) -> None: ...


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Transcript:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    def write(self, event: str, **data: Any) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": event, "t": now(), **data}, default=str) + "\n")


def strip_none(obj: Any) -> Any:
    """Drop null fields (response-only fields such as citations=None) before re-sending."""
    if isinstance(obj, dict):
        return {k: strip_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [strip_none(v) for v in obj]
    return obj


def response_to_dict(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    data: dict[str, Any] = response.to_dict()
    return data


@dataclass
class AgentResult:
    final_text: str
    end_reason: str          # end_turn | max_turns | token_budget | refusal | max_tokens_response | api_error | ...
    turns: int
    usage: Usage
    usage_spent: Usage       # only requests that were not served from the cache
    cache_hits: int
    response_models: list[str] = field(default_factory=list)
    request_ids: list[str] = field(default_factory=list)
    stop_details: dict[str, Any] | None = None
    tool_calls: int = 0
    error: str = ""


def run_agent(
    client: Client,
    cache: ResponseCache,
    shell: Shell,
    transcript: Transcript,
    system_prompt: str,
    user_message: str,
    agent_cfg: AgentConfig,
    limits: Limits,
    model: str,
    sample_idx: int,
    namespace: str,
    observe: Callable[[], dict[str, Any]] | None = None,
) -> AgentResult:
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
    usage, spent = Usage(), Usage()
    result = AgentResult("", "max_turns", 0, usage, spent, 0)

    for turn in range(1, limits.max_turns + 1):
        result.turns = turn
        request = {
            "model": model,
            "max_tokens": agent_cfg.max_tokens_per_response,
            "system": system_prompt,
            "tools": [BASH_TOOL],
            "messages": messages,
            "thinking": {"type": "adaptive", "display": agent_cfg.thinking_display},
            "output_config": {"effort": agent_cfg.effort},
            "cache_control": {"type": "ephemeral"},
        }
        key = cache.key(request, sample_idx, namespace)
        cached = cache.get(key)
        started = time.monotonic()
        if cached is not None:
            resp, request_id, from_cache = cached["response"], cached.get("request_id"), True
            result.cache_hits += 1
        else:
            try:
                raw = client.messages.create(**request)
            except Exception as exc:  # noqa: BLE001 - record any API failure and end the run
                result.end_reason, result.error = "api_error", f"{type(exc).__name__}: {exc}"
                transcript.write("api_error", turn=turn, error=result.error)
                break
            resp, request_id, from_cache = response_to_dict(raw), getattr(raw, "_request_id", None), False
            cache.put(key, resp, request_id)

        step_usage = Usage.from_response(resp.get("usage") or {})
        usage.add(step_usage)
        if not from_cache:
            spent.add(step_usage)
        result.response_models.append(str(resp.get("model")))
        if request_id:
            result.request_ids.append(request_id)
        content = strip_none(resp.get("content") or [])
        transcript.write(
            "response", turn=turn, request_hash=key, from_cache=from_cache, request_id=request_id,
            model=resp.get("model"), stop_reason=resp.get("stop_reason"), stop_details=resp.get("stop_details"),
            usage=step_usage.as_dict(), latency_s=round(time.monotonic() - started, 2), content=content,
        )
        messages.append({"role": "assistant", "content": content})
        stop = resp.get("stop_reason")
        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        texts = "\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()

        if stop == "refusal":
            result.end_reason, result.stop_details, result.final_text = "refusal", resp.get("stop_details"), texts
            break
        if usage.total_tokens >= limits.max_tokens_per_run:
            result.end_reason, result.final_text = "token_budget", texts
            transcript.write("budget_stop", turn=turn, total_tokens=usage.total_tokens,
                             pending_tool_calls=[t.get("input") for t in tool_uses])
            break
        if stop == "pause_turn":
            continue
        if not tool_uses:
            result.end_reason = "end_turn" if stop == "end_turn" else f"{stop}_response"
            result.final_text = texts
            break
        if stop == "max_tokens":  # a tool call may be cut off mid-input
            result.end_reason, result.final_text = "max_tokens_response", texts
            break

        tool_results = []
        for tu in tool_uses:
            result.tool_calls += 1
            tool_input = tu.get("input") or {}
            transcript.write("tool_call", turn=turn, tool_use_id=tu["id"], input=tool_input)
            if tool_input.get("restart"):
                shell.restart()
                content_text, is_error, truncated, record = "Shell session restarted.", False, False, None
            elif isinstance(tool_input.get("command"), str):
                record = shell.run(tool_input["command"])
                content_text, is_error, truncated = for_model(record, limits.max_tool_output_chars, limits.command_timeout_s)
            else:
                content_text, is_error, truncated, record = "Invalid input: expected 'command' or 'restart'.", True, False, None
            observation = observe() if observe else {}
            transcript.write(
                "tool_result", turn=turn, tool_use_id=tu["id"], is_error=is_error,
                exit_code=record.exit_code if record else None, timed_out=record.timed_out if record else False,
                session_ended=record.session_ended if record else False,
                duration_s=round(record.duration_s, 3) if record else 0.0,
                output_bytes=record.output_bytes if record else 0, truncated_for_model=truncated,
                output=record.output if record else content_text, model_content=content_text, observation=observation,
            )
            block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tu["id"], "content": content_text}
            if is_error:
                block["is_error"] = True
            tool_results.append(block)
        messages.append({"role": "user", "content": tool_results})
    else:
        result.end_reason = "max_turns"

    if result.end_reason == "max_turns":
        transcript.write("max_turns", turns=result.turns)
    transcript.write("final_message", text=result.final_text, end_reason=result.end_reason)
    return result


def cost(usage: Usage, price: Price) -> float:
    return round(usage.cost_usd(price), 6)
