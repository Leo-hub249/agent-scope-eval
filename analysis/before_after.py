"""D54: every metric before and after the post-experiment scorer fix (no API, no Docker).

  uv run python -m analysis.before_after   # -> results/scoring/d54_before_after.md
"""

from __future__ import annotations

import sys

from analysis.metrics import REPO, RUN_METRICS, by_model, finding_totals, fmt, load_runs, load_snapshot, ratio, short

SNAPSHOT = REPO / "results" / "scoring" / "experiment_scores_v2.jsonl"
OUT = REPO / "results" / "scoring" / "d54_before_after.md"


def main() -> int:
    after = load_runs()
    before = load_snapshot(SNAPSHOT)
    assert {r.run_id for r in after} == {r.run_id for r in before}, "snapshot and runs differ"
    versions = {r.scores["scoring_version"] for r in after}
    b, a = by_model(before), by_model(after)
    cols = list(a)
    lines = ["# D54: metrics before and after the scorer fix", "",
             f"{len(after)} experiment runs. Before: scoring v2 (frozen in `{SNAPSHOT.relative_to(REPO)}`); "
             f"after: scoring v{','.join(map(str, versions))} (`runs/*/scores.json`). Cells are runs, k/n; "
             "**bold** marks a change.", "",
             "| metric | " + " | ".join(f"{short(c)} before → after" if c != "all" else "all before → after" for c in cols) + " |",
             "|---|" + "---|" * len(cols)]
    for label, pred, within in RUN_METRICS:
        cells = []
        for c in cols:
            x, y = fmt(*ratio(b[c], pred, within)), fmt(*ratio(a[c], pred, within))
            cells.append(x if x == y else f"**{x} → {y}**")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines += ["", "Findings (not runs) by kind:", "", "| kind | " + " | ".join(short(c) if c != "all" else "all" for c in cols) + " |",
              "|---|" + "---|" * len(cols)]
    kinds = sorted(set(finding_totals(before)) | set(finding_totals(after)))
    for k in kinds:
        cells = []
        for c in cols:
            x, y = finding_totals(b[c]).get(k, 0), finding_totals(a[c]).get(k, 0)
            cells.append(str(x) if x == y else f"**{x} → {y}**")
        lines.append(f"| {k} | " + " | ".join(cells) + " |")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
