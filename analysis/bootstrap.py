"""Confidence intervals for run-level rates (D58).

  cluster_ci     two-stage cluster bootstrap: resample tasks with replacement, then runs within
                 each drawn task with replacement; 95% percentile interval. Samples of one task
                 are not independent, so tasks are the clusters.
  exact_ci       Clopper-Pearson 95% interval, used where the percentile bootstrap collapses
                 (0 or n events: every resample gives the same rate) and for simple random samples.
  rate_ci        the interval reported in the tables: cluster bootstrap, exact at the boundary.

No API, deterministic for a given seed.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import comb

import numpy as np

RESAMPLES = 10_000
SEED = 20261109


@dataclass(frozen=True)
class Rate:
    k: int
    n: int
    lo: float
    hi: float
    method: str  # "cluster" | "exact"

    @property
    def p(self) -> float:
        return self.k / self.n if self.n else float("nan")

    def cell(self) -> str:
        if not self.n:
            return "—"
        mark = "†" if self.method == "exact" else ""
        return f"{self.k}/{self.n} · {pct(self.p)}% [{pct(self.lo)}–{pct(self.hi)}]{mark}"


def pct(x: float) -> str:
    """Percent, one decimal below 10% so that small rates do not read as zero."""
    v = 100 * x
    return f"{v:.1f}" if 0 < v < 9.95 else f"{v:.0f}"


def _binom_cdf(k: int, n: int, p: float) -> float:
    return sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k + 1))


def _bisect(f, lo: float = 0.0, hi: float = 1.0) -> float:  # f increasing on [lo, hi], root inside
    for _ in range(100):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if f(mid) < 0 else (lo, mid)
    return (lo + hi) / 2


def exact_ci(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Clopper-Pearson: lo solves P(X >= k | p) = alpha/2, hi solves P(X <= k | p) = alpha/2."""
    if n == 0:
        return float("nan"), float("nan")
    lo = 0.0 if k == 0 else _bisect(lambda p: (1 - _binom_cdf(k - 1, n, p)) - alpha / 2)
    hi = 1.0 if k == n else _bisect(lambda p: alpha / 2 - _binom_cdf(k, n, p))
    return lo, hi


def cluster_ci(clusters: list[list[bool]], resamples: int = RESAMPLES, seed: int = SEED) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    t = len(clusters)
    m = max(len(c) for c in clusters)
    y = np.zeros((t, m))
    sizes = np.array([len(c) for c in clusters])
    for i, c in enumerate(clusters):
        y[i, : len(c)] = c
    drawn = rng.integers(0, t, size=(resamples, t))                 # stage 1: tasks
    n = sizes[drawn]                                                # (B, T)
    idx = np.floor(rng.random((resamples, t, m)) * n[..., None]).astype(int)  # stage 2: runs within a task
    vals = y[drawn[..., None], idx]
    vals *= np.arange(m)[None, None, :] < n[..., None]
    rates = vals.sum(axis=(1, 2)) / n.sum(axis=1)
    lo, hi = np.percentile(rates, [2.5, 97.5])
    return float(lo), float(hi)


def rate_ci(outcomes: list[tuple[str, bool]], seed: int = SEED) -> Rate:
    """outcomes: (task_id, outcome) per run."""
    k, n = sum(o for _, o in outcomes), len(outcomes)
    if n == 0:
        return Rate(0, 0, float("nan"), float("nan"), "exact")
    if k in (0, n):
        return Rate(k, n, *exact_ci(k, n), "exact")
    by_task: dict[str, list[bool]] = {}
    for task, o in outcomes:
        by_task.setdefault(task, []).append(bool(o))
    return Rate(k, n, *cluster_ci(list(by_task.values()), seed=seed), "cluster")
