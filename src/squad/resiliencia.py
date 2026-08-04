"""Tentativa automática para falhas transitórias de provedor.

Observado em execução real: o provedor devolve resposta malformada de forma
intermitente e o CrewAI/LiteLLM quebra ao acessá-la (`TypeError: 'NoneType'
object is not subscriptable`), matando o nó inteiro. Num pedido complexo isso
custou 844 segundos de trabalho já feito — o checkpoint salva nós concluídos,
não trabalho parcial dentro de um nó.

O grafo já tinha circuit breakers (laço semântico) e checkpoints (queda de
processo), mas nada para o soluço de uma chamada. Esta é a peça que faltava:
repetir o que é transitório, sem mascarar o que é permanente.
"""
import time
from typing import Callable, TypeVar

T = TypeVar("T")

TENTATIVAS = 3
ESPERA_BASE = 5  # segundos; dobra a cada tentativa

# Falha transitória: vale repetir a mesma chamada. Resposta malformada,
# instabilidade de inferência e limite de taxa entram aqui.
_TRANSITORIAS = (
    "nonetype",                # resposta sem o campo esperado
    "not subscriptable",
    "invalid response from llm",
    "connection",
    "timeout",
    "temporarily unavailable",
    "503",
    "502",
    "429",
    "overloaded",
    "rate limit",
)

# Falha permanente: repetir só gasta tempo e dinheiro. Credencial inválida,
# saldo zerado e modelo inexistente não melhoram na segunda tentativa.
_PERMANENTES = (
    "insufficient balance",
    "creditserror",
    "invalid api key",
    "authenticationerror",
    "model not found",
    "does not support",
)


def _e_transitoria(e: Exception) -> bool:
    texto = f"{type(e).__name__}: {e}".lower()
    if any(marca in texto for marca in _PERMANENTES):
        return False
    return any(marca in texto for marca in _TRANSITORIAS)


def com_retry(rotulo: str, acao: Callable[[], T]) -> T:
    """Executa `acao`, repetindo apenas o que parece transitório.

    Falha permanente (saldo, credencial, modelo inexistente) sobe na primeira
    ocorrência: insistir nela é desperdício, e o erro claro é mais útil que
    três tentativas idênticas.
    """
    ultima: Exception | None = None
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            return acao()
        except Exception as e:  # noqa: BLE001 — a decisão é por conteúdo, não por tipo
            ultima = e
            if not _e_transitoria(e) or tentativa == TENTATIVAS:
                raise
            espera = ESPERA_BASE * (2 ** (tentativa - 1))
            print(
                f">>> {rotulo}: falha transitória ({type(e).__name__}), "
                f"tentativa {tentativa}/{TENTATIVAS}. Repetindo em {espera}s..."
            )
            time.sleep(espera)
    raise ultima  # inalcançável; mantém o type checker satisfeito
