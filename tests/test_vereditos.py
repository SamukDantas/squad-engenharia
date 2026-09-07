"""Leitura de veredito em texto livre — RESILIENCIA.md, itens 7 e 27.

Cada caso aqui é um formato que um modelo de verdade produziu e que custou
rodada quando o parser não o entendeu.
"""
from src.squad.dominio.vereditos import veredito_aprovado, veredito_sim


# ---------- SIM / NAO (guards de aderência e de critérios) ----------

def test_sim_limpo():
    assert veredito_sim("SIM") is True
    assert veredito_sim("sim") is True


def test_sim_enfeitado_pelo_modelo():
    assert veredito_sim("SIM, a spec cobre o pedido.") is True
    assert veredito_sim("  Sim.  ") is True


def test_nao_em_todas_as_formas():
    for resposta in ("NAO", "não", "NÃO, faltam critérios", "Nao."):
        assert veredito_sim(resposta) is False, resposta


def test_indecifravel_conta_como_reprova():
    """Errar para o lado de repetir uma rodada é mais barato que errar para o
    lado de aprovar o que ninguém aprovou."""
    for resposta in ("", "talvez", "não sei dizer", "A resposta é complexa."):
        assert veredito_sim(resposta) is False, resposta


def test_resposta_qualificada_conta_como_reprova():
    """O defeito que este parser tinha, corrigido: `startswith("SIM")` fazia
    curto-circuito antes da exclusão de NAO, e a exclusão só olhava 20 chars —
    então o NAO no char 24 nem era visto. Passava como aprovação por dois
    motivos independentes."""
    assert veredito_sim("SIM para os requisitos, NAO para os critérios") is False
    assert veredito_sim("Bem, NAO — SIM seria exagero") is False


def test_justificativa_depois_do_ponto_nao_reprova():
    """O veredito é lido na primeira frase. O que vem depois do ponto é
    justificativa, e justificativa quase sempre contém "não" — ler a resposta
    inteira reprovaria toda aprovação explicada."""
    assert veredito_sim("SIM. A suíte cobre os critérios; não há triviais.") is True


def test_veredito_no_fim_de_uma_frase_longa_conta():
    """A janela fixa de 20 chars descartava isto. A frase é o recorte certo."""
    assert veredito_sim("Com base na análise dos critérios, a resposta é SIM.") is True


def test_sim_dentro_de_outra_palavra_nao_conta():
    """Sem fronteira de palavra, `ASSIM` e `SIMPLESMENTE` aprovavam sozinhos —
    e o `startswith` fazia de `SIMPLESMENTE não dá` uma aprovação."""
    assert veredito_sim("Assim que possível") is False
    assert veredito_sim("SIMPLESMENTE não dá") is False


def test_custo_aceito_da_correcao():
    """Fixado de propósito: "SIM, não há problemas" vira reprova, porque
    separá-lo de uma resposta qualificada exige entender a frase, não lê-la.

    A conta é assimétrica — reprovar à toa custa uma chamada barata de guard;
    aprovar uma suíte que não cobre os critérios custa a execução inteira
    seguindo sobre uma premissa falsa.
    """
    assert veredito_sim("SIM, não há problemas") is False


def test_aceita_objeto_que_nao_e_string():
    assert veredito_sim(None) is False


# ---------- APROVADO / REPROVADO (revisão de código) ----------

def test_aprovado_na_ultima_linha():
    assert veredito_aprovado("Tudo certo.\nAPROVADO") is True


def test_aprovado_em_negrito_seguido_de_conclusao():
    """RESILIENCIA 27: o revisor escrevia `**APROVADO**` e fechava com um
    parágrafo. Exigir o token na última linha lia a aprovação como reprova e
    custava uma rodada inteira do laço."""
    texto = (
        "A implementação cobre os critérios de aceite.\n\n"
        "**APROVADO**\n\n"
        "Em resumo, a entrega está coerente com a especificação e os testes "
        "exercitam o comportamento exigido."
    )
    assert veredito_aprovado(texto) is True


def test_vale_o_ultimo_veredito_porque_o_texto_discute_antes_de_concluir():
    texto = (
        "Se o tratamento de erro faltasse, isto seria REPROVADO.\n"
        "Mas ele existe.\n"
        "APROVADO"
    )
    assert veredito_aprovado(texto) is True


def test_reprovado_tem_precedencia_na_mesma_linha():
    assert veredito_aprovado("Veredito: REPROVADO (não APROVADO)") is False


def test_veredito_longe_do_fim_nao_conta():
    """Só as últimas linhas são examinadas: um 'APROVADO' no meio de uma
    discussão de 40 linhas não é a conclusão."""
    texto = "APROVADO seria o caso ideal.\n" + "\n".join(f"linha {i}" for i in range(20))
    assert veredito_aprovado(texto) is False


def test_texto_vazio_e_reprova():
    assert veredito_aprovado("") is False
    assert veredito_aprovado("   \n\n  ") is False


def test_relatorio_sem_veredito_reconhecivel_e_reprova():
    assert veredito_aprovado("O código está bom, mas eu mudaria o nome da função.") is False
