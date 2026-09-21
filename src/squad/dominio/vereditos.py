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


# ---------- o trecho da falha que vai para as métricas ----------

LIMITE_TRECHO_FALHA = 2_000

# Linhas que dizem por que falhou, nos formatos das três stacks: tsc/next,
# vitest, pytest, Maven. O resto da saída (banner, lista de rotas, progresso)
# só empurra a causa para fora do limite.
_LINHA_DE_ERRO = (
    "error", "Error", "ERROR", "FAIL", "Failed", "failed", "×",
    "Type error", "Cannot find", "not defined", "Traceback", "E   ",
    "expected", "BUILD FAILURE", "COMPILATION",
)


def trecho_da_falha(saida: str, limite: int = LIMITE_TRECHO_FALHA) -> str:
    """O pedaço da saída que explica uma falha de build ou de teste.

    Existe porque a causa se perdia: a saída de cada rodada fica no estado e é
    sobrescrita pela seguinte, e as métricas só guardavam `testes_ok`. Medido
    em três execuções seguidas da calculadora de juros: o build quebrou na
    primeira rodada, a seguinte corrigiu, e quando alguém foi olhar não havia
    mais o que ler.

    Prioriza as linhas de erro, com a anterior e a seguinte como contexto: o
    `next build` põe o arquivo (`./app/page.test.tsx:8:1`) na linha de cima da
    mensagem, e o pytest põe o trecho de código na de cima do `E   `. Sem
    nenhuma linha reconhecida, fica o fim da saída, que é onde runners e
    compiladores põem o resumo.
    """
    linhas = (saida or "").splitlines()
    escolhidas: list[str] = []
    for i, linha in enumerate(linhas):
        if any(m in linha for m in _LINHA_DE_ERRO):
            escolhidas.extend(linhas[max(0, i - 1):i + 2])
    trecho = "\n".join(dict.fromkeys(l for l in escolhidas if l.strip()))
    if not trecho:
        return (saida or "")[-limite:]
    return trecho[:limite]
