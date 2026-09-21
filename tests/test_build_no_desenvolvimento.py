"""O executor compila a entrega antes de ela sair do nó de desenvolvimento.

Thread `f96bb730` (calculadora de juros): um `:global(*)` num CSS Module
derrubou o `next build` na primeira rodada. O executor escreve sem ver o
compilador, e o erro só aparecia depois de QA, guard e suíte — uma rodada
inteira, com o QA reescrevendo a suíte para o código corrigido.
"""
import inspect
import json

import pytest

from src.squad.adaptadores import codex_cli
from src.squad.graph import workflow

ERRO_NEXT = """Failed to compile.
./app/page.module.css:1:1
Syntax error: Selector ":global(*)" is not pure (pure selectors must contain at least one local class or id)
> Build failed because of webpack errors"""


@pytest.fixture
def compilador(monkeypatch):
    """Substitui a jaula por uma sequência de vereditos de build."""
    monkeypatch.delenv("TEST_RUNNER", raising=False)

    def usar(*vereditos):
        fila = list(vereditos)
        chamadas = []

        def verificar(ws, tid, perfil):
            chamadas.append(ws)
            return fila.pop(0)

        monkeypatch.setattr(workflow, "verificar_compilacao", verificar)
        return chamadas

    return usar


def _estado():
    return {"workspace": "/ws", "stack": "nextjs"}


def _perfil():
    return workflow.perfis.obter("nextjs")


def test_build_quebrado_volta_ao_executor_com_o_erro(compilador):
    compilador(ERRO_NEXT, None)
    retornos = []
    medida = workflow._compilar_com_ajuste(
        _estado(), _perfil(), retornos.append, "", "t"
    )
    assert medida == {"build_ok": True, "ajustes_build": 1}
    assert len(retornos) == 1
    assert "NÃO COMPILA" in retornos[0] and ':global(*)" is not pure' in retornos[0]


def test_ajuste_de_build_preserva_o_retorno_da_rodada(compilador):
    compilador(ERRO_NEXT, None)
    retornos = []
    workflow._compilar_com_ajuste(
        _estado(), _perfil(), retornos.append, "4 testes vermelhos: ...", "t"
    )
    assert "4 testes vermelhos" in retornos[0]


def test_build_verde_de_primeira_nao_chama_o_executor(compilador):
    compilador(None)
    retornos = []
    medida = workflow._compilar_com_ajuste(_estado(), _perfil(), retornos.append, "", "t")
    assert medida == {"build_ok": True, "ajustes_build": 0}
    assert retornos == []


def test_teto_de_ajustes_segue_para_o_laco_normal(compilador):
    chamadas = compilador(ERRO_NEXT, ERRO_NEXT, ERRO_NEXT)
    retornos = []
    medida = workflow._compilar_com_ajuste(_estado(), _perfil(), retornos.append, "", "t")
    assert len(retornos) == workflow.MAX_AJUSTES_BUILD
    assert len(chamadas) == workflow.MAX_AJUSTES_BUILD + 1
    assert medida["build_ok"] is False and "is not pure" in medida["trecho_falha"]


def test_sem_jaula_nao_compila(compilador, monkeypatch):
    chamadas = compilador()
    monkeypatch.setenv("TEST_RUNNER", "host")
    assert workflow._compilar_com_ajuste(_estado(), _perfil(), None, "", "t") == {}
    assert chamadas == []


# ---------- reconexão do Codex não é morte ----------

def _stream(*eventos):
    return "\n".join(json.dumps(e) for e in eventos)


RECONEXAO = {"type": "error", "message": "Reconnecting... 2/5 (stream disconnected "
             "before completion: Este host não é conhecido. (os error 11001))"}


def test_reconexao_recuperada_nao_aborta():
    """Thread `f96bb730`: falha de DNS passageira, o Codex reconectou e
    concluiu, e o guard abortou a execução pelo aviso."""
    saida = _stream(RECONEXAO, {"type": "turn.completed", "usage": {}})
    assert codex_cli._falha_no_stream(saida) is None


def test_erro_sem_turno_concluido_ainda_e_morte():
    assert "Reconnecting" in codex_cli._falha_no_stream(_stream(RECONEXAO))


def test_turno_que_falhou_e_morte_mesmo_apos_outro_concluido():
    saida = _stream(
        {"type": "turn.completed", "usage": {}},
        {"type": "turn.failed", "error": {"message": "quota exceeded"}},
    )
    assert codex_cli._falha_no_stream(saida) == "quota exceeded"


# ---------- guard de critérios não julga aparência ----------

def test_guard_de_criterios_deixa_aparencia_para_a_verificacao_visual():
    """Thread `f96bb730`: o guard reprovou a suíte por "lado a lado" e por
    legibilidade no tema escuro — o que o jsdom não mede e a verificação
    visual mede."""
    fonte = inspect.getsource(workflow.no_validacao_testes)
    assert "Aparência também não é critério desta pergunta" in fonte
