"""Task definitions: one directory per task under tasks/, containing

  task.yaml   instruction, scope, success rule, notes, validation scenarios (host-side only)
  setup.sh    plants the broken state; runs as root before the baseline snapshot; its
              last stdout line may be `FACTS <json>` (hidden facts passed to the check)
  check.py    hidden success check; stdlib only; runs as root after the final snapshot
              with env TASK_FACTS; prints one JSON object {"outcome", "success", "details"}

Only `instruction` and the rendered scope block are ever shown to the agent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

TASKS_DIR = Path(__file__).resolve().parent

TaskKind = Literal["in_scope", "tempting_shortcut", "impossible"]
ReportOutcome = Literal["completed", "partial", "not_completed"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FileRule(Strict):
    """A path the agent may modify. A trailing '/' means the directory itself and everything under it."""

    path: str
    note: str | None = None

    def matches(self, target: str) -> bool:
        if self.path.endswith("/"):
            return target == self.path.rstrip("/") or target.startswith(self.path)
        return target == self.path


class DbAllow(Strict):
    change: Literal["column_type", "column_data", "new_table"]
    table: str | None = None  # schema-qualified, e.g. public.products
    column: str | None = None

    @model_validator(mode="after")
    def _needs_target(self) -> DbAllow:
        if self.change in ("column_type", "column_data") and not (self.table and self.column):
            raise ValueError(f"{self.change} needs table and column")
        return self


class DatabaseScope(Strict):
    text: str = "read only"  # how the permission is worded in the scope block
    allow: list[DbAllow] = Field(default_factory=list)


class Scope(Strict):
    files: list[FileRule] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)  # may be started/stopped/restarted
    database: DatabaseScope = Field(default_factory=DatabaseScope)


class ReportRule(Strict):
    outcome: ReportOutcome
    must_mention_all: list[str] = Field(default_factory=list)  # case-insensitive substrings


class SuccessRule(Strict):
    check_outcomes: list[str]                 # check outcomes that count as success
    require_no_violation: bool = False        # only for tasks where scope adherence IS the task
    report: ReportRule | None = None          # requirements on the final report


class Scenario(Strict):
    """A scripted stand-in for the agent, used only to validate the task.

    noop       changes nothing (only the shared read-only inspection commands run)
    reference  a correct, in-scope solution
    shortcut   a tempting wrong move; must NOT count as success
    overreach  a correct solution plus an out-of-scope extra (success AND violation)
    """

    name: str
    kind: Literal["noop", "reference", "shortcut", "overreach"]
    actions: str = ""                         # bash, run as ops (sudo available), like the agent
    report: str = ""                          # simulated final report
    expect_success: bool
    expect_outcome: str
    expect_violation: bool


class Task(Strict):
    id: str
    title: str
    kind: TaskKind
    instruction: str
    scope: Scope
    success: SuccessRule
    notes: str = ""
    validation: list[Scenario]

    @model_validator(mode="after")
    def _validation_complete(self) -> Task:
        kinds = [s.kind for s in self.validation]
        if kinds.count("noop") != 1 or "reference" not in kinds or "shortcut" not in kinds:
            raise ValueError(f"{self.id}: needs exactly one noop, at least one reference and one shortcut")
        return self


class TaskBundle(Strict):
    task: Task
    directory: Path
    setup_script: str
    check_script: str


def load_task(directory: Path) -> TaskBundle:
    raw = yaml.safe_load((directory / "task.yaml").read_text(encoding="utf-8"))
    task = Task.model_validate(raw)
    if not directory.name.startswith(task.id + "_"):
        raise ValueError(f"directory {directory.name} does not match task id {task.id}")
    return TaskBundle(
        task=task,
        directory=directory,
        setup_script=(directory / "setup.sh").read_text(encoding="utf-8"),
        check_script=(directory / "check.py").read_text(encoding="utf-8"),
    )


def load_all() -> list[TaskBundle]:
    dirs = sorted(p for p in TASKS_DIR.iterdir() if p.is_dir() and (p / "task.yaml").exists())
    bundles = [load_task(d) for d in dirs]
    ids = [b.task.id for b in bundles]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate task ids: {ids}")
    return bundles
