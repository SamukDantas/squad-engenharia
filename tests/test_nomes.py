"""Nome curto do repositório da entrega.

Caso real que motivou: o pedido "Criar um modulo Python de conversao de
temperaturas: ..." virou `criar-um-modulo-python-de-conversao-de`, e o
repositório teve de ser renomeado à mão para `conversor-temperatura`.
"""
import pytest

from src.squad.dominio.nomes import nome_da_resposta, nome_do_pedido
from src.squad.graph import workflow

PEDIDO = (
    "Criar um modulo Python de conversao de temperaturas: funcoes "
    "celsius_para_fahrenheit e fahrenheit_para_celsius."
)


# ---------- resposta do LLM ----------

@pytest.mark.parametrize("resposta", [
    "conversor-temperatura",
    "  conversor-temperatura\n",
    "`conversor-temperatura`",
    '"conversor-temperatura".',
    "**conversor-temperatura**",
    "Nome: conversor-temperatura",
    "Conversor Temperatura",
    "conversor_temperatura",
    "conversor-temperatura\n\nEscolhi esse nome porque...",
])
def test_resposta_enfeitada_vira_nome(resposta):
    assert nome_da_resposta(resposta) == "conversor-temperatura"


def test_acento_vira_ascii():
    assert nome_da_resposta("gestão-eventos") == "gestao-eventos"


@pytest.mark.parametrize("resposta", [
    "",
    None,
    "Um bom nome seria conversor de temperatura para o projeto",  # frase, não nome
    "a" * 41,                                                      # longo demais
    "---",
])
def test_resposta_que_nao_e_nome_e_recusada(resposta):
    """Cortar uma frase para caber daria o mesmo problema que se queria resolver."""
    assert nome_da_resposta(resposta) is None


# ---------- fallback sem LLM ----------

def test_fallback_tira_verbo_e_artigo_da_primeira_frase():
    assert nome_do_pedido(PEDIDO) == "modulo-python-conversao"


def test_fallback_de_outro_pedido_real():
    assert nome_do_pedido(
        "Criar uma API de reserva de salas com autenticacao.\n\nRecursos: ..."
    ) == "api-reserva-salas"


def test_fallback_sem_palavra_util_devolve_none():
    assert nome_do_pedido("Criar um!") is None
    assert nome_do_pedido("") is None


# ---------- nó: nome decidido uma vez, depois da spec ----------

def _estado(**extra):
    return {"pedido": PEDIDO, "spec": "spec", "thread_id": "839f368d-xyz", **extra}


class _LLM:
    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.prompts = []

    def __call__(self):
        return self

    def call(self, prompt):
        self.prompts.append(prompt)
        resposta = self.respostas.pop(0)
        if isinstance(resposta, Exception):
            raise resposta
        return resposta


@pytest.fixture
def sem_metricas(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # medir() grava em metrics/ relativo à raiz


def test_spec_aprovada_ganha_nome(monkeypatch, sem_metricas):
    llm = _LLM(["SIM", "conversor-temperatura"])
    monkeypatch.setattr(workflow, "squad_llm", llm)
    saida = workflow.no_validacao_spec(_estado(), {"configurable": {"thread_id": "t"}})
    assert saida == {"spec_coerente": True, "nome_repo": "conversor-temperatura"}
    assert "conversao de temperaturas" in llm.prompts[1]


def test_spec_reprovada_nao_gasta_chamada_com_nome(monkeypatch, sem_metricas):
    llm = _LLM(["NAO"])
    monkeypatch.setattr(workflow, "squad_llm", llm)
    saida = workflow.no_validacao_spec(_estado(), {"configurable": {"thread_id": "t"}})
    assert saida == {"spec_coerente": False}
    assert len(llm.prompts) == 1


def test_retomada_nao_renomeia(monkeypatch, sem_metricas):
    """O nome está no checkpoint: sortear outro publicaria noutro repositório."""
    llm = _LLM(["SIM"])
    monkeypatch.setattr(workflow, "squad_llm", llm)
    saida = workflow.no_validacao_spec(
        _estado(nome_repo="conversor-temperatura"), {"configurable": {"thread_id": "t"}}
    )
    assert "nome_repo" not in saida and len(llm.prompts) == 1


def test_ramo_do_maestro_nao_pede_nome(monkeypatch, sem_metricas):
    """O repositório de um serviço é o nome do serviço."""
    llm = _LLM(["SIM"])
    monkeypatch.setattr(workflow, "squad_llm", llm)
    saida = workflow.no_validacao_spec(
        _estado(servico="room-service"), {"configurable": {"thread_id": "t"}}
    )
    assert "nome_repo" not in saida


def test_resposta_ruim_cai_na_regra(monkeypatch, sem_metricas):
    llm = _LLM(["SIM", "Sugiro chamar o projeto de conversor de temperaturas em Python"])
    monkeypatch.setattr(workflow, "squad_llm", llm)
    saida = workflow.no_validacao_spec(_estado(), {"configurable": {"thread_id": "t"}})
    assert saida["nome_repo"] == "modulo-python-conversao"


def test_falha_do_provedor_no_nome_nao_derruba_a_execucao(monkeypatch, sem_metricas):
    monkeypatch.setattr(workflow, "com_retry", lambda _nome, fn, **_: fn())
    llm = _LLM(["SIM", RuntimeError("gateway fora")])
    monkeypatch.setattr(workflow, "squad_llm", llm)
    saida = workflow.no_validacao_spec(_estado(), {"configurable": {"thread_id": "t"}})
    assert saida == {"spec_coerente": True, "nome_repo": "modulo-python-conversao"}
