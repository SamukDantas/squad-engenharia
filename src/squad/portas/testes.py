"""Porta de execução da suíte: quem roda os testes e devolve o veredito.

Duas implementações hoje (Docker e host) e mais uma por stack depois. O que não
varia é o contrato: **exit code manda**. Verde é 0; qualquer outro valor,
inclusive 5 ("nenhum teste coletado"), e o timeout, são vermelho.
"""
from dataclasses import dataclass
from typing import Protocol

from .perfil import Cobertura, PerfilStack


@dataclass(frozen=True)
class ResultadoTestes:
    testes_ok: bool
    saida: str
    cobertura: Cobertura

    def como_estado(self, limite_saida: int) -> dict:
        return {
            **self.cobertura.como_estado(),
            "testes_ok": self.testes_ok,
            "saida_testes": self.saida[-limite_saida:],
        }


class ExecutorDeTestes(Protocol):
    def __call__(
        self, workspace: str, thread_id: str, perfil: PerfilStack
    ) -> ResultadoTestes:
        ...
