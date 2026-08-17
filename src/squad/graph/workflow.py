"""Grafo LangGraph: espinha determinística que orquestra as crews.

Regra de divisão:
- decisões caras (avançar, repetir, parar, chamar humano) ficam AQUI, no grafo;
- trabalho especialista (escrever código, escrever testes, revisar) fica nas
  crews CrewAI;
- vereditos vêm de execução, não de opinião: o laço de correções é roteado
  pelo exit code do pytest e pela cobertura (nós determinísticos), e o revisor
  LLM cobre apenas o que execução não pega.
"""
import os
import sqlite3
from fnmatch import fnmatch
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
from ..metricas import medir
from ..opencode import executar_opencode
from ..resiliencia import com_retry
from ..sandbox import executar_testes
from .state import EstadoProjeto

MAX_TENTATIVAS = 3
MAX_REPLANEJAMENTOS = 2
MAX_TESTES = 2                # reescritas da suíte por rodada de desenvolvimento
MAX_REVISOES = 2              # rodadas que a opinião do revisor pode custar
LIMITE_DUMP_CODIGO = 15_000   # chars de código injetados na tarefa do revisor
LIMITE_AMOSTRA_TESTES = 20_000  # chars da suíte mostrados ao guard de critérios
LIMITE_REVISAO_ANTERIOR = 4_000  # chars do veredito anterior devolvidos ao revisor


def _piso(variavel: str, padrao: float) -> float:
    try:
        return float(os.getenv(variavel, str(padrao)))
    except ValueError:
        return padrao


def _cobertura_minima() -> float:
    return _piso("COBERTURA_MINIMA", 70.0)


def _cobertura_minima_modulo() -> float:
    """Piso por módulo: o agregado sozinho deixa passar suíte que testa muito
    o que é fácil e ignora o arquivo central do pedido."""
    return _piso("COBERTURA_MINIMA_MODULO", 60.0)


def _tid(config: RunnableConfig) -> str:
    return str(config["configurable"]["thread_id"])


def _veredito_sim(resposta: object) -> bool:
    """Parser tolerante de SIM/NAO (RESILIENCIA.md, item 7): modelos enfeitam
    a resposta, e o caso indecifrável conta como reprova."""
    normalizado = str(resposta).strip().upper()
    return normalizado.startswith("SIM") or (
        "SIM" in normalizado[:20]
        and "NAO" not in normalizado[:20]
        and "NÃO" not in normalizado[:20]
    )


# Linhas finais examinadas em busca do veredito da revisão.
LINHAS_VEREDITO = 8


def _veredito_aprovado(texto: str) -> bool:
    """Procura APROVADO/REPROVADO nas últimas linhas, não só na última.

    Exigir o token na última linha é acoplar roteamento ao formato exato da
    saída (o que o item 7 do RESILIENCIA.md proíbe): um revisor que escreve
    "**APROVADO**" e fecha com um parágrafo de conclusão tinha a aprovação
    lida como reprova — custando uma rodada inteira do laço.

    Vale o **último** veredito encontrado, porque o texto discute
    apontamentos antes de concluir. Nada reconhecível conta como reprova.
    """
    if not texto:
        return False
    linhas = [ln for ln in texto.upper().splitlines() if ln.strip()]
    for linha in reversed(linhas[-LINHAS_VEREDITO:]):
        # REPROVADO primeiro: "APROVADO" não é substring dele, mas a ordem
        # deixa a precedência explícita para quem lê.
        if "REPROVADO" in linha:
            return False
        if "APROVADO" in linha:
            return True
    return False


# ---------- helpers de workspace ----------

# Diretórios de trabalho (caches de ferramentas e instruções da squad ao
# executor) que não fazem parte da entrega.
_IGNORAR_NO_WORKSPACE = {"__pycache__", ".pytest_cache", ".ruff_cache", ".squad", ".git"}

# Rastro que o executor deixa ao rodar comandos (saída de pytest, log de
# instalação): não é entrega, polui o dump enviado ao revisor e iria parar no
# deploy. Heurística por nome — conservadora de propósito, para não descartar
# arquivo legítimo.
_ARQUIVOS_TRANSITORIOS = ("*.log", "*_output.txt", "*_result.txt", "*_results.txt")


