"""O perfil de stack e o registro que o resolve.

O perfil é a costura por onde Next.js e Java entram. Estes testes fixam o
contrato que qualquer stack nova precisa cumprir — e o comportamento do perfil
Python, que até aqui estava espalhado por seis arquivos.
"""
import json

import pytest

from src.squad.adaptadores import perfil_python, perfis
from src.squad.portas.perfil import Cobertura
from src.squad.portas.testes import ResultadoTestes


# ---------- registro ----------

def test_padrao_e_python():
    assert perfis.obter(None).nome == "python"
    assert perfis.obter("").nome == "python"


def test_normaliza_caixa_e_espaco():
    assert perfis.obter("  PYTHON ").nome == "python"


def test_stack_desconhecida_diz_o_que_existe():
    """Falha na triagem, em 1s, e não lá no sandbox com uma imagem inexistente
    depois de o planejamento já ter sido pago."""
    with pytest.raises(ValueError) as e:
        perfis.obter("rust")
    assert "rust" in str(e.value)
    assert "python" in str(e.value), "a mensagem precisa listar o que existe"


# ---------- contrato que toda stack precisa cumprir ----------

def test_perfil_declara_tudo_que_o_pipeline_consulta():
    for perfil in (perfis.obter(n) for n in perfis.nomes()):
        assert perfil.nome and perfil.runner, perfil.nome
        assert perfil.imagem_sandbox and perfil.dockerfile_sandbox, perfil.nome
        assert perfil.relatorio_cobertura, perfil.nome
        assert perfil.gitignore_entrega, perfil.nome
        assert perfil.imagem_alvo, perfil.nome
        assert callable(perfil.e_teste), perfil.nome
        assert callable(perfil.ler_cobertura), perfil.nome
        assert perfil.timeout_testes > 0 and perfil.memoria, perfil.nome


def test_toda_stack_ignora_o_proprio_diretorio_de_trabalho_da_squad():
    """`.squad/` guarda config do runner e o manifesto de subida — não é
    entrega, e entrar na varredura o mandaria ao revisor e ao deploy."""
    for perfil in (perfis.obter(n) for n in perfis.nomes()):
        assert ".squad" in perfil.ignorar_no_workspace, perfil.nome
        assert ".git" in perfil.ignorar_no_workspace, perfil.nome


def test_gitignore_da_entrega_cobre_o_que_a_varredura_ignora():
    """As duas listas resolvem o mesmo problema em pontos diferentes — se o
    `.gitignore` esquecer o que a varredura filtra, o lixo chega ao push."""
    perfil = perfis.obter("python")
    for cache in ("__pycache__", ".pytest_cache", ".squad"):
        assert cache in perfil.gitignore_entrega, cache


# ---------- perfil Python ----------

def test_python_reconhece_a_suite_em_tests():
    perfil = perfis.obter("python")
    assert perfil.e_teste("tests/test_app.py") is True
    assert perfil.e_teste("app.py") is False


def test_python_permite_o_runner_de_host():
    """Só o Python permite: o runner de host roda a suíte no interpretador da
    própria squad, o que exige que a entrega seja Python também."""
    assert perfis.obter("python").permite_host is True


def test_comando_do_container_usa_a_forma_com_m():
    """RESILIENCIA 16: `pytest` e `python -m pytest` não são a mesma coisa —
    só a forma com -m põe o diretório atual no sys.path, e sem ela
    `from app import ...` quebra na coleta dentro do container."""
    cmd = perfis.obter("python").comando_container("/out")
    assert "python -m pytest" in cmd
    assert "/out/coverage.json" in cmd


def test_comando_do_host_aponta_para_o_mesmo_relatorio():
    argv = perfis.obter("python").comando_host(".squad/out")
    assert "-m" in argv and "pytest" in argv
    assert any(".squad/out/coverage.json" in a for a in argv)


# ---------- leitura de cobertura ----------

def _coverage_json(total, arquivos):
    return json.dumps({
        "totals": {"percent_covered": total},
        "files": {
            nome: {"summary": {"num_statements": n, "percent_covered": pct}}
            for nome, (n, pct) in arquivos.items()
        },
    })


def test_le_total_e_pior_modulo():
    """O agregado sozinho engana: medido em execução real, 89% no total com
    51% no módulo da API."""
    cob = perfil_python._ler_cobertura(
        _coverage_json(89.0, {"app/api.py": (40, 51.0), "app/util.py": (10, 100.0)})
    )
    assert cob.total == 89.0
    assert cob.pior == 51.0
    assert cob.pior_arquivo == "app/api.py"


def test_arquivo_sem_instrucoes_nao_vira_o_pior():
    """`__init__.py` vazio reporta 100% e não diz nada sobre a suíte — mas com
    0 instruções também não pode puxar o piso para baixo."""
    cob = perfil_python._ler_cobertura(
        _coverage_json(80.0, {"app/__init__.py": (0, 0.0), "app/main.py": (20, 80.0)})
    )
    assert cob.pior_arquivo == "app/main.py"
    assert cob.pior == 80.0


def test_sem_arquivos_o_pior_e_o_total():
    cob = perfil_python._ler_cobertura(_coverage_json(70.0, {}))
    assert cob.total == 70.0 and cob.pior == 70.0 and cob.pior_arquivo == ""


def test_relatorio_ilegivel_devolve_cobertura_zerada():
    """Relatório que não veio ou veio truncado não pode derrubar o nó: o
    veredito de teste é o exit code, e cobertura zero reprova pelo piso."""
    for texto in ("", "não é json", "{}", '{"totals": {}}'):
        assert perfil_python._ler_cobertura(texto) == Cobertura(), repr(texto)


def test_normaliza_separador_de_caminho_do_windows():
    dados = json.loads(_coverage_json(50.0, {"x": (1, 50.0)}))
    dados["files"] = {"app\\api.py": dados["files"]["x"]}
    cob = perfil_python._ler_cobertura(json.dumps(dados))
    assert cob.pior_arquivo == "app/api.py"


# ---------- tradução para o estado do grafo ----------

def test_cobertura_vira_os_campos_que_o_grafo_espera():
    campos = Cobertura(total=91.2, pior=60.0, pior_arquivo="app/api.py").como_estado()
    assert campos == {
        "cobertura": 91.2,
        "cobertura_pior": 60.0,
        "cobertura_pior_arquivo": "app/api.py",
    }


def test_saida_dos_testes_e_cortada_pela_cauda():
    """RESILIENCIA: o corte é pela cauda porque o fim da saída é onde estão as
    falhas — cortar pelo começo descartaria justamente a instrução de conserto."""
    resultado = ResultadoTestes(
        testes_ok=False, saida="cabeça" + "x" * 50 + "CAUDA", cobertura=Cobertura()
    )
    campos = resultado.como_estado(limite_saida=5)
    assert campos["saida_testes"] == "CAUDA"
    assert campos["testes_ok"] is False
