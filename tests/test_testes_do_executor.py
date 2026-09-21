"""O executor não escreve testes — e o grafo garante, não só o prompt.

Caso real (thread `055e0f17`, conversor de temperaturas com o Codex): o
executor criou `test_temperatura.py` na raiz. O perfil Python só reconhecia
teste dentro de `tests/`, então o arquivo entrou na cobertura como código a 0%
e a primeira medição deu 31,8% em vez de 100% — uma passada extra do QA.
"""
import pytest

from src.squad.adaptadores import perfis
from src.squad.dominio import guards
from src.squad.graph import workflow

E_TESTE = perfis.obter("python").e_teste


# ---------- o que o perfil Python chama de teste ----------

@pytest.mark.parametrize("caminho", [
    "tests/test_app.py", "test_temperatura.py", "pkg/test_x.py",
    "calc_test.py", "conftest.py", "tests/helpers.py",
])
def test_nomes_que_o_pytest_trata_como_teste(caminho):
    assert E_TESTE(caminho) is True


@pytest.mark.parametrize("caminho", ["temperatura.py", "testes.py", "contest.py", "main.py"])
def test_codigo_nao_vira_teste_por_parecido(caminho):
    assert E_TESTE(caminho) is False


# ---------- a decisão ----------

def test_so_os_testes_novos_sao_do_executor():
    """A suíte do QA de rodadas anteriores já estava lá; apagá-la destruiria o
    que o laço de correção está tentando satisfazer."""
    antes = {"tests/test_qa.py"}
    depois = ["temperatura.py", "tests/test_qa.py", "test_temperatura.py"]
    assert guards.testes_do_executor(antes, depois, E_TESTE) == ["test_temperatura.py"]


def test_arquivo_pedido_pela_spec_e_entrega_nao_rastro():
    """Thread `055e0f17`: a spec listava `test_temperatura.py` como parte da
    entrega e o QA escreveu um teste que o importava. Removê-lo quebrou a
    suíte; dentro do laço, a regra o apagaria a cada rodada."""
    spec = "## Arquivos\n- `temperatura.py`\n- `test_temperatura.py` com unittest"
    assert guards.testes_do_executor(set(), ["temperatura.py", "test_temperatura.py"],
                                     E_TESTE, spec=spec) == []


def test_spec_que_cita_outro_arquivo_nao_protege_este():
    """Fronteira de palavra: `test_a.py` na spec não protege `test_abc.py`, nem
    `tests/test_temp.py` protege um `test_temp.py` na raiz sem ser citado."""
    spec = "Inclua test_a.py e tests/test_temp.py"
    depois = ["test_abc.py", "test_a.py"]
    assert guards.testes_do_executor(set(), depois, E_TESTE, spec=spec) == ["test_abc.py"]


def test_nome_citado_sem_pasta_protege_o_arquivo_em_subpasta():
    spec = "O pacote traz um test_x.py de exemplo."
    assert guards.testes_do_executor(set(), ["pkg/test_x.py"], E_TESTE, spec=spec) == []


def test_sem_teste_novo_nada_e_removido():
    assert guards.testes_do_executor({"tests/a.py"}, ["app.py", "tests/a.py"], E_TESTE) == []


# ---------- o nó ----------

def test_no_apaga_o_teste_do_executor_e_preserva_o_resto(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # registrar() grava em metrics/ relativo à raiz
    ws = tmp_path / "ws"
    (ws / "tests").mkdir(parents=True)
    (ws / "temperatura.py").write_text("def c(): ...", encoding="utf-8")
    (ws / "tests" / "test_qa.py").write_text("def test_q(): ...", encoding="utf-8")
    (ws / "test_temperatura.py").write_text("def test_x(): ...", encoding="utf-8")

    workflow._remover_testes_do_executor(
        {"workspace": str(ws), "thread_id": "t"},
        {"tests/test_qa.py"},
        perfis.obter("python"),
    )

    assert not (ws / "test_temperatura.py").exists()
    assert (ws / "tests" / "test_qa.py").exists()
    assert (ws / "temperatura.py").exists()


def test_no_preserva_o_teste_que_a_spec_pediu(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "temperatura.py").write_text("def c(): ...", encoding="utf-8")
    (ws / "test_temperatura.py").write_text("def test_x(): ...", encoding="utf-8")

    workflow._remover_testes_do_executor(
        {"workspace": str(ws), "thread_id": "t",
         "spec": "Entregue `temperatura.py` e `test_temperatura.py`."},
        set(),
        perfis.obter("python"),
    )

    assert (ws / "test_temperatura.py").exists()
