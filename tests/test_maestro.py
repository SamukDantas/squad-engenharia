"""A divisão entre o ramo (um serviço) e o maestro (a execução inteira).

O pipeline sequencial virou subgrafo para poder ser instanciado N vezes em
paralelo. A extração não pode mudar o que o ramo faz — o que muda é quem manda
nele —, e é isso que os testes daqui cobram.
"""
import pytest

from src.squad.dominio import rotas
from src.squad.graph import workflow

RAMO = {n for n in workflow.construir_subgrafo_servico().get_graph().nodes}
MAESTRO = {n for n in workflow.construir_grafo().get_graph().nodes}


# ---------- o ramo é fechado sobre si mesmo ----------

def _destinos_possiveis():
    """Todo destino que alguma rota do domínio pode devolver.

    Varrido do estado, não escrito à mão: uma lista fixa aqui envelheceria em
    silêncio no dia em que uma rota nova aparecesse.
    """
    estados = [
        {}, {"tentativas": 0}, {"tentativas": rotas.MAX_TENTATIVAS},
        {"spec_coerente": True}, {"spec_tentativas": 1},
        {"testes_ok": True, "cobertura_ok": True, "tentativas": 0},
        {"testes_ok": True, "cobertura_ok": False, "testes_tentativas": 0, "tentativas": 0},
        {"testes_ok": False, "tentativas": 0},
        {"testes_aderentes": True}, {"testes_tentativas": rotas.MAX_TESTES},
        {"aprovado": True, "tentativas": 0}, {"aprovado": False, "tentativas": 0},
        {"pentest_ok": True, "tentativas": 0}, {"pentest_ok": False, "tentativas": 0},
        {"visual_ok": True, "tentativas": 0}, {"visual_ok": False, "tentativas": 0},
        {"ambientes_ok": True, "tentativas": 0}, {"ambientes_ok": False, "tentativas": 0},
        {"origem_feedback": "revisao", "arquivos": ["a.py", "tests/t.py"]},
        {"origem_feedback": "", "arquivos": []},
    ]
    funcoes = [
        rotas.pos_validacao_spec, rotas.pos_validacao_testes, rotas.pos_testes,
        rotas.pos_revisao, rotas.pos_pentest, rotas.pos_visual,
        rotas.pos_config_ambientes, rotas.pos_desenvolvimento,
    ]
    destinos = set()
    for fn in funcoes:
        for st in estados:
            try:
                d = fn({"tentativas": 0, **st})
            except (KeyError, TypeError):
                continue
            if d.destino:
                destinos.add(d.destino)
    return destinos


def test_toda_rota_do_dominio_aponta_para_um_no_do_ramo():
    """O domínio devolve o destino como string, e o LangGraph só descobre um
    nome inexistente ao passar por ele — no meio da execução, depois de o
    trabalho caro já ter sido pago."""
    faltando = _destinos_possiveis() - RAMO
    assert not faltando, f"rotas apontam para nós que não existem: {faltando}"


def test_o_ramo_nao_publica_nem_pergunta_ao_humano():
    """As duas pontas subiram para o maestro: com N serviços, N gates fariam o
    humano aprovar um antes de saber que outro reprovou, e N deploys precisam
    esperar essa decisão única."""
    assert "deploy" not in RAMO
    # O nó de saída do ramo tem o nome de rota `aprovacao_humana`, mas não
    # interrompe: quem interrompe é o gate do maestro.
    ramo = workflow.construir_subgrafo_servico()
    ligado = ramo.nodes["aprovacao_humana"].bound
    assert getattr(ligado, "func", None) is workflow.no_veredito_do_ramo


def test_o_maestro_tem_gate_e_deploy():
    assert {"aprovacao_humana", "deploy"} <= MAESTRO


def test_o_maestro_chama_o_ramo_como_um_no_so():
    assert "servico" in MAESTRO
    assert "desenvolvimento" not in MAESTRO


