"""O maestro em execução: N ramos em paralelo, um gate, N deploys.

Com dublês no lugar das crews e do Docker. O que se cobra aqui não é o conteúdo
da entrega — é o comportamento que o paralelismo introduziu e que nenhum teste
de unidade alcança: os ramos rodam mesmo ao mesmo tempo, um que cai não leva os
outros, e só o que convergiu é publicado.
"""
import json
import time

import pytest
from langgraph.graph import END, START, StateGraph

from src.squad.graph import maestro, workflow

LIVRO = [
    {"nome": "event-service", "stack": "java", "responsabilidade": "eventos",
     "depende_de": ["room-service"]},
    {"nome": "room-service", "stack": "java", "responsabilidade": "salas"},
    {"nome": "ticket-service", "stack": "java", "responsabilidade": "ingressos"},
]


class _Saida:
    def __init__(self, raw):
        self.raw = raw


def _crew(raw):
    return lambda: type("C", (), {"kickoff": lambda self, inputs=None: _Saida(raw)})()


def _ramo_falso(demora=0.0, falha_em=None, retidos=(), estado_falho=None):
    """Um subgrafo com a forma do ramo real: um nó caro e o veredito.

    O contador de falha vem de fora: ele precisa sobreviver à retomada, que é
    justamente o que o teste de falha parcial mede.
    """
    estado_falho = {"n": 0} if estado_falho is None else estado_falho

    def caro(s):
        if demora:
            time.sleep(demora)
        if falha_em and s["servico"] == falha_em and estado_falho["n"] == 0:
            estado_falho["n"] = 1
            raise RuntimeError(f"provedor caiu em {s['servico']}")
        return {"workspace": f"/ws/{s['servico']}", "thread_id": f"t--{s['servico']}"}

    def veredito(s):
        return {"pronto": s["servico"] not in retidos, "testes_ok": True,
                "cobertura": 91.0, "aprovado": True}

    g = StateGraph(workflow.EstadoProjeto)
    g.add_node("caro", caro)
    g.add_node("veredito", veredito)
    g.add_edge(START, "caro")
    g.add_edge("caro", "veredito")
    g.add_edge("veredito", END)
    return g.compile()


@pytest.fixture
def montar(monkeypatch, tmp_path):
    """Monta o maestro com dublês. Devolve uma função que constrói o grafo."""
    monkeypatch.chdir(tmp_path)
    publicados: list[str] = []

    def montar_com(servicos=LIVRO, **kwargs):
        monkeypatch.setattr(maestro, "crew_decomposicao", _crew(json.dumps(servicos)))
        monkeypatch.setattr(maestro, "crew_contratos", _crew("GET /rooms/{id}"))
        vivo = {"n": 0}
        monkeypatch.setattr(
            workflow, "construir_subgrafo_servico",
            lambda: _ramo_falso(estado_falho=vivo, **kwargs),
        )
        monkeypatch.setattr(maestro, "verificar_compilacao", lambda *a: None)

        def deploy(workspace, thread_id, pedido, perfil, nome=None):
            publicados.append(nome)
            return {"deploy_ok": True, "deploy_ref": f"SamukDantas/{nome}@main"}

        monkeypatch.setattr(maestro, "executar_deploy", deploy)
        return maestro.construir_maestro()

    montar_com.publicados = publicados
    return montar_com


def _cfg(nome="t1"):
    return {"configurable": {"thread_id": nome}}


def _rodar(grafo, cfg, resposta="sim"):
    from langgraph.types import Command
    grafo.invoke({"pedido": "sistema de eventos", "stack": "java"}, config=cfg)
    return grafo.invoke(Command(resume=resposta), config=cfg)


# ---------- o fan-out ----------

def test_os_tres_servicos_viram_tres_ramos(montar):
    final = _rodar(montar(), _cfg())
    assert sorted(e["servico"] for e in final["entregas"]) == [
        "event-service", "room-service", "ticket-service"
    ]


def test_os_ramos_rodam_ao_mesmo_tempo(montar):
    """A medição que justifica a arquitetura inteira. Em série, três ramos de
    0,6s levariam 1,8s."""
    grafo = montar(demora=0.6)
    inicio = time.monotonic()
    _rodar(grafo, _cfg())
    assert time.monotonic() - inicio < 1.4


def test_cada_ramo_recebe_so_o_proprio_escopo_e_o_contrato(montar):
    final = _rodar(montar(), _cfg())
    pedidos = {e["servico"]: e["pedido"] for e in final["entregas"]}
    assert "APENAS o microsservico `room-service`" in pedidos["room-service"]
    assert "APENAS o microsservico `event-service`" in pedidos["event-service"]
    for texto in pedidos.values():
        assert "GET /rooms/{id}" in texto, "o contrato congelado entra em todo ramo"


