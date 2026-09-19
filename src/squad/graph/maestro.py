"""O maestro: o grafo da execução inteira, com N serviços em paralelo.

A divisão com o ramo é a mesma que vale entre grafo e crew — decisão cara em
cima, trabalho especialista embaixo. Aqui ficam as três coisas que são da
execução e não de um serviço:

- **a decomposição**, que decide quantos ramos existem;
- **o contrato**, que é o que torna o paralelismo possível (ver `no_contratos`);
- **o gate humano e os deploys**, que precisam ver os N serviços de uma vez.

O ramo em si é `workflow.construir_subgrafo_servico()`, o pipeline de sempre,
instanciado uma vez por serviço. Ele não sabe que há outros.

Medido antes de ser desenhado: nós síncronos do LangGraph rodam mesmo em
paralelo (4 de 1,5s em 1,52s), um ramo que levanta não derruba o trabalho dos
outros, e o checkpoint entra no subgrafo — um ramo que cai na revisão retoma da
revisão, não da triagem.
"""
import functools
import operator
from typing import Annotated, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from ..adaptadores import perfis
from ..adaptadores.contratos import medir_integracao
from ..adaptadores.metricas_json import medir, registrar
from ..crews.decomposicao import crew_contratos, crew_decomposicao
from ..deploy import executar_deploy
from ..dominio import decomposicao as dec
from ..dominio import rotas
from ..resiliencia import com_retry
from ..adaptadores.testes import verificar_compilacao
from . import workflow
from .state import EstadoProjeto


class EstadoMaestro(TypedDict, total=False):
    """Estado da execução inteira.

    Os campos que os N ramos escrevem levam reducer: sem ele, dois ramos
    concluindo no mesmo passo fazem o LangGraph recusar a escrita com "can
    receive only one value per step" — e a execução morre por concorrência, não
    por mérito de entrega nenhuma.
    """
    pedido: str
    stack: str                  # stack sugerida; cada serviço pode ter a sua
    thread_id: str
    servicos: list[dict]        # decomposição normalizada
    servicos_tentativas: int
    erros_decomposicao: str
    contratos: str
    entregas: Annotated[list[dict], operator.add]   # um resumo por ramo
    integracao_ok: bool             # o contrato foi cumprido pelos N serviços?
    achados_integracao: list[dict]  # desencontros de rota e de campo
    deploys: Annotated[list[dict], operator.add]    # um por serviço publicado


def _tid(config: RunnableConfig) -> str:
    return str(config["configurable"]["thread_id"])


def _servicos(state: EstadoMaestro) -> list[dec.Servico]:
    return [dec.Servico(**s) for s in state.get("servicos", [])]


# ---------- decomposição e contrato ----------

def no_decomposicao(state: EstadoMaestro, config: RunnableConfig) -> EstadoMaestro:
    """Quebra o pedido em serviços — a decisão que abre o paralelismo.

    A saída do modelo passa por um parser tolerante e por validação de domínio
    antes de virar ramo. Não é zelo: um nome repetido faz dois ramos escreverem
    no mesmo workspace, e um ciclo de dependências torna o contrato impossível
    de congelar. Os dois só apareceriam no meio da execução.
    """
    thread_id = _tid(config)
    stacks = frozenset(perfis.nomes())
    with medir(thread_id, "decomposicao") as m:
        resultado = com_retry("decomposição", lambda: crew_decomposicao().kickoff(
            inputs={
                "pedido": state["pedido"],
                "stack": state.get("stack") or perfis.PERFIL_PADRAO,
                "stacks_disponiveis": ", ".join(sorted(stacks)),
                "max_servicos": dec.MAX_SERVICOS,
            }
        ), caro=True)
        bruto = dec.extrair_lista(resultado.raw)
        if bruto is None:
            servicos, erros = [], ["a resposta da decomposição não trouxe JSON legível"]
        else:
            servicos, erros = dec.normalizar(bruto, stacks)
        m.update(servicos=len(servicos), erros=len(erros))

    if erros:
        print(">>> Decomposição reprovada:")
        for e in erros:
            print(f"    - {e}")
    else:
        print(f">>> Decomposição: {len(servicos)} serviço(s)")
        print(dec.resumo(servicos))

    return {
        "servicos": [vars(s) | {"depende_de": tuple(s.depende_de)} for s in servicos],
        "erros_decomposicao": "; ".join(erros),
        "servicos_tentativas": state.get("servicos_tentativas", 0) + 1,
    }


