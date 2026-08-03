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

TIMEOUT_OPENCODE = 900  # segundos; implementação completa é mais lenta que um teste
LIMITE_SAIDA = 4_000    # chars da saída ecoados no log

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
            timeout=TIMEOUT_OPENCODE,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"OpenCode CLI excedeu {TIMEOUT_OPENCODE}s. Progresso salvo no "
            "checkpoint: retome com --thread para reexecutar o desenvolvimento."
        )

    saida = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    if r.returncode != 0:
        raise RuntimeError(
            f"OpenCode CLI falhou (exit {r.returncode}):\n{saida[-LIMITE_SAIDA:]}"
        )

    print(saida[-LIMITE_SAIDA:])
    return saida