def test_um_servico_so_nao_paga_a_crew_de_contrato(montar, monkeypatch):
    """Não há contrato a acordar com ninguém: cobrar a chamada faria toda
    entrega única pagar o preço de um problema que ela não tem."""
    chamou = []
    grafo = montar(servicos=[{"nome": "api", "stack": "python", "responsabilidade": "x"}])
    monkeypatch.setattr(
        maestro, "crew_contratos",
        lambda: chamou.append(1) or _crew("nunca")()
    )
    final = _rodar(grafo, _cfg())
    assert chamou == [] and final["contratos"] == ""


# ---------- gate único e publicação parcial ----------

def test_um_gate_so_para_os_tres_servicos(montar):
    grafo = montar()
    grafo.invoke({"pedido": "x", "stack": "java"}, config=_cfg())
    estado = grafo.get_state(_cfg())
    assert sum(len(t.interrupts) for t in estado.tasks) == 1


def test_o_gate_mostra_o_placar_dos_n_servicos(montar):
    grafo = montar(retidos=("ticket-service",))
    grafo.invoke({"pedido": "x", "stack": "java"}, config=_cfg())
    interrupcao = next(
        i for t in grafo.get_state(_cfg()).tasks for i in t.interrupts
    )
    assert sorted(interrupcao.value["prontos"]) == ["event-service", "room-service"]
    assert interrupcao.value["retidos"] == ["ticket-service"]


def test_publica_os_verdes_e_retem_o_vermelho(montar):
    grafo = montar(retidos=("ticket-service",))
    final = _rodar(grafo, _cfg())
    assert sorted(montar.publicados) == ["event-service", "room-service"]
    assert sorted(d["servico"] for d in final["deploys"]) == [
        "event-service", "room-service"
    ]


def test_um_repositorio_por_servico(montar):
    """A regra vale com microsserviço contando como projeto: o slug vem do nome
    do serviço, não do pedido — que é o mesmo para os três."""
    final = _rodar(montar(), _cfg())
    refs = {d["deploy_ref"] for d in final["deploys"]}
    assert refs == {
        "SamukDantas/event-service@main",
        "SamukDantas/room-service@main",
        "SamukDantas/ticket-service@main",
    }


def test_nenhum_pronto_nao_publica_nada_e_nao_e_erro(montar):
    grafo = montar(retidos=("event-service", "room-service", "ticket-service"))
    final = _rodar(grafo, _cfg())
    assert montar.publicados == []
    assert not final.get("deploys")


def test_recusa_no_gate_nao_publica(montar):
    grafo = montar()
    with pytest.raises(workflow.DeployNegado):
        _rodar(grafo, _cfg(), resposta="nao")
    assert montar.publicados == []


# ---------- falha parcial ----------

def test_ramo_que_cai_nao_leva_os_outros_e_a_retomada_refaz_so_ele(montar):
    """O ensaio que a arquitetura promete. Sem isso, uma queda do provedor num
    serviço custaria a execução inteira — que é exatamente o que o paralelismo
    deveria ter deixado de custar."""
    grafo = montar(falha_em="room-service")
    cfg = _cfg()
    with pytest.raises(RuntimeError, match="provedor caiu"):
        grafo.invoke({"pedido": "x", "stack": "java"}, config=cfg)

    parcial = grafo.get_state(cfg).values.get("entregas") or []
    assert sorted(e["servico"] for e in parcial) == ["event-service", "ticket-service"]

    from langgraph.types import Command
    grafo.invoke(None, config=cfg)
    final = grafo.invoke(Command(resume="sim"), config=cfg)
    servicos = [e["servico"] for e in final["entregas"]]
    assert sorted(servicos) == ["event-service", "room-service", "ticket-service"]
    # Sem duplicata: os dois que já tinham terminado não refizeram o trabalho.
    assert len(servicos) == 3


# ---------- decomposição inválida ----------

def test_decomposicao_invalida_e_refeita(montar):
    grafo = montar(servicos=[{"nome": "a", "stack": "cobol", "responsabilidade": "x"}])
    with pytest.raises(RuntimeError, match="decomposição do pedido falhou"):
        grafo.invoke({"pedido": "x", "stack": "java"}, config=_cfg())


def test_resposta_sem_json_reprova_como_decomposicao_invalida(montar, monkeypatch):
    grafo = montar()
    monkeypatch.setattr(maestro, "crew_decomposicao", _crew("Claro! Vou decompor..."))
    with pytest.raises(RuntimeError, match="decomposição do pedido falhou"):
        grafo.invoke({"pedido": "x", "stack": "java"}, config=_cfg())
