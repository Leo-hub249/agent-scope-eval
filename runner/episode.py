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
from tasks.schema import TaskBundle
from tasks.success import SuccessResult, evaluate

CHECKLIB = (REPO / "tasks" / "checklib.py").read_text(encoding="utf-8")


@dataclass
class Prepared:
    sandbox: Sandbox
    facts: dict[str, Any]
    baseline: dict[str, Any]
    docker_base: list[tuple[str, str]]
    setup_output: str


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
    extra: dict[str, Any] = field(default_factory=dict)


def _parse_facts(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.strip().splitlines()):
        if line.startswith("FACTS "):
            facts: dict[str, Any] = json.loads(line[len("FACTS "):])
            return facts
    return {}


def prepare(bundle: TaskBundle, sandbox: Sandbox) -> Prepared:
    """Plant the task's broken state in a running sandbox and take the baseline."""
    res = sandbox.run_script(bundle.setup_script, user="root", timeout=300)
    if not res.ok:
        raise SandboxError(f"{bundle.task.id} setup failed (exit {res.returncode}):\n{res.stdout}\n{res.stderr}")
    baseline = sandbox.snapshot()
    return Prepared(sandbox, _parse_facts(res.stdout), baseline, sandbox.changed_paths(), res.stdout)


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
    return Scored(changes, scope, success)
