"""Correção inerte num vermelho de asserção: o erro pode ser do teste.

Caso real (thread `33c19d49`, calculadora de juros em Next.js): 61 de 62 testes
passavam; o vermelho era um teste do QA que exigia 2 casas decimais com
tolerância 1e-6 num montante da ordem de 10^10. O código estava certo, o
executor não mudou nada — e o guard de correção derrubou a execução como se o
executor tivesse ignorado o feedback.
"""
import pytest

from src.squad.dominio import guards, rotas
from src.squad.graph import workflow

VITEST_ASSERCAO = """ FAIL  lib/juros.test.ts > invariantes numéricas > montante com 2 casas
AssertionError: expected false to be true // Object.is equality
 ❯ lib/juros.test.ts:191:41
 Test Files  1 failed | 6 passed (7)
      Tests  1 failed | 61 passed (62)"""

PYTEST_ASSERCAO = """    def test_soma():
>       assert soma(1, 2) == 4
E       assert 3 == 4
E        +  where 3 = soma(1, 2)
FAILED tests/test_app.py::test_soma - AssertionError"""

PYTEST_IMPORT = """ERROR collecting tests/test_app.py
ModuleNotFoundError: No module named 'app'"""

VITEST_AMBIENTE = """ReferenceError: React is not defined
AssertionError: expected 1 to be 2"""


# ---------- classificação do vermelho ----------

@pytest.mark.parametrize("saida", [VITEST_ASSERCAO, PYTEST_ASSERCAO])
def test_asercao_e_reconhecida(saida):
    assert guards.falha_de_assercao(saida) is True


@pytest.mark.parametrize("saida", [
    PYTEST_IMPORT,
    VITEST_AMBIENTE,  # asserção junto de erro de ambiente: o ambiente manda
    "error TS2304: Cannot find name 'x'.",
    "",
])
def test_import_ambiente_ou_vazio_nao_e_asercao(saida):
    """Nesses, não há teste para o QA revisar: é código ou ambiente."""
    assert guards.falha_de_assercao(saida) is False


# ---------- decisão ----------

def test_primeira_vez_revisa_a_suite():
    assert guards.destino_da_correcao_inerte(
        "testes", VITEST_ASSERCAO, False, revisoes_suite=0
    ) == "revisar_suite"


def test_depois_da_revisao_o_impasse_vai_ao_humano():
    assert guards.destino_da_correcao_inerte(
        "testes", VITEST_ASSERCAO, False, revisoes_suite=rotas.MAX_REVISOES_SUITE
    ) == "aprovacao_humana"


@pytest.mark.parametrize("origem,saida,build", [
    ("revisao", VITEST_ASSERCAO, False),  # correção pedida pelo revisor
    ("testes", PYTEST_IMPORT, False),     # vermelho de import
    ("testes", VITEST_ASSERCAO, True),    # não compilou: nenhum teste rodou
])
def test_fora_do_caso_mantem_o_erro_de_antes(origem, saida, build):
    assert guards.destino_da_correcao_inerte(origem, saida, build, 0) == "erro"


def test_brief_do_qa_traz_a_saida_e_proibe_enfraquecer():
    brief = guards.brief_revisao_suite(VITEST_ASSERCAO)
    assert "NÃO alterou o código" in brief
    assert "Não remova nem enfraqueça" in brief
    assert "lib/juros.test.ts:191" in brief


# ---------- rota ----------

def test_rota_leva_a_suite_de_volta_ao_qa():
    d = rotas.pos_desenvolvimento({"tentativas": 2, "correcao_inerte": "revisar_suite",
                                   "origem_feedback": "testes"})
    assert d.destino == "escrever_testes" and d.teto is None


def test_rota_leva_o_impasse_ao_gate_com_teto():
    d = rotas.pos_desenvolvimento({"tentativas": 3, "correcao_inerte": "aprovacao_humana"})
    assert d.destino == "aprovacao_humana"
    assert d.teto == rotas.Teto("suite_contestada", rotas.MAX_REVISOES_SUITE)


def test_correcao_que_mudou_segue_o_roteamento_de_sempre():
    d = rotas.pos_desenvolvimento({"tentativas": 2, "correcao_inerte": "",
                                   "origem_feedback": "revisao",
                                   "arquivos": ["tests/test_app.py"]})
    assert d.destino == "executar_testes"


# ---------- nó: tradução em estado ----------

IGUAL = {"lib/juros.ts": "abc"}


def _estado(**extra):
    return {"saida_testes": VITEST_ASSERCAO, "falha_de_build": False, **extra}


def test_no_marca_revisao_e_entrega_o_brief_ao_qa():
    extra = workflow._correcao_inerte(_estado(), IGUAL, dict(IGUAL), "testes")
    assert extra["correcao_inerte"] == "revisar_suite"
    assert extra["revisoes_suite"] == 1
    assert "NÃO alterou o código" in extra["feedback_qa"]


def test_no_leva_impasse_ao_gate_com_diagnostico():
    extra = workflow._correcao_inerte(_estado(revisoes_suite=1), IGUAL, dict(IGUAL), "testes")
    assert extra["correcao_inerte"] == "aprovacao_humana"
    assert "Impasse entre desenvolvimento e QA" in extra["motivo_gate"]


def test_no_mantem_o_erro_fora_do_caso():
    with pytest.raises(RuntimeError, match="sem alterar a entrega"):
        workflow._correcao_inerte(
            _estado(saida_testes=PYTEST_IMPORT), IGUAL, dict(IGUAL), "testes"
        )


def test_no_com_mudanca_nao_marca_nada():
    extra = workflow._correcao_inerte(_estado(), IGUAL, {"lib/juros.ts": "xyz"}, "testes")
    assert extra == {"correcao_inerte": ""}
