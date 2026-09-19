"""Entrega que não inicia: o veredito que a execução paralela revelou.

Medido numa execução real dos três microsserviços do livro. O `event-service`
tinha 95,4% de cobertura, revisão aprovada e configuração de ambientes verde — e
não subia: declarava a URL base do `room-service` como propriedade Spring e
nunca a definia em perfil nenhum. Os testes não pegam porque dublam o gateway;
o revisor não pega porque lê código parado. Só quem tenta **subir** vê.

O pipeline via isso como "verificação visual falhou", com o traceback truncado
em 300 chars na métrica — a causa ficava dentro do container e não chegava a
ninguém. Aqui o desfecho vira veredito, com o log de boot como instrução.
"""
from src.squad.visual import _formatar_feedback

BOOT = (
    "Caused by: PlaceholderResolutionException: Could not resolve placeholder "
    "'event-service.integrations.room-service.base-url'"
)


def achado_nao_sobe(log=BOOT):
    return {
        "tipo": "alvo_nao_sobe",
        "arquivo": "(a entrega inteira)",
        "tema": "-",
        "detalhe": "o servidor encerrou em vez de atender",
        "log": log,
    }


def achado_contraste():
    return {
        "tipo": "contraste", "arquivo": "index.html", "tema": "dark",
        "seletor": "h1", "texto": "Título", "razao": 2.1, "exigido": 4.5,
        "cor": "#333", "fundo": "#000", "fundo_declarado": True,
    }


def test_o_log_de_boot_chega_inteiro_ao_dev():
    """É a instrução de conserto: sem ele o dev recebe "não subiu" e não sabe
    por quê."""
    texto = _formatar_feedback([achado_nao_sobe()])
    assert "Could not resolve placeholder" in texto
    assert "room-service.base-url" in texto


def test_diz_que_o_problema_nao_e_de_estilo():
    """Mandar "corrija no CSS" para quem tem um servidor que não inicia aponta o
    dev para o lugar errado."""
    texto = _formatar_feedback([achado_nao_sobe()])
    assert "CSS" not in texto
    assert "não entrou" in texto or "NÃO INICIA" in texto


def test_explica_por_que_os_testes_nao_pegaram():
    """Sem isso, o dev com a suíte verde na frente conclui que o veredito está
    errado — e a rodada seguinte volta igual."""
    texto = _formatar_feedback([achado_nao_sobe()])
    assert "dublam" in texto or "testes" in texto


def test_achado_de_contraste_continua_falando_de_css():
    texto = _formatar_feedback([achado_contraste()])
    assert "CSS" in texto and "contraste" in texto


def test_log_ausente_nao_derruba_a_formatacao():
    achado = achado_nao_sobe()
    del achado["log"]
    assert "(sem log)" in _formatar_feedback([achado])