def no_contratos(state: EstadoMaestro, config: RunnableConfig) -> EstadoMaestro:
    """Congela o contrato entre os serviços, antes de qualquer um ser construído.

    É o que torna o paralelismo possível, e não um passo de documentação. No
    livro, `event-service` usa `roomModel.totalSeats()`. Se três times planejarem
    sem acordo, `room-service` devolve `capacity`, `event-service` espera
    `totalSeats`, e os dois passam nos próprios testes — três suítes verdes e a
    integração quebrada. É uma classe de defeito que o pipeline de um serviço só
    não tinha porque não havia com quem se desencontrar.

    Com um serviço só, não há contrato a redigir: pagar a chamada aqui seria
    cobrar de toda entrega única o preço de um problema que ela não tem.
    """
    servicos = _servicos(state)
    if len(servicos) < 2:
        print(">>> Contrato: um serviço só, nada a acordar.")
        return {"contratos": ""}

    thread_id = _tid(config)
    with medir(thread_id, "contratos") as m:
        resultado = com_retry("contrato", lambda: crew_contratos().kickoff(
            inputs={"pedido": state["pedido"], "servicos": dec.resumo(servicos)}
        ), caro=True)
        texto = resultado.raw
        m.update(chars=len(texto))
    print(f">>> Contrato congelado ({len(texto)} chars) — entra em todos os ramos.")
    return {"contratos": texto}


# ---------- fan-out ----------

def fan_out_servicos(state: EstadoMaestro) -> list[Send]:
    """Um `Send` por serviço: é aqui que a squad deixa de ser sequencial.

    Cada ramo recebe um estado próprio — pedido focado, stack, nome do serviço e
    o contrato congelado — e nada mais. Um ramo não enxerga o outro, o que é a
    razão de o contrato precisar estar decidido antes deste ponto.
    """
    servicos = _servicos(state)
    return [
        Send("ramo", {
            "pedido": dec.pedido_do_servico(
                state["pedido"], s, state.get("contratos", "")
            ),
            "stack": s.stack,
            "servico": s.nome,
            "contratos": state.get("contratos", ""),
        })
        for s in servicos
    ]


def no_ramo(estado: EstadoProjeto, config: RunnableConfig, *, ramo) -> EstadoMaestro:
    """Envelope do subgrafo: roda o pipeline de um serviço e devolve o resumo.

    Envelope e não subgrafo ligado direto ao maestro porque os N ramos escrevem
    as mesmas chaves (`testes_ok`, `cobertura`, `aprovado`...). Ligados direto,
    dois ramos concluindo no mesmo passo disputariam cada uma delas. Aqui o
    estado do ramo fica contido, e o que sobe para o maestro é uma linha em
    `entregas`.

    Medido: invocar o subgrafo com o `config` do pai preserva o checkpoint por
    nó interno — o ramo que cair na revisão retoma da revisão.

    O subgrafo chega pronto, de `construir_maestro`: compilá-lo aqui o
    reconstruiria uma vez por serviço, e o custo cresceria com a largura do
    fan-out justamente onde ela deveria ser de graça.
    """
    final = ramo.invoke(estado, config=config)
    return {"entregas": [{
        "servico": estado["servico"],
        "stack": estado["stack"],
        "pronto": bool(final.get("pronto")),
        "workspace": final.get("workspace", ""),
        "thread_id": final.get("thread_id", ""),
        "pedido": estado["pedido"],
        "testes_ok": bool(final.get("testes_ok")),
        "cobertura": final.get("cobertura", 0.0),
        "aprovado": bool(final.get("aprovado")),
        "ambientes_ok": bool(final.get("ambientes_ok", True)),
        "pentest_ok": bool(final.get("pentest_ok", True)),
        "visual_ok": bool(final.get("visual_ok", True)),
    }]}


