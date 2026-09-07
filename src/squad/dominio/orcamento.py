"""Repartição do contexto enviado ao LLM: o que cabe, e quem perde espaço.

Todo artefato que decide roteamento é injetado direto na tarefa, não deixado a
elos de contexto entre tarefas (RESILIENCIA.md, item 10) — e injetar tem teto.
Este módulo decide **como** o teto é repartido; quem lê o disco é a camada de
fora, que passa os conteúdos já lidos.

O defeito que motivou tudo: truncar pelo total (os primeiros N chars) faz quem
lê julgar por amostra sem saber que é amostra. Medido nas 14 entregas em disco,
o dump do revisor chegou a 43.918 chars com o teto em 15.000 e truncava em 10
delas — e, como `sorted()` põe a suíte por último, quem sumia era sempre ela, em
8 dos 11 casos. O revisor aprovava código que não tinha visto inteiro.
"""
from typing import Callable, Mapping, Sequence

# Extensões cujo conteúdo não é texto de revisar. Binário lido com
# `errors="replace"` vira ruído no contexto do LLM sem informar nada: medido nas
# entregas já existentes, `tarefas.db` (12.303 chars) e `tasks.db` (16.397) foram
# enviados inteiros ao revisor — 56% do dump de uma delas. A varredura do
# workspace continua vendo esses arquivos (eles são entrega e vão ao deploy); o
# corte é só no que se manda para o modelo.
EXTENSOES_BINARIAS = frozenset({
    ".db", ".sqlite", ".sqlite3", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".ico", ".svg", ".pdf", ".zip", ".gz", ".tar", ".whl", ".so", ".dll",
    ".exe", ".bin", ".pickle", ".pkl",
})


def e_binario(caminho: str, extensoes: frozenset[str] = EXTENSOES_BINARIAS) -> bool:
    nome = caminho.rsplit("/", 1)[-1]
    ponto = nome.rfind(".")
    return ponto > 0 and nome[ponto:].lower() in extensoes


def ordenar_para_dump(
    arquivos: Sequence[str], e_teste: Callable[[str], bool]
) -> list[str]:
    """Fonte antes de teste na fila do orçamento.

    O revisor já recebe a saída dos testes em separado, então perder trecho de
    suíte custa menos que perder o módulo que a spec descreve.
    """
    revisaveis = [a for a in arquivos if not e_binario(a)]
    return [a for a in revisaveis if not e_teste(a)] + [
        a for a in revisaveis if e_teste(a)
    ]


def blocos_com_orcamento(
    conteudos: Mapping[str, str], ordem: Sequence[str], limite: int, rotulo: str
) -> str:
    """Concatena arquivos dentro de um teto, **repartindo** o orçamento.

    Cada arquivo tem cota, o que sobra de arquivo pequeno é redistribuído aos
    grandes (fila em ordem crescente de tamanho), e o manifesto lista todos os
    nomes — inclusive os que entraram truncados, para que ninguém confunda
    ausência com omissão.
    """
    if not ordem:
        return ""

    manifesto = "\n".join(f"- {a}" for a in ordem)
    cabecalho = f"{rotulo} ({len(ordem)}):\n{manifesto}\n\n"

    # O teto vale para o texto inteiro, então cabeçalho de bloco e separador
    # saem do orçamento antes de ele ser repartido: reservar só o conteúdo
    # deixaria o total estourar em silêncio de novo, que é o defeito original.
    moldura = {rel: len(f"### {rel}\n") + 1 for rel in ordem}
    saldo = max(limite - len(cabecalho) - sum(moldura.values()), 0)

    # Água em copos: o menor primeiro leva só o que precisa, e o saldo restante
    # é redividido entre os que ainda não tiveram vez.
    fila = sorted(ordem, key=lambda rel: len(conteudos[rel]))
    cotas: dict[str, int] = {}
    for i, rel in enumerate(fila):
        cotas[rel] = min(len(conteudos[rel]), saldo // (len(fila) - i))
        saldo -= cotas[rel]

    partes = []
    for rel in ordem:
        conteudo = conteudos[rel]
        if len(conteudo) > cotas[rel]:
            # O marcador também ocupa a cota: sem descontá-lo, cada arquivo
            # truncado devolveria mais texto do que lhe foi orçado.
            marcador = (
                f"\n[... {rel} truncado aqui ({len(conteudo)} chars no total) ...]"
            )
            conteudo = conteudo[:max(cotas[rel] - len(marcador), 0)] + marcador
        partes.append(f"### {rel}\n{conteudo}")
    return cabecalho + "\n".join(partes)
