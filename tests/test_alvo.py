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
    """`next start` sem `next build` não sobe, e `java -jar target/app.jar` sem
    `mvn package` não tem jar nenhum para rodar. Quem sabe disso é o perfil."""
    assert "next build" in perfis.obter("nextjs").preparo_alvo
    java = perfis.obter("java").preparo_alvo
    assert "mvn" in java and "package" in java


def test_alvo_java_sobe_o_jar_que_o_pom_nomeia():
    """O `finalName` do pom de referência é `app`, e o run.json de exemplo conta
    com isso. Se um dos dois mudar sozinho, o alvo não acha o que subir."""
    perfil = perfis.obter("java")
    assert "target/app.jar" in perfil.exemplo_run_json
    assert "<finalName>app</finalName>" in perfil.instrucoes_executor


def test_python_nao_precisa_de_preparo():
    assert perfis.obter("python").preparo_alvo == ""


def test_preparo_encadeia_com_o_comando():
    """É concatenado antes do `exec`, então precisa terminar em `&& ` — sem
    isso o comando do run.json vira argumento do preparo."""
    for nome in perfis.nomes():
        preparo = perfis.obter(nome).preparo_alvo
        assert preparo == "" or preparo.endswith("&& "), (nome, preparo)


# ---------- nome que também é hostname ----------

def test_nome_curto_passa_inteiro():
    assert alvo.nome_de_rede("alvo", "abc123") == "alvo-abc123"


def test_nome_longo_cabe_no_limite_de_dns():
    """O nome do container VIRA o hostname dele na rede do Docker, e um label de
    DNS tem 63 caracteres. Estourar não dá erro na criação: o container sobe e
    simplesmente não é resolvível, e o sintoma aparece do outro lado como "o
    alvo não respondeu" — que se lê como entrega que não sobe."""
    longo = "882504eb-1419-49de-bc2c-a02271e80225--ticket-service"
    assert len(alvo.nome_de_rede("alvo-visual", longo)) <= alvo.LIMITE_HOSTNAME


def test_o_caso_real_que_quebrou():
    """Medido numa execução: `alvo-visual-<uuid>--ticket-service` deu 64 chars e
    o alvo ficou irresolvível, enquanto `--room-service`, com 62, funcionou. Uma
    letra a mais no nome do serviço decidia se o nó rodava."""
    uuid = "882504eb-1419-49de-bc2c-a02271e80225"
    for servico in ("ticket-service", "room-service", "event-service"):
        nome = alvo.nome_de_rede("alvo-visual", f"{uuid}--{servico}")
        assert len(nome) <= alvo.LIMITE_HOSTNAME, (servico, len(nome))


def test_ramos_do_mesmo_pai_nao_colidem():
    """Truncar pelo fim juntaria dois ramos cujo id só difere no sufixo — que é
    exatamente a forma de `<uuid>--<servico>`."""
    uuid = "882504eb-1419-49de-bc2c-a02271e80225"
    nomes = {
        alvo.nome_de_rede("alvo-visual", f"{uuid}--{s}")
        for s in ("ticket-service", "room-service", "event-service", "ticket-services")
    }
    assert len(nomes) == 4


def test_nome_e_um_hostname_valido():
    import re
    uuid = "882504eb-1419-49de-bc2c-a02271e80225"
    for servico in ("ticket-service", "room-service", "a"):
        nome = alvo.nome_de_rede("alvo-visual", f"{uuid}--{servico}")
        assert re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", nome), nome


def test_o_mesmo_id_da_sempre_o_mesmo_nome():
    """`derrubar` e `logs` recompõem o nome depois; instável, o cleanup erraria
    o alvo e deixaria container órfão."""
    longo = "882504eb-1419-49de-bc2c-a02271e80225--ticket-service"
    assert alvo.nome_de_rede("alvo", longo) == alvo.nome_de_rede("alvo", longo)
