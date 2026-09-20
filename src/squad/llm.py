"""LLM único da squad, servido pelo provedor escolhido em `LLM_PROVEDOR`.

- `gateway` (padrão): gateway de IA on-premise cedido por um cliente, com API
  compatível com OpenAI e autenticação por token Keycloak renovado a cada
  request (`adaptadores/keycloak_token.py`).
- `zen`: OpenCode Zen, pago, com chave fixa (`OPENCODE_API_KEY`). Fica como
  plano B — o gateway corporativo é ambiente lab.

O prefixo "openai/" no modelo faz o CrewAI usar o provedor nativo da OpenAI
com base_url customizada.
"""
import os
import uuid

import httpx
from crewai import LLM
from crewai.llms.hooks.base import BaseInterceptor

from .adaptadores.keycloak_token import token_valido

ZEN_BASE_URL = "https://opencode.ai/zen/v1"
GATEWAY_BASE_URL = "https://ai-gateway.example.com/v1"
PROVEDORES = ("gateway", "zen")


def provedor() -> str:
    valor = (os.getenv("LLM_PROVEDOR") or "gateway").strip().lower()
    if valor not in PROVEDORES:
        raise ValueError(f"LLM_PROVEDOR inválido: '{valor}'. Use 'gateway' ou 'zen'.")
    return valor


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


class _KeycloakInterceptor(BaseInterceptor[httpx.Request, httpx.Response]):
    """Troca o `Authorization` de cada request pelo token Keycloak vigente.

    Header fixo na construção do LLM venceria no meio de um nó longo — uma
    chamada pode levar ~27 min até o timeout desistir. Pedir o token por
    request custa uma leitura de cache enquanto ele vale.
    """

    def on_outbound(self, message: httpx.Request) -> httpx.Request:
        message.headers["Authorization"] = f"Bearer {token_valido()}"
        return message

    def on_inbound(self, message: httpx.Response) -> httpx.Response:
        return message

    async def aon_outbound(self, message: httpx.Request) -> httpx.Request:
        return self.on_outbound(message)

    async def aon_inbound(self, message: httpx.Response) -> httpx.Response:
        return message


def _zen(model: str | None, **teto) -> LLM:
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
        **{"timeout": _timeout(), **teto},
    )


def _gateway(model: str | None, **teto) -> LLM:
    return LLM(
        model=model or os.getenv("MODEL", "openai/gateway"),
        base_url=os.getenv("GATEWAY_BASE_URL", GATEWAY_BASE_URL),
        # O SDK exige uma chave; quem autentica de verdade é o Bearer que o
        # interceptor põe por cima em cada request.
        api_key="keycloak",
        interceptor=_KeycloakInterceptor(),
        **{"timeout": _timeout(), **teto},
    )


def squad_llm(model: str | None = None, *, acessoria: bool = False) -> LLM:
    """LLM do provedor ativo.

    `acessoria=True` é para chamada que a execução dispensa (hoje, o nome do
    repositório): teto curto e sem as repetições internas do SDK. Com o teto
    das chamadas que importam (300s x repetições ≈ 27 min), um gateway lento
    poderia segurar a execução inteira por causa de um nome que a regra
    determinística resolve de graça. Preventivo — não houve travamento medido.
    """
    teto = {"timeout": _timeout_acessoria(), "max_retries": 0} if acessoria else {}
    return _gateway(model, **teto) if provedor() == "gateway" else _zen(model, **teto)


def _timeout_acessoria() -> int:
    try:
        return int(os.getenv("TIMEOUT_LLM_ACESSORIA", "30"))
    except ValueError:
        return 30


# Nome antigo, de quando o Zen era o único provedor.
zen_llm = squad_llm


def conferir_credencial() -> None:
    """Credencial do provedor conferida na entrada, antes de pagar qualquer nó.

    O erro do token Keycloak nasce dentro do interceptor, no meio de uma
    chamada HTTP, e o SDK da OpenAI o reembala como `Failed to connect to
    OpenAI API: Connection error` — a instrução de login, que existe e é
    precisa, some no caminho. Medido: uma execução morreu no planejamento com
    essa mensagem genérica, apontando para rede quando o problema era sessão
    expirada.

    Na entrada e não num nó do grafo: é condição de partida, como a stack
    inválida, e o grafo continua sem tocar em credencial.
    """
    if provedor() != "gateway":
        return
    try:
        token_valido()
    except RuntimeError as e:
        raise RuntimeError(f"Credencial do provedor indisponível.\n{e}") from e
