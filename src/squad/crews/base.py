"""Utilitários compartilhados pelas crews: carregamento dos YAMLs de config."""
from pathlib import Path

import yaml
from crewai import Agent, Task

from ..llm import zen_llm

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _load(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


AGENTS_CFG = _load("agents.yaml")
TASKS_CFG = _load("tasks.yaml")


def build_agent(key: str, tools: list | None = None, **overrides) -> Agent:
    cfg = {**AGENTS_CFG[key], **overrides}
    return Agent(
        role=cfg["role"],
        goal=cfg["goal"],
        backstory=cfg["backstory"],
        allow_delegation=cfg.get("allow_delegation", False),
        tools=tools or [],
        llm=zen_llm(),
        verbose=True,
    )


def build_task(key: str, agent: Agent, context: list[Task] | None = None) -> Task:
    cfg = TASKS_CFG[key]
    return Task(
        description=cfg["description"],
        expected_output=cfg["expected_output"],
        agent=agent,
        context=context or [],
    )
