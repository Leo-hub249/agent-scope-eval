"""Command line entry points.

  python -m runner.cli show-prompt --task T02     # what the agent will see (no API call)
  python -m runner.cli dry-run --task T02         # one real run (needs ANTHROPIC_API_KEY)
  python -m runner.cli show-run runs/<run_id>     # readable transcript, usage and cost
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from runner.agent import BASH_TOOL
from runner.summary_line import summary_line
from runner.config import load_config, load_pricing
from runner.prompts import render_system_prompt, render_user_message
from tasks.schema import TaskBundle, load_all


def _bundle(task_id: str) -> TaskBundle:
    for b in load_all():
        if b.task.id == task_id:
            return b
    sys.exit(f"unknown task {task_id}")


def show_prompt(task_id: str) -> None:
    cfg = load_config()
    task = _bundle(task_id).task
    print("=== SYSTEM PROMPT ===")
    print(render_system_prompt(task, cfg.limits))
    print("\n=== FIRST USER MESSAGE ===")
    print(render_user_message(task))
    print("\n=== REQUEST PARAMETERS ===")
    print(json.dumps({
        "model": cfg.agent.model, "max_tokens": cfg.agent.max_tokens_per_response, "tools": [BASH_TOOL],
        "thinking": {"type": "adaptive", "display": cfg.agent.thinking_display},
        "output_config": {"effort": cfg.agent.effort}, "cache_control": {"type": "ephemeral"},
        "temperature": "not sent (rejected by API version 2026-10-21)",
    }, indent=2))


def _short(text: str, limit: int = 4000) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n[... {len(text) - limit} more characters in transcript.jsonl ...]"


def show_run(run_dir: Path) -> None:
    meta = json.loads((run_dir / "meta.json").read_text())
    print(f"run {meta['run_id']}  task {meta['task_id']}  model {meta['model']} -> {', '.join(meta['response_models'])}")
    print(f"commit {meta['provenance']['git_commit'][:12]} dirty={meta['provenance']['git_dirty']}  image {meta['provenance']['image_id_container'][:19]}")
    print(f"limits {meta['limits']}  effort {meta['parameters']['effort']}  temperature {meta['parameters']['temperature']}")
    print("=" * 100)
    for line in (run_dir / "transcript.jsonl").read_text().splitlines():
        ev = json.loads(line)
        kind = ev["event"]
        if kind == "response":
            u = ev["usage"]
            print(f"\n--- turn {ev['turn']}  model response  stop={ev['stop_reason']}  in={u['input_tokens']} "
                  f"cache_w={u['cache_creation_input_tokens']} cache_r={u['cache_read_input_tokens']} out={u['output_tokens']}"
                  f"{'  (from cache)' if ev['from_cache'] else ''}")
            for block in ev["content"]:
                if block["type"] == "thinking":
                    print(f"  [thinking, summarized] {_short(block.get('thinking', '') or '(empty)', 1500)}")
                elif block["type"] == "text":
                    print(f"  [text] {block['text']}")
                elif block["type"] == "tool_use":
                    inp = block["input"]
                    print(f"  [bash] $ {inp.get('command') if 'command' in inp else inp}")
        elif kind == "tool_result":
            flags = []
            if ev["timed_out"]:
                flags.append("TIMEOUT")
            if ev["truncated_for_model"]:
                flags.append(f"truncated for model ({ev['output_bytes']} bytes)")
            print(f"  [result] exit={ev['exit_code']} {ev['duration_s']}s {' '.join(flags)}")
            print("    " + _short(ev["output"], 3000).replace("\n", "\n    "))
            obs = ev.get("observation") or {}
            if obs.get("fs_changes"):
                print(f"    [observed fs changes] {obs['fs_changes']}")
        elif kind in ("api_error", "budget_stop", "max_turns"):
            print(f"\n!!! {kind}: {ev}")
    print("\n" + "=" * 100)
    a, r = meta["agent"], meta["results"]
    print(f"end_reason={a['end_reason']} turns={a['turns']} tool_calls={a['tool_calls']} cache_hits={a['cache_hits']}")
    print(f"usage {meta['usage']}")
    print(f"cost spent ${meta['cost_usd_spent']:.4f}  (if uncached ${meta['cost_usd_if_uncached']:.4f})")
    print(summary_line(meta))
    for v in r["violations"]:
        print(f"  violation: {v['domain']}:{v['kind']} {v['target']} {v['detail']}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="runner")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("show-prompt")
    p.add_argument("--task", required=True)
    d = sub.add_parser("dry-run")
    d.add_argument("--task", required=True)
    d.add_argument("--model")
    d.add_argument("--sample", type=int, default=0)
    d.add_argument("--allow-dirty", action="store_true")
    s = sub.add_parser("show-run")
    s.add_argument("run_dir", type=Path)
    pl = sub.add_parser("pilot")
    pl.add_argument("--tasks", nargs="+", required=True)
    pl.add_argument("--model")
    sub.add_parser("calibrate")
    su = sub.add_parser("summary")
    su.add_argument("run_dirs", type=Path, nargs="+")
    args = parser.parse_args(argv)

    if args.cmd == "show-prompt":
        show_prompt(args.task)
    elif args.cmd == "show-run":
        show_run(args.run_dir)
    elif args.cmd == "summary":
        for d in args.run_dirs:
            print(summary_line(json.loads((d / "meta.json").read_text())))
    elif args.cmd == "pilot":
        from runner.run import run_episode

        cfg = load_config()
        dirs = []
        for task_id in args.tasks:
            outcome = run_episode(_bundle(task_id), cfg, load_pricing(), args.model or cfg.agent.model,
                                  sample_idx=0, namespace="pilot")
            dirs.append(outcome.run_dir)
            print(summary_line(outcome.meta), flush=True)
        print("runs:", *[str(d) for d in dirs], sep="\n  ")
    elif args.cmd == "calibrate":
        from runner.budget import BudgetExceeded
        from runner.run import run_episode

        cfg = load_config()
        dirs = []
        for item in cfg.calibration:
            try:
                outcome = run_episode(_bundle(item.task), cfg, load_pricing(), item.model, sample_idx=0,
                                      namespace="calibration")
            except BudgetExceeded as exc:
                print(f"stopped: {exc}")
                break
            dirs.append(outcome.run_dir)
            print(summary_line(outcome.meta), flush=True)
        print("runs:", *[str(d) for d in dirs], sep="\n  ")
    elif args.cmd == "dry-run":
        from runner.run import run_episode

        cfg = load_config()
        outcome = run_episode(
            _bundle(args.task), cfg, load_pricing(), args.model or cfg.agent.model,
            sample_idx=args.sample, namespace="dry-run", allow_dirty=args.allow_dirty,
        )
        print(f"run written to {outcome.run_dir}")
        show_run(outcome.run_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
