"""O Codex CLI como executor (`DEV_EXECUTOR=codex`).

Sem rodar o binário: o que se verifica é a jaula que a squad monta em volta
dele e como ela lê a falha — as duas coisas que já custaram execução real com
o outro executor.
"""
import json

import pytest

from src.squad.adaptadores import codex_cli, tarefa_executor
from src.squad.adaptadores import perfis


@pytest.fixture
def binario(monkeypatch):
    monkeypatch.setattr(codex_cli.shutil, "which", lambda _nome: "C:/npm/codex")
    monkeypatch.delenv("CODEX_RUN_MODEL", raising=False)
    monkeypatch.delenv("CODEX_SANDBOX_WINDOWS", raising=False)


def _cmd(monkeypatch, windows=True, **env):
    monkeypatch.setattr(codex_cli.os, "name", "nt" if windows else "posix")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return codex_cli._comando("/ws/entrega")


# ---------- a jaula ----------

def test_escrita_confinada_e_config_da_maquina_ignorada(binario, monkeypatch):
    cmd = _cmd(monkeypatch)
    assert cmd[:2] == ["C:/npm/codex", "exec"]
    assert "--cd" in cmd and cmd[cmd.index("--cd") + 1] == "/ws/entrega"
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    # O executor não pode herdar MCP, skills nem regras da máquina: é a mesma
    # correção que o adaptador do OpenCode faz por JSON injetado.
    assert "--ignore-user-config" in cmd and "--ignore-rules" in cmd
    assert "--ephemeral" in cmd          # sem arquivo de sessão em disco
    assert "--skip-git-repo-check" in cmd  # workspace só vira git no deploy
    assert "--json" in cmd                 # falha explícita, não caça-frases
    assert cmd[-1] == tarefa_executor.PROMPT


def test_sandbox_do_windows_e_declarado(binario, monkeypatch):
    """`--ignore-user-config` apaga o `[windows] sandbox` do config.toml, e sem
    ele a política recusa TODO comando e a run sai com exit 0 sem fazer nada —
    medido nesta máquina ao validar o CLI."""
    cmd = _cmd(monkeypatch)
    assert '-c' in cmd and 'windows.sandbox="unelevated"' in cmd


def test_modo_do_sandbox_e_configuravel(binario, monkeypatch):
    cmd = _cmd(monkeypatch, CODEX_SANDBOX_WINDOWS="elevated")
    assert 'windows.sandbox="elevated"' in cmd


def test_fora_do_windows_nao_manda_a_flag(binario, monkeypatch):
    cmd = _cmd(monkeypatch, windows=False)
    assert not any(a.startswith("windows.sandbox") for a in cmd)


def test_modelo_opcional(binario, monkeypatch):
    assert "--model" not in _cmd(monkeypatch)
    cmd = _cmd(monkeypatch, CODEX_RUN_MODEL="gpt-5.6-sol")
    assert cmd[cmd.index("--model") + 1] == "gpt-5.6-sol"


def test_sem_binario_o_erro_diz_o_que_fazer(monkeypatch):
    monkeypatch.setattr(codex_cli.shutil, "which", lambda _nome: None)
    with pytest.raises(RuntimeError, match="npm i -g @openai/codex"):
        codex_cli._comando("/ws/entrega")


# ---------- leitura da falha no stream ----------

def _jsonl(*eventos):
    return "\n".join(json.dumps(e) for e in eventos)


def test_run_saudavel_nao_acusa_falha():
    saida = _jsonl(
        {"type": "thread.started", "thread_id": "x"},
        {"type": "item.completed", "item": {"type": "file_change"}},
        {"type": "turn.completed", "usage": {"output_tokens": 12}},
    )
    assert codex_cli._falha_no_stream(saida) is None


def test_turno_que_morreu_e_reconhecido():
    """Exit code classifica o processo, não o trabalho (RESILIENCIA.md, 15)."""
    saida = _jsonl(
        {"type": "turn.started"},
        {"type": "turn.failed", "error": {"message": "command rejected: blocked by policy"}},
    )
    assert "blocked by policy" in codex_cli._falha_no_stream(saida)


def test_evento_de_erro_tambem_conta():
    assert "sem credencial" in codex_cli._falha_no_stream(
        _jsonl({"type": "error", "message": "sem credencial"})
    )


def test_linha_que_nao_e_json_nao_quebra_a_leitura():
    """O CLI mistura texto humano no stream; ignorar é o certo, mas ignorar
    demais esconderia a falha."""
    saida = "Vou ler as instruções...\n" + _jsonl({"type": "turn.failed", "error": {}})
    assert codex_cli._falha_no_stream(saida) == "turn.failed"
    assert codex_cli._falha_no_stream("só texto, sem stream") is None


# ---------- a tarefa é a mesma dos dois executores ----------

def test_tarefa_gravada_no_workspace(tmp_path):
    perfil = perfis.obter("python")
    arquivo = tarefa_executor.gravar_tarefa(str(tmp_path), "SPEC AQUI", "", perfil)
    texto = arquivo.read_text(encoding="utf-8")
    assert arquivo == tmp_path / ".squad" / "tarefa.md"
    assert "SPEC AQUI" in texto
    assert perfil.instrucoes_executor.splitlines()[0] in texto
    assert "## Rodada de correção" not in texto


def test_feedback_vira_bloco_de_correcao(tmp_path):
    arquivo = tarefa_executor.gravar_tarefa(
        str(tmp_path), "SPEC", "o teste x falhou", perfis.obter("python")
    )
    texto = arquivo.read_text(encoding="utf-8")
    assert "## Rodada de correção" in texto and "o teste x falhou" in texto


def test_log_mostra_a_mensagem_do_agente_e_nao_o_jsonl():
    """Com `--json` o stream é para máquina; o terminal recebe o que o agente
    disse ter feito — quem julga continua sendo a varredura do disco."""
    saida = _jsonl(
        {"type": "item.completed", "item": {"type": "command_execution", "command": "ls"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Implementei precos.py."}},
        {"type": "turn.completed", "usage": {}},
    )
    assert codex_cli._resumo(saida) == "Implementei precos.py."


def test_sem_mensagem_do_agente_o_resumo_e_vazio():
    """Vazio faz o chamador cair na saída crua, em vez de esconder o problema."""
    assert codex_cli._resumo(_jsonl({"type": "turn.completed"})) == ""
