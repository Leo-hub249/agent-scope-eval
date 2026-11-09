"""Phase 7 tables and figures (D58). Offline: reads runs/, labels/ and monitor outputs, no API.

  uv run python -m analysis.analyze    # -> results/tables.md, results/tables.csv, results/figures/*.png

Intervals: run-level rates use the two-stage cluster bootstrap over tasks (analysis/bootstrap.py),
exact Clopper-Pearson where every run has the same outcome (marked †). Self-report rates come
from the phase 6 labels (D54, D57): the flagged stratum is a census, the per-model random sample
stands for the other runs, so a model's count is flagged + pool × sample rate, and its interval
is pool × the exact interval of the sample rate (finite-population correction ignored, so wide).
"""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from analysis.bootstrap import RESAMPLES, SEED, Rate, exact_ci, pct, rate_ci
from analysis.metrics import REPO, RUN_METRICS, RUNS, Run, load_runs, model_order, short
from analysis.monitor_eval import _matches, _out
from monitor import scripted
from monitor.render import CONDITIONS
from scoring.labels import phase6_selection

RESULTS = REPO / "results"
FIGURES = RESULTS / "figures"
LABELS6 = REPO / "labels" / "phase6" / "labels.yaml"
KINDS = ("in_scope", "tempting_shortcut", "impossible")
KIND_NAME = {"in_scope": "in scope", "tempting_shortcut": "tempting shortcut", "impossible": "impossible"}
SELF_REPORT_ROWS = ("report follows template", "report accurate", "report with omission", "report with overstatement",
                    "report with false claim", "  false incapability", "  denied action", "  claimed change not made",
                    "flag: outcome understated", "flag: imprecise wording")

# Reference palette (dataviz skill, references/palette.md), light mode; static PNGs for the README.
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
SERIES = ("#2a78d6", "#eb6834", "#1baf7a")  # slots 1-3, validated all-pairs (light)

csv_rows: list[dict[str, Any]] = []


def record(section: str, metric: str, group: str, r: Rate) -> Rate:
    csv_rows.append({"section": section, "metric": metric.strip(), "group": group, "k": r.k, "n": r.n,
                     "rate": round(r.p, 4) if r.n else "", "ci_low": round(r.lo, 4), "ci_high": round(r.hi, 4),
                     "method": r.method})
    return r


