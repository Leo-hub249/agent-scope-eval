"""Token accounting, cost, and the budget guard (per run and across all runs)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from runner.config import Budget, Price


@dataclass
class Usage:
    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0

    @classmethod
    def from_response(cls, usage: dict[str, Any]) -> Usage:
        return cls(
            input_tokens=int(usage.get("input_tokens") or 0),
            cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens") or 0),
            cache_read_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        )

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.output_tokens += other.output_tokens

    @property
    def total_tokens(self) -> int:
        """What max_tokens_per_run limits: all input (uncached + cache writes + cache reads) + output."""
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens + self.output_tokens

    def cost_usd(self, price: Price) -> float:
        return (
            self.input_tokens * price.input
            + self.cache_creation_input_tokens * price.cache_write
            + self.cache_read_input_tokens * price.cache_read
            + self.output_tokens * price.output
        ) / 1_000_000

    def as_dict(self) -> dict[str, int]:
        return {**asdict(self), "total_tokens": self.total_tokens}


class BudgetExceeded(RuntimeError):
    pass


class Ledger:
    """Append-only record of every run that called (or would have called) the API."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def spent_usd(self) -> float:
        return sum(float(e.get("cost_usd_spent", 0.0)) for e in self.entries())

    def runs(self) -> int:
        return len(self.entries())

    def check_can_start(self, budget: Budget) -> None:
        if self.runs() >= budget.max_runs:
            raise BudgetExceeded(f"max_runs reached ({self.runs()}/{budget.max_runs}); raise budget.max_runs in config to continue")
        if self.spent_usd() >= budget.max_total_usd:
            raise BudgetExceeded(f"max_total_usd reached (${self.spent_usd():.2f}/${budget.max_total_usd:.2f})")

    def append(self, entry: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
