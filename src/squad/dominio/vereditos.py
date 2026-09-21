"""Leitura dos vereditos que vêm em texto livre de um LLM.

Roteamento não pode depender do formato exato da saída do modelo
(RESILIENCIA.md, item 7): modelos enfeitam a resposta, e o parser precisa
tolerar isso sem passar a aceitar qualquer coisa. O caso indecifrável conta
sempre como reprova — errar para o lado de repetir uma rodada é mais barato que
errar para o lado de aprovar o que ninguém aprovou.
"""

import re

# Linhas finais examinadas em busca do veredito da revisão.
LINHAS_VEREDITO = 8

# O veredito é lido na **primeira frase**, não numa janela de N caracteres.
# A janela fixa era o furo: em "SIM para os requisitos, NAO para os critérios"
# o NAO cai no char 24 e a exclusão nem chegava a vê-lo. A frase é o recorte
# certo porque é a unidade em que a resposta se qualifica — o que vem depois do
# ponto é justificativa, e justificativa quase sempre contém "não".
_FIM_DE_FRASE = re.compile(r"[.;!?\n]")
LIMITE_FRASE = 200  # teto contra resposta sem pontuação nenhuma

# Fronteira de palavra, não substring: sem ela `ASSIM` e `SIMPLESMENTE` contam
# como SIM, e a versão anterior aprovava com `startswith` em qualquer uma delas.
_SIM = re.compile(r"\bSIM\b")
_NAO = re.compile(r"\bN[AÃ]O\b")


def veredito_sim(resposta: object) -> bool:
    """Parser tolerante de SIM/NAO.

    Duas correções sobre a versão anterior, e as duas iam no mesmo sentido
    errado — aprovar o que não foi aprovado:

    - `startswith("SIM")` fazia curto-circuito **antes** da exclusão de NAO, e a
      exclusão só olhava 20 caracteres. "SIM para os requisitos, NAO para os
      critérios" passava como aprovação por dois motivos independentes;
    - a busca era por substring, então `ASSIM` e `SIMPLESMENTE` contavam como
      SIM.

    Agora o veredito é lido na primeira frase, com fronteira de palavra, e um
    NAO ali dentro reprova mesmo que o SIM venha primeiro.

    O custo aceito: "SIM, não há problemas" também vira reprova, porque separar
    esse caso de uma resposta qualificada exige entender a frase, não lê-la. A
    conta é assimétrica e conhecida — reprovar à toa custa uma chamada barata de
    guard, e aprovar uma suíte que não cobre os critérios custa a execução
    inteira seguindo sobre uma premissa falsa.
    """
    texto = str(resposta).strip().upper()
    frase = _FIM_DE_FRASE.split(texto, 1)[0][:LIMITE_FRASE]
    if _NAO.search(frase):
        return False
    return bool(_SIM.search(frase))


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


# ---------- de que tipo é o vermelho da suíte ----------

# Asserção que falhou: o teste rodou, comparou e discordou. O código pode estar
# errado — e, às vezes, o próprio teste (expectativa errada, tolerância rígida
# demais).
_MARCAS_ASSERCAO = (
    "AssertionError", "AssertionFailedError", "\nE       assert", "expected ",
)
# O teste nem chegou a comparar: import, sintaxe, tipo, coleta, nome indefinido.
_MARCAS_AMBIENTE = (
    "ModuleNotFoundError", "ImportError", "Cannot find module",
    "Failed to resolve import", "SyntaxError", "error TS", "ERROR collecting",
    "no tests ran", "No test files found", "COMPILATION ERROR", "NameError",
    "ReferenceError", "is not defined",
)


def falha_de_assercao(saida: str) -> bool:
    """O vermelho veio de uma asserção, e só dela."""
    saida = saida or ""
    return any(m in saida for m in _MARCAS_ASSERCAO) and not any(
        m in saida for m in _MARCAS_AMBIENTE
    )


LIMITE_JUSTIFICATIVA = 800


def justificativa(resposta: object) -> str:
    """O que vem depois do veredito de uma resposta SIM/NAO.

    O guard de critérios responde o veredito na primeira linha e, quando
    reprova, lista o que falta nas seguintes. O veredito continua sendo lido
    por `veredito_sim`, na primeira frase; isto aqui só recupera o resto, para
    virar feedback acionável em vez de "reescreva tudo".
    """
    texto = str(resposta or "").strip()
    primeira = _FIM_DE_FRASE.search(texto)
    resto = texto[primeira.end():] if primeira else ""
    # Só o separador sai: o hífen de uma lista é conteúdo, não pontuação.
    return resto.lstrip(" \t\r\n:").rstrip()[:LIMITE_JUSTIFICATIVA]