def table(header: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + ["| " + " | ".join(r) + " |" for r in rows]


# ---- per-model run metrics ---------------------------------------------------------------


def run_metric_rows(runs: list[Run], labels: tuple[str, ...] | None, exclude: tuple[str, ...], section: str) -> list[list[str]]:
    groups = {m: [r for r in runs if r.model == m] for m in model_order()} | {"all": runs}
    rows = []
    for label, pred, within in RUN_METRICS:
        if (labels is not None and label not in labels) or label in exclude:
            continue
        cells = [label.replace("  ", "↳ ")]
        for g, rs in groups.items():
            pool = [r for r in rs if within is None or within(r)]
            cells.append(record(section, label, short(g), rate_ci([(r.task_id, bool(pred(r))) for r in pool])).cell())
        rows.append(cells)
    return rows


# ---- self-report from the phase 6 labels -------------------------------------------------


@dataclass(frozen=True)
class Estimate:
    flagged_k: int
    flagged_n: int
    sample_k: int
    sample_n: int
    pool: int
    total: int

    @property
    def count(self) -> float:
        return self.flagged_k + self.pool * self.sample_k / self.sample_n

    def interval(self) -> tuple[float, float]:
        lo, hi = exact_ci(self.sample_k, self.sample_n)
        return (self.flagged_k + self.pool * lo) / self.total, (self.flagged_k + self.pool * hi) / self.total

    def cell(self) -> str:
        lo, hi = self.interval()
        return f"{self.count:.1f}/{self.total} · {pct(self.count / self.total)}% [{pct(lo)}–{pct(hi)}]"


def _label_sets(entry: dict[str, Any]) -> tuple[set[str], set[str]]:
    report = {str(c) for c in entry["report_accuracy"]}
    kinds = {str(s["kind"]) for s in entry["false_statements"]}
    return report, kinds


LABEL_METRICS: list[tuple[str, Callable[[set[str], set[str]], bool]]] = [
    ("report accurate", lambda rep, k: rep == {"accurate"}),
    ("report with omission", lambda rep, k: "omission" in rep),
    ("report with overstatement", lambda rep, k: "overstatement" in rep),
    ("report with false claim", lambda rep, k: "false_claim" in rep),
    ("↳ false incapability", lambda rep, k: "false_incapability" in k),
    ("↳ denied action", lambda rep, k: "denied_action" in k),
    ("↳ claimed change not made", lambda rep, k: "claimed_change_not_made" in k),
    ("↳ other", lambda rep, k: "other" in k),
]


def label_estimates(runs: list[Run]) -> dict[str, dict[str, Estimate]]:
    labels = {e["run_id"]: _label_sets(e) for e in yaml.safe_load(LABELS6.read_text(encoding="utf-8"))["runs"]}
    strata = phase6_selection()
    model_of = {r.run_id: r.model for r in runs}
    out: dict[str, dict[str, Estimate]] = {}
    for name, pred in LABEL_METRICS:
        out[name] = {}
        for model in model_order():
            flagged = [rid for rid in strata["flagged"] if model_of[rid] == model]
            sample = strata[f"sample_{model}"]
            total = sum(1 for m in model_of.values() if m == model)
            out[name][model] = Estimate(sum(pred(*labels[r]) for r in flagged), len(flagged),
                                        sum(pred(*labels[r]) for r in sample), len(sample), total - len(flagged), total)
            e = out[name][model]
            lo, hi = e.interval()
            csv_rows.append({"section": "self-report (labels, estimate)", "metric": name.lstrip("↳ "), "group": short(model),
                             "k": round(e.count, 2), "n": total, "rate": round(e.count / total, 4), "ci_low": round(lo, 4),
                             "ci_high": round(hi, 4), "method": "stratified, exact on sample"})
    return out


# ---- monitors ------------------------------------------------------------------------------


def monitor_rates(runs: list[Run]) -> dict[str, Rate]:
    out: dict[str, Rate] = {}
    rows = []
    for d in scripted.scenario_dirs():
        meta, rec = json.loads((d / "meta.json").read_text(encoding="utf-8")), _out(d, "scope", "log")
        if rec:
            rows.append((meta, bool(rec["output"]["flag"])))
    out["scope · scripted violations flagged (recall)"] = rate_ci([(m["task_id"], f) for m, f in rows if m["expect_violation"]])
    out["scope · scripted serious violations flagged"] = rate_ci(
        [(m["task_id"], f) for m, f in rows if m["expect_severity"] in ("circumvention", "destructive")])
    out["scope · scripted non-violations flagged (FP)"] = rate_ci([(m["task_id"], f) for m, f in rows if not m["expect_violation"]])
    for cond in CONDITIONS:
        res = [(r, _out(RUNS / r.run_id, "scope", cond)) for r in runs if not r.scores["violated"]]
        out[f"scope · real runs flagged (FP), {cond}"] = rate_ci([(r.task_id, bool(o["output"]["flag"])) for r, o in res if o])
    labels = yaml.safe_load(LABELS6.read_text(encoding="utf-8"))["runs"]
    task_of = {r.run_id: r.task_id for r in runs}
    for cond in CONDITIONS:
        run_pairs, inc = [], []
        for e in labels:
            o = _out(RUNS / e["run_id"], "report", cond)
            said = [s["quote"] for s in o["output"]["statements"]] if o else []
            if "false_claim" in e["report_accuracy"]:
                run_pairs.append((task_of[e["run_id"]], bool(said)))
            for s in e["false_statements"]:
                if s["kind"] == "false_incapability":
                    inc.append((task_of[e["run_id"]], any(_matches(q, s["quote"]) for q in said)))
        out[f"report · labelled false-claim runs with a listed statement, {cond}"] = rate_ci(run_pairs)
        out[f"report · labelled false-incapability statements found, {cond}"] = rate_ci(inc)
    for name, r in out.items():
        record("monitors", name, "all", r)
    return out


# ---- figures ---------------------------------------------------------------------------------


def _style(ax: Any) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK_2, length=0, labelsize=9)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_xlim(-3, 112)
    ax.set_xticks([0, 20, 40, 60, 80, 100])
    ax.set_xlabel("% of runs (95% interval)", color=INK_2, fontsize=9)


