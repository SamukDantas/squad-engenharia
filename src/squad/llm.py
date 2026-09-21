"""LLM único da squad, servido pelo provedor escolhido em `LLM_PROVEDOR`.

- `gateway` (padrão): gateway de IA on-premise cedido por um cliente, com API
  compatível com OpenAI e autenticação por token Keycloak renovado a cada
  request (`adaptadores/keycloak_token.py`).
- `zen`: OpenCode Zen, pago, com chave fixa (`OPENCODE_API_KEY`). Fica como
  plano B — o gateway corporativo é ambiente lab.
- `codex`: o Codex CLI, com o login da conta ChatGPT, responde pelos agentes
  (`CodexLLM`). Sem endpoint HTTP e sem Keycloak: cada chamada é um
  `codex exec` read-only num diretório vazio.

O prefixo "openai/" no modelo faz o CrewAI usar o provedor nativo da OpenAI
com base_url customizada.
"""
import os
import uuid

import httpx
from crewai import LLM
from crewai.llms.hooks.base import BaseInterceptor

from crewai.llms.base_llm import BaseLLM

from .adaptadores import codex_cli
from .adaptadores.keycloak_token import token_valido

ZEN_BASE_URL = "https://opencode.ai/zen/v1"
GATEWAY_BASE_URL = "https://ai-gateway.example.com/v1"
PROVEDORES = ("gateway", "zen", "codex")


def provedor() -> str:
    valor = (os.getenv("LLM_PROVEDOR") or "codex").strip().lower()
    if valor not in PROVEDORES:
        raise ValueError(
            f"LLM_PROVEDOR inválido: '{valor}'. Use 'codex', 'gateway' ou 'zen'."
        )
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


def _achatar(mensagens) -> str:
    """Mensagens de chat viram um texto só, com o papel de cada uma marcado.

    O Codex recebe um pedido, não uma conversa: o que o CrewAI separa em
    `system`/`user` (o papel do agente, a tarefa, o formato de resposta) chega
    junto, na ordem, e o modelo lê as instruções do sistema antes da tarefa.
    """
    if isinstance(mensagens, str):
        return mensagens
    partes = []
    for m in mensagens:
        papel = str(m.get("role", "user")).upper()
        conteudo = m.get("content", "")
        if isinstance(conteudo, list):  # conteúdo multimodal: só o texto
            conteudo = "\n".join(str(c.get("text", "")) for c in conteudo if isinstance(c, dict))
        partes.append(f"[{papel}]\n{conteudo}")
    return "\n\n".join(partes)


class CodexLLM(BaseLLM):
    """LLM do CrewAI servido pelo `codex exec` (`LLM_PROVEDOR=codex`).

    Só para agentes que respondem texto — planejamento, revisão, decomposição,
    guards. Agente com ferramenta não passa por aqui: o QA escreve a suíte
    rodando o Codex direto no workspace, como o executor
    (`codex_cli.executar_qa`), porque é o Codex quem sabe mexer em arquivo, e
    reimplementar isso por cima de tool calling em texto seria frágil.
    """

    llm_type: str = "codex"
    timeout_s: int = 300

    def call(self, messages, tools=None, callbacks=None, available_functions=None,
             from_task=None, from_agent=None, response_model=None, **_):
        if tools:
            raise ValueError(
                "CodexLLM não serve agente com ferramenta: com LLM_PROVEDOR=codex, "
                "o QA roda pelo Codex no workspace e o executor deve ser "
                "DEV_EXECUTOR=codex."
            )
        rotulo = getattr(from_agent, "role", None) or "agente"
        return codex_cli.responder(_achatar(messages), self.timeout_s, rotulo=str(rotulo))

    def supports_function_calling(self) -> bool:
        return False

    def supports_stop_words(self) -> bool:
        return False

    def get_context_window_size(self) -> int:
        return 200_000


def _codex(model: str | None, **teto) -> LLM:
    return CodexLLM(
        model=codex_cli.modelo(),
        timeout_s=int(teto.get("timeout") or _timeout()),
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
    fabrica = {"gateway": _gateway, "zen": _zen, "codex": _codex}[provedor()]
    return fabrica(model, **teto)


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
    if provedor() == "codex":
        _conferir_login_codex()
        return
    if provedor() != "gateway":
        return
    try:
        token_valido()
    except RuntimeError as e:
        raise RuntimeError(f"Credencial do provedor indisponível.\n{e}") from e


def _conferir_login_codex() -> None:
    """O Codex precisa do CLI instalado e do login da conta feito uma vez."""
    import subprocess
    r = subprocess.run(
        [codex_cli._binario(), "login", "status"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=60,
    )
    texto = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 or "logged in" not in texto.lower():
        raise RuntimeError(
            "Credencial do provedor indisponível: o Codex CLI não está logado.\n"
            "Rode `codex` uma vez num terminal e faça o login com a conta ChatGPT."
        )
