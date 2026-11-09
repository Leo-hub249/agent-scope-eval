"""Interval helpers (D58): exact bounds against known values, bootstrap determinism and range."""

from __future__ import annotations

import pytest

from analysis.bootstrap import cluster_ci, exact_ci, rate_ci


def test_exact_ci_known_values() -> None:
    assert exact_ci(0, 10) == (0.0, pytest.approx(0.3085, abs=1e-4))
    lo, hi = exact_ci(5, 10)
    assert lo == pytest.approx(0.1871, abs=1e-4) and hi == pytest.approx(0.8129, abs=1e-4)
    assert exact_ci(75, 75)[1] == 1.0 and exact_ci(75, 75)[0] == pytest.approx(0.9520, abs=1e-4)


def test_cluster_ci_is_deterministic_and_brackets_the_rate() -> None:
    clusters = [[True, False, False, False, False]] * 3 + [[False] * 5] * 12
    a, b = cluster_ci(clusters, resamples=2000, seed=1), cluster_ci(clusters, resamples=2000, seed=1)
    assert a == b and 0.0 <= a[0] <= 3 / 75 <= a[1] <= 1.0


def test_rate_ci_falls_back_to_exact_at_the_boundary() -> None:
    assert rate_ci([("T01", False)] * 10).method == "exact"
    r = rate_ci([("T01", True), ("T01", False), ("T02", False), ("T02", True)])
    assert r.method == "cluster" and (r.k, r.n) == (2, 4)
