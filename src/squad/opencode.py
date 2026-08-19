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
import sys
from pathlib import Path

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
LIMITE_SAIDA = 4_000    # chars da saída ecoados no log

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

LIBS_PERMITIDAS = "fastapi, flask, httpx, requests"

# Diretório de trabalho da squad dentro do workspace: instruções para o
# executor, fora da entrega (ignorado nas varreduras e no deploy).
DIR_SQUAD = ".squad"
ARQUIVO_TAREFA = "tarefa.md"


def _relatavel(texto: str) -> str:
    """Saída do executor legível em qualquer stdout.

    O OpenCode imprime setas, ícones e box-drawing. Quando o stdout é
    redirecionado (arquivo, pipe, CI), o Python no Windows usa a codepage
    local — cp1252, que não tem esses caracteres — e o `print` levanta
    UnicodeEncodeError. Medido na thread `20051ccc`: o nó rodou os 244s,
    escreveu a entrega inteira e morreu ao **relatar** o que tinha feito,
    derrubando o grafo com a entrega já em disco.

    Vale tanto para o eco no log quanto para as mensagens de erro, que a
    `main` também imprime — relatar a falha não pode ser uma segunda falha.
    """
    codificacao = sys.stdout.encoding or "utf-8"
    return texto.encode(codificacao, "replace").decode(codificacao, "replace")


def _arquivo_config_global() -> Path:
    base = os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "opencode" / "opencode.json"


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
        dados = json.loads(conteudo)
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
    return json.dumps(config)


def _instrucoes(spec: str, feedback_qa: str) -> str:
    correcao = ""
    if feedback_qa and feedback_qa.strip():
        correcao = (
            "\n\n## Rodada de correção\n"
            "Antes de qualquer outra coisa, leia os arquivos existentes e "
            "corrija os pontos abaixo:\n\n"
            f"{feedback_qa}\n"
        )
    return (
        "# Tarefa de implementação\n\n"
        "Implemente a especificação abaixo neste diretório, escrevendo "
        "arquivos reais.\n\n"
        "## Regras obrigatórias\n"
        "- Python apenas, usando somente a biblioteca padrão e estas libs já "
        f"instaladas: {LIBS_PERMITIDAS}. Não use nenhuma outra dependência.\n"
        "- NÃO crie nem edite nada dentro de `tests/` — os testes são escritos "
        "por outro agente da equipe de qualidade.\n"
        "- Escreva ou atualize o `README.md` com a estrutura e as instruções "
        "de execução.\n"
        "- NÃO deixe arquivos de rastro na entrega (saída de comandos, logs, "
        "relatórios de teste). Se precisar salvar algo transitório, use o "
        "diretório `.squad/`.\n"
        "- NÃO crie scripts para rodar os testes (`run_tests.py` e afins): a "
        "suíte é executada pelo próprio pipeline.\n"
        "- Mantenha o escopo estritamente na especificação.\n\n"
        "## Especificação\n\n"
        f"{spec}\n"
        f"{correcao}"
    )


def executar_opencode(workspace: str, spec: str, feedback_qa: str = "") -> str:
    binario = shutil.which("opencode")
    if not binario:
        raise RuntimeError(
            "OpenCode CLI não encontrado no PATH. Instale-o (npm i -g opencode-ai) "
            "ou use o executor alternativo com DEV_EXECUTOR=crews no .env."
        )

    dir_squad = Path(workspace) / DIR_SQUAD
    dir_squad.mkdir(parents=True, exist_ok=True)
    (dir_squad / ARQUIVO_TAREFA).write_text(
        _instrucoes(spec, feedback_qa), encoding="utf-8"
    )

    comando = [binario, "run", "--dir", workspace]
    modelo = (os.getenv("OPENCODE_RUN_MODEL") or "").strip()
    if modelo:
        comando += ["-m", modelo]
    comando.append(
        f"Leia o arquivo {DIR_SQUAD}/{ARQUIVO_TAREFA} e execute exatamente a "
        "tarefa descrita nele, respeitando todas as regras obrigatórias."
    )

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
    trecho = _relatavel(saida[-LIMITE_SAIDA:])
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
