"""Grafo LangGraph: espinha determinística que orquestra as crews.

Regra de divisão:
- decisões caras (avançar, repetir, parar, chamar humano) ficam AQUI, no grafo;
- trabalho especialista (escrever código, escrever testes, revisar) fica nas
  crews CrewAI;
- vereditos vêm de execução, não de opinião: o laço de correções é roteado
  pelo exit code do pytest e pela cobertura (nós determinísticos), e o revisor
  LLM cobre apenas o que execução não pega.
"""
import hashlib
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
from ..dominio import guards, orcamento, rotas
from ..dominio.vereditos import veredito_aprovado, veredito_sim
from ..llm import zen_llm
from ..adaptadores import perfis
from ..adaptadores.metricas_json import medir, registrar
from ..adaptadores.opencode_cli import executar_opencode
from ..pentest import executar_pentest, habilitado as pentest_habilitado
from ..resiliencia import com_retry
from ..adaptadores.testes import LIMITE_SAIDA, executar_testes
from ..visual import executar_visual, habilitado as visual_habilitado
from .state import EstadoProjeto

# Os tetos de rodada moram no domínio, junto das decisões que os aplicam.
# Reexportados aqui porque nós e mensagens ainda os citam.
MAX_TENTATIVAS = rotas.MAX_TENTATIVAS
MAX_REPLANEJAMENTOS = rotas.MAX_REPLANEJAMENTOS
MAX_TESTES = rotas.MAX_TESTES
MAX_REVISOES = rotas.MAX_REVISOES
MAX_PENTEST = rotas.MAX_PENTEST
MAX_VISUAL = rotas.MAX_VISUAL

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


def _perfil(state: EstadoProjeto):
    """A stack desta execução, vinda do estado.

    Do estado e não do ambiente: a thread atravessa vários processos, e uma
    retomada depois de alguém editar o `.env` rodaria com a stack errada — o
    mesmo motivo pelo qual o `thread_id` também mora aqui.
    """
    return perfis.obter(state.get("stack"))


# ---------- helpers de workspace ----------

# Rastro que o executor deixa ao rodar comandos (saída de pytest, log de
# instalação): não é entrega, polui o dump enviado ao revisor e iria parar no
# deploy. Heurística por nome — conservadora de propósito, para não descartar
# arquivo legítimo.
_ARQUIVOS_TRANSITORIOS = ("*.log", "*_output.txt", "*_result.txt", "*_results.txt")


def _e_transitorio(rel: str) -> bool:
    nome = rel.rsplit("/", 1)[-1]
    return any(fnmatch(nome, padrao) for padrao in _ARQUIVOS_TRANSITORIOS)


def _arquivos_do_workspace(workspace: str, perfil) -> list[str]:
    raiz = Path(workspace)
    return sorted(
        rel
        for rel in (
            str(p.relative_to(raiz)).replace("\\", "/")
            for p in raiz.rglob("*")
            if p.is_file()
            and not perfil.ignorar_no_workspace.intersection(p.parts)
            and p.suffix not in perfil.extensoes_descartaveis
        )
        if not _e_transitorio(rel)
    )


def _tem_conteudo(workspace: str, rel: str) -> bool:
    """Arquivo com algo dentro — 0 byte ou só espaço em branco não conta."""
    caminho = Path(workspace) / rel
    try:
        if caminho.stat().st_size == 0:
            return False
        return bool(caminho.read_text(encoding="utf-8", errors="replace").strip())
    except OSError:
        return False


def _conferir_entrega(arquivos: list[str], workspace: str, perfil) -> None:
    """Lê o disco, monta o manifesto e deixa o domínio julgar."""
    manifesto = {a: _tem_conteudo(workspace, a) for a in arquivos}
    falha = guards.conferir_entrega(manifesto, workspace, perfil.e_teste)
    if falha:
        raise RuntimeError(falha)


def _impressao_entrega(workspace: str, perfil) -> dict[str, str]:
    """Hash do conteúdo de cada arquivo da entrega, para comparar rodadas.

    Só a entrega: `tests/` é do QA e o executor é proibido de tocar, então
    mudança lá não prova que a correção pedida foi feita. Compara conteúdo e
    não mtime — executor que reescreve o arquivo idêntico não corrigiu nada.
    """
    raiz = Path(workspace)
    impressao: dict[str, str] = {}
    for rel in _arquivos_do_workspace(workspace, perfil):
        if perfil.e_teste(rel):
            continue
        try:
            impressao[rel] = hashlib.sha256((raiz / rel).read_bytes()).hexdigest()
        except OSError:
            continue
    return impressao


