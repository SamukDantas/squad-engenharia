"""As decisões de roteamento do grafo, testadas sem grafo.

Até aqui, conferir "a revisão estourou o teto?" exigia montar dicionários à mão
num script solto e ler o stdout. Estas são as mesmas perguntas, agora
verificáveis em 0,1s e sem Docker, LLM ou disco.
"""
from src.squad.dominio import rotas
from src.squad.dominio.rotas import (
    MAX_PENTEST,
    MAX_REPLANEJAMENTOS,
    MAX_REVISOES,
    MAX_TENTATIVAS,
    MAX_TESTES,
    MAX_VISUAL,
)

# Estado mínimo: as rotas leem `tentativas` sem `.get`, então ele é obrigatório.
BASE = {"tentativas": 0}


def estado(**campos):
    return {**BASE, **campos}


# ---------- guard de aderência da spec ----------

def test_spec_coerente_segue_para_desenvolvimento():
    d = rotas.pos_validacao_spec(estado(spec_coerente=True))
    assert d.destino == "desenvolvimento"
    assert d.teto is None and d.erro is None


def test_spec_incoerente_com_orcamento_replaneja():
    assert rotas.pos_validacao_spec(estado(spec_tentativas=1)).destino == "planejamento"


def test_spec_incoerente_no_teto_interrompe_a_execucao():
    d = rotas.pos_validacao_spec(estado(spec_tentativas=MAX_REPLANEJAMENTOS + 1))
    assert d.destino is None
    assert d.erro and "incoerentes" in d.erro
    assert d.teto == rotas.Teto("planejamento", MAX_REPLANEJAMENTOS)


# ---------- pós-desenvolvimento: preservar a suíte ----------

def test_correcao_de_revisao_preserva_a_suite():
    """RESILIENCIA 29: repagar `escrever_testes` por apontamento de
    legibilidade foi metade do custo de uma execução."""
    d = rotas.pos_desenvolvimento(
        estado(origem_feedback="revisao", arquivos=["app.py", "tests/test_app.py"])
    )
    assert d.destino == "executar_testes"
    assert d.aviso and "suíte preservada" in d.aviso


def test_correcao_de_pentest_e_de_visual_tambem_preservam():
    for origem in ("pentest", "visual"):
        d = rotas.pos_desenvolvimento(
            estado(origem_feedback=origem, arquivos=["app.py", "tests/t.py"])
        )
        assert d.destino == "executar_testes", origem


def test_sem_suite_no_workspace_o_qa_precisa_escrever():
    d = rotas.pos_desenvolvimento(
        estado(origem_feedback="revisao", arquivos=["app.py"])
    )
    assert d.destino == "escrever_testes"


def test_rodada_inicial_e_correcao_de_teste_reescrevem_a_suite():
    for origem in ("", "testes"):
        d = rotas.pos_desenvolvimento(
            estado(origem_feedback=origem, arquivos=["app.py", "tests/t.py"])
        )
        assert d.destino == "escrever_testes", origem


def test_convencao_de_teste_e_injetavel():
    """A suíte vive em `tests/` no Python e em outro lugar nas demais stacks —
    por isso o predicado é parâmetro, não constante."""
    d = rotas.pos_desenvolvimento(
        estado(origem_feedback="revisao", arquivos=["src/app.ts", "src/app.test.ts"]),
        e_teste=lambda c: c.endswith(".test.ts"),
    )
    assert d.destino == "executar_testes"


# ---------- guard de critérios ----------

def test_suite_aderente_vai_executar():
    assert rotas.pos_validacao_testes(estado(testes_aderentes=True)).destino == "executar_testes"


def test_suite_nao_aderente_com_orcamento_volta_ao_qa():
    d = rotas.pos_validacao_testes(estado(testes_aderentes=False, testes_tentativas=1))
    assert d.destino == "escrever_testes"
    assert d.teto is None


def test_guard_de_criterios_no_teto_segue_mesmo_assim():
    """Qualidade de teste é sinal mais brando que teste vermelho: ao estourar,
    não bloqueia — registra e segue."""
    d = rotas.pos_validacao_testes(
        estado(testes_aderentes=False, testes_tentativas=MAX_TESTES)
    )
    assert d.destino == "executar_testes"
    assert d.teto == rotas.Teto("escrever_testes", MAX_TESTES)


# ---------- pós-pytest ----------

def test_pytest_vermelho_com_orcamento_volta_ao_desenvolvimento():
    d = rotas.pos_testes(estado(testes_ok=False, tentativas=1))
    assert d.destino == "desenvolvimento"


def test_pytest_vermelho_sem_orcamento_chama_o_humano():
    d = rotas.pos_testes(estado(testes_ok=False, tentativas=MAX_TENTATIVAS))
    assert d.destino == "aprovacao_humana"
    assert d.teto == rotas.Teto("correcao", MAX_TENTATIVAS)


