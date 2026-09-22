"""O Luna como reserva do Sol.

O `gpt-5.6-sol` é o modelo da squad desde que a conta foi para o plano Plus.
No plano gratuito o servidor o recusava com "not supported when using Codex
with a ChatGPT account"; no Plus, a recusa previsível é o limite da janela de
uso. Nos dois casos a chamada é repetida com o `gpt-5.6-luna`.
"""
from types import SimpleNamespace

import pytest

from src.squad.adaptadores import codex_cli, consumo

RECUSA = '{"type":"turn.failed","error":{"message":"400 gpt-5.6-sol not supported when using Codex with a ChatGPT account"}}'
LIMITE = '{"type":"error","message":"You have hit your usage limit. Try again in 2 hours."}'
OK = '{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}\n{"type":"turn.completed","usage":{}}'
SANDBOX = '{"type":"turn.failed","error":{"message":"command blocked by policy"}}'


@pytest.fixture
def cli(monkeypatch):
    """O Codex dublado: devolve a saída conforme o modelo pedido."""
    monkeypatch.setattr(codex_cli.shutil, "which", lambda _n: "C:/npm/codex")
    monkeypatch.setattr(codex_cli, "_em_reserva", False)
    monkeypatch.delenv("CODEX_RUN_MODEL", raising=False)
    monkeypatch.delenv("CODEX_FALLBACK_MODEL", raising=False)
    modelos = []

    def usar(por_modelo):
        def run(cmd, **k):
            m = cmd[cmd.index("--model") + 1]
            modelos.append(m)
            return SimpleNamespace(returncode=0, stdout=por_modelo[m], stderr="")
        monkeypatch.setattr(codex_cli.subprocess, "run", run)
        return modelos

    return usar


def test_padrao_e_o_sol_com_o_luna_de_reserva(cli):
    assert codex_cli.modelo() == "gpt-5.6-sol"
    assert codex_cli.modelo_reserva() == "gpt-5.6-luna"


def test_modelo_recusado_repete_com_o_reserva(cli):
    modelos = cli({"gpt-5.6-sol": RECUSA, "gpt-5.6-luna": OK})
    with consumo.contar() as contagem:
        assert codex_cli.responder("PEDIDO", timeout=10) == "OK"
    assert modelos == ["gpt-5.6-sol", "gpt-5.6-luna"]
    assert contagem["fallbacks"] == 1


def test_limite_de_uso_tambem_leva_ao_reserva(cli):
    modelos = cli({"gpt-5.6-sol": LIMITE, "gpt-5.6-luna": OK})
    assert codex_cli.responder("PEDIDO", timeout=10) == "OK"
    assert modelos == ["gpt-5.6-sol", "gpt-5.6-luna"]


def test_depois_da_troca_o_processo_segue_no_reserva(cli):
    """Uma recusa por limite não passa em segundos: tentar o Sol a cada chamada
    pagaria uma ida ao servidor recusada por vez."""
    modelos = cli({"gpt-5.6-sol": RECUSA, "gpt-5.6-luna": OK})
    codex_cli.responder("A", timeout=10)
    codex_cli.responder("B", timeout=10)
    assert modelos == ["gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-luna"]
    assert codex_cli.modelo() == "gpt-5.6-luna"


def test_falha_da_tarefa_nao_troca_de_modelo(cli):
    """Política do sandbox não se resolve com outro modelo, e a troca
    esconderia o erro de verdade."""
    modelos = cli({"gpt-5.6-sol": SANDBOX, "gpt-5.6-luna": OK})
    with pytest.raises(RuntimeError, match="blocked by policy"):
        codex_cli.responder("PEDIDO", timeout=10)
    assert modelos == ["gpt-5.6-sol"]


def test_reserva_pode_ser_desligado(cli, monkeypatch):
    monkeypatch.setenv("CODEX_FALLBACK_MODEL", "nenhum")
    modelos = cli({"gpt-5.6-sol": RECUSA})
    with pytest.raises(RuntimeError, match="not supported"):
        codex_cli.responder("PEDIDO", timeout=10)
    assert modelos == ["gpt-5.6-sol"]


def test_reserva_igual_ao_principal_nao_conta(cli, monkeypatch):
    monkeypatch.setenv("CODEX_RUN_MODEL", "gpt-5.6-luna")
    assert codex_cli.modelo_reserva() is None


def test_executor_e_qa_tambem_tem_reserva(cli, tmp_path):
    modelos = cli({"gpt-5.6-sol": RECUSA, "gpt-5.6-luna": OK})
    codex_cli.executar_qa(str(tmp_path), "tarefa")
    assert modelos == ["gpt-5.6-sol", "gpt-5.6-luna"]
