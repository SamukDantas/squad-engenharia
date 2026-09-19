"""Codex CLI como executor do desenvolvimento (`DEV_EXECUTOR=codex`).

Mesmo contrato do OpenCode: escreve no workspace e não devolve nada que o
grafo acredite — quem lê o resultado é a varredura do disco.

O que muda é a jaula. O OpenCode precisa de um JSON injetado por variável de
ambiente para desligar MCP e skills da máquina; o Codex tem flag para isso:

- `--sandbox workspace-write`: escrita só no diretório de trabalho;
- `--ignore-user-config` e `--ignore-rules`: nada do `config.toml` nem das
  `.rules` da máquina entra na execução;
- `--ephemeral`: sem arquivos de sessão em disco;
- `--skip-git-repo-check`: o workspace só vira repositório git no deploy;
- `--json`: a saída vira JSONL com eventos, e `turn.failed` diz explicitamente
  que a run morreu — sinal melhor que procurar frase em texto livre.

O `-c windows.sandbox=...` é o detalhe que custou duas tentativas ao ser
medido nesta máquina: no Windows o modo do sandbox nativo vem do `config.toml`,
e `--ignore-user-config` (que é o que dá o isolamento por execução) apaga essa
configuração junto. Sem ela a política do Codex recusa **todo** comando,
inclusive os de leitura, e a run termina com **exit 0** sem ter feito nada —
a mesma armadilha do item 15 do RESILIENCIA.md, agora com outro binário.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

from .tarefa_executor import (
    DIR_SQUAD,
    LIMITE_SAIDA,
    PROMPT,
    gravar_tarefa,
    relatavel,
)

# Modo do sandbox nativo do Windows: `elevated` é o recomendado, mas exige
# instalação com privilégio de administrador; `unelevated` é o fallback que
# funciona sem isso. Fora do Windows a flag não existe e não é enviada.
SANDBOX_WINDOWS_PADRAO = "unelevated"


def _timeout() -> int:
    """Teto por rodada de desenvolvimento — o mesmo do OpenCode.

    Medido em execução real: rodadas de ~500s numa API multi-arquivo, e a
    terceira estourando os 900s originais. O teto existe contra CLI travado,
    não contra trabalho legítimo demorado.
    """
    try:
        return int(os.getenv("TIMEOUT_DESENVOLVIMENTO", "1800"))
    except ValueError:
        return 1800


def _sandbox_windows() -> str:
    return (os.getenv("CODEX_SANDBOX_WINDOWS") or SANDBOX_WINDOWS_PADRAO).strip()


def _comando(workspace: str) -> list[str]:
    binario = shutil.which("codex")
    if not binario:
        raise RuntimeError(
            "Codex CLI não encontrado no PATH. Instale-o (npm i -g @openai/codex) "
            "e faça o login uma vez (`codex`), ou troque o executor no .env com "
            "DEV_EXECUTOR=opencode."
        )
    comando = [
        binario, "exec",
        "--cd", workspace,
        "--sandbox", "workspace-write",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--json",
    ]
    if os.name == "nt":
        comando += ["-c", f'windows.sandbox="{_sandbox_windows()}"']
    modelo = (os.getenv("CODEX_RUN_MODEL") or "").strip()
    if modelo:
        comando += ["--model", modelo]
    comando.append(PROMPT)
    return comando


def _falha_no_stream(saida: str) -> str | None:
    """Lê o JSONL e devolve o motivo quando a run morreu.

    O Codex pode sair com 0 e mesmo assim não ter feito nada — comando barrado
    pela política, turno abortado. `turn.failed` e `error` são explícitos, o
    que evita repetir o caça-frases que o adaptador do OpenCode precisa fazer.
    Linha que não é JSON é ignorada: o stream é para máquina, mas o CLI ainda
    imprime coisa para humano no meio.
    """
    motivos: list[str] = []
    for linha in saida.splitlines():
        linha = linha.strip()
        if not linha.startswith("{"):
            continue
        try:
            evento = json.loads(linha)
        except ValueError:
            continue
        tipo = evento.get("type")
        if tipo == "turn.failed":
            erro = evento.get("error") or {}
            motivos.append(str(erro.get("message") or erro or "turn.failed"))
        elif tipo == "error":
            motivos.append(str(evento.get("message") or "error"))
    return "; ".join(motivos)[:500] or None


def _resumo(saida: str) -> str:
    """As mensagens do agente, sem o JSONL cru.

    Com `--json` a saída é um stream de eventos — ótimo para decidir se a run
    morreu, ilegível como log. O que interessa a quem lê o terminal é o que o
    agente disse ter feito; a varredura do disco continua sendo quem julga.
    """
    mensagens = []
    for linha in saida.splitlines():
        linha = linha.strip()
        if not linha.startswith("{"):
            continue
        try:
            evento = json.loads(linha)
        except ValueError:
            continue
        item = evento.get("item") or {}
        if evento.get("type") == "item.completed" and item.get("type") == "agent_message":
            mensagens.append(str(item.get("text", "")).strip())
    return "\n".join(m for m in mensagens if m)


def executar_codex(workspace: str, spec: str, feedback_qa: str, perfil) -> str:
    gravar_tarefa(workspace, spec, feedback_qa, perfil)
    comando = _comando(workspace)

    print(f">>> Desenvolvimento via Codex CLI ({os.getenv('CODEX_RUN_MODEL') or 'modelo padrão'})...")
    if os.name == "nt":
        print(f"    sandbox: workspace-write ({_sandbox_windows()}) | config e rules da máquina: ignorados")
    try:
        r = subprocess.run(
            comando,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            # Sem stdin herdado: o CLI não pode consumir a entrada do processo
            # pai (a resposta do gate humano) nem travar esperando input. No
            # Codex isso é duplamente importante: stdin com conteúdo vira
            # contexto extra do prompt.
            stdin=subprocess.DEVNULL,
            timeout=_timeout(),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Codex CLI excedeu {_timeout()}s. Progresso salvo no checkpoint: "
            "retome com --thread para reexecutar o desenvolvimento."
        )

    saida = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    trecho = relatavel(saida[-LIMITE_SAIDA:])
    if r.returncode != 0:
        raise RuntimeError(f"Codex CLI falhou (exit {r.returncode}):\n{trecho}")

    motivo = _falha_no_stream(saida)
    if motivo:
        raise RuntimeError(
            f"Codex CLI abortou com exit 0: {motivo}\nExit code classifica o "
            "processo, não o trabalho: sem este guard a squad paga QA, guards e "
            "pytest em cima de um workspace intocado. Se o motivo for comando "
            "barrado pela política, confira CODEX_SANDBOX_WINDOWS (o sandbox "
            f"nativo precisa estar declarado, ver {DIR_SQUAD}/tarefa.md)."
        )

    print(relatavel((_resumo(saida) or trecho)[-LIMITE_SAIDA:]))
    return saida
