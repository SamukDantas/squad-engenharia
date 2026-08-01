"""Crew de planejamento: analista de requisitos + arquiteto (processo sequencial)."""
from crewai import Crew, Process

from .base import build_agent, build_task


def crew_planejamento() -> Crew:
    analista = build_agent("analista_requisitos")
    arquiteto = build_agent("arquiteto")
    return Crew(
        agents=[analista, arquiteto],
        tasks=[
            build_task("levantar_requisitos", analista),
            build_task("definir_arquitetura", arquiteto),
        ],
        process=Process.sequential,
        verbose=True,
    )
