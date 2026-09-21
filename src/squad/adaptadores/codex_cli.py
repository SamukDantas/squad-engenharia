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
import tempfile
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


def _binario() -> str:
    binario = shutil.which("codex")
    if not binario:
        raise RuntimeError(
            "Codex CLI não encontrado no PATH. Instale-o (npm i -g @openai/codex) "
            "e faça o login uma vez (`codex`), ou troque o executor no .env com "
            "DEV_EXECUTOR=opencode."
        )
    return binario


def modelo() -> str:
    """O modelo de todo uso do Codex na squad — executor e agentes."""
    return (os.getenv("CODEX_RUN_MODEL") or "").strip()


def _base(diretorio: str, sandbox: str) -> list[str]:
    """As flags comuns a toda chamada: escopo por execução e falha explícita.

    `sandbox` é `workspace-write` para quem grava a entrega (executor, QA) e
    `read-only` para quem só responde texto (planejamento, revisão, guards).
    """
    comando = [
        _binario(), "exec",
        "--cd", diretorio,
        "--sandbox", sandbox,
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--json",
    ]
    if os.name == "nt":
        comando += ["-c", f'windows.sandbox="{_sandbox_windows()}"']
    if modelo():
        comando += ["--model", modelo()]
    return comando


def _comando(workspace: str, prompt: str = PROMPT) -> list[str]:
    return _base(workspace, "workspace-write") + [prompt]


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


def _rodar(comando: list[str], timeout: int, rotulo: str, entrada: str | None = None) -> str:
    """Roda o `codex exec` e devolve a saída, ou levanta com o motivo real.

    `entrada` vai por stdin: o Codex trata o argumento como a instrução e o
    stdin como contexto. É por aí que texto longo entra — argumento multilinha
    é truncado na primeira quebra de linha pelo shim do npm no Windows. Sem
    `entrada`, o stdin é fechado: o CLI não pode consumir a resposta do gate
    humano nem ficar esperando input.
    """
    try:
        r = subprocess.run(
            comando,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            input=entrada,
            stdin=None if entrada is not None else subprocess.DEVNULL,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Codex CLI excedeu {timeout}s em {rotulo}. Progresso salvo no "
            "checkpoint: retome com --thread para reexecutar o nó."
        )

    saida = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    trecho = relatavel(saida[-LIMITE_SAIDA:])
    if r.returncode != 0:
        raise RuntimeError(f"Codex CLI falhou em {rotulo} (exit {r.returncode}):\n{trecho}")

    motivo = _falha_no_stream(saida)
    if motivo:
        raise RuntimeError(
            f"Codex CLI abortou com exit 0 em {rotulo}: {motivo}\nExit code "
            "classifica o processo, não o trabalho: sem este guard a squad paga "
            "QA, guards e pytest em cima de um workspace intocado. Se o motivo "
            "for comando barrado pela política, confira CODEX_SANDBOX_WINDOWS "
            "(o sandbox nativo precisa estar declarado)."
        )
    return saida


def executar_codex(workspace: str, spec: str, feedback_qa: str, perfil) -> str:
    gravar_tarefa(workspace, spec, feedback_qa, perfil)
    print(f">>> Desenvolvimento via Codex CLI ({modelo() or 'modelo padrão'})...")
    if os.name == "nt":
        print(f"    sandbox: workspace-write ({_sandbox_windows()}) | config e rules da máquina: ignorados")
    saida = _rodar(_comando(workspace), _timeout(), "desenvolvimento")
    print(relatavel((_resumo(saida) or saida)[-LIMITE_SAIDA:]))
    return saida


# ---------- agentes que só respondem texto ----------

# A instrução vai no argumento, curta e de uma linha; o pedido de verdade vai no
# stdin. O agente não tem arquivo para ler nem comando para rodar: ele só
# responde, num diretório vazio e com o sandbox em read-only.
PROMPT_TEXTO = (
    "Responda à solicitação que está no contexto fornecido pela entrada padrão. "
    "Não leia arquivos nem execute comandos: responda apenas com o texto pedido, "
    "no formato que a solicitação exigir."
)


def responder(texto: str, timeout: int, rotulo: str = "chamada de texto") -> str:
    """Uma resposta de texto do Codex — o equivalente a um `chat/completions`.

    Roda num diretório temporário vazio, com sandbox read-only: o agente que
    planeja, revisa ou julga não tem por que ver a máquina, nem o workspace
    (o que ele precisa julgar vem dentro do próprio texto, como já vinha).
    """
    with tempfile.TemporaryDirectory(prefix="squad-codex-") as vazio:
        saida = _rodar(
            _base(vazio, "read-only") + [PROMPT_TEXTO], timeout, rotulo, entrada=texto
        )
    resposta = _resumo(saida)
    if not resposta:
        raise RuntimeError(f"Codex CLI não devolveu mensagem em {rotulo}.")
    return resposta


# ---------- QA: o agente que escreve a suíte ----------

ARQUIVO_TAREFA_QA = "tarefa_qa.md"
PROMPT_QA = (
    f"Leia o arquivo {DIR_SQUAD}/{ARQUIVO_TAREFA_QA} e execute exatamente a "
    "tarefa descrita nele, respeitando todas as regras obrigatórias."
)

REGRAS_QA = """# Tarefa de QA: escrever a suíte de testes

## Regras obrigatórias
- Escreva SOMENTE arquivos de teste. NÃO altere, crie nem apague nenhum outro
  arquivo da entrega: o código é de outro agente, e qualquer mudança fora da
  suíte é desfeita pelo pipeline depois desta rodada.
- Não rode instalação de dependências.
- Não deixe arquivos de rastro (saída de comandos, relatórios).

## Tarefa
"""


def executar_qa(workspace: str, descricao: str) -> str:
    """O QA rodando pelo Codex direto no workspace (`LLM_PROVEDOR=codex`).

    `descricao` é a tarefa `escrever_testes` do `tasks.yaml` já preenchida —
    a mesma que o QA do CrewAI recebe, para o caminho pelo Codex não ter
    regras diferentes. O grafo confere depois que nada fora da suíte mudou.
    """
    dir_squad = Path(workspace) / DIR_SQUAD
    dir_squad.mkdir(parents=True, exist_ok=True)
    (dir_squad / ARQUIVO_TAREFA_QA).write_text(REGRAS_QA + descricao, encoding="utf-8")
    print(f">>> QA via Codex CLI ({modelo() or 'modelo padrão'})...")
    saida = _rodar(_comando(workspace, PROMPT_QA), _timeout(), "escrita de testes")
    print(relatavel((_resumo(saida) or saida)[-LIMITE_SAIDA:]))
    return saida
