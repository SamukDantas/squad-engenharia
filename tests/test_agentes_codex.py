"""Os agentes da squad rodando pelo Codex (`LLM_PROVEDOR=codex`).

Sem rodar o binário: o que se verifica é como a squad monta a chamada, como
lê a resposta e como protege o código da entrega do QA.
"""
import subprocess

import pytest

from src.squad import llm
from src.squad.adaptadores import codex_cli, perfis
from src.squad.dominio import guards
from src.squad.graph import workflow

PYTHON = perfis.obter("python")


@pytest.fixture
def codex(monkeypatch):
    monkeypatch.setenv("LLM_PROVEDOR", "codex")
    monkeypatch.setenv("CODEX_RUN_MODEL", "gpt-5.6-luna")
    monkeypatch.setattr(codex_cli.shutil, "which", lambda _n: "C:/npm/codex")


# ---------- o LLM dos agentes ----------

def test_provedor_codex_da_um_codexllm_com_o_modelo_fixado(codex):
    modelo = llm.squad_llm()
    assert isinstance(modelo, llm.CodexLLM)
    assert modelo.model == "gpt-5.6-luna"


def test_chamada_acessoria_leva_teto_curto(codex, monkeypatch):
    monkeypatch.delenv("TIMEOUT_LLM_ACESSORIA", raising=False)
    assert llm.squad_llm(acessoria=True).timeout_s == 30


def test_mensagens_de_chat_viram_um_texto_na_ordem():
    texto = llm._achatar([
        {"role": "system", "content": "Você é o analista."},
        {"role": "user", "content": "Levante os requisitos."},
    ])
    assert texto.index("[SYSTEM]") < texto.index("Você é o analista.") < texto.index("[USER]")


def test_call_devolve_a_resposta_do_codex(codex, monkeypatch):
    recebido = {}

    def falso(texto, timeout, rotulo=""):
        recebido.update(texto=texto, timeout=timeout)
        return "RESPOSTA"

    monkeypatch.setattr(codex_cli, "responder", falso)
    assert llm.squad_llm().call("pergunta") == "RESPOSTA"
    assert recebido["texto"] == "pergunta"


def test_agente_com_ferramenta_e_recusado(codex):
    with pytest.raises(ValueError, match="ferramenta"):
        llm.squad_llm().call("x", tools=[{"name": "escrever_arquivo"}])


def test_build_agent_com_ferramenta_falha_na_montagem(codex):
    """Falhar na montagem poupa pagar planejamento e guards para morrer depois."""
    from src.squad.crews.base import build_agent
    with pytest.raises(RuntimeError, match="DEV_EXECUTOR=codex"):
        build_agent("qa_engineer", tools=[object()])


# ---------- a chamada de texto ----------

def test_texto_vai_por_stdin_em_sandbox_read_only(codex, monkeypatch):
    """Argumento multilinha é truncado pelo shim do npm no Windows; o pedido
    longo entra por stdin, e o agente que só responde não escreve nada."""
    chamado = {}

    def run(cmd, **kw):
        chamado.update(cmd=cmd, input=kw.get("input"))
        saida = '{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}'
        return subprocess.CompletedProcess(cmd, 0, stdout=saida, stderr="")

    monkeypatch.setattr(codex_cli.subprocess, "run", run)
    assert codex_cli.responder("PEDIDO\nLONGO", timeout=10) == "OK"
    cmd = chamado["cmd"]
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert cmd[cmd.index("--model") + 1] == "gpt-5.6-luna"
    assert cmd[-1] == codex_cli.PROMPT_TEXTO
    assert chamado["input"] == "PEDIDO\nLONGO"


def test_resposta_vazia_falha_alto(codex, monkeypatch):
    monkeypatch.setattr(codex_cli.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))
    with pytest.raises(RuntimeError, match="não devolveu mensagem"):
        codex_cli.responder("x", timeout=10)


# ---------- credencial ----------

def test_codex_sem_login_falha_com_instrucao(codex, monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="Not logged in", stderr=""))
    with pytest.raises(RuntimeError, match="não está logado"):
        llm.conferir_credencial()


