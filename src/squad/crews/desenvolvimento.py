"""Crew de desenvolvimento: processo sequencial com tarefas explícitas.

backend implementa -> frontend integra -> tech lead consolida.
Sem delegação dinâmica: mais determinístico, mais barato em tokens e
compatível com qualquer modelo (a delegação hierárquica exige modelos
fortes em uso de ferramentas).
"""
from crewai import Crew, Process

from .base import build_agent, build_task


def crew_desenvolvimento() -> Crew:
    backend = build_agent("dev_backend")
    frontend = build_agent("dev_frontend")
    tech_lead = build_agent("tech_lead", allow_delegation=False)

    t_backend = build_task("implementar_backend", backend)
    t_frontend = build_task("implementar_frontend", frontend, context=[t_backend])
    t_consolidar = build_task(
        "consolidar_entrega", tech_lead, context=[t_backend, t_frontend]
    )

    return Crew(
        agents=[backend, frontend, tech_lead],
        tasks=[t_backend, t_frontend, t_consolidar],
        process=Process.sequential,
        verbose=True,
    )
