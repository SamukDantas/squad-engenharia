"""Grafo LangGraph: espinha determinística que orquestra as crews.

Regra de divisão:
- decisões caras (avançar, repetir, parar, chamar humano) ficam AQUI, no grafo;
- trabalho especialista (escrever, testar, revisar) fica nas crews CrewAI.
"""
import sqlite3

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

try:
    from langgraph.checkpoint.sqlite import SqliteSaver
except ImportError:  # pacote opcional ausente: cai no checkpointer em memória
    SqliteSaver = None

from ..crews.desenvolvimento import crew_desenvolvimento
from ..crews.planejamento import crew_planejamento
from ..crews.qualidade import crew_qualidade
from ..llm import zen_llm
from .state import EstadoProjeto

MAX_TENTATIVAS = 3
MAX_REPLANEJAMENTOS = 2


# ---------- nós determinísticos ----------

def no_triagem(state: EstadoProjeto) -> EstadoProjeto:
    """Valida a entrada e inicializa contadores. Nenhum LLM envolvido."""
    pedido = (state.get("pedido") or "").strip()
    if not pedido:
        raise ValueError("Pedido vazio — nada a fazer.")
    return {"pedido": pedido, "tentativas": 0}


def no_aprovacao_humana(state: EstadoProjeto) -> EstadoProjeto:
    """Gate human-in-the-loop: pausa a execução até um humano decidir."""
    resposta = interrupt(
        {
            "mensagem": "Código aprovado pela qualidade. Autorizar deploy?",
            "relatorio_qa": state.get("relatorio_qa", ""),
        }
    )
    if str(resposta).strip().lower() not in {"sim", "s", "yes", "aprovar"}:
        raise RuntimeError("Deploy negado pelo aprovador humano.")
    return {}


def no_deploy(state: EstadoProjeto) -> EstadoProjeto:
    """Ação determinística: aqui entraria o comando real de deploy (CI/CD)."""
    print(">>> Executando deploy...")
    return {"deploy_ok": True}


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
    resultado = crew_desenvolvimento().kickoff(
        inputs={
            "spec": state["spec"],
            "feedback_qa": state.get("feedback_qa", "Nenhum — primeira rodada."),
        }
    )
    return {"codigo": resultado.raw, "tentativas": state["tentativas"] + 1}


def no_qualidade(state: EstadoProjeto) -> EstadoProjeto:
    resultado = crew_qualidade().kickoff(
        inputs={"codigo": state["codigo"], "spec": state["spec"]}
    )
    texto = resultado.raw
    aprovado = "APROVADO" in texto.upper().splitlines()[-1] if texto else False
    return {
        "relatorio_qa": texto,
        "aprovado": aprovado,
        "feedback_qa": "" if aprovado else texto,
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


def rota_pos_qualidade(state: EstadoProjeto) -> str:
    if state.get("aprovado"):
        return "aprovacao_humana"
    if state["tentativas"] >= MAX_TENTATIVAS:
        # Circuit breaker: humano decide o que fazer com o trabalho reprovado.
        return "aprovacao_humana"
    return "desenvolvimento"


# ---------- montagem do grafo ----------

def construir_grafo():
    g = StateGraph(EstadoProjeto)

    g.add_node("triagem", no_triagem)
    g.add_node("planejamento", no_planejamento)
    g.add_node("validacao_spec", no_validacao_spec)
    g.add_node("desenvolvimento", no_desenvolvimento)
    g.add_node("qualidade", no_qualidade)
    g.add_node("aprovacao_humana", no_aprovacao_humana)
    g.add_node("deploy", no_deploy)

    g.add_edge(START, "triagem")
    g.add_edge("triagem", "planejamento")
    g.add_edge("planejamento", "validacao_spec")
    g.add_conditional_edges("validacao_spec", rota_pos_validacao_spec)
    g.add_edge("desenvolvimento", "qualidade")
    g.add_conditional_edges("qualidade", rota_pos_qualidade)
    g.add_edge("aprovacao_humana", "deploy")
    g.add_edge("deploy", END)

    # Checkpointer persistente: sobrevive a quedas do processo, permitindo
    # retomar a execução do último nó concluído (ex.: falha 503 do provedor).
    if SqliteSaver is not None:
        conn = sqlite3.connect("checkpoints.sqlite", check_same_thread=False)
        return g.compile(checkpointer=SqliteSaver(conn))
    return g.compile(checkpointer=MemorySaver())
