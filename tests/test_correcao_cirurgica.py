"""Correção cirúrgica de teste: o QA mexe no que o retorno aponta, não na suíte.

Na calculadora de juros (thread `00b92498`) três passadas do QA somaram 52 dos
73 min de nós; na retomada seguinte (`33c19d49`), quatro somaram 35 de 69. O
nó que escreve a suíte é o mais caro do grafo, e no gateway corporativo o
custo cresce com o tamanho da resposta — reenviar a suíte inteira a cada
passada era o que mais custava.
"""
from src.squad.adaptadores import perfis
from src.squad.dominio.vereditos import justificativa, veredito_sim
from src.squad.graph import workflow

PYTHON = perfis.obter("python")


# ---------- o guard de critérios diz o que falta ----------

def test_resposta_com_lista_mantem_o_veredito_e_recupera_o_que_falta():
    resposta = "NAO\n- prazo zero devolve o capital\n- taxa negativa é recusada"
    assert veredito_sim(resposta) is False
    assert justificativa(resposta) == "- prazo zero devolve o capital\n- taxa negativa é recusada"


def test_sim_sozinho_nao_tem_justificativa():
    assert veredito_sim("SIM") is True
    assert justificativa("SIM") == ""


def test_feedback_do_guard_lista_o_que_falta_e_proibe_reescrever():
    feedback = workflow._feedback_criterios("NAO\n- taxa negativa é recusada")
    assert "taxa negativa é recusada" in feedback
    assert "sem reescrever" in feedback


def test_guard_sem_lista_ainda_orienta_a_complementar():
    """Modelo que só responde NAO continua recebendo um retorno útil."""
    feedback = workflow._feedback_criterios("NAO")
    assert "Complemente" in feedback and "Reescreva" not in feedback


# ---------- o QA em modo ajuste ----------

def test_primeira_rodada_nao_e_ajuste():
    assert workflow._feedback_para_qa({"arquivos": ["app.py"]}, PYTHON) == "Nenhum — primeira rodada."


def test_com_suite_no_disco_o_qa_recebe_modo_ajuste():
    estado = {
        "arquivos": ["app.py", "tests/test_app.py", "tests/conftest.py"],
        "feedback_qa": "Cobertura de app.py em 40%.",
    }
    texto = workflow._feedback_para_qa(estado, PYTHON)
    assert texto.startswith("MODO AJUSTE")
    assert "tests/test_app.py" in texto and "tests/conftest.py" in texto
    assert "NÃO regrave" in texto
    assert texto.endswith("Cobertura de app.py em 40%.")


def test_sem_suite_o_retorno_vai_como_veio():
    """Não há o que ajustar: o QA escreve do zero, com o retorno como guia."""
    estado = {"arquivos": ["app.py"], "feedback_qa": "Faltam testes."}
    assert workflow._feedback_para_qa(estado, PYTHON) == "Faltam testes."
