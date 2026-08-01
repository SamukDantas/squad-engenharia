"""Crew de qualidade: QA escreve/roda testes e revisor emite o veredito."""
from crewai import Crew, Process

from .base import build_agent, build_task


def crew_qualidade() -> Crew:
    qa = build_agent("qa_engineer")
    revisor = build_agent("revisor_codigo")
    return Crew(
        agents=[qa, revisor],
        tasks=[
            build_task("testar", qa),
            build_task("revisar", revisor),
        ],
        process=Process.sequential,
        verbose=True,
    )
