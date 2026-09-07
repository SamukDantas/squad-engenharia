"""Porta do executor de desenvolvimento: quem escreve o código da entrega.

Duas implementações desde antes desta refatoração — o OpenCode CLI como mão de
obra especialista e as crews CrewAI como caminho sem dependência externa —, hoje
escolhidas por `DEV_EXECUTOR`. A porta só dá nome ao que já era uma troca.

O contrato é estreito de propósito: o executor recebe onde trabalhar e o que
fazer, e **não devolve nada**. O que ele produziu é lido do disco pelo grafo,
numa varredura determinística do workspace — porque exit code classifica o
processo, não o trabalho, e auto-relato de nó já custou 855s de QA em cima de um
workspace vazio (RESILIENCIA.md, item 28).
"""
from typing import Protocol

from .perfil import PerfilStack


class ExecutorDeDesenvolvimento(Protocol):
    def __call__(
        self, workspace: str, spec: str, feedback: str, perfil: PerfilStack
    ) -> None:
        ...