def test_aprovacao_humana_e_nome_de_rota_nos_dois_grafos():
    """O mesmo nome, comportamentos diferentes: no ramo ele fecha o veredito e
    vai a END; no maestro é o gate de verdade. Se o nome sumisse do ramo, as
    rotas do domínio quebrariam."""
    assert "aprovacao_humana" in RAMO and "aprovacao_humana" in MAESTRO


# ---------- veredito do ramo ----------

def _convergido(**troca):
    base = {
        "testes_ok": True, "cobertura_ok": True, "aprovado": True,
        "pentest_ok": True, "visual_ok": True,
    }
    return {**base, **troca}


def test_ramo_convergido_esta_pronto():
    assert workflow.no_veredito_do_ramo(_convergido())["pronto"] is True


@pytest.mark.parametrize(
    "sinal", ["testes_ok", "cobertura_ok", "aprovado", "pentest_ok", "visual_ok"]
)
def test_qualquer_sinal_vermelho_retem_o_ramo(sinal):
    """Chegar ao fim do ramo não é o mesmo que ter convergido: os tetos de
    circuit breaker mandam para cá de propósito, com a suíte vermelha, para o
    humano decidir. Publicar isso seria publicar o que não passou."""
    assert workflow.no_veredito_do_ramo(_convergido(**{sinal: False}))["pronto"] is False


def test_pentest_e_visual_desligados_nao_retem_o_ramo():
    """Os dois curto-circuitam verde quando desligados, e uma execução sem eles
    não pode ficar impublicável por ausência."""
    estado = {"testes_ok": True, "cobertura_ok": True, "aprovado": True}
    assert workflow.no_veredito_do_ramo(estado)["pronto"] is True


# ---------- id do ramo ----------

def test_sem_servico_o_id_da_thread_e_o_de_sempre(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    saida = workflow.no_triagem(
        {"pedido": "x", "stack": "python"},
        {"configurable": {"thread_id": "abc123"}},
    )
    assert saida["thread_id"] == "abc123"


def test_com_servico_o_id_carrega_o_nome(tmp_path, monkeypatch):
    """Dois ramos paralelos batizam workspace, containers e arquivo de métricas
    com esse id. Compartilhá-lo faria um container ser recusado por nome
    duplicado, longe da causa."""
    monkeypatch.chdir(tmp_path)
    saida = workflow.no_triagem(
        {"pedido": "x", "stack": "java", "servico": "room-service"},
        {"configurable": {"thread_id": "abc123"}},
    )
    assert saida["thread_id"] == "abc123--room-service"
    assert saida["workspace"].endswith("abc123--room-service")


def test_ids_de_servicos_diferentes_nao_colidem(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ids = {
        workflow.no_triagem(
            {"pedido": "x", "stack": "java", "servico": s},
            {"configurable": {"thread_id": "t"}},
        )["thread_id"]
        for s in ("event-service", "room-service", "ticket-service")
    }
    assert len(ids) == 3


def test_os_nos_leem_o_id_do_estado_nao_do_config():
    """Dentro de um subgrafo o `config` carrega o `thread_id` do pai — ler dali
    faria os N ramos escreverem no mesmo lugar."""
    config = {"configurable": {"thread_id": "do-pai"}}
    assert workflow._tid({"thread_id": "do-ramo"}, config) == "do-ramo"
    assert workflow._tid({}, config) == "do-pai"


# ---------- checkpoint sob concorrência ----------

def test_checkpointer_liga_wal(tmp_path, monkeypatch):
    """Em modo padrão, dois ramos concluindo ao mesmo tempo disputam o arquivo e
    o SQLite recusa a segunda escrita com `database is locked` — matando um ramo
    por disputa de arquivo, não por mérito da entrega."""
    monkeypatch.chdir(tmp_path)
    saver = workflow._checkpointer()
    conn = getattr(saver, "conn", None)
    if conn is None:  # sem o pacote de sqlite, cai no checkpointer em memória
        pytest.skip("langgraph-checkpoint-sqlite ausente")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
