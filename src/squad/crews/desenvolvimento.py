"""Crew de desenvolvimento: processo sequencial com tarefas explícitas.

backend implementa -> frontend integra -> tech lead consolida.
Sem delegação dinâmica: mais determinístico, mais barato em tokens e
compatível com qualquer modelo (a delegação hierárquica exige modelos
fortes em uso de ferramentas).

Os três agentes recebem as ferramentas de arquivo confinadas ao workspace:
o código é materializado como arquivos reais, não como texto no estado.
"""
from crewai import Crew, Process

from ..tools import ferramentas_workspace
from .base import build_agent, build_task


def crew_desenvolvimento(workspace: str) -> Crew:
    tools = ferramentas_workspace(workspace)
    backend = build_agent("dev_backend", tools=tools)
    frontend = build_agent("dev_frontend", tools=tools)
    tech_lead = build_agent("tech_lead", tools=tools, allow_delegation=False)

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
