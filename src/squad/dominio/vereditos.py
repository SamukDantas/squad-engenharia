"""Leitura dos vereditos que vêm em texto livre de um LLM.

Roteamento não pode depender do formato exato da saída do modelo
(RESILIENCIA.md, item 7): modelos enfeitam a resposta, e o parser precisa
tolerar isso sem passar a aceitar qualquer coisa. O caso indecifrável conta
sempre como reprova — errar para o lado de repetir uma rodada é mais barato que
errar para o lado de aprovar o que ninguém aprovou.
"""

# Linhas finais examinadas em busca do veredito da revisão.
LINHAS_VEREDITO = 8


def veredito_sim(resposta: object) -> bool:
    """Parser tolerante de SIM/NAO."""
    normalizado = str(resposta).strip().upper()
    return normalizado.startswith("SIM") or (
        "SIM" in normalizado[:20]
        and "NAO" not in normalizado[:20]
        and "NÃO" not in normalizado[:20]
    )


def veredito_aprovado(texto: str) -> bool:
    """Procura APROVADO/REPROVADO nas últimas linhas, não só na última.

    Exigir o token na última linha é acoplar roteamento ao formato exato da
    saída: um revisor que escreve "**APROVADO**" e fecha com um parágrafo de
    conclusão tinha a aprovação lida como reprova — custando uma rodada inteira
    do laço.

    Vale o **último** veredito encontrado, porque o texto discute apontamentos
    antes de concluir. Nada reconhecível conta como reprova.
    """
    if not texto:
        return False
    linhas = [ln for ln in texto.upper().splitlines() if ln.strip()]
    for linha in reversed(linhas[-LINHAS_VEREDITO:]):
        # REPROVADO primeiro: "APROVADO" não é substring dele, mas a ordem
        # deixa a precedência explícita para quem lê.
        if "REPROVADO" in linha:
            return False
        if "APROVADO" in linha:
            return True
    return False