def _conferir_correcao(antes: dict[str, str], depois: dict[str, str], origem: str) -> None:
    falha = guards.conferir_correcao(antes, depois, origem)
    if falha:
        raise RuntimeError(falha)


def _blocos_com_orcamento(
    workspace: str, arquivos: list[str], limite: int, rotulo: str
) -> str:
    """Lê os arquivos e entrega ao domínio, que reparte o orçamento."""
    if not arquivos:
        return ""
    conteudos = {
        rel: (Path(workspace) / rel).read_text(encoding="utf-8", errors="replace")
        for rel in arquivos
    }
    return orcamento.blocos_com_orcamento(conteudos, arquivos, limite, rotulo)


def _dump_codigo(workspace: str, arquivos: list[str], perfil) -> str:
    """Conteúdo real da entrega para injetar na tarefa do revisor — artefato que
    decide roteamento não pode depender só de elos de contexto entre tarefas
    (RESILIENCIA.md, item 10).

    A primeira versão acrescentava o bloco e **depois** conferia o total, então o
    teto não limitava nada: medido nas 14 entregas em disco, o dump chegou a
    43.918 chars com `LIMITE_DUMP_CODIGO` em 15.000 (193% acima) e truncava em 10
    delas. E como `sorted()` põe `tests/` por último, quem sumia era sempre a
    suíte — em 8 dos 11 casos truncados. O revisor aprovava código que não tinha
    visto inteiro, sem saber que faltava pedaço.

    É o mesmo defeito que `_amostra_testes` já corrigia logo abaixo, então os
    dois passaram a dividir `_blocos_com_orcamento`. Fonte antes de teste na
    fila do manifesto: o revisor já recebe `saida_testes` em separado, então
    perder trecho de teste custa menos que perder o módulo que a spec descreve.
    """
    ordem = orcamento.ordenar_para_dump(arquivos, perfil.e_teste)
    return _blocos_com_orcamento(
        workspace, ordem, LIMITE_DUMP_CODIGO, "Arquivos da entrega"
    )


def _amostra_testes(workspace: str, arquivos: list[str], perfil) -> str:
    """Amostra da suíte para o guard de critérios, com **todos** os arquivos
    representados — senão ele reprova por não ver testes que existem."""
    testes = [
        a for a in arquivos
        if perfil.e_teste(a) and not orcamento.e_binario(a)
    ]
    return _blocos_com_orcamento(
        workspace, testes, LIMITE_AMOSTRA_TESTES, "Arquivos de teste na suíte"
    )


# ---------- nós determinísticos ----------