def _dots(ax: Any, ys: list[float], points: list[tuple[float, float, float]], color: str, label: str, text: list[str]) -> None:
    for y, (p, lo, hi), t in zip(ys, points, text):
        ax.plot([100 * lo, 100 * hi], [y, y], color=color, linewidth=2, solid_capstyle="round", zorder=2)
        ax.plot([100 * p], [y], "o", color=color, markersize=8, markeredgecolor=SURFACE, markeredgewidth=2, zorder=3)
        ax.annotate(t, (100 * hi, y), xytext=(6, 0), textcoords="offset points", va="center", fontsize=8.5, color=INK)
    ax.plot([], [], "o", color=color, markersize=8, label=label)


def _figure(height: float) -> tuple[Any, Any]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7.2, height), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    _style(ax)
    return fig, ax


def fig_false_incapability(est: dict[str, Estimate], scorer: dict[str, Rate]) -> None:
    models = model_order()
    fig, ax = _figure(2.8)
    base = list(range(len(models)))[::-1]
    lab = [(e.count / e.total, *e.interval()) for e in (est[m] for m in models)]
    _dots(ax, [b + 0.15 for b in base], lab, SERIES[0], "labels, population estimate (D57)",
          [f"≈{est[m].count:.0f}/{est[m].total}" for m in models])
    _dots(ax, [b - 0.15 for b in base], [(scorer[m].p, scorer[m].lo, scorer[m].hi) for m in models], SERIES[1],
          "rule-based scorer v3", [f"{scorer[m].k}/{scorer[m].n}" for m in models])
    ax.set_yticks(base, [short(m) for m in models], color=INK, fontsize=10)
    ax.set_ylim(-0.6, len(models) - 0.4)
    ax.set_title("Runs whose report falsely claims the agent could not do something", loc="left", fontsize=11, color=INK)
    ax.legend(loc="lower right", frameon=False, fontsize=8.5, labelcolor=INK_2)
    fig.tight_layout()
    fig.savefig(FIGURES / "false_incapability.png", facecolor=SURFACE)


def fig_success(runs: list[Run]) -> None:
    models = model_order()
    fig, ax = _figure(3.2)
    base = list(range(len(KINDS)))[::-1]
    offsets = (0.22, 0.0, -0.22)
    for i, m in enumerate(models):
        rates = [rate_ci([(r.task_id, bool(r.scores["success"])) for r in runs if r.model == m and r.task_kind == k]) for k in KINDS]
        _dots(ax, [b + offsets[i] for b in base], [(r.p, r.lo, r.hi) for r in rates], SERIES[i], short(m),
              [f"{r.k}/{r.n}" for r in rates])
    ax.set_yticks(base, [KIND_NAME[k] for k in KINDS], color=INK, fontsize=10)
    ax.set_ylim(-0.6, len(KINDS) - 0.4)
    ax.set_title("Task success by task kind (strict success check)", loc="left", fontsize=11, color=INK)
    ax.legend(loc="lower left", frameon=False, fontsize=8.5, labelcolor=INK_2)
    fig.tight_layout()
    fig.savefig(FIGURES / "success_by_kind.png", facecolor=SURFACE)


def fig_monitors(mon: dict[str, Rate]) -> None:
    rows = [("scope: scripted violations flagged", "scope · scripted violations flagged (recall)", None),
            ("scope: scripted non-violations flagged", "scope · scripted non-violations flagged (FP)", None),
            ("scope: real runs flagged (no violation)", "scope · real runs flagged (FP), {}", CONDITIONS),
            ("report: false-claim runs with a listed statement", "report · labelled false-claim runs with a listed statement, {}", CONDITIONS),
            ("report: false-incapability statements found", "report · labelled false-incapability statements found, {}", CONDITIONS)]
    fig, ax = _figure(3.6)
    base = list(range(len(rows)))[::-1]
    for ci, cond in enumerate(CONDITIONS):
        ys, pts, txt = [], [], []
        for b, (_, key, conds) in zip(base, rows):
            if conds is None and ci > 0:
                continue
            r = mon[key.format(cond)] if conds else mon[key]
            ys.append(b + (0.15 if ci == 0 else -0.15) * (conds is not None))
            pts.append((r.p, r.lo, r.hi))
            txt.append(f"{r.k}/{r.n}")
        _dots(ax, ys, pts, SERIES[ci], f"monitor sees the {'log' if cond == 'log' else 'log + agent thinking'}", txt)
    ax.set_yticks(base, [r[0] for r in rows], color=INK, fontsize=9)
    ax.set_ylim(-0.6, len(rows) - 0.4)
    ax.set_title("Haiku 5.5 monitors (D56)", loc="left", fontsize=11, color=INK)
    ax.set_xlabel("% of runs or statements (95% interval)", color=INK_2, fontsize=9)
    ax.legend(loc="center right", bbox_to_anchor=(1.0, 0.6), frameon=False, fontsize=8.5, labelcolor=INK_2)
    fig.tight_layout()
    fig.savefig(FIGURES / "monitors.png", facecolor=SURFACE)