def _e_transitorio(rel: str) -> bool:
    nome = rel.rsplit("/", 1)[-1]
    return any(fnmatch(nome, padrao) for padrao in _ARQUIVOS_TRANSITORIOS)


def _arquivos_do_workspace(workspace: str) -> list[str]:
    raiz = Path(workspace)
    return sorted(
        rel
        for rel in (
            str(p.relative_to(raiz)).replace("\\", "/")
            for p in raiz.rglob("*")
            if p.is_file()
            and not _IGNORAR_NO_WORKSPACE.intersection(p.parts)
            and p.suffix != ".pyc"
        )
        if not _e_transitorio(rel)
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


def _amostra_testes(workspace: str, arquivos: list[str]) -> str:
    """Amostra da suíte para o guard de critérios, com **todos** os arquivos
    representados.

    Truncar a suíte no total (os primeiros N chars) faz o guard julgar por
    amostra sem saber: arquivos no fim da ordem alfabética ficam invisíveis, e
    ele reprova por não ver testes que existem. Aqui o orçamento é dividido
    entre os arquivos e o manifesto lista a suíte inteira.
    """
    testes = [a for a in arquivos if a.startswith("tests/")]
    if not testes:
        return ""

    manifesto = "\n".join(f"- {a}" for a in testes)
    cabecalho = f"Arquivos de teste na suíte ({len(testes)}):\n{manifesto}\n\n"
    por_arquivo = max((LIMITE_AMOSTRA_TESTES - len(cabecalho)) // len(testes), 500)

    partes = []
    for rel in testes:
        conteudo = (Path(workspace) / rel).read_text(encoding="utf-8", errors="replace")
        if len(conteudo) > por_arquivo:
            conteudo = f"{conteudo[:por_arquivo]}\n[... {rel} truncado aqui ...]"
        partes.append(f"### {rel}\n{conteudo}")
    return cabecalho + "\n".join(partes)


# ---------- nós determinísticos ----------

def no_triagem(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    """Valida a entrada, cria o workspace da execução e zera contadores."""
    thread_id = _tid(config)
    with medir(thread_id, "triagem"):
        pedido = (state.get("pedido") or "").strip()
        if not pedido:
            raise ValueError("Pedido vazio — nada a fazer.")
        workspace = Path("workspace") / thread_id
        workspace.mkdir(parents=True, exist_ok=True)
        print(f">>> Workspace desta execução: {workspace.resolve()}")
    return {
        "pedido": pedido,
        "tentativas": 0,
        "testes_tentativas": 0,
        "revisao_tentativas": 0,
        "origem_feedback": "",
        "workspace": str(workspace.resolve()),
    }


def no_executar_testes(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    """Veredito por execução: roda a suíte na jaula (sandbox Docker por
    padrão) e traduz o resultado em estado. Sem LLM."""
    thread_id = _tid(config)
    with medir(thread_id, "executar_testes", runner=os.getenv("TEST_RUNNER", "docker")) as m:
        resultado = executar_testes(state["workspace"], thread_id)
        m.update(
            testes_ok=resultado["testes_ok"],
            cobertura=resultado["cobertura"],
            cobertura_pior=resultado["cobertura_pior"],
            cobertura_pior_arquivo=resultado["cobertura_pior_arquivo"],
        )

    saida = resultado["saida_testes"]
    total, pior = resultado["cobertura"], resultado["cobertura_pior"]
    pior_arquivo = resultado["cobertura_pior_arquivo"]
    # Dois pisos: o agregado pega suíte fraca no geral; o por módulo pega a
    # suíte que testa muito o que é fácil e ignora o arquivo central.
    agregado_ok = total >= _cobertura_minima()
    modulo_ok = pior >= _cobertura_minima_modulo()
    cobertura_ok = agregado_ok and modulo_ok

    if not resultado["testes_ok"]:
        feedback = (
            "Os testes automatizados FALHARAM. Corrija o código (ou os "
            f"imports/estrutura) com base na saída real do pytest:\n{saida}"
        )
    elif not agregado_ok:
        # Problema do teste, não do código: quem reescreve é o QA.
        feedback = (
            f"Os testes passam, mas cobrem apenas {total}% da entrega (mínimo "
            f"{_cobertura_minima()}%). Escreva testes que exercitem os módulos "
            "e os caminhos ainda não cobertos."
        )
    elif not modulo_ok:
        print(
            f">>> Cobertura agregada {total}% ok, mas {pior_arquivo} está em "
            f"{pior}% (mínimo por módulo: {_cobertura_minima_modulo()}%)."
        )
        feedback = (
            f"A cobertura total ({total}%) esconde um módulo sem teste: "
            f"{pior_arquivo} está em apenas {pior}% (mínimo por módulo "
            f"{_cobertura_minima_modulo()}%). Escreva testes que exercitem "
            f"{pior_arquivo} de ponta a ponta, pelo comportamento observável "
            "que a spec exige — não apenas as funções auxiliares."
        )
    else:
        feedback = ""
    return {
        **resultado,
        "cobertura_ok": cobertura_ok,
        "feedback_qa": feedback,
        # Rodada movida por execução: se voltar ao desenvolvimento, a suíte é
        # reescrita (o veredito veio dela).
        "origem_feedback": "testes" if feedback else "",
    }


def no_aprovacao_humana(state: EstadoProjeto) -> EstadoProjeto:
    """Gate human-in-the-loop: pausa a execução até um humano decidir."""
    resposta = interrupt(
        {
            "mensagem": "Autorizar deploy?",
            "testes_ok": state.get("testes_ok", False),
            "cobertura": state.get("cobertura", 0.0),
            "testes_aderentes": state.get("testes_aderentes", False),
            "relatorio_qa": state.get("relatorio_qa", ""),
        }
    )
    if str(resposta).strip().lower() not in {"sim", "s", "yes", "aprovar"}:
        raise RuntimeError("Deploy negado pelo aprovador humano.")
    return {}


def no_deploy(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    """Deploy real (Fase 4): git commit + push da entrega, sem LLM. Só roda
    após aprovação humana explícita no gate."""
    thread_id = _tid(config)
    with medir(thread_id, "deploy") as m:
        resultado = executar_deploy(state["workspace"], thread_id, state["pedido"])
        m.update(deploy_ref=resultado.get("deploy_ref", ""))
    return resultado


# ---------- nós que invocam crews ----------

def no_planejamento(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    with medir(_tid(config), "planejamento"):
        resultado = com_retry(
            "planejamento",
            lambda: crew_planejamento().kickoff(inputs={"pedido": state["pedido"]}),
            caro=True,
        )
    return {
        "spec": resultado.raw,
        "spec_tentativas": state.get("spec_tentativas", 0) + 1,
    }


def no_validacao_spec(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    """Guard de aderência: uma chamada única e barata que confere se a spec
    produzida trata mesmo do pedido, antes de gastar tokens com desenvolvimento.
    Protege contra alucinação da crew de planejamento (spec de outro tema)."""
    with medir(_tid(config), "validacao_spec") as m:
        veredito = com_retry("guard de aderência", lambda: zen_llm().call(
            "Você é um verificador rigoroso. Responda APENAS com a palavra SIM ou "
            f'NAO. A especificação técnica abaixo trata do pedido "{state["pedido"]}"'
            " — mesmo assunto e mesmo escopo, sem substituí-lo por outro tema?\n\n"
            f"Especificação:\n{state['spec'][:8000]}"
        ))
        coerente = _veredito_sim(veredito)
        m.update(spec_coerente=coerente)
    if not coerente:
        print(f">>> Guard: spec reprovada (não adere ao pedido). Veredito: {str(veredito)[:40]}")
    return {"spec_coerente": coerente}


def no_desenvolvimento(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    """Escreve a implementação no workspace. O executor é intercambiável
    (DEV_EXECUTOR): o OpenCode CLI como mão de obra especialista, ou as crews
    CrewAI como caminho sem dependência externa. A governança do grafo é a
    mesma nos dois casos."""
    executor = os.getenv("DEV_EXECUTOR", "opencode").strip().lower()
    feedback = state.get("feedback_qa", "")
    origem = state.get("origem_feedback") or "inicial"
    with medir(_tid(config), "desenvolvimento", executor=executor, origem=origem):
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
        # Código novo, suíte nova: o laço de testes recomeça do zero.
        "testes_tentativas": 0,
    }


def no_escrever_testes(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    with medir(_tid(config), "escrever_testes"):
        com_retry("escrita de testes", lambda: crew_testes(state["workspace"]).kickoff(
            inputs={
                "spec": state["spec"],
                "arquivos": "\n".join(state.get("arquivos", [])) or "(workspace vazio)",
                "feedback_qa": state.get("feedback_qa", "") or "Nenhum — primeira rodada.",
            }
        ), caro=True)
    # Revarre o workspace: os testes agora fazem parte da entrega e entram
    # no dump que o revisor recebe.
    arquivos = _arquivos_do_workspace(state["workspace"])
    return {
        "arquivos": arquivos,
        "codigo": _dump_codigo(state["workspace"], arquivos),
        "testes_tentativas": state.get("testes_tentativas", 0) + 1,
    }


def no_validacao_testes(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    """Guard de critérios: testes verdes não provam correção se a própria
    suíte ignora os critérios de aceite. Espelha o guard de aderência —
    uma chamada barata antes de executar, porque reprovar aqui custa
    centavos e uma rodada inteira do laço custa a execução."""
    testes = _amostra_testes(state["workspace"], state.get("arquivos", []))
    if not testes.strip():
        print(">>> Guard de critérios: nenhum arquivo de teste encontrado.")
        return {"testes_aderentes": False}

    with medir(_tid(config), "validacao_testes") as m:
        veredito = com_retry("guard de critérios", lambda: zen_llm().call(
            "Você é um verificador rigoroso de testes. Responda APENAS com a "
            "palavra SIM ou NAO. Os testes abaixo verificam de fato os "
            "critérios de aceite da especificação — cobrindo o comportamento "
            "exigido, e não apenas asserções triviais ou detalhes irrelevantes?"
            f"\n\nEspecificação:\n{state['spec'][:6000]}"
            f"\n\nTestes:\n{testes}"
        ))
        aderentes = _veredito_sim(veredito)
        m.update(testes_aderentes=aderentes)

    if not aderentes:
        print(">>> Guard de critérios: testes não cobrem os critérios de aceite.")
        return {
            "testes_aderentes": False,
            "feedback_qa": (
                "Os testes escritos não verificam os critérios de aceite da "
                "spec. Reescreva-os cobrindo o comportamento exigido, com "
                "asserções sobre resultados reais — não asserções triviais."
            ),
        }
    return {"testes_aderentes": True, "feedback_qa": ""}


def no_revisao(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    """Revisor LLM: só roda com testes verdes (não paga revisão de código que
    nem passa). Cobre o que execução não pega.

    Recebe o próprio veredito da rodada anterior. Sem essa memória o revisor
    julga cada rodada do zero e manda desfazer o que ele mesmo exigiu antes —
    o código oscila entre dois pólos e cada volta custa uma rodada inteira
    (RESILIENCIA.md, item 27).
    """
    # Ainda é o relatório da rodada anterior: este nó só o sobrescreve ao retornar.
    anterior = (state.get("relatorio_qa") or "")[:LIMITE_REVISAO_ANTERIOR]
    with medir(_tid(config), "revisao") as m:
        resultado = com_retry("revisão", lambda: crew_revisao().kickoff(
            inputs={
                "codigo": state["codigo"],
                "spec": state["spec"],
                "saida_testes": state.get("saida_testes", ""),
                "revisao_anterior": anterior or "Nenhuma — primeira revisão.",
            }
        ), caro=True)
        texto = resultado.raw
        aprovado = _veredito_aprovado(texto)
        m.update(aprovado=aprovado)
    return {
        "relatorio_qa": texto,
        "revisao_anterior": anterior,
        "aprovado": aprovado,
        "revisao_tentativas": state.get("revisao_tentativas", 0) + (0 if aprovado else 1),
        "origem_feedback": "" if aprovado else "revisao",
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


def rota_pos_desenvolvimento(state: EstadoProjeto) -> str:
    """Roteamento seletivo: a rodada nascida de reprovação de revisão não
    reescreve a suíte.

    O código mudou, então os testes precisam *rodar* de novo — não ser
    *escritos* de novo. `escrever_testes` é o nó mais caro do grafo (8 min
    medidos), e repagá-lo por um apontamento de legibilidade foi metade do
    custo da execução que motivou esta mudança. Ir direto ao pytest também
    pula o guard de critérios, que julgaria uma suíte inalterada.

    As redes já existentes cobrem o risco: se a correção quebrar a suíte, o
    pytest fica vermelho e a rodada volta ao desenvolvimento com o stack
    trace; se adicionar código sem teste, o piso de cobertura devolve ao QA.
    """
    tem_suite = any(a.startswith("tests/") for a in state.get("arquivos", []))
    if state.get("origem_feedback") == "revisao" and tem_suite:
        print(">>> Correção de revisão: suíte preservada, indo direto ao pytest.")
        return "executar_testes"
    return "escrever_testes"


def rota_pos_validacao_testes(state: EstadoProjeto) -> str:
    """Suíte que não adere aos critérios volta para o QA reescrever. Ao
    estourar o teto, segue mesmo assim: qualidade de teste é sinal mais
    brando que teste vermelho, e fica registrado no estado para o gate
    humano e as métricas."""
    if state.get("testes_aderentes"):
        return "executar_testes"
    if state.get("testes_tentativas", 0) >= MAX_TESTES:
        print(">>> Guard de critérios: teto de reescritas atingido, seguindo assim mesmo.")
        return "executar_testes"
    return "escrever_testes"


def rota_pos_testes(state: EstadoProjeto) -> str:
    if not state.get("testes_ok"):
        if state["tentativas"] >= MAX_TENTATIVAS:
            # Circuit breaker: humano decide o que fazer com o trabalho reprovado.
            return "aprovacao_humana"
        return "desenvolvimento"
    # Verdes, mas sem exercitar a entrega: problema do teste — laço curto,
    # só o QA reescreve, sem pagar outra rodada de desenvolvimento.
    if not state.get("cobertura_ok") and state.get("testes_tentativas", 0) < MAX_TESTES:
        return "escrever_testes"
    return "revisao"


def rota_pos_revisao(state: EstadoProjeto) -> str:
    """Teto próprio para a opinião do revisor.

    `tentativas` é orçamento compartilhado com falha de teste. Sem um teto
    separado, o sinal caro e subjetivo (revisão) consome sozinho as rodadas
    reservadas ao sinal barato e determinístico (pytest vermelho). Ao estourar,
    o gate humano decide — com o relatório em mãos.
    """
    if state.get("aprovado"):
        return "aprovacao_humana"
    if state.get("revisao_tentativas", 0) >= MAX_REVISOES:
        print(
            f">>> Revisão reprovou {MAX_REVISOES}x: teto de rodadas por opinião "
            "atingido, levando ao gate humano."
        )
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
    g.add_node("validacao_testes", no_validacao_testes)
    g.add_node("executar_testes", no_executar_testes)
    g.add_node("revisao", no_revisao)
    g.add_node("aprovacao_humana", no_aprovacao_humana)
    g.add_node("deploy", no_deploy)

    g.add_edge(START, "triagem")
    g.add_edge("triagem", "planejamento")
    g.add_edge("planejamento", "validacao_spec")
    g.add_conditional_edges("validacao_spec", rota_pos_validacao_spec)
    g.add_conditional_edges("desenvolvimento", rota_pos_desenvolvimento)
    g.add_edge("escrever_testes", "validacao_testes")
    g.add_conditional_edges("validacao_testes", rota_pos_validacao_testes)
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