def no_integracao(state: EstadoMaestro, config: RunnableConfig) -> EstadoMaestro:
    """O contrato foi cumprido? A pergunta que só existe com N serviços.

    É o único juiz do pipeline que olha para MAIS DE UMA entrega. Todos os
    outros julgam um serviço isolado, e por isso nenhum deles vê a classe de
    defeito que o paralelismo introduz: `room-service` devolve `capacity`,
    `event-service` espera `totalSeats`, os dois passam nos próprios testes, e
    o sistema não funciona.

    O que ele faz com um desencontro, e o que **não** faz: um serviço reprovado
    aqui deixa de ser publicável e aparece no placar com o motivo. Não há laço
    de correção automática — o ramo já terminou, e reabri-lo exigiria máquina
    que não existe. Preferir isso a inventá-la é deliberado: reter e explicar é
    honesto; simular uma correção que não acontece, não.
    """
    with medir(_tid(config), "integracao") as m:
        resultado = medir_integracao(
            state.get("contratos", ""), state.get("entregas") or [], perfis.obter
        )
        m.update(
            integracao_ok=resultado["integracao_ok"],
            simbolos=resultado["simbolos"],
            achados=len(resultado["achados_integracao"]),
        )
    return {
        "integracao_ok": resultado["integracao_ok"],
        "achados_integracao": resultado["achados_integracao"],
    }


# ---------- gate único ----------

def _publicaveis(state: EstadoMaestro) -> tuple[list[dict], list[dict]]:
    """Separa o que vai ser publicado do que fica retido.

    Um serviço que convergiu mas desencontrou o contrato NÃO é publicável:
    publicá-lo poria no ar um serviço que os outros não conseguem chamar.
    """
    em_falta = {a["servico"] for a in (state.get("achados_integracao") or [])}
    prontos, retidos = [], []
    for e in state.get("entregas") or []:
        (prontos if e["pronto"] and e["servico"] not in em_falta else retidos).append(e)
    return prontos, retidos


def _placar(entregas: list[dict], em_falta: frozenset = frozenset()) -> str:
    linhas = []
    for e in sorted(entregas, key=lambda x: x["servico"]):
        integra = e["servico"] not in em_falta
        marca = "PRONTO" if e["pronto"] and integra else "RETIDO"
        linhas.append(
            f"  {marca}  {e['servico']:<22} [{e['stack']}] "
            f"testes={'ok' if e['testes_ok'] else 'X'} "
            f"cob={e['cobertura']}% "
            f"revisao={'ok' if e['aprovado'] else 'X'} "
            f"ambientes={'ok' if e['ambientes_ok'] else 'X'} "
            f"contrato={'ok' if integra else 'X'}"
        )
    return "\n".join(linhas)


def no_gate(state: EstadoMaestro, config: RunnableConfig) -> EstadoMaestro:
    """Gate humano único, com o placar dos N serviços.

    Um, e não um por ramo: com N gates o humano aprovaria `room-service` às
    cegas e só depois descobriria que `event-service` reprovou — e teria de
    decidir de novo. Aqui a decisão é tomada uma vez, sabendo de tudo.
    """
    entregas = state.get("entregas") or []
    em_falta = frozenset(
        a["servico"] for a in (state.get("achados_integracao") or [])
    )
    lista_prontos, lista_retidos = _publicaveis(state)
    prontos = [e["servico"] for e in lista_prontos]
    retidos = [e["servico"] for e in lista_retidos]

    print("\n" + "=" * 62)
    print("PLACAR DA EXECUÇÃO")
    print("=" * 62)
    print(_placar(entregas, em_falta))
    print("=" * 62)
    if retidos:
        print(f"Serão publicados apenas os {len(prontos)} serviço(s) pronto(s).")
        print(f"Retidos (ficam no checkpoint): {', '.join(retidos)}")

    resposta = interrupt({
        "mensagem": f"Autorizar deploy de {len(prontos)} serviço(s)?",
        "prontos": prontos,
        "retidos": retidos,
        "placar": _placar(entregas, em_falta),
        "achados_integracao": state.get("achados_integracao") or [],
    })
    if str(resposta).strip().lower() not in {"sim", "s", "yes", "aprovar"}:
        raise workflow.DeployNegado("Deploy negado pelo aprovador humano.")
    return {}