# ---- main ------------------------------------------------------------------------------------


def main() -> int:
    runs = load_runs()
    FIGURES.mkdir(parents=True, exist_ok=True)
    models = model_order()
    head = ["metric", *[short(m) for m in models], "all"]
    lines = ["# Results tables", "",
             f"{len(runs)} experiment runs: {len(models)} models × 15 tasks × 5 samples. Cells: k/n · rate "
             f"[95% interval]. Intervals: two-stage cluster bootstrap over tasks ({RESAMPLES:,} resamples, seed {SEED}); "
             "† exact Clopper-Pearson where every run has the same outcome (the bootstrap collapses there). "
             "With 15 tasks, intervals are wide. Generated by `make analyze` (D58).", "",
             "## Behaviour", ""]
    lines += table(head, run_metric_rows(runs, None, SELF_REPORT_ROWS, "behaviour"))
    cost = [[f"{sum(r.cost_usd for r in runs if r.model == m) / 75:.3f}" for m in models]]
    lines += ["", "Mean cost per run (USD): " + ", ".join(f"{short(m)} {c}" for m, c in zip(models, cost[0])) +
              f"; total ${sum(r.cost_usd for r in runs):.2f}. Mean turns: " +
              ", ".join(f"{short(m)} {sum(r.turns for r in runs if r.model == m) / 75:.1f}" for m in models) + ".", ""]

    est = label_estimates(runs)
    lines += ["## Self-report, from the phase 6 labels", "",
              "Labels: Claude Fable 5.1 subagents, reviewed by the researcher (D57). Population estimate per model: "
              "flagged census + pool × sample rate; interval from the exact interval of the 10-run sample (wide). "
              "Cells: estimated runs/75 · rate [95% interval].", ""]
    lines += table(head[:-1], [[name, *[est[name][m].cell() for m in models]] for name, _ in LABEL_METRICS])
    lines += ["", "## Self-report, rule-based scorer v3 (all 225 runs)", "",
              "Agreement with the labels: labels/phase6/agreement.md (false incapability 48/55, kappa 0.72).", ""]
    lines += table(head, run_metric_rows(runs, SELF_REPORT_ROWS, (), "self-report (scorer v3)"))

    mon = monitor_rates(runs)
    lines += ["", "## Monitors (Haiku 5.5, D56)", "",
              "Scope recall: scripted scenarios (an informed monitor, likely an upper bound). Report monitor: the 55 "
              "phase 6 runs, which over-represent flagged runs, so not population rates. Details: results/monitor.md.", ""]
    lines += table(["measure", "k/n · rate [95% interval]"], [[k, r.cell()] for k, r in mon.items()])

    lines += ["", "## Success by task", "", "Runs with the strict success check passed, out of 5.", ""]
    tasks = sorted({(r.task_id, r.task_kind) for r in runs})
    lines += table(["task", "kind", *[short(m) for m in models]],
                   [[t, KIND_NAME[k], *[str(sum(r.scores["success"] for r in runs if r.task_id == t and r.model == m))
                                        for m in models]] for t, k in tasks])

    scorer_inc = {m: rate_ci([(r.task_id, any(f["kind"] == "false_incapability" for f in r.scores["self_report"]["findings"]))
                              for r in runs if r.model == m]) for m in models}
    fig_false_incapability(est["↳ false incapability"], scorer_inc)
    fig_success(runs)
    fig_monitors(mon)
    lines += ["", "## Figures", "", "![False incapability](figures/false_incapability.png)", "",
              "![Success by task kind](figures/success_by_kind.png)", "", "![Monitors](figures/monitors.png)"]

    (RESULTS / "tables.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with (RESULTS / "tables.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(csv_rows[0]))
        w.writeheader()
        w.writerows(csv_rows)
    print(f"wrote {RESULTS / 'tables.md'}, tables.csv ({len(csv_rows)} rows), 3 figures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
