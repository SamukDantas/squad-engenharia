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
"""
import os
import shutil
import subprocess
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
# próprio OpenCode barrando uma ferramenta (medido: o executor tentou escrever
# em /tmp, que no Windows resolve para fora do workspace, e a run morreu antes
# de criar qualquer arquivo da entrega). Exit code classifica o processo, não
# o trabalho: sem isto o nó conclui "com sucesso" e a squad paga QA, guards e
# pytest em cima de um workspace vazio (RESILIENCIA.md, itens 15 e 28).
_ABORTOS_COM_EXIT_ZERO = (
    "the user rejected permission to use this specific tool call",
    "auto-rejecting",
)

LIBS_PERMITIDAS = "fastapi, flask, httpx, requests"

# Diretório de trabalho da squad dentro do workspace: instruções para o
# executor, fora da entrega (ignorado nas varreduras e no deploy).
DIR_SQUAD = ".squad"
ARQUIVO_TAREFA = "tarefa.md"


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

    print(f">>> Desenvolvimento via OpenCode CLI ({modelo or 'modelo padrão'})...")
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
            timeout=_timeout(),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"OpenCode CLI excedeu {_timeout()}s. Progresso salvo no "
            "checkpoint: retome com --thread para reexecutar o desenvolvimento."
        )

    saida = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    if r.returncode != 0:
        raise RuntimeError(
            f"OpenCode CLI falhou (exit {r.returncode}):\n{saida[-LIMITE_SAIDA:]}"
        )

    motivo = _aborto_silencioso(saida)
    if motivo:
        raise RuntimeError(
            f"OpenCode CLI abortou com exit 0: {motivo!r} na saída. A permissão "
            "negada mata a run antes da entrega — em modo headless não há quem "
            "responda ao pedido. Verifique se o executor está tentando escrever "
            "fora do workspace (ex.: /tmp, que no Windows resolve para outro "
            f"volume).\n{saida[-LIMITE_SAIDA:]}"
        )

    print(saida[-LIMITE_SAIDA:])
    return saida


def _aborto_silencioso(saida: str) -> str | None:
    baixa = saida.lower()
    return next((f for f in _ABORTOS_COM_EXIT_ZERO if f in baixa), None)
