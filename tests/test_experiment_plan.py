"""The experiment plan: every task x model x sample once, in a fixed shuffled order (D53)."""

from __future__ import annotations

from runner.config import load_config
from runner.experiment import RESERVE_USD, plan
from tasks.schema import load_all


def test_plan_is_complete_unique_and_reproducible() -> None:
    cfg, bundles = load_config(), load_all()
    items = plan(cfg, bundles)
    assert len(items) == len(set(items)) == 15 * 3 * 5
    assert items == plan(cfg, bundles)
    assert items != sorted(items)                     # shuffled, so time effects do not line up with models
    assert {m for _, m, _ in items} == {"claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"}


def test_budget_reservation_covers_a_run_at_the_token_cap() -> None:
    # Opus near the 400K-token cap: ~355K cache reads, 30K cache writes, 15K output, at config prices
    opus = (355_000 * 0.20 + 30_000 * 5.00 + 15_000 * 20.0) / 1e6
    assert RESERVE_USD > opus
    cfg = load_config()
    assert cfg.budget.namespaces["experiment"].max_usd == 60.0 and cfg.budget.namespaces["experiment"].max_runs == 225