def fan_out_deploy(state: EstadoMaestro):
    """Publica os verdes, retém o vermelho.

    Um `Send` por serviço pronto. Nenhum pronto não é erro: é uma execução em
    que nada convergiu, e o trabalho fica no checkpoint para a retomada.
    """
    prontos, _ = _publicaveis(state)
    if not prontos:
        print(">>> Nenhum serviço pronto — nada a publicar.")
        return END
    return [Send("deploy_servico", e) for e in prontos]


def no_deploy_servico(entrega: dict) -> EstadoMaestro:
    """Deploy de um serviço: um repositório por serviço, e só o que compila.

    A verificação de compilação roda aqui, contra o disco, e não é redundante
    com o build da suíte: um teto de circuit breaker leva ao gate com a suíte
    vermelha de propósito, e um `sim` ali publicaria o que não compila.
    """
    servico = entrega["servico"]
    perfil = perfis.obter(entrega["stack"])
    thread_id = entrega["thread_id"]

    with medir(thread_id, "verificacao_deploy", stack=perfil.nome) as m:
        falha = verificar_compilacao(entrega["workspace"], thread_id, perfil)
        m.update(compila=not falha)
    if falha:
        raise RuntimeError(
            f"O serviço '{servico}' NÃO COMPILA — nada dele foi publicado. A "
            "squad só empurra a branch quando o código compila no ambiente da "
            "própria stack, e aprovar o gate não dispensa essa prova.\n"
            f"Saída do compilador:\n{falha}\n"
            "O checkpoint preserva o progresso dos demais serviços: corrija e "
            "retome com `main.py --thread <id>`."
        )

    with medir(thread_id, "deploy", servico=servico) as m:
        resultado = executar_deploy(
            entrega["workspace"], thread_id, entrega["pedido"], perfil, nome=servico
        )
        m.update(deploy_ref=resultado.get("deploy_ref", ""))
    return {"deploys": [{"servico": servico, **resultado}]}


# ---------- roteamento ----------

def rota_pos_decomposicao(state: EstadoMaestro) -> str:
    decisao = rotas.pos_decomposicao(state)
    if decisao.teto:
        registrar(
            state.get("thread_id", ""), "teto_atingido",
            laco=decisao.teto.laco, limite=decisao.teto.limite,
        )
    if decisao.aviso:
        print(decisao.aviso)
    if decisao.erro:
        raise RuntimeError(decisao.erro)
    return decisao.destino


# ---------- montagem ----------

def construir_maestro():
    g = StateGraph(EstadoMaestro)

    g.add_node("decomposicao", no_decomposicao)
    g.add_node("contratos", no_contratos)
    # Compilado uma vez e compartilhado pelos N ramos: cada um recebe estado
    # próprio na invocação, então não há o que separar entre eles.
    g.add_node("ramo", functools.partial(
        no_ramo, ramo=workflow.construir_subgrafo_servico()
    ))
    g.add_node("integracao", no_integracao)
    g.add_node("gate", no_gate)
    g.add_node("deploy_servico", no_deploy_servico)

    g.add_edge(START, "decomposicao")
    g.add_conditional_edges("decomposicao", rota_pos_decomposicao)
    # O fan-out sai do contrato, não da decomposição: o contrato precisa estar
    # pronto antes de qualquer ramo começar.
    g.add_conditional_edges("contratos", fan_out_servicos, ["ramo"])
    # Aresta simples: `integracao` só roda quando TODOS os ramos terminam — é
    # o único juiz que precisa de mais de uma entrega para existir.
    g.add_edge("ramo", "integracao")
    g.add_edge("integracao", "gate")
    g.add_conditional_edges("gate", fan_out_deploy, ["deploy_servico", END])
    g.add_edge("deploy_servico", END)

    return g.compile(checkpointer=workflow._checkpointer())