def test_verde_sem_cobertura_faz_o_laco_curto_do_qa():
    """Não paga outra rodada de desenvolvimento: o problema é do teste."""
    d = rotas.pos_testes(estado(testes_ok=True, cobertura_ok=False, testes_tentativas=0))
    assert d.destino == "escrever_testes"


def test_verde_sem_cobertura_mas_sem_orcamento_de_qa_segue_para_revisao():
    d = rotas.pos_testes(
        estado(testes_ok=True, cobertura_ok=False, testes_tentativas=MAX_TESTES)
    )
    assert d.destino == "revisao"


def test_verde_e_coberto_vai_a_revisao():
    assert rotas.pos_testes(estado(testes_ok=True, cobertura_ok=True)).destino == "revisao"


# ---------- os três sinais caros, com teto próprio ----------

def test_revisao_aprovada_segue_ao_pentest_nao_ao_gate():
    """Testes verdes e revisão limpa não provam que a entrega resiste a ataque."""
    assert rotas.pos_revisao(estado(aprovado=True)).destino == "pentest"


def test_pentest_aprovado_segue_a_verificacao_visual():
    assert rotas.pos_pentest(estado(pentest_ok=True)).destino == "visual"


def test_visual_aprovado_segue_ao_gate_humano():
    assert rotas.pos_visual(estado(visual_ok=True)).destino == "aprovacao_humana"


def test_cada_sinal_caro_tem_teto_proprio():
    """RESILIENCIA: sem teto separado, o sinal caro e subjetivo consome sozinho
    as rodadas reservadas ao sinal barato e determinístico."""
    casos = [
        (rotas.pos_revisao, {"aprovado": False, "revisao_tentativas": MAX_REVISOES},
         rotas.Teto("revisao", MAX_REVISOES)),
        (rotas.pos_pentest, {"pentest_ok": False, "pentest_tentativas": MAX_PENTEST},
         rotas.Teto("pentest", MAX_PENTEST)),
        (rotas.pos_visual, {"visual_ok": False, "visual_tentativas": MAX_VISUAL},
         rotas.Teto("visual", MAX_VISUAL)),
    ]
    for rota, campos, teto in casos:
        d = rota(estado(**campos))
        assert d.destino == "aprovacao_humana", rota.__name__
        assert d.teto == teto, rota.__name__
        assert d.aviso, rota.__name__


def test_sinal_caro_com_orcamento_volta_ao_desenvolvimento():
    casos = [
        (rotas.pos_revisao, {"aprovado": False, "revisao_tentativas": 0}),
        (rotas.pos_pentest, {"pentest_ok": False, "pentest_tentativas": 0}),
        (rotas.pos_visual, {"visual_ok": False, "visual_tentativas": 0}),
    ]
    for rota, campos in casos:
        d = rota(estado(tentativas=1, **campos))
        assert d.destino == "desenvolvimento", rota.__name__
        assert d.teto is None, rota.__name__


def test_teto_geral_encerra_mesmo_com_orcamento_do_sinal_caro():
    """`tentativas` é orçamento compartilhado: estourar ele encerra o laço
    mesmo que o teto específico ainda tenha folga."""
    casos = [
        (rotas.pos_revisao, {"aprovado": False, "revisao_tentativas": 0}),
        (rotas.pos_pentest, {"pentest_ok": False, "pentest_tentativas": 0}),
        (rotas.pos_visual, {"visual_ok": False, "visual_tentativas": 0}),
    ]
    for rota, campos in casos:
        d = rota(estado(tentativas=MAX_TENTATIVAS, **campos))
        assert d.destino == "aprovacao_humana", rota.__name__
        assert d.teto == rotas.Teto("correcao", MAX_TENTATIVAS), rota.__name__


def test_todo_teto_nomeia_um_laco_conhecido():
    """O painel agrupa por `laco`; um nome novo entrando calado viraria uma
    categoria órfã na tela de série."""
    conhecidos = {"planejamento", "escrever_testes", "correcao", "revisao",
                  "pentest", "visual"}
    disparos = [
        rotas.pos_validacao_spec(estado(spec_tentativas=MAX_REPLANEJAMENTOS + 1)),
        rotas.pos_validacao_testes(estado(testes_tentativas=MAX_TESTES)),
        rotas.pos_testes(estado(testes_ok=False, tentativas=MAX_TENTATIVAS)),
        rotas.pos_revisao(estado(revisao_tentativas=MAX_REVISOES)),
        rotas.pos_pentest(estado(pentest_tentativas=MAX_PENTEST)),
        rotas.pos_visual(estado(visual_tentativas=MAX_VISUAL)),
    ]
    for d in disparos:
        assert d.teto is not None
        assert d.teto.laco in conhecidos, d.teto
