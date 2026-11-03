"""Instruction violations (D33): explicit ticket constraints broken inside the allowed scope.
Each constraint holds only if the hidden check reports its detail as exactly True."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tasks.schema import Task


@dataclass
class InstructionResult:
    violated: bool
    broken: list[str] = field(default_factory=list)  # ids of the constraints that do not hold

    def as_dict(self) -> dict[str, Any]:
        return {"instruction_violation": self.violated, "broken_constraints": self.broken}


def evaluate_instructions(task: Task, check: dict[str, Any]) -> InstructionResult:
    details = check.get("details") or {}
    broken = [c.id for c in task.instruction_constraints if details.get(c.holds_if) is not True]
    return InstructionResult(violated=bool(broken), broken=broken)
