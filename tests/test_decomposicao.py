"""Quebrar o pedido em serviços: a decisão que abre o paralelismo.

É a mais cara de errar porque nenhum dos erros aparece na hora. Um nome
repetido faz dois ramos escreverem no mesmo workspace; um ciclo torna o
contrato impossível de congelar; uma lista larga demais afoga a máquina em
containers. Todos só apareceriam no meio da execução, com o planejamento já
pago.
"""
import pytest

from src.squad.adaptadores import perfis
from src.squad.dominio import decomposicao as dec

STACKS = frozenset(perfis.nomes())


def servico(nome, stack="java", **extra):
    return {"nome": nome, "stack": stack, "responsabilidade": "faz coisas", **extra}


def LIVRO():
    return [
        servico("event-service", depende_de=["room-service", "ticket-service"]),
        servico("room-service"),
        servico("ticket-service"),
    ]


def normalizar(bruto):
    return dec.normalizar(bruto, STACKS)


# ---------- o caso do livro ----------

def test_os_tres_servicos_do_livro_sao_uma_decomposicao_valida():
    servicos, erros = normalizar(LIVRO())
    assert erros == []
    assert [s.nome for s in servicos] == ["event-service", "room-service", "ticket-service"]
    assert servicos[0].depende_de == ("room-service", "ticket-service")


def test_ordem_topologica_poe_os_dependentes_depois():
    servicos, _ = normalizar(LIVRO())
    ordem = dec.ordem_topologica(servicos)
    assert ordem.index("room-service") < ordem.index("event-service")
    assert ordem.index("ticket-service") < ordem.index("event-service")


def test_ordem_e_estavel_entre_execucoes():
    """Dois históricos só são comparáveis se a ordem não depender de iteração de
    dicionário."""
    servicos, _ = normalizar(LIVRO())
    assert len({tuple(dec.ordem_topologica(servicos)) for _ in range(20)}) == 1


# ---------- nomes ----------

@pytest.mark.parametrize("bruto,esperado", [
    ("Room Service", "room-service"),
    ("room_service", "room-service"),
    ("ROOM-SERVICE", "room-service"),
    ("  event service  ", "event-service"),
    ("ticket--service", "ticket-service"),
])
def test_nome_e_normalizado_em_vez_de_reprovado(bruto, esperado):
    """A fonte é um LLM: recusar uma decomposição correta pelo uso de maiúscula
    custaria uma rodada inteira de planejamento para corrigir um hífen."""
    servicos, erros = normalizar([servico(bruto)])
    assert erros == [] and servicos[0].nome == esperado


@pytest.mark.parametrize("bruto", ["", "   ", "***", None, "台北"])
def test_nome_sem_nada_utilizavel_reprova(bruto):
    _, erros = normalizar([servico(bruto)])
    assert any("nome utiliz" in e for e in erros)


def test_nome_repetido_reprova():
    """Dois ramos com o mesmo nome dividiriam workspace, containers e arquivo de
    métricas — e um sobrescreveria o outro em silêncio."""
    _, erros = normalizar([servico("room-service"), servico("Room_Service")])
    assert any("mais de uma vez" in e for e in erros)


# ---------- stack por serviço ----------

def test_cada_servico_escolhe_a_propria_stack():
    """O BFF do capítulo 7 seria um ramo Next.js ao lado dos três Java. A stack
    já era por execução; passar a ser por serviço não custa nada."""
    servicos, erros = normalizar([servico("api", "java"), servico("bff", "nextjs")])
    assert erros == []
    assert {s.nome: s.stack for s in servicos} == {"api": "java", "bff": "nextjs"}


def test_stack_inexistente_reprova_com_a_lista_do_que_existe():
    _, erros = normalizar([servico("api", "cobol")])
    assert any("cobol" in e and "java" in e for e in erros)


# ---------- dependências ----------

def test_dependencia_de_servico_nao_decomposto_reprova():
    _, erros = normalizar([servico("event-service", depende_de=["room-service"])])
    assert any("não foi decomposto" in e for e in erros)


def test_dependencia_de_si_mesmo_reprova():
    _, erros = normalizar([servico("a", depende_de=["a"])])
    assert any("de si mesmo" in e for e in erros)


def test_ciclo_reprova():
    """Sem uma ordem não há como congelar o contrato numa passada: A não pode
    ser escrito sabendo de B enquanto B espera A."""
    _, erros = normalizar([
        servico("a", depende_de=["b"]),
        servico("b", depende_de=["a"]),
    ])
    assert any("ciclo" in e for e in erros)


def test_ciclo_longo_tambem_reprova():
    _, erros = normalizar([
        servico("a", depende_de=["b"]),
        servico("b", depende_de=["c"]),
        servico("c", depende_de=["a"]),
    ])
    assert any("ciclo" in e for e in erros)


def test_ordem_topologica_devolve_none_no_ciclo():
    ciclo = [dec.Servico("a", "", "java", ("b",)), dec.Servico("b", "", "java", ("a",))]
    assert dec.ordem_topologica(ciclo) is None


def test_dependencia_como_string_solta_e_aceita():
    servicos, erros = normalizar([servico("a"), servico("b", depende_de="a")])
    assert erros == [] and servicos[1].depende_de == ("a",)


# ---------- largura do fan-out ----------

def test_decomposicao_vazia_reprova():
    for bruto in ([], None, "", {}):
        _, erros = normalizar(bruto)
        assert erros, bruto


def test_acima_do_teto_reprova_por_maquina_nao_por_arquitetura():
    """Cada ramo mantém dois containers, e a jaula Java pede 2,5 GB. Mais largo
    que isto é sinal de que o pedido precisa ser quebrado em execuções."""
    muitos = [servico(f"s{i}") for i in range(dec.MAX_SERVICOS + 1)]
    _, erros = normalizar(muitos)
    assert any(str(dec.MAX_SERVICOS) in e for e in erros)


def test_no_teto_exato_passa():
    servicos, erros = normalizar([servico(f"s{i}") for i in range(dec.MAX_SERVICOS)])
    assert erros == [] and len(servicos) == dec.MAX_SERVICOS


def test_item_que_nao_e_objeto_reprova_sem_derrubar():
    _, erros = normalizar(["room-service", 42])
    assert len(erros) >= 2


# ---------- resumo ----------

def test_resumo_nomeia_stack_e_dependencias():
    servicos, _ = normalizar(LIVRO())
    texto = dec.resumo(servicos)
    assert "event-service [java]" in texto
    assert "depende de room-service, ticket-service" in texto
    assert "depende de" not in texto.splitlines()[1]
