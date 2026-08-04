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

TENTATIVAS_PADRAO = 3   # chamadas curtas (guards): repetir é barato
TENTATIVAS_CARAS = 1    # kickoff de crew: uma tentativa cara já basta
ESPERA_BASE = 5         # segundos; dobra a cada tentativa

# Falha transitória: vale repetir a mesma chamada. Instabilidade de
# inferência, limite de taxa e problema de rede entram aqui.
_TRANSITORIAS = (
    "nonetype",                # resposta sem o campo esperado
    "not subscriptable",
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

# Saturação de contexto (RESILIENCIA.md, item 5): o modelo estoura e devolve
# vazio. PARECE transitória — o sintoma é o mesmo `NoneType` de uma resposta
# malformada —, mas é determinística: o contexto não encolhe entre tentativas.
# Medido: repetir custou 45 minutos para chegar ao mesmo erro três vezes.
_SATURACAO = (
    "invalid response from llm",
    "none or empty",
)


def _e_saturacao(texto: str) -> bool:
    return any(marca in texto for marca in _SATURACAO)


def _e_transitoria(e: Exception) -> bool:
    texto = f"{type(e).__name__}: {e}".lower()
    if _e_saturacao(texto) or any(marca in texto for marca in _PERMANENTES):
        return False
    return any(marca in texto for marca in _TRANSITORIAS)


def com_retry(rotulo: str, acao: Callable[[], T], caro: bool = False) -> T:
    """Executa `acao`, repetindo apenas o que é genuinamente transitório.

    `caro=True` para chamadas de centenas de segundos (kickoff de crew): ali
    o retry custa mais que a própria falha, então uma tentativa basta. Guards
    curtos ficam no padrão.

    Falha permanente (saldo, credencial, modelo incapaz) e **saturação de
    contexto** sobem na primeira ocorrência: nas duas, insistir só troca um
    erro rápido por um erro lento.
    """
    limite = TENTATIVAS_CARAS if caro else TENTATIVAS_PADRAO
    for tentativa in range(1, limite + 1):
        try:
            return acao()
        except Exception as e:  # noqa: BLE001 — a decisão é por conteúdo, não por tipo
            if _e_saturacao(f"{type(e).__name__}: {e}".lower()):
                raise RuntimeError(
                    f"{rotulo}: o modelo devolveu resposta vazia — saturação de "
                    "contexto (RESILIENCIA.md, item 5). Repetir não resolve: o "
                    "contexto é o mesmo. Reduza o tamanho dos artefatos que "
                    "chegam neste nó ou use um modelo com mais capacidade."
                ) from e
            if not _e_transitoria(e) or tentativa == limite:
                raise
            espera = ESPERA_BASE * (2 ** (tentativa - 1))
            print(
                f">>> {rotulo}: falha transitória ({type(e).__name__}), "
                f"tentativa {tentativa}/{limite}. Repetindo em {espera}s..."
            )
            time.sleep(espera)
    raise AssertionError("inalcançável")  # o laço sempre retorna ou levanta
