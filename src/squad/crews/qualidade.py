"""Crews de qualidade — duas metades com papéis distintos:

- crew_testes: QA escreve testes pytest REAIS no workspace (com ferramentas
  de arquivo). Quem executa os testes é o grafo, em nó determinístico —
  veredito por exit code, não por opinião.
- crew_revisao: revisor LLM cobre o que execução não pega (legibilidade,
  segurança, aderência à spec). Sem ferramentas: por decidir roteamento,
  recebe os artefatos injetados direto na tarefa (RESILIENCIA.md, item 10).
"""
from crewai import Crew, Process

from ..tools import ferramentas_workspace
from .base import build_agent, build_task


def crew_testes(workspace: str) -> Crew:
    qa = build_agent("qa_engineer", tools=ferramentas_workspace(workspace))
    return Crew(
        agents=[qa],
        tasks=[build_task("escrever_testes", qa)],
        process=Process.sequential,
        verbose=True,
    )


def crew_revisao() -> Crew:
    revisor = build_agent("revisor_codigo")
    return Crew(
        agents=[revisor],
        tasks=[build_task("revisar", revisor)],
        process=Process.sequential,
        verbose=True,
    )
