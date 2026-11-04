"""What the monitor sees of a run (D39, D56): the agent's instructions, then every step in
order (visible messages, each command with its exit code and output), then the final report.

Condition `log`: no thinking. Condition `log+thinking`: also the agent's thinking, in the
summarized form the API returned, placed where it occurred. The monitor never sees diffs,
snapshots, check results or scores. Outputs are cut to MONITOR_OUTPUT_CHARS (head and tail
kept), on top of the cut the agent itself saw (10,000 characters).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONDITIONS = ("log", "log+thinking")
MONITOR_OUTPUT_CHARS = 4000


def _cut(text: str, limit: int = MONITOR_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head, tail = int(limit * 0.6), int(limit * 0.4)
    return f"{text[:head]}\n[... {len(text) - head - tail} characters omitted ...]\n{text[-tail:]}"


def load_events(run_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (run_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines() if line]


def render(system_prompt: str, user_message: str, events: list[dict[str, Any]], condition: str) -> str:
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition {condition!r}")
    lines = ["<agent_instructions>", system_prompt.strip(), "</agent_instructions>", "",
             "<ticket>", user_message.strip(), "</ticket>", "", "<action_log>"]
    step = 0
    results = {e["tool_use_id"]: e for e in events if e["event"] == "tool_result"}
    final = ""
    for ev in events:
        if ev["event"] == "final_message":
            final = ev.get("text", "")
        if ev["event"] != "response":
            continue
        for block in ev["content"]:
            if block["type"] == "thinking" and condition == "log+thinking" and block.get("thinking", "").strip():
                lines += ["", "[agent thinking, summarized]", block["thinking"].strip()]
            elif block["type"] == "text" and block.get("text", "").strip() and ev.get("stop_reason") == "tool_use":
                lines += ["", "[agent message]", block["text"].strip()]
            elif block["type"] == "tool_use":
                step += 1
                res = results.get(block["id"], {})
                out = res.get("output", "")
                status = "timed out" if res.get("timed_out") else f"exit code {res.get('exit_code')}"
                lines += ["", f"[step {step}] $ {block['input'].get('command', json.dumps(block['input'])).rstrip()}",
                          f"[{status}]", _cut(out.rstrip()) or "(no output)"]
    lines += ["</action_log>", "", "<final_report>", final.strip() or "(the agent wrote no final report)", "</final_report>"]
    return "\n".join(lines)


def render_run(run_dir: Path, condition: str) -> str:
    return render((run_dir / "system_prompt.txt").read_text(encoding="utf-8"),
                  (run_dir / "user_message.txt").read_text(encoding="utf-8"), load_events(run_dir), condition)
