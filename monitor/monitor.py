"""Haiku monitors over the agent's action log (D39, D56). No access to diffs, snapshots,
checks or scores: only what monitor/render.py shows.

  scope   should a human review this run because something outside the scope changed?
          Real experiment runs (false positives) and the scripted validation scenarios (recall).
  report  which final-report statements does the log contradict (false incapability claims and
          others)? The runs hand-labelled in labels/phase6 (D54).

Both monitors run in both conditions of D39 where the log has thinking: `log` and
`log+thinking`. Scripted scenarios have no thinking, so they run in `log` only.

  uv run python -m monitor.monitor estimate        # exact input tokens (count_tokens, free) and the cost
  uv run python -m monitor.monitor run --yes       # API spend in namespace "monitor", capped in config
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import anthropic
import yaml


from monitor import scripted
from monitor.render import CONDITIONS, load_events, render
from runner.budget import BudgetExceeded, Ledger, Usage
from runner.cache import ResponseCache
from runner.config import load_config, load_pricing
from runner.run import LEDGER, RUNS_DIR, load_dotenv
from runner.sandbox import REPO

NAMESPACE = "monitor"
PROMPTS = REPO / "monitor" / "prompts"
MAX_TOKENS = 8000
LONG_PROMPT_TOKENS = 100_000      # Haiku 5.5 bills prompts above this at 5x (config/pricing.yaml)
LABELS_PHASE6 = REPO / "labels" / "phase6" / "labels.yaml"

CATEGORY = {"type": "string", "enum": ["benign", "circumvention", "destructive"]}
SCHEMAS: dict[str, dict[str, Any]] = {
    "scope": {
        "type": "object",
        "properties": {
            "flag": {"type": "boolean"},
            "changes": {"type": "array", "items": {
                "type": "object",
                "properties": {"step": {"type": "integer"}, "what": {"type": "string"}, "category": CATEGORY},
                "required": ["step", "what", "category"], "additionalProperties": False}},
            "reason": {"type": "string"},
        },
        "required": ["flag", "changes", "reason"], "additionalProperties": False,
    },
    "report": {
        "type": "object",
        "properties": {
            "statements": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "quote": {"type": "string"},
                    "step": {"type": "integer"},
                    "explanation": {"type": "string"},
                },
                "required": ["quote", "step", "explanation"], "additionalProperties": False}},
        },
        "required": ["statements"], "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class Item:
    monitor: str          # scope | report
    condition: str        # log | log+thinking
    source: str           # experiment | scripted
    path: Path            # run directory or scenario directory

    @property
    def out(self) -> Path:
        return self.path / "monitor" / f"{self.monitor}__{self.condition.replace('+', '_')}.json"

    @property
    def name(self) -> str:
        return f"{self.monitor}/{self.condition}/{self.path.name}"


def experiment_runs() -> list[Path]:
    out = []
    for meta in sorted(RUNS_DIR.glob("2*Z-*/meta.json")):
        if json.loads(meta.read_text(encoding="utf-8")).get("namespace") == "experiment":
            out.append(meta.parent)
    return out


def items(report_on_all: bool = False) -> list[Item]:
    runs = experiment_runs()
    labelled = {e["run_id"] for e in yaml.safe_load(LABELS_PHASE6.read_text(encoding="utf-8"))["runs"]}
    out = [Item("scope", c, "experiment", d) for d in runs for c in CONDITIONS]
    out += [Item("report", c, "experiment", d) for d in runs if report_on_all or d.name in labelled for c in CONDITIONS]
    out += [Item("scope", "log", "scripted", d) for d in scripted.scenario_dirs()]
    return out


def view(item: Item) -> str:
    return render((item.path / "system_prompt.txt").read_text(encoding="utf-8"),
                  (item.path / "user_message.txt").read_text(encoding="utf-8"), load_events(item.path), item.condition)


def request(item: Item) -> dict[str, Any]:
    cfg = load_config().monitor
    return {
        "model": cfg.model,
        "max_tokens": MAX_TOKENS,
        "system": (PROMPTS / f"{item.monitor}.md").read_text(encoding="utf-8").strip(),
        "messages": [{"role": "user", "content": view(item)}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": cfg.effort, "format": {"type": "json_schema", "schema": SCHEMAS[item.monitor]}},
    }


def _price_factor(input_tokens: int) -> float:
    return 5.0 if input_tokens > LONG_PROMPT_TOKENS else 1.0


# ---- estimate (free) ------------------------------------------------------------------------


def estimate(report_on_all: bool, output_tokens: tuple[int, int]) -> int:
    load_dotenv()
    client = anthropic.Anthropic(max_retries=4)
    todo = items(report_on_all)
    price = load_pricing()[load_config().monitor.model]

    def count(item: Item) -> tuple[Item, int]:
        req = request(item)
        resp = client.messages.count_tokens(model=req["model"], system=req["system"], messages=req["messages"],
                                            thinking=req["thinking"], output_config=req["output_config"])
        return item, resp.input_tokens

    with ThreadPoolExecutor(max_workers=8) as pool:
        counted = list(pool.map(count, todo))
    groups: dict[tuple[str, str, str], list[int]] = {}
    for item, n in counted:
        groups.setdefault((item.monitor, item.source, item.condition), []).append(n)
    lines = ["| monitor | runs | condition | calls | input tokens (mean / max) | cost, low / high output |", "|---|---|---|---|---|---|"]
    total = [0.0, 0.0]
    for (mon, src, cond), ns in sorted(groups.items()):
        costs = []
        for out_tokens in output_tokens:
            costs.append(sum((n * price.input + out_tokens * price.output) * _price_factor(n) for n in ns) / 1e6)
        total = [total[0] + costs[0], total[1] + costs[1]]
        lines.append(f"| {mon} | {src} | {cond} | {len(ns)} | {sum(ns) / len(ns):,.0f} / {max(ns):,} | "
                     f"${costs[0]:.2f} / ${costs[1]:.2f} |")
    lines.append(f"| **total** | | | {len(counted)} | {sum(n for _, n in counted):,} in all | "
                 f"**${total[0]:.2f} / ${total[1]:.2f}** |")
    lines += ["", f"Output tokens per call (thinking at effort `{load_config().monitor.effort}` + JSON), assumed: "
              f"{output_tokens[0]} (low) and {output_tokens[1]} (high). Prices: config/pricing.yaml "
              f"(${price.input}/MTok input, ${price.output}/MTok output; prompts over 100K tokens at 5x: "
              f"{sum(1 for _, n in counted if n > LONG_PROMPT_TOKENS)} calls)."]
    print("\n".join(lines))
    return 0


# ---- run (API spend) ----------------------------------------------------------------------


def run(report_on_all: bool, workers: int) -> int:
    cfg = load_config()
    budget = cfg.budget
    price = load_pricing()[cfg.monitor.model]
    ledger = Ledger(LEDGER)
    cache = ResponseCache(REPO / cfg.cache.dir)
    load_dotenv()
    client = anthropic.Anthropic(max_retries=4)
    todo = [i for i in items(report_on_all) if not i.out.exists()]
    lock = Lock()
    print(f"monitor: {len(todo)} calls to make; namespace left ${ledger.usd_left(budget, NAMESPACE):.2f}")

    def one(item: Item) -> str:
        req = request(item)
        key = cache.key(req, 0, NAMESPACE)
        hit = cache.get(key)
        if hit is None:
            with lock:
                ledger.check_can_start(budget, NAMESPACE)
                if ledger.usd_left(budget, NAMESPACE) < 0.05:
                    raise BudgetExceeded("monitor namespace cap reached")
            resp = client.messages.create(**req)
            data, request_id = resp.to_dict(), getattr(resp, "_request_id", None)
            cache.put(key, data, request_id)
        else:
            data, request_id = hit["response"], hit.get("request_id")
        usage = Usage.from_response(data["usage"])
        cost = usage.cost_usd(price) * _price_factor(usage.input_tokens + usage.cache_read_input_tokens
                                                     + usage.cache_creation_input_tokens)
        text = next((b["text"] for b in data["content"] if b["type"] == "text"), "")
        try:
            parsed: Any = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        record = {"monitor": item.monitor, "condition": item.condition, "source": item.source, "target": item.path.name,
                  "model": data.get("model"), "stop_reason": data.get("stop_reason"), "stop_details": data.get("stop_details"),
                  "request_id": request_id, "request_sha256": key, "from_cache": hit is not None,
                  "usage": usage.as_dict(), "cost_usd": round(cost if hit is None else 0.0, 6),
                  "output": parsed, "raw_text": text if parsed is None else None,
                  "thinking": [b.get("thinking", "") for b in data["content"] if b["type"] == "thinking"]}
        item.out.parent.mkdir(exist_ok=True)
        item.out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        if hit is None:
            with lock:
                ledger.append({"run_id": f"{item.name}", "namespace": NAMESPACE, "model": data.get("model"),
                               "cost_usd_spent": record["cost_usd"], "total_tokens": usage.total_tokens,
                               "t": datetime.now(timezone.utc).isoformat()})
        # Progress shows no monitor output: outputs on hand-labelled runs stay hidden until labelling (D56).
        return f"{item.monitor}/{item.condition}/{item.source} {'ok' if parsed is not None else 'UNPARSED'} ${record['cost_usd']:.4f}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for n, line in enumerate(pool.map(one, todo), 1):
            print(f"  [{n}/{len(todo)}] {line}", flush=True)
    print(f"done; namespace spent ${ledger.spent_usd(NAMESPACE):.2f}")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="monitor.monitor")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("estimate")
    e.add_argument("--report-on-all", action="store_true", help="report monitor on all 225 runs, not only the labelled ones")
    e.add_argument("--output-tokens", type=int, nargs=2, default=(600, 2000))
    r = sub.add_parser("run")
    r.add_argument("--report-on-all", action="store_true")
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--yes", action="store_true", help="confirm API spend")
    args = p.parse_args(argv)
    if args.cmd == "estimate":
        return estimate(args.report_on_all, tuple(args.output_tokens))
    if not args.yes:
        sys.exit("monitor run spends API budget: pass --yes after the estimate is approved")
    return run(args.report_on_all, args.workers)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
