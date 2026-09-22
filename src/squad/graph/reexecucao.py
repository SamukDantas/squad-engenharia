"""Reexecução a partir de um nó: validar uma mudança sem pagar a execução inteira.

Cada correção de prompt ou de laço era validada disparando o pedido inteiro —
planejamento, desenvolvimento, QA, revisão, visual —, mesmo quando a mudança
só mexia no QA. Medido em 21/09 (calculadora de juros): duas execuções de
validação, `f96bb730` e `9a86ddb0`, gastaram 11 pontos da cota do Codex e
2,4 M de tokens sem entregar nada, contra 2 pontos de uma execução limpa.

A reexecução bifurca uma thread existente: pega o estado do grafo
imediatamente antes de um nó e roda dali em diante numa **thread nova**, com
workspace e métricas próprios. A thread de origem não muda.

De onde vem o estado: os checkpoints do subgrafo do serviço guardam o estado
antes de cada passo, e o canal `branch:to:<nó>` marca o checkpoint em que
aquele nó é o próximo. Num laço o nó roda mais de uma vez, e por padrão vale
a PRIMEIRA ocorrência: é o ponto limpo, antes de qualquer retorno de juiz.
Medido na validação `dd2d9d0b`: partindo da última passada do QA da
`3842268c`, a suíte da primeira passada já existia, o QA só a complementou em
modo ajuste, e a validação não exercitou a regra que queria validar. A última
ocorrência continua disponível (`ocorrencia="ultima"`).

O limite, que precisa estar dito: o disco não tem checkpoint. O workspace
novo é uma cópia do workspace de origem **como ele está agora**, menos os
arquivos que não existiam naquele ponto (ausentes do manifesto `arquivos` do
checkpoint). Bifurcar em `escrever_testes` remove a suíte que o QA escreveu
depois; um arquivo que uma rodada de correção posterior alterou fica na
versão mais recente.
"""
import shutil
from pathlib import Path

from .state import EstadoProjeto

# Nós do subgrafo do serviço que fazem sentido como ponto de partida. A
# triagem é o começo normal; o `aprovacao_humana` do ramo é só uma rota.
NOS_REEXECUTAVEIS = (
    "planejamento", "validacao_spec", "desenvolvimento", "escrever_testes",
    "validacao_testes", "executar_testes", "config_ambientes", "revisao",
    "pentest", "visual",
)

_CAMPOS_DO_ESTADO = frozenset(EstadoProjeto.__annotations__)

# Fica fora do manifesto mas precisa ir junto: `.squad/run.json` é como a
# verificação visual e o pentest sobem a entrega. Medido na primeira
# reexecução (`cc4cf813`, de `35d3bceb` a partir de `visual`): sem ele, o nó
# visual passou por "nada a renderizar" uma página que a origem renderizava.
_MANTER_NA_COPIA = frozenset({".squad"})
# O `.git` é do deploy da origem (remoto e branch dela), não da entrega.
_FORA_DA_COPIA = frozenset({".git"})


def pastas_fora_da_copia(ignorar_no_workspace) -> frozenset[str]:
    """Dependências e build ficam de fora; o que o grafo lê de `.squad`, não."""
    return (frozenset(ignorar_no_workspace) - _MANTER_NA_COPIA) | _FORA_DA_COPIA


OCORRENCIAS = ("primeira", "ultima")


def estado_antes_do_no(checkpointer, thread_id: str, no: str,
                       ocorrencia: str = "primeira") -> dict | None:
    """O estado do serviço na primeira (ou última) vez em que `no` era o
    próximo passo."""
    if ocorrencia not in OCORRENCIAS:
        raise ValueError(f"ocorrência inválida: '{ocorrencia}'. Use {' ou '.join(OCORRENCIAS)}.")
    mais_nova = ocorrencia == "ultima"
    marca = f"branch:to:{no}"
    melhor = None
    for tupla in checkpointer.list({"configurable": {"thread_id": thread_id}}):
        ns = tupla.config["configurable"].get("checkpoint_ns", "")
        if not ns.startswith("servico:"):
            continue
        valores = tupla.checkpoint.get("channel_values", {})
        if marca not in valores:
            continue
        passo = tupla.metadata.get("step", -1)
        if melhor is None or (passo > melhor[0] if mais_nova else passo < melhor[0]):
            melhor = (passo, valores)
    if melhor is None:
        return None
    return {k: v for k, v in melhor[1].items() if k in _CAMPOS_DO_ESTADO}


def copiar_workspace(origem: str, destino: Path, arquivos_no_ponto: list[str],
                     arquivos_agora: list[str], ignorar: frozenset[str]) -> list[str]:
    """Copia o workspace e tira o que não existia no ponto da bifurcação.

    Devolve os arquivos removidos, para quem chama dizer ao humano.
    """
    shutil.copytree(
        origem, destino, ignore=shutil.ignore_patterns(*ignorar), dirs_exist_ok=False,
    )
    no_ponto = set(arquivos_no_ponto)
    removidos = [a for a in arquivos_agora if a not in no_ponto]
    for rel in removidos:
        (destino / rel).unlink(missing_ok=True)
    return removidos


def preparar(estado: dict, novo_thread_id: str, arquivos_agora: list[str],
             ignorar: frozenset[str]) -> tuple[dict, list[str]]:
    """O estado da thread nova, com workspace próprio.

    `thread_id` e `workspace` batizam métricas, containers e o diretório de
    trabalho: herdados da origem, a thread nova escreveria nos dela.
    """
    destino = Path("workspace") / novo_thread_id
    removidos = copiar_workspace(
        estado["workspace"], destino, estado.get("arquivos", []), arquivos_agora, ignorar,
    )
    return {
        **estado,
        "thread_id": novo_thread_id,
        "workspace": str(destino.resolve()),
    }, removidos
