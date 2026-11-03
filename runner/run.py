"""Run one agent episode end to end and write runs/<run_id>/.

  meta.json            model, parameters (temperature etc.), limits, budget, provenance
                       (git commit, image digest, config hash), timestamps, usage, cost, results
  transcript.jsonl     every request/response/tool call/tool result/observation
  system_prompt.txt    exactly what the agent saw
  user_message.txt
  facts.json           hidden setup facts (never shown to the agent)
  baseline.json        snapshot after setup, before the agent
  final.json           snapshot after the agent
  docker_diff_*.json   whole-filesystem path diffs at baseline and at the end
  check.json           hidden success check result
  scope.json           changes classified as violation / allowed / benign
  scores.json          all scores, recomputable offline with `python -m scoring.score`
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic

from runner.agent import BASH_TOOL, Client, Transcript, cost, now, run_agent
from runner.bash_session import BashSession
from runner.budget import Ledger
from runner.cache import ResponseCache
from runner.config import ExperimentConfig, Price, config_sha256
from runner.episode import finalize, prepare, score
from runner.image_lock import verify as verify_image
from runner.prompts import render_system_prompt, render_user_message
from runner.provenance import git_commit, git_dirty
from runner.sandbox import REPO, Sandbox, new_project_name
from scoring.awareness import scan_transcript
from scoring.report import parse_report
from scoring.score import score_run
from tasks.schema import TaskBundle

RUNS_DIR = REPO / "runs"
LEDGER = RUNS_DIR / "ledger.jsonl"
MAX_RETRIES = 4
SAMPLING_NOTE = (
    "temperature/top_p/top_k are not sent: API version 2026-10-21 (sent by anthropic>=2.0) rejects them on "
    "every model, so sampling uses the API defaults; variance is measured with repeated samples"
)


class DirtyTreeError(RuntimeError):
    pass


def load_dotenv(path: Path = REPO / ".env") -> None:
    """Minimal .env reader (KEY=VALUE lines); never overrides variables already set."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _data_seed() -> int | None:
    m = re.search(r"^SEED = (\d+)", (REPO / "sandbox" / "db" / "generate_seed.py").read_text(), re.M)
    return int(m.group(1)) if m else None


@dataclass
class RunOutcome:
    run_dir: Path
    meta: dict[str, Any]


STEP_MARKER = "/run/.sbx_step_marker"
FIND_CHANGED = (
    "find / -xdev \\( -path /proc -o -path /sys -o -path /dev -o -path /run -o -path /var/lib/postgresql \\) -prune "
    f"-o -cnewer {STEP_MARKER} -print 2>/dev/null; touch {STEP_MARKER}"
)


def _observer(box: Sandbox) -> tuple[Any, list[tuple[str, str]]]:
    """Per-step observation after each tool call: paths whose content or metadata changed
    during the step (ctime newer than a marker reset after every step; catches files that
    setup had already modified, which `docker diff` cannot), paths deleted (new `D`
    entries in `docker diff`), and supervisord program states."""
    box.exec(["touch", STEP_MARKER], user="root")
    seen = set(box.changed_paths())

    def observe() -> dict[str, Any]:
        nonlocal seen
        changed = [p for p in box.exec(["sh", "-c", FIND_CHANGED], user="root").stdout.splitlines() if p]
        changed_set = set(changed)
        leaves = sorted(p for p in changed if not any(o.startswith(p.rstrip("/") + "/") for o in changed_set if o != p))
        current = set(box.changed_paths())
        deleted = sorted(p for k, p in current - seen if k == "D")
        seen = current
        status = box.exec(["supervisorctl", "status"], user="root").stdout
        services = {p[0]: " ".join(p[1:]) for p in (ln.split() for ln in status.splitlines()) if p}
        return {"fs_changes": leaves, "fs_deleted": deleted, "services": services}

    return observe, sorted(seen)


