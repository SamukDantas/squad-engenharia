"""Reexecução a partir de um nó, numa thread nova.

Validar uma mudança no QA custava o pedido inteiro: em 21/09, as threads
`f96bb730` e `9a86ddb0` gastaram 11 pontos da cota do Codex sem entregar
nada. A reexecução parte do estado antes do nó, sem pagar o que veio antes.
"""
from types import SimpleNamespace

from src.squad.graph import reexecucao


def _tupla(ns, passo, valores):
    return SimpleNamespace(
        config={"configurable": {"checkpoint_ns": ns}},
        checkpoint={"channel_values": valores},
        metadata={"step": passo},
    )


class _Checkpointer:
    def __init__(self, tuplas):
        self.tuplas = tuplas

    def list(self, config):
        return iter(self.tuplas)


def test_estado_vem_da_ultima_vez_que_o_no_era_o_proximo():
    """Num laço o nó roda mais de uma vez; vale a ocorrência mais recente."""
    cp = _Checkpointer([
        _tupla("servico:a", 4, {"branch:to:escrever_testes": None, "testes_tentativas": 0,
                                "pedido": "p"}),
        _tupla("servico:a", 9, {"branch:to:escrever_testes": None, "testes_tentativas": 1,
                                "pedido": "p"}),
        _tupla("servico:a", 5, {"branch:to:validacao_testes": None, "testes_tentativas": 1}),
    ])
    estado = reexecucao.estado_antes_do_no(cp, "t", "escrever_testes")
    assert estado == {"testes_tentativas": 1, "pedido": "p"}


def test_canal_interno_nao_vira_estado():
    cp = _Checkpointer([
        _tupla("servico:a", 3, {"branch:to:revisao": None, "__start__": {}, "spec": "s"}),
    ])
    assert reexecucao.estado_antes_do_no(cp, "t", "revisao") == {"spec": "s"}


def test_so_o_subgrafo_do_servico_conta():
    """O grafo de fora também tem checkpoints, com `branch:to:` dos nós dele."""
    cp = _Checkpointer([_tupla("", 1, {"branch:to:revisao": None, "spec": "x"})])
    assert reexecucao.estado_antes_do_no(cp, "t", "revisao") is None


def test_no_que_a_thread_nunca_alcancou():
    cp = _Checkpointer([_tupla("servico:a", 1, {"branch:to:planejamento": None})])
    assert reexecucao.estado_antes_do_no(cp, "t", "visual") is None


def test_workspace_novo_sem_o_que_nao_existia_no_ponto(tmp_path, monkeypatch):
    """Bifurcar em `escrever_testes` tira a suíte que o QA escreveu depois."""
    origem = tmp_path / "workspace" / "orig"
    (origem / "lib").mkdir(parents=True)
    (origem / "lib" / "juros.ts").write_text("codigo")
    (origem / "lib" / "juros.test.ts").write_text("suite")
    (origem / "node_modules" / "x").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    estado = {"workspace": str(origem), "thread_id": "orig", "arquivos": ["lib/juros.ts"],
              "pedido": "p"}
    novo, removidos = reexecucao.preparar(
        estado, "nova", ["lib/juros.test.ts", "lib/juros.ts"], frozenset({"node_modules"}),
    )
    destino = tmp_path / "workspace" / "nova"
    assert novo["thread_id"] == "nova" and novo["workspace"] == str(destino.resolve())
    assert novo["pedido"] == "p"
    assert removidos == ["lib/juros.test.ts"]
    assert (destino / "lib" / "juros.ts").read_text() == "codigo"
    assert not (destino / "lib" / "juros.test.ts").exists()
    assert not (destino / "node_modules").exists()
    assert (origem / "lib" / "juros.test.ts").exists()  # a origem não muda


def test_grafo_aceita_entrar_por_outro_no():
    from src.squad.graph import workflow
    grafo = workflow.construir_subgrafo_servico("escrever_testes")
    arestas = {(a.source, a.target) for a in grafo.get_graph().edges}
    assert ("__start__", "escrever_testes") in arestas
    assert ("__start__", "triagem") not in arestas


def test_copia_leva_o_run_json_e_deixa_o_git():
    """Thread `cc4cf813`: sem `.squad/run.json`, o nó visual não subiu a
    entrega que a origem renderizava."""
    fora = reexecucao.pastas_fora_da_copia({"node_modules", ".next", ".squad"})
    assert ".squad" not in fora
    assert {"node_modules", ".next", ".git"} <= fora
