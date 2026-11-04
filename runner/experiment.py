"""The full experiment (D53): every task x model x sample, in one order shuffled with
`sampling.task_order_seed`, namespace "experiment".

  uv run python -m runner.experiment [--workers 3] [--dry-plan]

- Resumable: runs already in runs/ for this namespace (meta.json) are skipped, so an
  interrupted experiment continues where it stopped.
- Budget: each in-flight run reserves RESERVE_USD; a run starts only if what is left of the
  namespace's and the global cap covers the reservations of the runs in flight plus its own,
  and it may spend at most its reservation (the agent loop stops at that point). The cap is
  therefore never passed, whatever the number of workers.
- Infrastructure failures (sandbox errors, crashes before a run is written) are retried once
  and logged in runs/experiment_failures.jsonl. API errors inside a run are part of the run
  (end_reason api_error) and are not retried.
- The tree must stay clean for the whole experiment (every run records the exact commit).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from runner.budget import BudgetExceeded, Ledger
from runner.config import ExperimentConfig, load_config, load_pricing
from runner.run import LEDGER, RUNS_DIR, run_episode
from tasks.schema import TaskBundle, load_all

NAMESPACE = "experiment"
RESERVE_USD = 0.75          # more than any run can cost (Opus near the 400K-token cap is about $0.52)
FAILURES = RUNS_DIR / "experiment_failures.jsonl"
PROGRESS = RUNS_DIR / "experiment_progress.log"


def plan(cfg: ExperimentConfig, bundles: list[TaskBundle]) -> list[tuple[str, str, int]]:
    items = [(b.task.id, model, s) for b in bundles for model in cfg.agent.allowed_models()
             for s in range(cfg.sampling.samples_per_task)]
    random.Random(cfg.sampling.task_order_seed).shuffle(items)
    return items


def done_runs() -> set[tuple[str, str, int]]:
    out = set()
    for meta_path in RUNS_DIR.glob("*/meta.json"):
        try:
            m = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if m.get("namespace") == NAMESPACE:
            out.add((m["task_id"], m["model"], int(m["sample_idx"])))
    return out


def log(line: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with PROGRESS.open("a", encoding="utf-8") as fh:
        fh.write(f"{stamp} {line}\n")
    print(f"{stamp} {line}", flush=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="runner.experiment")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--dry-plan", action="store_true", help="print the plan and what is left, run nothing")
    args = parser.parse_args(argv)

    cfg, pricing = load_config(), load_pricing()
    bundles = {b.task.id: b for b in load_all()}
    items = plan(cfg, list(bundles.values()))
    todo = [it for it in items if it not in done_runs()]
    ledger = Ledger(LEDGER)
    log(f"plan: {len(items)} runs, {len(items) - len(todo)} already done, {len(todo)} to go; "
        f"namespace left ${ledger.usd_left(cfg.budget, NAMESPACE):.2f}")
    if args.dry_plan:
        for it in todo[:20]:
            print("  ", *it)
        return 0

    from runner.summary_line import summary_line  # noqa: PLC0415 - avoid a CLI import cycle

    lock = threading.Lock()
    state = {"inflight": 0, "stopped": False}

    def reserve() -> float | None:
        with lock:
            if state["stopped"]:
                return None
            left = ledger.usd_left(cfg.budget, NAMESPACE) - RESERVE_USD * state["inflight"]
            if left < RESERVE_USD:
                state["stopped"] = True
                log(f"budget: ${left + RESERVE_USD * state['inflight']:.2f} left, below what the next run may need; stopping")
                return None
            state["inflight"] += 1
            return RESERVE_USD

    def release() -> None:
        with lock:
            state["inflight"] -= 1

    def work(item: tuple[str, str, int]) -> None:
        task_id, model, sample = item
        allowance = reserve()
        if allowance is None:
            return
        try:
            for attempt in (1, 2):
                try:
                    outcome = run_episode(bundles[task_id], cfg, pricing, model, sample_idx=sample, namespace=NAMESPACE,
                                          usd_left=allowance)
                    log(summary_line(outcome.meta) + f"  sample={sample}")
                    return
                except BudgetExceeded as exc:
                    with lock:
                        state["stopped"] = True
                    log(f"budget: {exc}")
                    return
                except Exception as exc:  # noqa: BLE001 - infrastructure failure: retry once, then record
                    with FAILURES.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"task": task_id, "model": model, "sample": sample, "attempt": attempt,
                                             "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-2000:],
                                             "t": datetime.now(timezone.utc).isoformat()}) + "\n")
                    log(f"FAILED {task_id} {model} s{sample} attempt {attempt}: {type(exc).__name__}: {str(exc)[:200]}")
                    time.sleep(5)
        finally:
            release()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(work, todo))
    left = [it for it in items if it not in done_runs()]
    log(f"finished: {len(items) - len(left)}/{len(items)} runs done; spent in namespace ${ledger.spent_usd(NAMESPACE):.2f}")
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
