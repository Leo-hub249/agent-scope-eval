"""Agent prompts. The system prompt states the role, the environment, the task's scope
block (tasks/prompt.py, identical format for every task) and the required final report;
the first user message is the ticket text only."""

from __future__ import annotations

from pathlib import Path
from string import Template

from runner.config import Limits
from tasks.prompt import render_scope
from tasks.schema import Task

SYSTEM_TEMPLATE = (Path(__file__).resolve().parent / "system.md").read_text(encoding="utf-8")


def render_system_prompt(task: Task, limits: Limits) -> str:
    return Template(SYSTEM_TEMPLATE).substitute(
        command_timeout_s=limits.command_timeout_s,
        scope_block=render_scope(task.scope),
    ).strip()


def render_user_message(task: Task) -> str:
    return task.instruction.strip()
