"""OpenCode CLI como executor do desenvolvimento (Fase 5).

A assinatura vira a mão de obra e a squad vira a gerência: o CLI escreve o
código no workspace, enquanto guard de aderência, QA que executa pytest e
gate humano continuam governando o processo.

As instruções vão para `.squad/tarefa.md` dentro do workspace e o prompt do
CLI é uma única linha apontando para o arquivo. Motivo: no Windows o binário
resolvido é o shim `opencode.CMD` do npm, e argumentos multilinha passados
por cmd.exe são truncados na primeira quebra de linha (a spec sumia, e specs
longas ainda esbarrariam no limite de tamanho de argumento).

Do ponto de vista do grafo o nó continua determinístico: quem lê o resultado
é a varredura do disco, não o texto devolvido pelo executor.

O `--dir` troca o diretório de trabalho mas NÃO isola a configuração: o
`opencode.json` global do usuário é sempre mesclado, e com ele entram os
servidores MCP e as skills instaladas na máquina. Medido: 15 MCP habilitados
(incluindo controle do SO, Docker e GitHub com token) e 623 skills, que
sozinhas respondiam por 94.255 dos 104.798 tokens de preâmbulo — 90% do que o
executor lia antes de chegar na spec, e uma superfície de ferramenta muito
maior que a jaula descrita no README. `_config_escopo` fecha isso por
execução, sem tocar no config global da máquina.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

from ..llm import GATEWAY_BASE_URL, provedor
from .keycloak_token import token_valido
from .tarefa_executor import LIMITE_SAIDA, PROMPT, gravar_tarefa, relatavel


def _timeout() -> int:
    """Teto por rodada de desenvolvimento.

    Medido em execução real: rodadas de ~500s numa API multi-arquivo, e a
    terceira estourando os 900s originais — rodadas de correção ficam mais
    caras conforme o código cresce e o feedback acumula. O teto existe contra
    CLI travado, não contra trabalho legítimo demorado.
    """
    try:
        return int(os.getenv("TIMEOUT_DESENVOLVIMENTO", "1800"))
    except ValueError:
        return 1800

# Frases com que o CLI aborta a run e mesmo assim devolve exit 0 — a jaula do
# próprio OpenCode barrando uma ferramenta. A causa raiz é o modelo de
# permissão: `external_directory` tem default `ask`, e em modo headless não há
# quem responda, então o CLI auto-rejeita e mata a run. O /tmp do Windows foi
# o gatilho, não a causa. Exit code classifica o processo, não o trabalho: sem
# isto o nó conclui "com sucesso" e a squad paga QA, guards e pytest em cima de
# um workspace vazio (RESILIENCIA.md, itens 15 e 28).
#
# `_config_escopo` torna a permissão explícita, então este guard vira rede de
# segurança em vez de caminho esperado — mas continua valendo: qualquer outra
# ferramenta barrada mata a run do mesmo jeito.
_ABORTOS_COM_EXIT_ZERO = (
    "the user rejected permission to use this specific tool call",
    "auto-rejecting",
)

# Servidores MCP declarados no config global do OpenCode na máquina de
# referência. Não é política — é linha de base para detectar deriva: um
# servidor novo no global passaria a enxergar o workspace sem ninguém pedir.
MCP_CONHECIDOS = (
    "blender", "chrome-devtools", "context7", "docker", "github", "grafana",
    "kubernetes", "make", "newrelic", "ngrok", "prometheus", "supabase",
    "testsprite", "th0th", "trello", "unityMCP", "upstash", "vercel",
    "windows-mcp",
)

def _arquivo_config_global() -> Path:
    base = os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "opencode" / "opencode.json"


def _sem_jsonc(texto: str) -> str:
    """JSONC → JSON: tira comentários e vírgulas finais, fora de strings.

    O OpenCode lê o config como JSONC. A leitura estrita daqui recusava o que o
    próprio OpenCode aceita: uma vírgula sobrando no `plugin` do config global
    derrubava toda execução com `DEV_EXECUTOR=opencode`, dizendo que "o OpenCode
    falharia na mesma leitura" — e ele não falhava.
    """
    saida: list[str] = []
    i, n = 0, len(texto)
    while i < n:
        c = texto[i]
        if c == '"':
            j = i + 1
            while j < n and texto[j] != '"':
                j += 2 if texto[j] == "\\" else 1
            saida.append(texto[i:j + 1])
            i = j + 1
        elif texto.startswith("//", i):
            fim = texto.find("\n", i)
            i = n if fim < 0 else fim
        elif texto.startswith("/*", i):
            fim = texto.find("*/", i + 2)
            i = n if fim < 0 else fim + 2
        elif c == ",":
            j = i + 1
            while j < n and texto[j].isspace():
                j += 1
            if j < n and texto[j] in "}]":
                i += 1  # vírgula final: descarta
            else:
                saida.append(c)
                i += 1
        else:
            saida.append(c)
            i += 1
    return "".join(saida)


def _mcp_declarados() -> list[str]:
    """Nomes de MCP no config global da máquina.

    Lista vazia quando não há config global — máquina sem config não tem o
    problema que `_config_escopo` resolve.
    """
    arquivo = _arquivo_config_global()
    try:
        conteudo = arquivo.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as e:
        raise RuntimeError(f"não foi possível ler {arquivo}: {e}")
    try:
        dados = json.loads(_sem_jsonc(conteudo))
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"{arquivo} não é JSON válido ({e}). O OpenCode falharia na mesma "
            "leitura — corrija o arquivo antes de rodar a squad."
        )
    return sorted(dados.get("mcp") or {})


def _mcp_permitidos() -> set[str]:
    bruto = os.getenv("OPENCODE_MCP_PERMITIDOS", "")
    return {n.strip() for n in bruto.split(",") if n.strip()}


def _skills_ligadas() -> bool:
    """Skills globais do OpenCode no prompt do executor: desligadas por padrão.

    Só `1` liga. Qualquer outro valor desliga, inclusive vazio ou lixo — o
    default caro é o errado para se cair por engano de digitação.
    """
    return os.getenv("OPENCODE_SKILLS", "0").strip() == "1"


def _config_escopo(workspace: str) -> str:
    """Config que o executor recebe por `OPENCODE_CONFIG_CONTENT`.

    O OpenCode mescla este conteúdo com o `opencode.json` global por chave, e
    entradas mais tardias vencem — dá para fechar o escopo por execução sem
    tocar na máquina do usuário. Três coisas são fixadas aqui:

    - **MCP**: todos desligados, menos os de `OPENCODE_MCP_PERMITIDOS`. O nó
      escreve Python (stdlib + as libs pré-provisionadas) num workspace
      isolado; não precisa de GitHub, Supabase nem controle do SO para isso.
    - **skills**: o tool `skill` desligado, salvo `OPENCODE_SKILLS=1`. Medido
      em 3 execuções por braço no mesmo pedido: as skills globais custam
      **94.255 tokens de preâmbulo por rodada** (104.798 contra 10.543) e
      **4,5x o custo** ($0,1658 contra $0,0367), com entrega idêntica. Quem
      precisar de uma skill específica liga a variável; carregar 623 para
      talvez usar uma não se paga.
    - **permissão**: `external_directory` explícito (workspace liberado, resto
      negado) no lugar do `ask` que em headless vira auto-rejeição silenciosa.

    Com `LLM_PROVEDOR=gateway` entra uma quarta: o provider do gateway e o
    plugin Keycloak, pelo mesmo canal (`_config_gateway`).
    """
    permitidos = _mcp_permitidos()
    # União com os declarados: um servidor que apareça no global depois desta
    # lista também precisa ser desligado, senão a correção envelhece calada.
    alvos = (set(MCP_CONHECIDOS) | set(_mcp_declarados())) - permitidos

    raiz = Path(workspace).resolve().as_posix()
    config: dict = {
        "mcp": {nome: {"enabled": False} for nome in sorted(alvos)},
        "agent": {
            "build": {
                "permission": {
                    "external_directory": {"*": "deny", f"{raiz}/**": "allow"},
                },
            },
        },
    }
    if not _skills_ligadas():
        config["agent"]["build"]["tools"] = {"skill": False}
    if provedor() == "gateway":
        config.update(_config_gateway())
    return json.dumps(config)


def _plugin_keycloak() -> str:
    bruto = (os.getenv("GATEWAY_OPENCODE_PLUGIN") or "").strip()
    caminho = Path(bruto).expanduser() if bruto else (
        Path.home() / ".config" / "opencode" / "keycloak-token.ts"
    )
    return caminho.as_posix()


def _config_gateway() -> dict:
    """Provedor gateway corporativo para o executor, no mesmo escopo por execução.

    Espelha o `keys/gateway.json` do cliente sem depender dele: o provider
    compatível com OpenAI e o plugin que injeta o Bearer do Keycloak em cada
    chamada. A `apiKey` é placeholder — quem autentica é o plugin.
    """
    return {
        "plugin": [_plugin_keycloak()],
        "provider": {
            "gateway": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Gateway corporativo",
                "apiKey": "keycloak",
                "options": {"baseURL": os.getenv("GATEWAY_BASE_URL", GATEWAY_BASE_URL)},
                "models": {"gateway": {"name": "Gateway corporativo"}},
            },
        },
    }


def _modelo_executor() -> str:
    modelo = (os.getenv("OPENCODE_RUN_MODEL") or "").strip()
    if modelo:
        return modelo
    return "gateway/gateway" if provedor() == "gateway" else ""


def executar_opencode(workspace: str, spec: str, feedback_qa: str, perfil) -> str:
    binario = shutil.which("opencode")
    if not binario:
        raise RuntimeError(
            "OpenCode CLI não encontrado no PATH. Instale-o (npm i -g opencode-ai) "
            "ou use o executor alternativo com DEV_EXECUTOR=crews no .env."
        )

    gravar_tarefa(workspace, spec, feedback_qa, perfil)

    if provedor() == "gateway":
        # Sem token válido, o plugin abre um navegador que ninguém vê e espera
        # 5 min antes de desistir. Aqui a falha é imediata e diz o que fazer.
        token_valido()

    comando = [binario, "run", "--dir", workspace]
    modelo = _modelo_executor()
    if modelo:
        comando += ["-m", modelo]
    comando.append(PROMPT)

    permitidos = _mcp_permitidos()
    print(f">>> Desenvolvimento via OpenCode CLI ({modelo or 'modelo padrão'})...")
    print(
        f"    MCP: {', '.join(sorted(permitidos)) if permitidos else 'nenhum'}"
        f" | skills: {'sim' if _skills_ligadas() else 'não'}"
    )
    try:
        r = subprocess.run(
            comando,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            # Sem stdin herdado: o CLI não pode consumir a entrada do processo
            # pai (a resposta do gate humano) nem travar esperando input.
            stdin=subprocess.DEVNULL,
            # Escopo por execução: sem isto o CLI herda os MCP e as skills do
            # config global da máquina (ver docstring do módulo).
            env={**os.environ, "OPENCODE_CONFIG_CONTENT": _config_escopo(workspace)},
            timeout=_timeout(),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"OpenCode CLI excedeu {_timeout()}s. Progresso salvo no "
            "checkpoint: retome com --thread para reexecutar o desenvolvimento."
        )

    saida = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    trecho = relatavel(saida[-LIMITE_SAIDA:])
    if r.returncode != 0:
        raise RuntimeError(f"OpenCode CLI falhou (exit {r.returncode}):\n{trecho}")

    motivo = _aborto_silencioso(saida)
    if motivo:
        raise RuntimeError(
            f"OpenCode CLI abortou com exit 0: {motivo!r} na saída. A permissão "
            "negada mata a run antes da entrega — em modo headless não há quem "
            "responda ao pedido. O acesso fora do workspace já é negado "
            "explicitamente por _config_escopo, então isto aponta para outra "
            "ferramenta barrada: leia a saída abaixo para identificar qual e "
            "decida se ela deve ser liberada no escopo ou evitada na spec.\n"
            f"{trecho}"
        )

    print(trecho)
    return saida


def _aborto_silencioso(saida: str) -> str | None:
    baixa = saida.lower()
    return next((f for f in _ABORTOS_COM_EXIT_ZERO if f in baixa), None)
