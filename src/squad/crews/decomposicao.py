"""Crews que rodam uma vez por execucao, antes do fan-out.

Duas crews e nao uma com duas tarefas: o grafo precisa dos DOIS resultados, e o
CrewAI devolve so o `raw` da ultima tarefa. Separadas, cada no do maestro guarda
o que produziu, e uma retomada nao repaga a decomposicao para chegar ao
contrato.
"""
from crewai import Crew, Process

from .base import build_agent, build_task


def crew_decomposicao() -> Crew:
    arquiteto = build_agent("arquiteto_de_sistemas")
    return Crew(
        agents=[arquiteto],
        tasks=[build_task("decompor_servicos", arquiteto)],
        process=Process.sequential,
        verbose=True,
    )


def crew_contratos() -> Crew:
    arquiteto = build_agent("arquiteto_de_sistemas")
    return Crew(
        agents=[arquiteto],
        tasks=[build_task("redigir_contratos", arquiteto)],
        process=Process.sequential,
        verbose=True,
    )