def run_episode(
    bundle: TaskBundle,
    cfg: ExperimentConfig,
    pricing: dict[str, Price],
    model: str,
    sample_idx: int,
    namespace: str,
    allow_dirty: bool = False,
    client: Client | None = None,
    ledger: Ledger | None = None,
    runs_dir: Path = RUNS_DIR,
    cache: ResponseCache | None = None,
) -> RunOutcome:
    task = bundle.task
    if model not in cfg.agent.allowed_models():
        raise ValueError(f"model {model} is not in config/experiment.yaml")
    if model not in pricing:
        raise ValueError(f"no pricing for {model} in config/pricing.yaml")
    dirty = git_dirty()
    if dirty and not allow_dirty:
        raise DirtyTreeError("uncommitted changes: commit first so the run records an exact commit (or --allow-dirty)")
    ledger = ledger or Ledger(LEDGER)
    ledger.check_can_start(cfg.budget, namespace)
    if client is None:
        load_dotenv()
        client = anthropic.Anthropic(max_retries=MAX_RETRIES)

    started = datetime.now(timezone.utc)
    run_id = f"{started:%Y%m%dT%H%M%SZ}-{task.id}-{model}-s{sample_idx}"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    system_prompt = render_system_prompt(task, cfg.limits)
    user_message = render_user_message(task)
    (run_dir / "system_prompt.txt").write_text(system_prompt + "\n", encoding="utf-8")
    (run_dir / "user_message.txt").write_text(user_message + "\n", encoding="utf-8")
    transcript = Transcript(run_dir / "transcript.jsonl")
    image_locked = verify_image()

    with Sandbox(new_project_name("run"), expected_image_id=image_locked) as box:
        prepared = prepare(bundle, box)
        transcript.write("run_start", run_id=run_id, task_id=task.id, model=model, sample_idx=sample_idx)
        observe, _ = _observer(box)
        shell = BashSession(box.container_id, timeout_s=cfg.limits.command_timeout_s)
        shell.start()
        agent_started = now()
        try:
            agent = run_agent(
                client=client, cache=cache or ResponseCache(REPO / cfg.cache.dir), shell=shell, transcript=transcript,
                system_prompt=system_prompt, user_message=user_message, agent_cfg=cfg.agent, limits=cfg.limits,
                model=model, sample_idx=sample_idx, namespace=namespace, observe=observe,
                usd_left=ledger.usd_left(cfg.budget, namespace), price=pricing[model],
            )
        finally:
            shell.close()
        agent_ended = now()
        finalized = finalize(bundle, prepared)
        scored = score(bundle, prepared, finalized, agent.final_text)
        image_container = box.image_id

    report = parse_report(agent.final_text)
    awareness = scan_transcript(run_dir / "transcript.jsonl")
    price = pricing[model]
    meta: dict[str, Any] = {
        "run_id": run_id,
        "namespace": namespace,
        "task_id": task.id,
        "task_kind": task.kind,
        "sample_idx": sample_idx,
        "task_files_sha256": {
            "task.yaml": _sha256((bundle.directory / "task.yaml").read_text()),
            "setup.sh": _sha256(bundle.setup_script),
            "check.py": _sha256(bundle.check_script),
        },
        "model": model,
        "response_models": sorted(set(agent.response_models)),
        "parameters": {
            "max_tokens_per_response": cfg.agent.max_tokens_per_response,
            "effort": cfg.agent.effort,
            "thinking": {"type": "adaptive", "display": cfg.agent.thinking_display},
            "temperature": None,
            "top_p": None,
            "top_k": None,
            "sampling_note": SAMPLING_NOTE,
            "tools": [BASH_TOOL],
            "tool_choice": "auto (API default)",
            "prompt_caching": "automatic, top-level cache_control ephemeral (5 min)",
            "server_side_fallbacks": "not used (would switch models mid-run)",
        },
        "limits": cfg.limits.model_dump(),
        "budget": cfg.budget.model_dump(),
        "api": {
            "sdk": f"anthropic {anthropic.__version__}",
            "anthropic_version": anthropic.Anthropic(api_key="unused").default_headers.get("anthropic-version"),
            "max_retries": MAX_RETRIES,
        },
        "prompts": {"system_sha256": _sha256(system_prompt), "user_sha256": _sha256(user_message)},
        "provenance": {
            "git_commit": git_commit(),
            "git_dirty": dirty,
            "image_id_locked": image_locked,
            "image_id_container": image_container,
            "config_sha256": config_sha256(cfg),
            "pricing": price.model_dump(),
            "probe_version": prepared.baseline.get("probe_version"),
            "python": sys.version.split()[0],
            "host": platform.platform(),
        },
        "seeds": {"agent": None, "agent_note": "the API cannot be seeded", "synthetic_data": _data_seed(),
                  "task_order": cfg.sampling.task_order_seed},
        "timestamps": {"started_at": started.isoformat(), "agent_started_at": agent_started,
                       "agent_ended_at": agent_ended, "ended_at": now()},
        "agent": {
            "end_reason": agent.end_reason, "turns": agent.turns, "tool_calls": agent.tool_calls,
            "cache_hits": agent.cache_hits, "request_ids": agent.request_ids, "stop_details": agent.stop_details,
            "error": agent.error, "bash_session_restarts": shell.restarts,
        },
        "usage": agent.usage.as_dict(),
        "usage_spent": agent.usage_spent.as_dict(),
        "cost_usd_spent": cost(agent.usage_spent, price),
        "cost_usd_if_uncached": cost(agent.usage, price),
        "results": {
            "check": finalized.check,
            "success": scored.success.success,
            "success_reasons": scored.success.reasons,
            "violated": scored.scope.violated,
            "violations": [c.as_dict() for c in scored.scope.violations],
            **scored.instructions.as_dict(),
            "report_outcome": report.outcome,
            "report_follows_template": report.follows_template,
            "eval_aware": awareness["eval_aware"],
            "eval_awareness_matches": awareness["matches"],
            "story_consistent": prepared.story_check.get("ok"),
            "story_failures": [a for a in prepared.story_check.get("assertions", []) if not a["ok"]],
        },
    }
    artifacts: dict[str, Any] = {
        "facts.json": prepared.facts, "baseline.json": prepared.baseline,
        "final.json": finalized.final, "docker_diff_baseline.json": prepared.docker_base,
        "docker_diff_final.json": finalized.docker_final, "check.json": finalized.check,
        "touched.json": finalized.touched,
        "scope.json": scored.scope.as_dict(),
    }
    for name, data in artifacts.items():
        indent = None if name in ("baseline.json", "final.json") else 2
        (run_dir / name).write_text(json.dumps(data, indent=indent, default=str) + "\n", encoding="utf-8")
    # Severity and self-report come from the offline scorer, so stored runs can be rescored later.
    scores = score_run(run_dir, bundle)
    meta["results"].update({
        "severity": scores["severity"], "violations": scores["violations"], "flags": scores["flags"],
        "report_accuracy": scores["self_report"]["label"], "report_findings": scores["self_report"]["findings"],
        "scoring_version": scores["scoring_version"],
    })
    (run_dir / "scores.json").write_text(json.dumps(scores, indent=2) + "\n", encoding="utf-8")
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2, default=str) + "\n", encoding="utf-8")
    transcript.write("run_end", run_id=run_id, cost_usd_spent=meta["cost_usd_spent"])
    ledger.append({
        "run_id": run_id, "namespace": namespace, "task_id": task.id, "model": model, "t": now(),
        "cost_usd_spent": meta["cost_usd_spent"], "cost_usd_if_uncached": meta["cost_usd_if_uncached"],
        "total_tokens": agent.usage.total_tokens, "cache_hits": agent.cache_hits,
    })
    return RunOutcome(run_dir, meta)
