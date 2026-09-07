"""LLM único da squad, servido pelo OpenCode Zen (API compatível com OpenAI).

O CrewAI usa LiteLLM por baixo; o prefixo "openai/" indica endpoint
OpenAI-compatível com base_url customizada.
"""
import os
import uuid

from crewai import LLM

ZEN_BASE_URL = "https://opencode.ai/zen/v1"


def _timeout() -> int:
    """Teto por requisição HTTP ao provedor.

    Sem ele, uma resposta que nunca chega trava o nó para sempre: medido em
    execução real, 9 horas paradas no guard de aderência com 0,2% de CPU. E
    nenhuma das três camadas de resiliência ajuda — retry precisa de exceção,
    checkpoint precisa do processo morrer, circuit breaker precisa da rodada
    terminar. Todas pressupõem que a chamada retorna.
    """
    try:
        return int(os.getenv("TIMEOUT_LLM", "300"))
    except ValueError:
        return 300


# Sessão desta execução da squad. A rota "Go" do provedor passou a exigir o
# header `x-opencode-session` e rejeita a chamada sem ele com 400
# `MissingSessionID` — o que derrubou uma execução no planejamento, com a
# mensagem vindo de dentro do CrewAI e sem relação aparente com a squad.
#
# Um id por processo, e não por chamada: o header existe para o provedor rotear
# e cachear, e trocá-lo a cada requisição desperdiçaria exatamente o que ele
# serve para ganhar. `OPENCODE_SESSION` fixa o valor quando alguém quiser
# correlacionar execuções do lado do provedor.
_SESSAO = os.getenv("OPENCODE_SESSION") or str(uuid.uuid4())


def zen_llm(model: str | None = None) -> LLM:
    return LLM(
        model=model or os.getenv("MODEL", "openai/kimi-k2.7-code"),
        base_url=os.getenv("OPENCODE_BASE_URL", ZEN_BASE_URL),
        api_key=os.environ["OPENCODE_API_KEY"],
        extra_headers={"x-opencode-session": _SESSAO},
        # O SDK por baixo repete internamente, então o tempo real de uma
        # chamada travada é ~5,5x este teto (medido). Dimensionado com essa
        # multiplicação em mente: 300s aqui ≈ 27 min até desistir — muito,
        # mas finito, contra as 9 horas que custou sem teto nenhum.
        # `num_retries` não desliga isso: o CrewAI repassa kwargs ao SDK da
        # OpenAI, que rejeita o parâmetro e quebra a chamada.
        timeout=_timeout(),
    )
