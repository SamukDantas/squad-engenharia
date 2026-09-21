"""A spec separa requisito de decisão, e o revisor só bloqueia por requisito.

Dois casos reais motivaram:
- calculadora de juros (thread `33c19d49`): a spec dizia "nenhuma cor em CSS
  global", o pedido só pedia a página legível no tema escuro. A verificação
  visual exigiu fundo declarado na raiz, o revisor exigiu o contrário pela
  spec, e o laço oscilou até o gate;
- conversor de temperaturas (thread `055e0f17`): a spec listava
  `test_temperatura.py` como entrega, invadindo a suíte, que é do QA.
"""
from pathlib import Path

import yaml

from src.squad.graph import workflow

TAREFAS = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "src/squad/config/tasks.yaml").read_text(encoding="utf-8")
)


def test_arquiteto_separa_requisitos_de_decisoes():
    texto = TAREFAS["definir_arquitetura"]["description"]
    assert "## Requisitos" in texto and "## Decisões de implementação" in texto
    assert "rastreável a uma frase do pedido" in texto


def test_arquiteto_nao_poe_teste_na_spec_nem_promove_escolha_a_requisito():
    texto = TAREFAS["definir_arquitetura"]["description"]
    assert "NÃO inclua" in texto and "arquivos de teste" in texto
    assert "NÃO transforme uma escolha de" in texto
    assert "têm" in texto and "precedência" in texto


def test_revisor_so_bloqueia_por_requisito_do_pedido():
    texto = TAREFAS["revisar"]["description"]
    assert "PEDIDO ORIGINAL" in texto
    assert '"## Decisões de implementação" da spec NÃO é' in texto


def test_revisor_recebe_o_pedido_original(monkeypatch, tmp_path):
    """Sem o pedido, o revisor só tem a spec, e tudo nela parece obrigação."""
    monkeypatch.chdir(tmp_path)  # medir() grava em metrics/ relativo à raiz
    capturado = {}

    class _Crew:
        def kickoff(self, inputs):
            capturado.update(inputs)
            return type("R", (), {"raw": "Tudo certo.\nAPROVADO"})()

    monkeypatch.setattr(workflow, "crew_revisao", lambda: _Crew())
    estado = {
        "pedido": "PEDIDO ORIGINAL AQUI", "spec": "spec", "codigo": "codigo",
        "saida_testes": "", "thread_id": "t", "stack": "python",
    }
    saida = workflow.no_revisao(estado, {"configurable": {"thread_id": "t"}})
    assert capturado["pedido"] == "PEDIDO ORIGINAL AQUI"
    assert saida["aprovado"] is True
