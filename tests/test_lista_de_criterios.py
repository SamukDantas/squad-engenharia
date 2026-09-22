"""A lista do que o guard vai cobrar, escrita antes do QA.

Nas 7 últimas execuções o primeiro veredito do guard de critérios foi NAO em
todas: o QA e o guard liam os requisitos de jeitos diferentes, e a diferença
só aparecia na reprova, ao custo de uma passada inteira do QA.
"""
import inspect

import pytest

from src.squad.adaptadores import perfis
from src.squad.crews.base import TASKS_CFG
from src.squad.dominio import spec
from src.squad.graph import workflow

NEXT = perfis.obter("nextjs")


def test_lista_expande_requisito_universal_e_deixa_aparencia_de_fora():
    pedido = spec.pedido_da_lista_de_criterios("## Requisitos\n- a", "vitest")
    assert "um item por elemento" in pedido and '"todos"' in pedido
    assert "nada de aparência" in pedido
    assert "## Requisitos\n- a" in pedido and "vitest" in pedido
    assert f"no máximo {spec.MAX_ITENS_CRITERIOS} itens" in pedido


def test_guard_julga_so_contra_a_lista():
    trecho = spec.julgamento_pela_lista("1. getFunnelByStage([]) devolve zeros")
    assert "getFunnelByStage([])" in trecho
    assert "Não cobre nada que não esteja na lista" in trecho
    assert spec.julgamento_pela_lista("") == ""


@pytest.fixture
def metricas(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


class _LLM:
    def __init__(self, resposta=None, erro=None):
        self.resposta, self.erro, self.chamadas = resposta, erro, 0

    def call(self, texto):
        self.chamadas += 1
        if self.erro:
            raise self.erro
        return self.resposta


def test_lista_sai_uma_vez_por_spec(metricas, monkeypatch):
    llm = _LLM("1. a\n2. b")
    monkeypatch.setattr(workflow, "squad_llm", lambda: llm)
    estado = {"spec": "## Requisitos\n- a", "thread_id": "t"}
    cfg = {"configurable": {"thread_id": "t"}}
    assert workflow._lista_de_criterios(estado, cfg, NEXT) == "1. a\n2. b"
    estado["criterios_verificacao"] = "1. a\n2. b"
    assert workflow._lista_de_criterios(estado, cfg, NEXT) == "1. a\n2. b"
    assert llm.chamadas == 1  # a segunda passada do QA reaproveita


def test_lista_indisponivel_nao_derruba_a_execucao(metricas, monkeypatch):
    """A lista é otimização: sem ela o guard julga pelos requisitos, como antes."""
    monkeypatch.setattr(workflow, "squad_llm", lambda: _LLM(erro=RuntimeError("caiu")))
    monkeypatch.setattr(workflow, "com_retry", lambda rotulo, fn, **k: fn())
    estado = {"spec": "## Requisitos\n- a", "thread_id": "t"}
    assert workflow._lista_de_criterios(estado, {"configurable": {"thread_id": "t"}}, NEXT) == ""


def test_qa_recebe_a_lista_e_ela_fica_no_estado():
    assert "{criterios}" in TASKS_CFG["escrever_testes"]["description"]
    assert "OBRIGATÓRIA" in TASKS_CFG["escrever_testes"]["description"]
    fonte = inspect.getsource(workflow.no_escrever_testes)
    assert '"criterios": criterios' in fonte
    assert '"criterios_verificacao": criterios' in fonte


def test_guard_recebe_a_lista():
    fonte = inspect.getsource(workflow.no_validacao_testes)
    assert "julgamento_pela_lista(state.get('criterios_verificacao', ''))" in fonte


def test_spec_nova_pede_lista_nova():
    assert '"criterios_verificacao": ""' in inspect.getsource(workflow.no_planejamento)
