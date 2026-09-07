"""Subir a entrega como servidor: o manifesto e o preparo de cada stack.

O módulo `alvo` nasceu de uma duplicação que ia acontecer: o pentest já subia a
entrega numa rede isolada, e a verificação visual de SPA precisa exatamente do
mesmo — julgar a entrega **de pé**, não em repouso. O que muda é o que se faz
com o alvo no ar.
"""
import json

import pytest

from src.squad import alvo
from src.squad.adaptadores import perfis

EXIGIDO = "O pentest"
DESLIGAR = "desligue com PENTEST_HABILITADO=0"


def _escrever(tmp_path, dados):
    destino = tmp_path / alvo.ARQUIVO_RUN
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(
        dados if isinstance(dados, str) else json.dumps(dados), encoding="utf-8"
    )
    return tmp_path


# ---------- manifesto de subida ----------

def test_le_o_manifesto_completo(tmp_path):
    ws = _escrever(tmp_path, {
        "cmd": ["uvicorn", "main:app"], "port": 8000, "health_path": "/health",
    })
    run = alvo.ler_run(str(ws), EXIGIDO, DESLIGAR)
    assert run == {"cmd": ["uvicorn", "main:app"], "port": 8000, "health_path": "/health"}


def test_health_path_tem_padrao(tmp_path):
    ws = _escrever(tmp_path, {"cmd": ["node", "server.js"], "port": 3000})
    assert alvo.ler_run(str(ws), EXIGIDO, DESLIGAR)["health_path"] == "/"


def test_manifesto_ausente_diz_quem_o_exigiu(tmp_path):
    """Dois nós chegam aqui, e a mensagem precisa dizer qual deles parou — senão
    quem lê não sabe o que desligar."""
    with pytest.raises(RuntimeError) as e:
        alvo.ler_run(str(tmp_path), EXIGIDO, DESLIGAR)
    assert EXIGIDO in str(e.value) and DESLIGAR in str(e.value)
    assert alvo.ARQUIVO_RUN in str(e.value)


def test_manifesto_invalido_mostra_o_que_recebeu(tmp_path):
    """O executor escreve o arquivo; quando ele erra o formato, a mensagem tem
    de mostrar o que veio, não só dizer que está errado."""
    for dados in ({"cmd": [], "port": 8000}, {"cmd": ["x"]}, {"cmd": "uvicorn", "port": 1}):
        ws = _escrever(tmp_path, dados)
        with pytest.raises(RuntimeError) as e:
            alvo.ler_run(str(ws), EXIGIDO, DESLIGAR)
        assert "cmd" in str(e.value) and "port" in str(e.value), dados


def test_manifesto_nao_json_nao_derruba_com_traceback(tmp_path):
    ws = _escrever(tmp_path, "isto não é json")
    with pytest.raises(RuntimeError) as e:
        alvo.ler_run(str(ws), EXIGIDO, DESLIGAR)
    assert "não é JSON válido" in str(e.value)


def test_argumentos_do_cmd_sao_escapados_para_o_shell_do_container():
    """O comando entra num `sh -c` do container Linux. Sem escapar, um argumento
    com espaço ou aspas quebraria a linha de comando — ou pior, a estenderia."""
    assert alvo.shquote("main:app") == "main:app"
    assert " " not in alvo.shquote("a b") or alvo.shquote("a b").startswith("'")
    assert alvo.shquote("; rm -rf /").startswith("'")


# ---------- preparo por stack ----------

def test_stack_que_compila_prepara_o_alvo_antes_de_subir():
    """`next start` sem `next build` não sobe, e `java -cp target/classes` sem
    `mvn compile` não acha classe nenhuma. Quem sabe disso é o perfil."""
    assert "next build" in perfis.obter("nextjs").preparo_alvo
    assert "mvn" in perfis.obter("java").preparo_alvo and "compile" in perfis.obter("java").preparo_alvo


def test_python_nao_precisa_de_preparo():
    assert perfis.obter("python").preparo_alvo == ""


def test_preparo_encadeia_com_o_comando():
    """É concatenado antes do `exec`, então precisa terminar em `&& ` — sem
    isso o comando do run.json vira argumento do preparo."""
    for nome in perfis.nomes():
        preparo = perfis.obter(nome).preparo_alvo
        assert preparo == "" or preparo.endswith("&& "), (nome, preparo)
