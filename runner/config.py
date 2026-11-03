"""Load and validate config/experiment.yaml and config/pricing.yaml."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, PositiveInt

from runner.sandbox import REPO

CONFIG_DIR = REPO / "config"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentConfig(Strict):
    model: str
    comparison_models: list[str] = Field(default_factory=list)
    effort: Literal["low", "medium", "high", "xhigh", "max"]
    thinking_display: Literal["summarized", "omitted"]
    max_tokens_per_response: PositiveInt

    def allowed_models(self) -> list[str]:
        return [self.model, *self.comparison_models]


class Limits(Strict):
    command_timeout_s: PositiveInt
    max_tool_output_chars: PositiveInt
    max_turns: PositiveInt
    max_tokens_per_run: PositiveInt


class MonitorConfig(Strict):
    model: str
    effort: Literal["low", "medium", "high", "xhigh", "max"]


class Sampling(Strict):
    samples_per_task: PositiveInt
    task_order_seed: int


class Budget(Strict):
    max_runs: PositiveInt
    max_total_usd: PositiveFloat


class CacheConfig(Strict):
    dir: str


class ExperimentConfig(Strict):
    agent: AgentConfig
    limits: Limits
    monitor: MonitorConfig
    sampling: Sampling
    budget: Budget
    cache: CacheConfig


class Price(Strict):
    input: float
    output: float
    cache_read: float
    cache_write: float


def load_config(path: Path = CONFIG_DIR / "experiment.yaml") -> ExperimentConfig:
    return ExperimentConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_pricing(path: Path = CONFIG_DIR / "pricing.yaml") -> dict[str, Price]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {model: Price.model_validate(p) for model, p in raw.items()}


def config_sha256(cfg: ExperimentConfig) -> str:
    return hashlib.sha256(json.dumps(cfg.model_dump(), sort_keys=True).encode()).hexdigest()
