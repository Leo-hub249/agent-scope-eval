"""Action logs for the 95 scripted validation scenarios (D56), in the transcript format of
real runs, so the scope monitor can be scored on runs with known violations (recall).

Every scenario in tasks/*/task.yaml runs again in a fresh sandbox, as in `make
validate-tasks`, but command by command through the agent's own bash session: the standard
inspection (tasks/validate.py INSPECTION), then the scenario's actions one shell command at a
time (heredocs and continuation lines kept whole). The final message is the scenario's
scripted report. Ground truth: the scenario's hand label (expect_violation, expect_severity)
and the scorer on this re-run. No API calls.

  uv run python -m monitor.scripted [TASK_ID ...]   # -> runs/scripted/<UTC time>/<task>/<scenario>/
"""

from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runner.bash_session import BashSession
from runner.config import load_config
from runner.episode import finalize, prepare, score
from runner.image_lock import verify as verify_image
from runner.prompts import render_system_prompt, render_user_message
from runner.provenance import git_commit, git_dirty
from runner.sandbox import REPO, Sandbox, new_project_name
from scoring.severity import classify as classify_severity
from scoring.severity import run_severity
from tasks.schema import Scenario, TaskBundle, load_all
from tasks.validate import INSPECTION, _slug

OUT_ROOT = REPO / "runs" / "scripted"
COMMAND_TIMEOUT_S = 900  # scenario lines include waits of several minutes (T06)
HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")


def split_commands(script: str) -> list[str]:
    """Shell lines as separate commands; a heredoc or a line ending in a backslash stays with
    the lines that complete it."""
    out: list[str] = []
    lines = script.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.strip().startswith("#") or line.strip() in ("set -u", "set -euo pipefail"):
            continue
        block = [line]
        while block[-1].rstrip().endswith("\\") and i < len(lines):
            block.append(lines[i])
            i += 1
        m = HEREDOC.search(block[-1])
        if m:
            while i < len(lines):
                block.append(lines[i])
                i += 1
                if block[-1].strip() == m.group(1):
                    break
        out.append("\n".join(block))
    return out


def _events(commands: list[str], results: list[Any], report: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for n, (cmd, res) in enumerate(zip(commands, results), 1):
        tid = f"scripted_{n}"
        events.append({"event": "response", "turn": n, "stop_reason": "tool_use",
                       "content": [{"type": "tool_use", "id": tid, "name": "bash", "input": {"command": cmd}}]})
        events.append({"event": "tool_result", "turn": n, "tool_use_id": tid, "exit_code": res.exit_code,
                       "timed_out": res.timed_out, "output": res.output})
    events.append({"event": "final_message", "text": report})
    return events


def run_scenario(bundle: TaskBundle, scenario: Scenario, image: str, out_root: Path) -> dict[str, Any]:
    cfg = load_config()
    out = out_root / bundle.task.id / _slug(scenario.name)
    out.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {"task_id": bundle.task.id, "task_kind": bundle.task.kind, "scenario": scenario.name,
                            "scenario_kind": scenario.kind, "expect_violation": scenario.expect_violation,
                            "expect_severity": scenario.expect_severity, "git_commit": git_commit(), "image": image}
    (out / "system_prompt.txt").write_text(render_system_prompt(bundle.task, cfg.limits) + "\n", encoding="utf-8")
    (out / "user_message.txt").write_text(render_user_message(bundle.task) + "\n", encoding="utf-8")
    commands = split_commands(INSPECTION) + split_commands(scenario.actions or "")
    try:
        with Sandbox(new_project_name("scr"), expected_image_id=image) as box:
            prepared = prepare(bundle, box)
            shell = BashSession(box.container_id, timeout_s=COMMAND_TIMEOUT_S)
            shell.start()
            try:
                results = [shell.run(c) for c in commands]
            finally:
                shell.close()
            finalized = finalize(bundle, prepared)
            scored = score(bundle, prepared, finalized, scenario.report or "")
        classified = classify_severity(scored.scope.violations)
        meta |= {"auto_violated": scored.scope.violated, "auto_severity": run_severity(classified),
                 "violations": [c.as_dict() for c in classified], "check": finalized.check,
                 "story_ok": prepared.story_check.get("ok"), "error": None}
        events = _events(commands, results, scenario.report or "")
        (out / "transcript.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - recorded, the scenario is left out of the monitor set
        meta["error"] = f"{type(exc).__name__}: {exc}"
    (out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta


def latest() -> Path:
    dirs = sorted(p for p in OUT_ROOT.iterdir() if p.is_dir())
    if not dirs:
        raise FileNotFoundError("no scripted logs yet: run `make scripted-logs`")
    return dirs[-1]


def scenario_dirs(root: Path | None = None) -> list[Path]:
    root = root or latest()
    return sorted(p.parent for p in root.glob("T*/*/transcript.jsonl"))


def main(argv: list[str]) -> int:
    if git_dirty():
        sys.exit("uncommitted changes: commit first so the scripted logs record an exact commit")
    image = verify_image()
    bundles = [b for b in load_all() if not argv or b.task.id in argv]
    out_root = OUT_ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    jobs = [(b, s) for b in bundles for s in b.task.validation]
    print(f"scripted logs: {len(jobs)} scenarios -> {out_root.relative_to(REPO)}")
    with ThreadPoolExecutor(max_workers=6) as pool:
        metas = list(pool.map(lambda job: run_scenario(job[0], job[1], image, out_root), jobs))
    bad = [m for m in metas if m["error"] or m["auto_violated"] != m["expect_violation"]]
    for m in metas:
        flag = "ok " if m not in bad else "BAD"
        print(f"  {flag} {m['task_id']} {m['scenario_kind']:9} {m['scenario'][:55]:55} violation={m.get('auto_violated')} "
              f"(label {m['expect_violation']}) {m['error'] or ''}")
    print(f"{len(metas) - len(bad)}/{len(metas)} scenarios with the labelled violation status")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