def test_codex_logado_passa(codex, monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="Logged in using ChatGPT", stderr=""))
    assert llm.conferir_credencial() is None


# ---------- QA pelo Codex: o código da entrega fica protegido ----------

def test_regra_separa_o_que_o_qa_mexeu_fora_da_suite():
    antes = {"app.py": "h1", "tests/test_a.py": "t1", "util.py": "h2"}
    depois = {"app.py": "MUDOU", "tests/test_a.py": "t2", "tests/test_b.py": "t3", "novo.py": "n"}
    alterados, criados = guards.fora_da_suite(antes, depois, PYTHON.e_teste)
    assert alterados == ["app.py", "util.py"]   # alterado e apagado
    assert criados == ["novo.py"]               # teste novo não conta


def test_qa_pelo_codex_desfaz_o_que_mexeu_no_codigo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # registrar() grava em metrics/ relativo à raiz
    ws = tmp_path / "ws"
    (ws / "tests").mkdir(parents=True)
    (ws / "app.py").write_text("ORIGINAL", encoding="utf-8")
    (ws / "util.py").write_text("UTIL", encoding="utf-8")

    def qa_que_trapaceia(workspace, descricao):
        assert "Spec: SPEC" in descricao  # a tarefa do tasks.yaml, preenchida
        (ws / "app.py").write_text("QA MUDOU O CÓDIGO", encoding="utf-8")
        (ws / "util.py").unlink()
        (ws / "extra.py").write_text("x", encoding="utf-8")
        (ws / "tests" / "test_app.py").write_text("def test_x(): ...", encoding="utf-8")

    monkeypatch.setattr(workflow, "executar_qa_codex", qa_que_trapaceia)
    entradas = {**workflow._do_perfil(PYTHON), "spec": "SPEC", "arquivos": "app.py",
                "feedback_qa": "Nenhum — primeira rodada."}
    workflow._qa_pelo_codex({"workspace": str(ws), "thread_id": "t"}, PYTHON, entradas)

    assert (ws / "app.py").read_text(encoding="utf-8") == "ORIGINAL"
    assert (ws / "util.py").read_text(encoding="utf-8") == "UTIL"
    assert not (ws / "extra.py").exists()
    assert (ws / "tests" / "test_app.py").exists()  # a suíte é dele


# ---------- guard de critérios: só comportamento ----------

def test_guard_de_criterios_julga_so_comportamento():
    """Thread `fecf5303`: rodando pelo Codex, o guard reprovou a suíte duas
    vezes por "usa só a biblioteca padrão", "sem estado global" e "anotações
    de tipo" — requisitos que não se verificam por teste de comportamento."""
    import inspect
    fonte = inspect.getsource(workflow.no_validacao_testes)
    assert "FUNCIONAIS" in fonte and "Julgue SÓ comportamento" in fonte


# ---------- run.json só para servidor ----------

def test_executor_so_escreve_run_json_se_a_entrega_for_servidor(tmp_path):
    """No mesmo caso, a tarefa exigia run.json de toda entrega, e o executor
    inventou um `main.py` HTTP para um módulo — que ficou a 0% e derrubou a
    cobertura para 46,9%."""
    from src.squad.adaptadores import tarefa_executor
    texto = tarefa_executor.instrucoes("SPEC", "", PYTHON)
    assert "SÓ SE a entrega for um servidor" in texto
    assert "NÃO crie servidor HTTP nem `run.json`" in texto


def test_pentest_sem_run_json_nao_tem_o_que_atacar(tmp_path):
    """Sem servidor não há superfície HTTP: verde, sem Docker — como a
    verificação visual faz sem HTML."""
    from src.squad import pentest
    resultado = pentest.executar_pentest(str(tmp_path), "t", PYTHON)
    assert resultado == {"pentest_ok": True, "vulnerabilidades": []}


def test_guard_de_criterios_recebe_o_ambiente_de_testes():
    import inspect
    fonte = inspect.getsource(workflow.no_validacao_testes)
    assert "ambiente_testes" in fonte and "Só " in fonte
