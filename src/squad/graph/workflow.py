"""Grafo LangGraph: espinha determinística que orquestra as crews.

Regra de divisão:
- decisões caras (avançar, repetir, parar, chamar humano) ficam AQUI, no grafo;
- trabalho especialista (escrever código, escrever testes, revisar) fica nas
  crews CrewAI;
- vereditos vêm de execução, não de opinião: o laço de correções é roteado
  pelo exit code do pytest (nó determinístico), e o revisor LLM cobre apenas
  o que execução não pega.
"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

try:
    from langgraph.checkpoint.sqlite import SqliteSaver
except ImportError:  # pacote opcional ausente: cai no checkpointer em memória
    SqliteSaver = None

from ..crews.desenvolvimento import crew_desenvolvimento
from ..crews.planejamento import crew_planejamento
from ..crews.qualidade import crew_revisao, crew_testes
from ..deploy import executar_deploy
from ..llm import zen_llm
from ..opencode import executar_opencode
from .state import EstadoProjeto

MAX_TENTATIVAS = 3
MAX_REPLANEJAMENTOS = 2
TIMEOUT_PYTEST = 120          # segundos; estourou = reprova (loop infinito etc.)
LIMITE_DUMP_CODIGO = 15_000   # chars de código injetados na tarefa do revisor
LIMITE_SAIDA_TESTES = 8_000   # chars da saída do pytest guardados no estado


# ---------- helpers de workspace ----------

# Diretórios de trabalho (caches de ferramentas e instruções da squad ao
# executor) que não fazem parte da entrega.
_IGNORAR_NO_WORKSPACE = {"__pycache__", ".pytest_cache", ".ruff_cache", ".squad", ".git"}


def _arquivos_do_workspace(workspace: str) -> list[str]:
    raiz = Path(workspace)
    return sorted(
        str(p.relative_to(raiz)).replace("\\", "/")
        for p in raiz.rglob("*")
        if p.is_file()
        and not _IGNORAR_NO_WORKSPACE.intersection(p.parts)
        and p.suffix != ".pyc"
    )


def _dump_codigo(workspace: str, arquivos: list[str]) -> str:
    """Concatena o conteúdo real dos arquivos (limitado) para injetar na
    tarefa do revisor — artefato que decide roteamento não pode depender só
    de elos de contexto entre tarefas (RESILIENCIA.md, item 10)."""
    partes: list[str] = []
    total = 0
    for rel in arquivos:
        conteudo = (Path(workspace) / rel).read_text(encoding="utf-8", errors="replace")
        bloco = f"### {rel}\n{conteudo}\n"
        partes.append(bloco)
        total += len(bloco)
        if total > LIMITE_DUMP_CODIGO:
            partes.append("### [dump truncado no limite]")
            break
    return "\n".join(partes)


# ---------- nós determinísticos ----------

def no_triagem(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    """Valida a entrada, cria o workspace da execução e zera contadores."""
    pedido = (state.get("pedido") or "").strip()
    if not pedido:
        raise ValueError("Pedido vazio — nada a fazer.")
    thread_id = config["configurable"]["thread_id"]
    workspace = Path("workspace") / str(thread_id)
    workspace.mkdir(parents=True, exist_ok=True)
    print(f">>> Workspace desta execução: {workspace.resolve()}")
    return {"pedido": pedido, "tentativas": 0, "workspace": str(workspace.resolve())}


def no_executar_testes(state: EstadoProjeto) -> EstadoProjeto:
    """Veredito por execução: roda o pytest de verdade no workspace.

    Sem LLM. Exit code 0 = verde; qualquer outro (inclusive 5, "nenhum teste
    coletado") = vermelho. Timeout = vermelho. O sandbox Docker desta etapa é
    migração futura (docs/DESENVOLVIMENTO-REAL.md) — por ora, subprocess no
    host com teto de tempo.
    """
    print(">>> Executando pytest no workspace...")
    comando = [sys.executable, "-m", "pytest", "tests", "--tb=short", "-q"]
    try:
        r = subprocess.run(
            comando,
            cwd=state["workspace"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,  # teste que pede input falha, não trava
            timeout=TIMEOUT_PYTEST,
        )
        saida = (r.stdout + "\n" + r.stderr).strip()[-LIMITE_SAIDA_TESTES:]
        testes_ok = r.returncode == 0
        print(f">>> pytest exit code {r.returncode} — {'verde' if testes_ok else 'vermelho'}")
    except subprocess.TimeoutExpired as e:
        parcial = str(e.stdout or "")[-2_000:]
        saida = (
            f"TIMEOUT: pytest excedeu {TIMEOUT_PYTEST}s — provável loop "
            f"infinito ou teste travado.\nSaída parcial:\n{parcial}"
        )
        testes_ok = False
        print(">>> pytest estourou o timeout — vermelho")
    return {
        "testes_ok": testes_ok,
        "saida_testes": saida,
        "feedback_qa": "" if testes_ok else (
            "Os testes automatizados FALHARAM. Corrija o código (ou os "
            f"imports/estrutura) com base na saída real do pytest:\n{saida}"
        ),
    }


def no_aprovacao_humana(state: EstadoProjeto) -> EstadoProjeto:
    """Gate human-in-the-loop: pausa a execução até um humano decidir."""
    resposta = interrupt(
        {
            "mensagem": "Autorizar deploy?",
            "testes_ok": state.get("testes_ok", False),
            "relatorio_qa": state.get("relatorio_qa", ""),
        }
    )
    if str(resposta).strip().lower() not in {"sim", "s", "yes", "aprovar"}:
        raise RuntimeError("Deploy negado pelo aprovador humano.")
    return {}


def no_deploy(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    """Deploy real (Fase 4): git commit + push da entrega, sem LLM. Só roda
    após aprovação humana explícita no gate."""
    thread_id = config["configurable"]["thread_id"]
    return executar_deploy(state["workspace"], str(thread_id), state["pedido"])


# ---------- nós que invocam crews ----------

def no_planejamento(state: EstadoProjeto) -> EstadoProjeto:
    resultado = crew_planejamento().kickoff(inputs={"pedido": state["pedido"]})
    return {
        "spec": resultado.raw,
        "spec_tentativas": state.get("spec_tentativas", 0) + 1,
    }


def no_validacao_spec(state: EstadoProjeto) -> EstadoProjeto:
    """Guard de aderência: uma chamada única e barata que confere se a spec
    produzida trata mesmo do pedido, antes de gastar tokens com desenvolvimento.
    Protege contra alucinação da crew de planejamento (spec de outro tema)."""
    veredito = zen_llm().call(
        "Você é um verificador rigoroso. Responda APENAS com a palavra SIM ou "
        f'NAO. A especificação técnica abaixo trata do pedido "{state["pedido"]}"'
        " — mesmo assunto e mesmo escopo, sem substituí-lo por outro tema?\n\n"
        f"Especificação:\n{state['spec'][:8000]}"
    )
    normalizado = str(veredito).strip().upper()
    coerente = normalizado.startswith("SIM") or (
        "SIM" in normalizado[:20] and "NAO" not in normalizado[:20]
        and "NÃO" not in normalizado[:20]
    )
    if not coerente:
        print(f">>> Guard: spec reprovada (não adere ao pedido). Veredito: {normalizado[:40]}")
    return {"spec_coerente": coerente}


def no_desenvolvimento(state: EstadoProjeto) -> EstadoProjeto:
    """Escreve a implementação no workspace. O executor é intercambiável
    (DEV_EXECUTOR): o OpenCode CLI como mão de obra especialista, ou as crews
    CrewAI como caminho sem dependência externa. A governança do grafo é a
    mesma nos dois casos."""
    executor = os.getenv("DEV_EXECUTOR", "opencode").strip().lower()
    feedback = state.get("feedback_qa", "")
    if executor == "opencode":
        executar_opencode(state["workspace"], state["spec"], feedback)
    elif executor == "crews":
        crew_desenvolvimento(state["workspace"]).kickoff(
            inputs={
                "spec": state["spec"],
                "feedback_qa": feedback or "Nenhum — primeira rodada.",
            }
        )
    else:
        raise ValueError(
            f"DEV_EXECUTOR inválido: '{executor}'. Use 'opencode' ou 'crews'."
        )
    # O que vale é o que está no disco: o manifesto do estado vem de uma
    # varredura determinística do workspace, não do texto do executor.
    arquivos = _arquivos_do_workspace(state["workspace"])
    return {
        "arquivos": arquivos,
        "codigo": _dump_codigo(state["workspace"], arquivos),
        "tentativas": state["tentativas"] + 1,
    }


def no_escrever_testes(state: EstadoProjeto) -> EstadoProjeto:
    crew_testes(state["workspace"]).kickoff(
        inputs={
            "spec": state["spec"],
            "arquivos": "\n".join(state.get("arquivos", [])) or "(workspace vazio)",
        }
    )
    # Revarre o workspace: os testes agora fazem parte da entrega e entram
    # no dump que o revisor recebe.
    arquivos = _arquivos_do_workspace(state["workspace"])
    return {
        "arquivos": arquivos,
        "codigo": _dump_codigo(state["workspace"], arquivos),
    }


def no_revisao(state: EstadoProjeto) -> EstadoProjeto:
    """Revisor LLM: só roda com testes verdes (não paga revisão de código que
    nem passa). Cobre o que execução não pega."""
    resultado = crew_revisao().kickoff(
        inputs={
            "codigo": state["codigo"],
            "spec": state["spec"],
            "saida_testes": state.get("saida_testes", ""),
        }
    )
    texto = resultado.raw
    aprovado = "APROVADO" in texto.upper().splitlines()[-1] if texto else False
    return {
        "relatorio_qa": texto,
        "aprovado": aprovado,
        "feedback_qa": "" if aprovado else (
            f"A revisão de código reprovou a entrega:\n{texto}"
        ),
    }


# ---------- roteamento ----------

def rota_pos_validacao_spec(state: EstadoProjeto) -> str:
    if state.get("spec_coerente"):
        return "desenvolvimento"
    if state.get("spec_tentativas", 0) > MAX_REPLANEJAMENTOS:
        raise RuntimeError(
            "Planejamento produziu specs incoerentes com o pedido "
            f"{MAX_REPLANEJAMENTOS + 1} vezes seguidas. Interrompendo para "
            "evitar desperdício de tokens — revise o modelo ou o prompt."
        )
    return "planejamento"


def rota_pos_testes(state: EstadoProjeto) -> str:
    if state.get("testes_ok"):
        return "revisao"
    if state["tentativas"] >= MAX_TENTATIVAS:
        # Circuit breaker: humano decide o que fazer com o trabalho reprovado.
        return "aprovacao_humana"
    return "desenvolvimento"


def rota_pos_revisao(state: EstadoProjeto) -> str:
    if state.get("aprovado"):
        return "aprovacao_humana"
    if state["tentativas"] >= MAX_TENTATIVAS:
        return "aprovacao_humana"
    return "desenvolvimento"


# ---------- montagem do grafo ----------

def construir_grafo():
    g = StateGraph(EstadoProjeto)

    g.add_node("triagem", no_triagem)
    g.add_node("planejamento", no_planejamento)
    g.add_node("validacao_spec", no_validacao_spec)
    g.add_node("desenvolvimento", no_desenvolvimento)
    g.add_node("escrever_testes", no_escrever_testes)
    g.add_node("executar_testes", no_executar_testes)
    g.add_node("revisao", no_revisao)
    g.add_node("aprovacao_humana", no_aprovacao_humana)
    g.add_node("deploy", no_deploy)

    g.add_edge(START, "triagem")
    g.add_edge("triagem", "planejamento")
    g.add_edge("planejamento", "validacao_spec")
    g.add_conditional_edges("validacao_spec", rota_pos_validacao_spec)
    g.add_edge("desenvolvimento", "escrever_testes")
    g.add_edge("escrever_testes", "executar_testes")
    g.add_conditional_edges("executar_testes", rota_pos_testes)
    g.add_conditional_edges("revisao", rota_pos_revisao)
    g.add_edge("aprovacao_humana", "deploy")
    g.add_edge("deploy", END)

    # Checkpointer persistente: sobrevive a quedas do processo, permitindo
    # retomar a execução do último nó concluído (ex.: falha 503 do provedor).
    if SqliteSaver is not None:
        conn = sqlite3.connect("checkpoints.sqlite", check_same_thread=False)
        return g.compile(checkpointer=SqliteSaver(conn))
    return g.compile(checkpointer=MemorySaver())
