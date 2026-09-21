"""A mesma asserção depois da correção devolve a suíte ao QA.

Thread `9a86ddb0` (calculadora de juros): o QA calculou de cabeça
1234,56 x 1,025^3 = 1329,67 (o certo é 1329,49). O executor mexeu no código
duas vezes tentando satisfazer o número, a mesma asserção falhou nas duas, e a
execução bateu o teto de correção sem a suíte voltar ao QA — a rota de suíte
contestada só existia para a rodada que não mudava nada.
"""
from src.squad.adaptadores import perfis
from src.squad.crews.base import TASKS_CFG
from src.squad.dominio import guards, rotas

VITEST = """ ❯ lib/juros.test.ts (9 tests | 1 failed) 16ms
   × funções de juros > interpreta a taxa como percentual 8ms
     → expected { montante: 1329.49, …(1) } to deeply equal { montante: 1329.67, …(1) }
 FAIL  lib/juros.test.ts > funções de juros > interpreta a taxa como percentual
AssertionError: expected { montante: 1329.49 } to deeply equal { montante: 1329.67 }"""

PYTEST = """FAILED tests/test_conv.py::test_kelvin - AssertionError: assert 273.1 == 273.15
FAILED tests/test_conv.py::test_zero - AssertionError: assert 1 == 0"""


def test_nomes_dos_testes_vermelhos_nos_dois_runners():
    assert guards.testes_que_falharam(VITEST) == {
        "lib/juros.test.ts > funções de juros > interpreta a taxa como percentual"
    }
    assert guards.testes_que_falharam(PYTEST) == {
        "tests/test_conv.py::test_kelvin", "tests/test_conv.py::test_zero",
    }


def test_mesma_assercao_antes_e_depois_e_falha_repetida():
    assert guards.falha_repetida(VITEST, VITEST)


def test_mensagem_com_outro_valor_ainda_e_o_mesmo_teste():
    depois = PYTEST.replace("273.1 ==", "273.2 ==")
    assert guards.falha_repetida(PYTEST, depois)


def test_outro_conjunto_de_testes_vermelhos_e_progresso():
    so_um = PYTEST.splitlines()[0]
    assert not guards.falha_repetida(PYTEST, so_um)


def test_vermelho_de_import_nao_e_suite_contestada():
    """Import quebrado costuma apontar para o que não existe: quem conserta
    é o QA pela rota normal, não por contestação."""
    saida = "FAILED tests/test_x.py::test_a - ModuleNotFoundError: No module named 'x'"
    assert not guards.falha_repetida(saida, saida)


def test_suite_contestada_vai_ao_qa_mesmo_no_fim_do_orcamento():
    decisao = rotas.pos_testes({
        "testes_ok": False, "suite_contestada": True,
        "tentativas": rotas.MAX_TENTATIVAS,
    })
    assert decisao.destino == "escrever_testes"


def test_sem_contestacao_segue_o_laco_normal():
    decisao = rotas.pos_testes({"testes_ok": False, "tentativas": 1})
    assert decisao.destino == "desenvolvimento"


def test_brief_manda_recalcular_por_comando():
    brief = guards.brief_falha_repetida(VITEST)
    assert "nunca de cabeça" in brief and "1329.67" in brief


# ---------- as regras que evitam o erro na origem ----------

def test_qa_calcula_valor_esperado_por_comando():
    descricao = TASKS_CFG["escrever_testes"]["description"]
    assert "calculado com um comando do terminal, nunca de cabeça" in descricao


def test_seletor_estavel_dos_dois_lados():
    """Thread `9a86ddb0`: o QA selecionou `.amount`, a página usava
    `className={styles.amount}`, e 4 testes leram vazio."""
    perfil = perfis.obter("nextjs")
    assert "NUNCA selecione pela classe de um CSS Module" in perfil.instrucoes_qa
    assert "data-testid" in perfil.instrucoes_executor