def no_triagem(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    """Valida a entrada, resolve a stack, cria o workspace e zera contadores.

    A stack é resolvida aqui de propósito: nome inválido reprova em 1s, na
    triagem, e não lá no sandbox com uma imagem inexistente depois de o
    planejamento já ter sido pago."""
    thread_id = _tid(config)
    with medir(thread_id, "triagem"):
        pedido = (state.get("pedido") or "").strip()
        if not pedido:
            raise ValueError("Pedido vazio — nada a fazer.")
        perfil = perfis.obter(state.get("stack"))
        workspace = Path("workspace") / thread_id
        workspace.mkdir(parents=True, exist_ok=True)
        print(f">>> Stack: {perfil.nome} (sandbox {perfil.imagem_sandbox})")
        print(f">>> Workspace desta execução: {workspace.resolve()}")
    return {
        "pedido": pedido,
        "thread_id": thread_id,
        "stack": perfil.nome,
        "tentativas": 0,
        "testes_tentativas": 0,
        "revisao_tentativas": 0,
        "visual_tentativas": 0,
        "origem_feedback": "",
        "workspace": str(workspace.resolve()),
    }


def no_executar_testes(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    """Veredito por execução: roda a suíte na jaula (sandbox Docker por
    padrão) e traduz o resultado em estado. Sem LLM."""
    thread_id = _tid(config)
    perfil = _perfil(state)
    with medir(
        thread_id, "executar_testes",
        runner=os.getenv("TEST_RUNNER", "docker"), stack=perfil.nome,
    ) as m:
        resultado = executar_testes(state["workspace"], thread_id, perfil)
        m.update(
            testes_ok=resultado.testes_ok,
            cobertura=resultado.cobertura.total,
            cobertura_pior=resultado.cobertura.pior,
            cobertura_pior_arquivo=resultado.cobertura.pior_arquivo,
        )

    campos = resultado.como_estado(LIMITE_SAIDA)
    saida = campos["saida_testes"]
    total, pior = resultado.cobertura.total, resultado.cobertura.pior
    pior_arquivo = resultado.cobertura.pior_arquivo
    # Dois pisos: o agregado pega suíte fraca no geral; o por módulo pega a
    # suíte que testa muito o que é fácil e ignora o arquivo central.
    agregado_ok = total >= _cobertura_minima()
    modulo_ok = pior >= _cobertura_minima_modulo()
    cobertura_ok = agregado_ok and modulo_ok

    if not resultado.testes_ok:
        feedback = (
            "Os testes automatizados FALHARAM. Corrija o código (ou os "
            f"imports/estrutura) com base na saída real do {perfil.runner}:"
            f"\n{saida}"
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
        **campos,
        "cobertura_ok": cobertura_ok,
        "feedback_qa": feedback,
        # Rodada movida por execução: se voltar ao desenvolvimento, a suíte é
        # reescrita (o veredito veio dela).
        "origem_feedback": "testes" if feedback else "",
    }


def no_pentest(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    """Veredito por execução ofensiva: sobe a entrega e a ataca num sandbox
    isolado. Espelha o no_executar_testes — pytest prova que o código funciona;
    o pentest prova que ele não é trivialmente explorável.

    Roda uma vez por convergência (testes verdes + revisão aprovada), não dentro
    do laço barato: é o nó de execução mais caro. Desligado por padrão
    (PENTEST_HABILITADO), curto-circuita com veredito verde para não mudar o
    comportamento de quem ainda não construiu as imagens."""
    thread_id = _tid(config)
    if not pentest_habilitado():
        print(">>> Pentest desligado (PENTEST_HABILITADO=0) — pulando.")
        return {"pentest_ok": True, "vulnerabilidades": []}

    with medir(thread_id, "pentest") as m:
        resultado = executar_pentest(state["workspace"], thread_id, _perfil(state))
        vulns = resultado["vulnerabilidades"]
        m.update(
            pentest_ok=resultado["pentest_ok"],
            vulns_total=len(vulns),
            vulns_bloqueantes=resultado["bloqueantes"],
        )

    aprovado = resultado["pentest_ok"]
    return {
        "pentest_ok": aprovado,
        "vulnerabilidades": vulns,
        "pentest_tentativas": state.get("pentest_tentativas", 0) + (0 if aprovado else 1),
        "origem_feedback": "" if aprovado else "pentest",
        "feedback_qa": "" if aprovado else resultado["feedback_seguranca"],
    }


def no_visual(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    """Terceira camada de veredito por execução: renderiza a entrega e mede o
    contraste do que aparece na tela.

    Espelha o no_pentest. O pytest prova que funciona, o pentest que não é
    trivialmente explorável, este que é legível. Cobre a classe de defeito que
    escapa das outras duas por construção: o revisor LLM lê a cor do texto e não
    sabe o que aparece atrás, e o pytest não pinta pixel.

    Roda depois do pentest, no fim da convergência, pelo mesmo motivo que ele:
    é execução cara e não pertence ao laço barato. Desligado por padrão
    (VISUAL_HABILITADO), curto-circuita verde para não mudar o comportamento de
    quem ainda não construiu a imagem."""
    thread_id = _tid(config)
    if not visual_habilitado():
        print(">>> Verificação visual desligada (VISUAL_HABILITADO=0) — pulando.")
        return {"visual_ok": True, "problemas_visuais": []}

    with medir(thread_id, "visual") as m:
        resultado = executar_visual(state["workspace"], thread_id)
        m.update(
            visual_ok=resultado["visual_ok"],
            problemas=len(resultado["problemas"]),
            paginas=resultado["paginas"],
        )

    aprovado = resultado["visual_ok"]
    return {
        "visual_ok": aprovado,
        "problemas_visuais": resultado["problemas"],
        "visual_tentativas": state.get("visual_tentativas", 0) + (0 if aprovado else 1),
        "origem_feedback": "" if aprovado else "visual",
        "feedback_qa": "" if aprovado else resultado["feedback_visual"],
    }


class DeployNegado(RuntimeError):
    """Recusa deliberada no gate humano.

    Tipo próprio em vez de um RuntimeError genérico porque recusa não é falha:
    quem trata o desfecho precisa distinguir "o humano decidiu não publicar" de
    "o provedor caiu", e casar pelo texto da mensagem quebraria na primeira vez
    que alguém reescrevesse a frase.
    """


def no_aprovacao_humana(state: EstadoProjeto) -> EstadoProjeto:
    """Gate human-in-the-loop: pausa a execução até um humano decidir."""
    vulns = state.get("vulnerabilidades") or []
    resposta = interrupt(
        {
            "mensagem": "Autorizar deploy?",
            "testes_ok": state.get("testes_ok", False),
            "cobertura": state.get("cobertura", 0.0),
            "testes_aderentes": state.get("testes_aderentes", False),
            "pentest_ok": state.get("pentest_ok", False),
            "vulnerabilidades": len(vulns),
            "visual_ok": state.get("visual_ok", False),
            "problemas_visuais": len(state.get("problemas_visuais") or []),
            "relatorio_qa": state.get("relatorio_qa", ""),
        }
    )
    if str(resposta).strip().lower() not in {"sim", "s", "yes", "aprovar"}:
        raise DeployNegado("Deploy negado pelo aprovador humano.")
    return {}


def no_deploy(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    """Deploy real (Fase 4): git commit + push da entrega, sem LLM. Só roda
    após aprovação humana explícita no gate."""
    thread_id = _tid(config)
    with medir(thread_id, "deploy") as m:
        resultado = executar_deploy(
            state["workspace"], thread_id, state["pedido"], _perfil(state)
        )
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
        m.update(chars_contexto=len(state["pedido"]) + len(state["spec"][:8000]))
        veredito = com_retry("guard de aderência", lambda: zen_llm().call(
            "Você é um verificador rigoroso. Responda APENAS com a palavra SIM ou "
            f'NAO. A especificação técnica abaixo trata do pedido "{state["pedido"]}"'
            " — mesmo assunto e mesmo escopo, sem substituí-lo por outro tema?\n\n"
            f"Especificação:\n{state['spec'][:8000]}"
        ))
        coerente = veredito_sim(veredito)
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
    # Só numa rodada de correção há "antes" com que comparar; na inicial o
    # guard de entrega vazia já é o critério certo.
    perfil = _perfil(state)
    antes = (
        _impressao_entrega(state["workspace"], perfil) if origem != "inicial" else None
    )
    with medir(
        _tid(config), "desenvolvimento", executor=executor, origem=origem
    ) as m:
        m.update(chars_contexto=len(state["spec"]) + len(feedback))
        if executor == "opencode":
            executar_opencode(
                state["workspace"], state["spec"], feedback, perfil
            )
        elif executor == "crews":
            crew_desenvolvimento(state["workspace"]).kickoff(
                inputs={
                    "spec": state["spec"],
                    "feedback_qa": feedback or "Nenhum — primeira rodada.",
                    "stack": perfil.nome,
                    "libs_permitidas": perfil.libs_permitidas,
                }
            )
        else:
            raise ValueError(
                f"DEV_EXECUTOR inválido: '{executor}'. Use 'opencode' ou 'crews'."
            )
    # O que vale é o que está no disco: o manifesto do estado vem de uma
    # varredura determinística do workspace, não do texto do executor.
    arquivos = _arquivos_do_workspace(state["workspace"], perfil)
    _conferir_entrega(arquivos, state["workspace"], perfil)
    if antes is not None:
        _conferir_correcao(
            antes, _impressao_entrega(state["workspace"], perfil), origem
        )
    return {
        "arquivos": arquivos,
        "codigo": _dump_codigo(state["workspace"], arquivos, _perfil(state)),
        "tentativas": state["tentativas"] + 1,
        # Código novo, suíte nova: o laço de testes recomeça do zero.
        "testes_tentativas": 0,
    }


def no_escrever_testes(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    perfil = _perfil(state)
    arquivos_antes = "\n".join(state.get("arquivos", []))
    with medir(_tid(config), "escrever_testes") as m:
        m.update(chars_contexto=(
            len(state["spec"])
            + len(arquivos_antes)
            + len(state.get("feedback_qa", ""))
        ))
        com_retry("escrita de testes", lambda: crew_testes(state["workspace"]).kickoff(
            inputs={
                "spec": state["spec"],
                "arquivos": arquivos_antes or "(workspace vazio)",
                "feedback_qa": state.get("feedback_qa", "") or "Nenhum — primeira rodada.",
                "libs_permitidas": perfil.libs_permitidas,
                "instrucoes_qa": perfil.instrucoes_qa,
            }
        ), caro=True)
    # Revarre o workspace: os testes agora fazem parte da entrega e entram
    # no dump que o revisor recebe.
    arquivos = _arquivos_do_workspace(state["workspace"], perfil)
    return {
        "arquivos": arquivos,
        "codigo": _dump_codigo(state["workspace"], arquivos, perfil),
        "testes_tentativas": state.get("testes_tentativas", 0) + 1,
    }


def no_validacao_testes(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    """Guard de critérios: testes verdes não provam correção se a própria
    suíte ignora os critérios de aceite. Espelha o guard de aderência —
    uma chamada barata antes de executar, porque reprovar aqui custa
    centavos e uma rodada inteira do laço custa a execução."""
    testes = _amostra_testes(
        state["workspace"], state.get("arquivos", []), _perfil(state)
    )
    if not testes.strip():
        print(">>> Guard de critérios: nenhum arquivo de teste encontrado.")
        return {"testes_aderentes": False}

    with medir(_tid(config), "validacao_testes") as m:
        m.update(chars_contexto=len(state["spec"][:6000]) + len(testes))
        veredito = com_retry("guard de critérios", lambda: zen_llm().call(
            "Você é um verificador rigoroso de testes. Responda APENAS com a "
            "palavra SIM ou NAO. Os testes abaixo verificam de fato os "
            "critérios de aceite da especificação — cobrindo o comportamento "
            "exigido, e não apenas asserções triviais ou detalhes irrelevantes?"
            f"\n\nEspecificação:\n{state['spec'][:6000]}"
            f"\n\nTestes:\n{testes}"
        ))
        aderentes = veredito_sim(veredito)
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
        m.update(chars_contexto=(
            len(state["codigo"])
            + len(state["spec"])
            + len(state.get("saida_testes", ""))
            + len(anterior)
        ))
        resultado = com_retry("revisão", lambda: crew_revisao().kickoff(
            inputs={
                "codigo": state["codigo"],
                "spec": state["spec"],
                "saida_testes": state.get("saida_testes", ""),
                "revisao_anterior": anterior or "Nenhuma — primeira revisão.",
            }
        ), caro=True)
        texto = resultado.raw
        aprovado = veredito_aprovado(texto)
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

def _registrar_teto(state: EstadoProjeto, laco: str, limite: int) -> None:
    """Circuit breaker disparado é o dado que mais explica uma execução: diz que
    o laço parou por orçamento, não por mérito. Até aqui isso só existia no
    stdout, e o stdout não sobrevive à execução.

    As funções de rota recebem só o estado, não o `config` — daí o thread_id vir
    do estado, posto na triagem. Em checkpoint antigo o campo não existe: o
    registro é pulado sem quebrar o roteamento, que é o que importa.
    """
    thread_id = state.get("thread_id")
    if not thread_id:
        return
    registrar(
        thread_id, "teto_atingido", laco=laco, limite=limite,
        tentativas=state.get("tentativas", 0),
    )


def _aplicar(state: EstadoProjeto, decisao: rotas.Decisao) -> str:
    """Traduz a decisão do domínio em efeito, na ordem que sempre valeu:
    registrar o teto, imprimir o aviso, levantar o erro.

    As rotas abaixo existem só para o grafo ter nomes estáveis a que se ligar —
    a decisão em si mora em `dominio/rotas.py`, e é lá que ela é testada.
    """
    if decisao.teto:
        _registrar_teto(state, decisao.teto.laco, decisao.teto.limite)
    if decisao.aviso:
        print(decisao.aviso)
    if decisao.erro:
        raise RuntimeError(decisao.erro)
    return decisao.destino


def rota_pos_validacao_spec(state: EstadoProjeto) -> str:
    return _aplicar(state, rotas.pos_validacao_spec(state))


def rota_pos_desenvolvimento(state: EstadoProjeto) -> str:
    return _aplicar(
        state, rotas.pos_desenvolvimento(state, _perfil(state).e_teste)
    )


def rota_pos_validacao_testes(state: EstadoProjeto) -> str:
    return _aplicar(state, rotas.pos_validacao_testes(state))


def rota_pos_testes(state: EstadoProjeto) -> str:
    return _aplicar(state, rotas.pos_testes(state))


def rota_pos_revisao(state: EstadoProjeto) -> str:
    return _aplicar(state, rotas.pos_revisao(state))


def rota_pos_pentest(state: EstadoProjeto) -> str:
    return _aplicar(state, rotas.pos_pentest(state))


def rota_pos_visual(state: EstadoProjeto) -> str:
    return _aplicar(state, rotas.pos_visual(state))


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
    g.add_node("pentest", no_pentest)
    g.add_node("visual", no_visual)
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
    g.add_conditional_edges("pentest", rota_pos_pentest)
    g.add_conditional_edges("visual", rota_pos_visual)
    g.add_edge("aprovacao_humana", "deploy")
    g.add_edge("deploy", END)

    # Checkpointer persistente: sobrevive a quedas do processo, permitindo
    # retomar a execução do último nó concluído (ex.: falha 503 do provedor).
    if SqliteSaver is not None:
        conn = sqlite3.connect("checkpoints.sqlite", check_same_thread=False)
        return g.compile(checkpointer=SqliteSaver(conn))
    return g.compile(checkpointer=MemorySaver())
