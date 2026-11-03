"""One episode = fresh sandbox -> task setup -> baseline -> (agent or scripted actions)
-> final snapshot -> hidden check -> scoring. Used by task validation now and by the
agent runner in Phase 4 (which plugs the model loop in between prepare and finalize)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from runner.sandbox import REPO, Sandbox, SandboxError
from scoring.diff import Change, diff_all
from scoring.scope import ScopeResult, classify
from tasks.instructions import InstructionResult, evaluate_instructions
from tasks.schema import TaskBundle
from tasks.success import SuccessResult, evaluate

CHECKLIB = (REPO / "tasks" / "checklib.py").read_text(encoding="utf-8")
STORYLIB = (REPO / "tasks" / "story_lib.py").read_text(encoding="utf-8")


@dataclass
class Prepared:
    sandbox: Sandbox
    facts: dict[str, Any]
    baseline: dict[str, Any]
    docker_base: list[tuple[str, str]]
    setup_output: str
    story_check: dict[str, Any] = field(default_factory=dict)


@dataclass
class Finalized:
    final: dict[str, Any]
    docker_final: list[tuple[str, str]]
    check: dict[str, Any]


@dataclass
class Scored:
    changes: list[Change]
    scope: ScopeResult
    success: SuccessResult
    instructions: InstructionResult
    extra: dict[str, Any] = field(default_factory=dict)


def _parse_facts(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.strip().splitlines()):
        if line.startswith("FACTS "):
            facts: dict[str, Any] = json.loads(line[len("FACTS "):])
            return facts
    return {}


def story_script(bundle: TaskBundle) -> str:
    return STORYLIB + "\n" + bundle.story_script + "\nmain()\n"


def _story(sandbox: Sandbox, bundle: TaskBundle, phase: str, env: dict[str, str]) -> str:
    res = sandbox.run_python(story_script(bundle), user="root", timeout=600, env=env, args=[phase])
    if not res.ok:
        raise SandboxError(f"{bundle.task.id} story {phase} failed (exit {res.returncode}):\n{res.stderr[-3000:]}")
    return res.stdout


def prepare(bundle: TaskBundle, sandbox: Sandbox) -> Prepared:
    """Plant the task's broken state, make the history consistent with the ticket (D37),
    check that consistency, and take the baseline."""
    env = {"STORY_ANCHOR": sandbox.exec(["date", "+%s.%N"], user="root").stdout.strip()}
    pre = _story(sandbox, bundle, "pre", env)
    res = sandbox.run_script(bundle.setup_script, user="root", timeout=300)
    if not res.ok:
        raise SandboxError(f"{bundle.task.id} setup failed (exit {res.returncode}):\n{res.stdout}\n{res.stderr}")
    post = _story(sandbox, bundle, "post", env)
    check_out = _story(sandbox, bundle, "check", env).strip().splitlines()
    story_check: dict[str, Any] = json.loads(check_out[-1]) if check_out else {"ok": False, "assertions": []}
    facts = {**_parse_facts(pre), **_parse_facts(res.stdout), **_parse_facts(post)}
    baseline = sandbox.snapshot()
    return Prepared(sandbox, facts, baseline, sandbox.changed_paths(), res.stdout, story_check)


def finalize(bundle: TaskBundle, prepared: Prepared) -> Finalized:
    sandbox = prepared.sandbox
    final = sandbox.snapshot(baseline=prepared.baseline)
    docker_final = sandbox.changed_paths()  # before the check, which may write (e.g. a log line)
    res = sandbox.run_python(
        CHECKLIB + "\n" + bundle.check_script,
        user="root",
        timeout=300,
        env={"TASK_FACTS": json.dumps(prepared.facts, separators=(",", ":"))},
    )
    try:
        check: dict[str, Any] = json.loads(res.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        check = {"outcome": "check_error", "success": False, "details": {"stdout": res.stdout, "stderr": res.stderr}}
    return Finalized(final, docker_final, check)


def score(bundle: TaskBundle, prepared: Prepared, finalized: Finalized, report_text: str) -> Scored:
    changes = diff_all(prepared.baseline, finalized.final, prepared.docker_base, finalized.docker_final)
    scope = classify(changes, bundle.task.scope)
    success = evaluate(bundle.task, finalized.check, scope.violated, report_text)
    return Scored(changes, scope, success, evaluate_instructions(bundle.task, finalized.check))
