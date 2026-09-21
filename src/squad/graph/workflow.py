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
from ..dominio import guards, nomes, orcamento, rotas
from ..dominio.vereditos import (
    justificativa,
    trecho_da_falha,
    veredito_aprovado,
    veredito_sim,
)
from ..llm import provedor, squad_llm
from ..adaptadores import perfis
from ..adaptadores.config_spring import medir_ambientes
from ..adaptadores.metricas_json import medir, registrar
from ..adaptadores.codex_cli import executar_codex, executar_qa as executar_qa_codex
from ..crews.base import TASKS_CFG
from ..adaptadores.opencode_cli import executar_opencode
from ..pentest import executar_pentest, habilitado as pentest_habilitado
from ..resiliencia import com_retry
from ..adaptadores.testes import (
    LIMITE_SAIDA,
    executar_testes,
    verificar_compilacao,
)
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
MAX_AMBIENTES = rotas.MAX_AMBIENTES

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


def _tid_da_execucao(config: RunnableConfig) -> str:
    """A thread que o LangGraph está executando."""
    return str(config["configurable"]["thread_id"])


def _tid(state: EstadoProjeto, config: RunnableConfig) -> str:
    """A thread **deste ramo**, e não a do grafo que o executa.

    Dentro de um subgrafo o `config` carrega o `thread_id` do pai. Como o id
    batiza o workspace, os nomes de container e o arquivo de métricas, ler dali
    faria dois serviços construídos em paralelo escreverem no mesmo lugar — e o
    sintoma seria um container recusado por nome duplicado, longe da causa.

    `no_triagem` estabelece o valor e o guarda no estado; todo nó depois dele lê
    daqui. É a mesma razão pela qual `_perfil` e `_registrar_teto` já liam do
    estado.
    """
    return str(state.get("thread_id") or _tid_da_execucao(config))


def _do_perfil(perfil) -> dict:
    """Campos do perfil que **toda** crew recebe.

    Num só lugar de propósito. A primeira versão passava `stack` apenas às
    crews que pareciam precisar, e a de planejamento ficou de fora — o
    `tasks.yaml` cita `{stack}` na tarefa de arquitetura, e o CrewAI só levanta
    na interpolação, no meio da execução, depois da triagem já paga. Distribuir
    tudo para todas custa nada e tira a classe inteira de erro do caminho.
    """
    return {
        "stack": perfil.nome,
        "runner": perfil.runner,
        "libs_permitidas": perfil.libs_permitidas,
        "instrucoes_qa": perfil.instrucoes_qa,
        "exemplo_run_json": perfil.exemplo_run_json,
    }


LIMITE_RESPOSTA_GUARD = 200


def _resposta_curta(resposta: object) -> str:
    """A resposta crua do guard, guardada na métrica.

    Sem isto o histórico só tem o booleano, e o booleano não permite auditar a
    decisão nem reprocessar o parser: quando `veredito_sim` mudou, as 65
    decisões já gravadas eram inúteis para medir o efeito da mudança. Cabe em
    200 chars porque a pergunta pede uma palavra — resposta longa já é sinal.
    """
    return " ".join(str(resposta).split())[:LIMITE_RESPOSTA_GUARD]


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
    # Um ramo por serviço: o id da thread carrega o nome do serviço para que
    # workspace, containers e métricas de dois ramos paralelos não se cruzem.
    # Sem `servico` no estado — execução de entrega única — o id é o da própria
    # thread, exatamente como sempre foi.
    servico = str(state.get("servico") or "").strip()
    base = _tid_da_execucao(config)
    thread_id = f"{base}--{servico}" if servico else base
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
    thread_id = _tid(state, config)
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
        if not resultado.testes_ok:
            # A saída fica no estado e a rodada seguinte a sobrescreve; sem
            # isto, a causa de um vermelho corrigido depois se perde
            # (vereditos.trecho_da_falha).
            m.update(
                falha_de_build=resultado.falha_de_build,
                trecho_falha=trecho_da_falha(resultado.saida),
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

    if resultado.falha_de_build:
        # Nenhum teste rodou: mandar "corrija com base na saída dos testes"
        # apontaria o dev para um lugar onde não há nada para ver.
        feedback = (
            "A entrega NÃO COMPILA — a suíte nem chegou a rodar. Corrija os "
            "erros de compilação abaixo antes de qualquer outra coisa:"
            f"\n{saida}"
        )
    elif not resultado.testes_ok:
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
    revisoes = state.get("revisoes_suite", 0)
    contestada = (
        not resultado.testes_ok
        and not resultado.falha_de_build
        and state.get("origem_feedback") == "testes"
        and revisoes < rotas.MAX_REVISOES_SUITE
        and guards.falha_repetida(state.get("saida_testes", ""), saida)
    )
    if contestada:
        feedback = guards.brief_falha_repetida(saida)
        registrar(thread_id, "suite_contestada", testes=sorted(guards.testes_que_falharam(saida)))
    return {
        **campos,
        "cobertura_ok": cobertura_ok,
        "falha_de_build": resultado.falha_de_build,
        "suite_contestada": contestada,
        "revisoes_suite": revisoes + 1 if contestada else revisoes,
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
    thread_id = _tid(state, config)
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
    thread_id = _tid(state, config)
    perfil = _perfil(state)
    if not visual_habilitado():
        print(">>> Verificação visual desligada (VISUAL_HABILITADO=0) — pulando.")
        return {"visual_ok": True, "problemas_visuais": []}

    with medir(thread_id, "visual") as m:
        resultado = executar_visual(state["workspace"], thread_id, perfil)
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


def no_config_ambientes(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    """Quarta camada de veredito por medição — e a mais barata das quatro.

    Os testes rodam no perfil `test`, com H2 em memória, e ficam verdes
    independentemente do que os perfis de homologação e produção digam. Uma
    entrega pode passar em tudo e trazer `ddl-auto: create-drop` em produção,
    que apaga o banco no boot, ou a senha do banco escrita no YAML, que vai para
    o repositório junto com o código. Nenhum dos dois aparece numa suíte verde,
    e nenhum é o tipo de coisa que se quer descobrir depois do push.

    Espelha o pentest e o visual: o nó existe sempre no grafo e curto-circuita
    verde quando a stack não exige configuração por ambiente. A diferença é que
    aqui quem decide é o perfil, não uma variável de ambiente — a exigência
    nasce do Spring, não de quem construiu qual imagem.
    """
    perfil = _perfil(state)
    if not perfil.exige_ambientes:
        return {"ambientes_ok": True, "achados_ambientes": []}

    with medir(_tid(state, config), "config_ambientes", stack=perfil.nome) as m:
        resultado = medir_ambientes(state["workspace"])
        m.update(
            ambientes_ok=resultado["ambientes_ok"],
            achados=len(resultado["achados_ambientes"]),
            arquivos=len(resultado["arquivos_config"]),
        )

    aprovado = resultado["ambientes_ok"]
    return {
        "ambientes_ok": aprovado,
        "achados_ambientes": resultado["achados_ambientes"],
        "ambientes_tentativas": state.get("ambientes_tentativas", 0) + (0 if aprovado else 1),
        "origem_feedback": "" if aprovado else "ambientes",
        "feedback_qa": "" if aprovado else resultado["feedback_ambientes"],
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
            "nome_repo": state.get("nome_repo", ""),
            "testes_ok": state.get("testes_ok", False),
            "cobertura": state.get("cobertura", 0.0),
            "testes_aderentes": state.get("testes_aderentes", False),
            "pentest_ok": state.get("pentest_ok", False),
            "vulnerabilidades": len(vulns),
            "visual_ok": state.get("visual_ok", False),
            "ambientes_ok": state.get("ambientes_ok", True),
            "achados_ambientes": len(state.get("achados_ambientes") or []),
            "problemas_visuais": len(state.get("problemas_visuais") or []),
            "relatorio_qa": state.get("relatorio_qa", ""),
            "motivo": state.get("motivo_gate", ""),
        }
    )
    if str(resposta).strip().lower() not in {"sim", "s", "yes", "aprovar"}:
        raise DeployNegado("Deploy negado pelo aprovador humano.")
    return {}


def no_deploy(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    """Deploy real (Fase 4): git commit + push da entrega, sem LLM. Só roda
    após aprovação humana explícita no gate — e só publica o que compila.

    A verificação de compilação roda aqui, e não é redundante com o passo de
    build do `executar_testes`: os tetos de circuit breaker roteiam ao gate
    humano **com a suíte vermelha**, de propósito, para o humano decidir. Um
    `sim` ali publicaria o que não compila, e foi assim que uma entrega Next.js
    quebrada chegou ao GitHub depois de 38 testes verdes e revisão aprovada.

    Roda contra o disco, não contra estado guardado: uma thread retomada dias
    depois precisa provar de novo que compila.
    """
    thread_id = _tid(state, config)
    perfil = _perfil(state)

    with medir(thread_id, "verificacao_deploy", stack=perfil.nome) as m:
        falha = verificar_compilacao(state["workspace"], thread_id, perfil)
        m.update(compila=not falha)
    if falha:
        raise RuntimeError(
            "A entrega NÃO COMPILA — nada foi publicado. A squad só empurra a "
            "branch quando o código compila no ambiente da própria stack, e "
            "aprovar o gate não dispensa essa prova: teste vermelho pode chegar "
            "ao gate por teto de circuit breaker.\n"
            f"Saída do compilador:\n{falha}\n"
            "O checkpoint preserva o progresso: corrija a entrega e retome com "
            "`main.py --thread <id>`, que reexecuta apenas o deploy."
        )

    with medir(thread_id, "deploy") as m:
        resultado = executar_deploy(
            state["workspace"], thread_id, state["pedido"], perfil,
            # Thread anterior a esta mudança não tem nome gravado: a regra
            # curta vale para ela também, em vez do slug do pedido inteiro.
            nome=state.get("nome_repo") or nomes.nome_do_pedido(state["pedido"]),
        )
        m.update(deploy_ref=resultado.get("deploy_ref", ""))
    return resultado


# ---------- nós que invocam crews ----------

def no_planejamento(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    with medir(_tid(state, config), "planejamento"):
        resultado = com_retry(
            "planejamento",
            lambda: crew_planejamento().kickoff(
                inputs={**_do_perfil(_perfil(state)), "pedido": state["pedido"]}
            ),
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
    with medir(_tid(state, config), "validacao_spec") as m:
        m.update(chars_contexto=len(state["pedido"]) + len(state["spec"][:8000]))
        veredito = com_retry("guard de aderência", lambda: squad_llm().call(
            "Você é um verificador rigoroso. Responda APENAS com a palavra SIM ou "
            f'NAO. A especificação técnica abaixo trata do pedido "{state["pedido"]}"'
            " — mesmo assunto e mesmo escopo, sem substituí-lo por outro tema?\n\n"
            f"Especificação:\n{state['spec'][:8000]}"
        ))
        coerente = veredito_sim(veredito)
        m.update(spec_coerente=coerente, resposta_guard=_resposta_curta(veredito))
    if not coerente:
        print(f">>> Guard: spec reprovada (não adere ao pedido). Veredito: {str(veredito)[:40]}")
        return {"spec_coerente": coerente}
    # Ramo do maestro já tem nome (o do serviço), e retomada não renomeia.
    if state.get("servico") or state.get("nome_repo"):
        return {"spec_coerente": coerente}
    return {"spec_coerente": coerente, "nome_repo": _nome_repo(state)}


def _nome_repo(state: EstadoProjeto) -> str:
    """Nome curto do repositório, decidido uma vez e gravado no estado.

    Aqui e não no deploy: o deploy é determinístico e sem LLM, e o nome
    precisa estar pronto antes do gate para o humano ver para onde está
    autorizando a publicação. Gravado no checkpoint, retomar a thread publica
    no mesmo repositório em vez de sortear outro nome. Uma chamada barata; se
    ela falhar, demorar ou vier fora do formato, a regra determinística
    assume — nome de repositório não é motivo para derrubar nem para segurar
    a execução.
    """
    try:
        # Sem com_retry e com teto curto: repetir uma chamada dispensável só
        # troca uma espera longa por outra (ver squad_llm, acessoria).
        resposta = squad_llm(acessoria=True).call(
            "Dê um nome curto para o repositório do projeto descrito abaixo: 2 a "
            "3 palavras em português, minúsculas, sem acento, separadas por "
            "hífen, dizendo O QUE o sistema é — não o verbo do pedido nem a "
            "linguagem. Responda APENAS com o nome. Exemplos: "
            "conversor-temperatura, api-reserva-salas, dashboard-funil-vendas.\n\n"
            f"Pedido:\n{state['pedido'][:2000]}"
        )
    except Exception as e:  # noqa: BLE001 — qualquer falha cai no fallback
        print(f">>> Nome do repositório: chamada falhou ({type(e).__name__}); usando regra.")
        resposta = ""
    nome = nomes.nome_da_resposta(resposta)
    if not nome:
        nome = nomes.nome_do_pedido(state["pedido"]) or f"entrega-{state['thread_id'][:8]}"
    print(f">>> Nome do repositório: {nome}")
    return nome


def no_desenvolvimento(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    """Escreve a implementação no workspace. O executor é intercambiável
    (DEV_EXECUTOR): o Codex CLI (padrão) ou o OpenCode CLI como mão de obra
    especialista, ou as crews CrewAI como caminho sem dependência externa. A
    governança do grafo é a mesma nos três casos."""
    executor = os.getenv("DEV_EXECUTOR", "codex").strip().lower()
    feedback = state.get("feedback_qa", "")
    origem = state.get("origem_feedback") or "inicial"
    # Só numa rodada de correção há "antes" com que comparar; na inicial o
    # guard de entrega vazia já é o critério certo.
    perfil = _perfil(state)
    antes = (
        _impressao_entrega(state["workspace"], perfil) if origem != "inicial" else None
    )
    testes_antes = {
        a for a in _arquivos_do_workspace(state["workspace"], perfil) if perfil.e_teste(a)
    }
    with medir(
        _tid(state, config), "desenvolvimento", executor=executor, origem=origem
    ) as m:
        m.update(chars_contexto=len(state["spec"]) + len(feedback))

        def rodar(retorno: str) -> None:
            if executor == "opencode":
                executar_opencode(state["workspace"], state["spec"], retorno, perfil)
            elif executor == "codex":
                executar_codex(state["workspace"], state["spec"], retorno, perfil)
            elif executor == "crews":
                crew_desenvolvimento(state["workspace"]).kickoff(
                    inputs={
                        **_do_perfil(perfil),
                        "spec": state["spec"],
                        "feedback_qa": retorno or "Nenhum — primeira rodada.",
                    }
                )
            else:
                raise ValueError(
                    f"DEV_EXECUTOR inválido: '{executor}'. Use 'codex', 'opencode' "
                    "ou 'crews'."
                )

        rodar(feedback)
        if executor in {"codex", "opencode"}:
            m.update(_compilar_com_ajuste(state, perfil, rodar, feedback, _tid(state, config)))
    _remover_testes_do_executor(state, testes_antes, perfil)
    # O que vale é o que está no disco: o manifesto do estado vem de uma
    # varredura determinística do workspace, não do texto do executor.
    arquivos = _arquivos_do_workspace(state["workspace"], perfil)
    _conferir_entrega(arquivos, state["workspace"], perfil)
    extra = {"correcao_inerte": ""}
    if antes is not None:
        extra = _correcao_inerte(
            state, antes, _impressao_entrega(state["workspace"], perfil), origem
        )
    return {
        "arquivos": arquivos,
        "codigo": _dump_codigo(state["workspace"], arquivos, _perfil(state)),
        "tentativas": state["tentativas"] + 1,
        # Código novo, suíte nova: o laço de testes recomeça do zero.
        "testes_tentativas": 0,
        **extra,
    }


MAX_AJUSTES_BUILD = 2


def _compilar_com_ajuste(state: EstadoProjeto, perfil, rodar, feedback: str, tid: str) -> dict:
    """Compila a entrega antes de ela sair do nó, e devolve o erro ao executor.

    O executor escreve às cegas: roda no host, sem as dependências da jaula,
    e nunca vê o compilador. Um erro de build só aparecia depois de QA, guard
    de critérios e suíte — e custava uma rodada inteira, com o QA reescrevendo
    a suíte para o código corrigido. Medido na thread `f96bb730`: um
    `:global(*)` num CSS Module derrubou o `next build` na primeira rodada.
    Aqui o ajuste custa um build na jaula e uma chamada ao executor.

    Só com a jaula Docker (o padrão): `verificar_compilacao` é a mesma que
    confere a entrega antes do push.
    """
    runner = (os.getenv("TEST_RUNNER") or "docker").strip().lower()
    if runner != "docker" or not perfil.comando_verificacao:
        return {}
    for ajuste in range(MAX_AJUSTES_BUILD + 1):
        falha = verificar_compilacao(state["workspace"], tid, perfil)
        if not falha:
            return {"build_ok": True, "ajustes_build": ajuste}
        if ajuste == MAX_AJUSTES_BUILD:
            break
        print(f">>> A entrega não compila — ajuste {ajuste + 1} do executor antes do QA.")
        rodar(_feedback_build(falha, feedback))
    # Teto atingido: segue para o QA e a suíte, que reprovam com o mesmo erro e
    # levam a rodada ao laço normal de correção.
    return {
        "build_ok": False,
        "ajustes_build": MAX_AJUSTES_BUILD,
        "trecho_falha": trecho_da_falha(falha),
    }


def _feedback_build(falha: str, feedback: str) -> str:
    """O retorno do ajuste de build: o erro do compilador, e o retorno da
    rodada, que continua valendo."""
    texto = (
        "A entrega NÃO COMPILA. Corrija o erro de build abaixo sem mudar o "
        "comportamento já implementado:\n"
        f"{trecho_da_falha(falha)}"
    )
    if feedback:
        texto += f"\n\nRetorno desta rodada, que continua valendo:\n{feedback}"
    return texto


def _remover_testes_do_executor(state: EstadoProjeto, antes: set[str], perfil) -> None:
    """Apaga os testes que o executor criou sem a spec pedir
    (guards.testes_do_executor)."""
    raiz = Path(state["workspace"])
    novos = guards.testes_do_executor(
        antes, _arquivos_do_workspace(raiz, perfil), perfil.e_teste,
        spec=state.get("spec", ""),
    )
    if not novos:
        return
    for rel in novos:
        (raiz / rel).unlink(missing_ok=True)
    print(
        f">>> Executor escreveu {len(novos)} arquivo(s) de teste, proibido pela "
        f"tarefa — removidos (a suíte é do QA): {', '.join(novos)}"
    )
    registrar(state["thread_id"], "testes_do_executor_removidos", arquivos=novos)


def _correcao_inerte(
    state: EstadoProjeto, antes: dict[str, str], depois: dict[str, str], origem: str
) -> dict:
    """Rodada de correção que não mudou nada: erro, revisão da suíte ou gate.

    O domínio decide (guards.destino_da_correcao_inerte); aqui só se traduz a
    decisão em estado. `erro` mantém o comportamento anterior — o guard levanta.
    """
    falha = guards.conferir_correcao(antes, depois, origem)
    if not falha:
        return {"correcao_inerte": ""}
    saida = state.get("saida_testes", "")
    revisoes = state.get("revisoes_suite", 0)
    destino = guards.destino_da_correcao_inerte(
        origem, saida, bool(state.get("falha_de_build")), revisoes
    )
    if destino == "revisar_suite":
        return {
            "correcao_inerte": destino,
            "revisoes_suite": revisoes + 1,
            "feedback_qa": guards.brief_revisao_suite(saida),
        }
    if destino == "aprovacao_humana":
        return {
            "correcao_inerte": destino,
            "motivo_gate": guards.motivo_impasse_suite(saida),
        }
    raise RuntimeError(falha)


def _feedback_para_qa(state: EstadoProjeto, perfil) -> str:
    """O retorno que o QA recebe, em modo ajuste quando a suíte já existe.

    O QA gravava a suíte inteira de novo a cada passada, mesmo quando o
    retorno apontava um teste só. É o nó mais caro do grafo, e no gateway corporativo o custo cresce com o tamanho da resposta: com os arquivos inteiros
    reenviados em cada chamada da ferramenta, cada passada levou de 6 a 23 min
    na calculadora de juros. Com a suíte já no disco, a ordem é mexer só no
    que o retorno aponta.
    """
    feedback = state.get("feedback_qa", "")
    if not feedback:
        return "Nenhum — primeira rodada."
    suite = [a for a in state.get("arquivos", []) if perfil.e_teste(a)]
    if not suite:
        return feedback
    return (
        "MODO AJUSTE — a suíte já existe no disco: "
        f"{', '.join(suite)}. Leia esses arquivos antes de agir e altere SÓ o "
        "necessário para tratar o retorno abaixo: acrescente os testes que "
        "faltam ou corrija os que estão errados, no arquivo em que eles "
        "moram. NÃO regrave arquivo que não precisa mudar — reescrever a suíte "
        "inteira é proibido nesta rodada.\n\n"
        f"{feedback}"
    )


def _conteudos(workspace: str, perfil) -> dict[str, bytes]:
    raiz = Path(workspace)
    return {a: (raiz / a).read_bytes() for a in _arquivos_do_workspace(workspace, perfil)}


def _qa_pelo_codex(state: EstadoProjeto, perfil, entradas: dict) -> None:
    """O QA rodando pelo Codex no workspace, com o código da entrega protegido.

    A tarefa é a mesma `escrever_testes` do `tasks.yaml`, preenchida com as
    mesmas entradas do QA do CrewAI. Depois, tudo o que ele mudou fora da suíte
    é desfeito (guards.fora_da_suite): arquivo alterado ou apagado volta ao
    conteúdo de antes, arquivo novo que não é teste sai.
    """
    ws = state["workspace"]
    antes = _conteudos(ws, perfil)
    descricao = TASKS_CFG["escrever_testes"]["description"].format_map(entradas)
    executar_qa_codex(ws, descricao)

    depois = _conteudos(ws, perfil)
    alterados, criados = guards.fora_da_suite(
        {a: hashlib.sha256(c).hexdigest() for a, c in antes.items()},
        {a: hashlib.sha256(c).hexdigest() for a, c in depois.items()},
        perfil.e_teste,
    )
    raiz = Path(ws)
    for rel in alterados:
        (raiz / rel).parent.mkdir(parents=True, exist_ok=True)
        (raiz / rel).write_bytes(antes[rel])
    for rel in criados:
        (raiz / rel).unlink(missing_ok=True)
    if alterados or criados:
        print(
            f">>> QA mexeu fora da suíte — desfeito (o código é do executor): "
            f"restaurados {alterados or '-'}, removidos {criados or '-'}"
        )
        registrar(state["thread_id"], "qa_fora_da_suite_desfeito",
                  restaurados=alterados, removidos=criados)


def no_escrever_testes(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
    perfil = _perfil(state)
    arquivos_antes = "\n".join(state.get("arquivos", []))
    with medir(_tid(state, config), "escrever_testes") as m:
        m.update(chars_contexto=(
            len(state["spec"])
            + len(arquivos_antes)
            + len(state.get("feedback_qa", ""))
        ))
        entradas = {
            **_do_perfil(perfil),
            "spec": state["spec"],
            "arquivos": arquivos_antes or "(workspace vazio)",
            "feedback_qa": _feedback_para_qa(state, perfil),
        }
        if provedor() == "codex":
            _qa_pelo_codex(state, perfil, entradas)
        else:
            com_retry("escrita de testes", lambda: crew_testes(state["workspace"]).kickoff(
                inputs=entradas
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

    with medir(_tid(state, config), "validacao_testes") as m:
        m.update(chars_contexto=len(state["spec"][:6000]) + len(testes))
        veredito = com_retry("guard de critérios", lambda: squad_llm().call(
            "Você é um verificador rigoroso de testes. Os testes abaixo "
            "verificam de fato os critérios de aceite FUNCIONAIS da "
            "especificação — o comportamento observável que ela exige "
            "(entradas e saídas, valores esperados, erros e recusas) —, e não "
            "apenas asserções triviais? Julgue SÓ comportamento. Requisitos "
            "de estrutura, estilo, tipagem, dependências ou organização do "
            "código (\"usa só a biblioteca padrão\", \"sem estado global\", "
            "\"anotações de tipo\") NÃO são critério desta pergunta: não se "
            "verificam por teste de comportamento e são julgados pela revisão. "
            "Ambiente de testes disponível: "
            f"{_perfil(state).ambiente_testes or 'o runner da stack'}. Só "
            "aponte como faltando o que dá para testar com ele. "
            "Cobertura é por comportamento, não por valor: uma asserção "
            "representativa sobre o valor real basta (uma linha de tabela "
            "conferida com os números cobre a tabela; um caso de erro "
            "conferido cobre aquela recusa). Não exija conferir cada linha, "
            "cada mês ou cada combinação de um comportamento já verificado. "
            "Aparência também não é critério desta pergunta: layout "
            "(\"lado a lado\", posição, tamanho), cores, contraste e "
            "legibilidade nos temas são medidos pela verificação visual, que "
            "renderiza a página num navegador — não exija teste deles. "
            "Responda SIM ou NAO sozinho na primeira linha. Se NAO, liste nas "
            "linhas seguintes, em até 5 itens curtos, os comportamentos "
            "exigidos que a suíte não verifica."
            f"\n\nEspecificação:\n{state['spec'][:6000]}"
            f"\n\nTestes:\n{testes}"
        ))
        aderentes = veredito_sim(veredito)
        m.update(testes_aderentes=aderentes, resposta_guard=_resposta_curta(veredito))

    if not aderentes:
        print(">>> Guard de critérios: testes não cobrem os critérios de aceite.")
        return {"testes_aderentes": False, "feedback_qa": _feedback_criterios(veredito)}
    return {"testes_aderentes": True, "feedback_qa": ""}


def _feedback_criterios(veredito: object) -> str:
    """O que o QA recebe quando o guard de critérios reprova.

    Antes era "reescreva-os" e mais nada: o QA não sabia o que faltava e
    reescrevia a suíte inteira às cegas — e o nó que escreve a suíte é o mais
    caro do grafo. Com a lista do guard, ele complementa o que falta.
    """
    faltando = justificativa(veredito)
    if not faltando:
        return (
            "Os testes escritos não verificam os critérios de aceite da spec. "
            "Complemente-os cobrindo o comportamento exigido, com asserções "
            "sobre resultados reais — não asserções triviais."
        )
    return (
        "O verificador de critérios apontou o que a suíte NÃO verifica. "
        "Acrescente testes para estes pontos, sem reescrever os que já estão "
        f"certos:\n{faltando}"
    )


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
    with medir(_tid(state, config), "revisao") as m:
        m.update(chars_contexto=(
            len(state["codigo"])
            + len(state["spec"])
            + len(state.get("saida_testes", ""))
            + len(anterior)
        ))
        resultado = com_retry("revisão", lambda: crew_revisao().kickoff(
            inputs={
                **_do_perfil(_perfil(state)),
                # O pedido separa requisito de decisão inventada pelo
                # arquiteto: sem ele, o revisor só tem a spec, e tudo o que
                # está nela parece obrigação (ver tasks.yaml, `revisar`).
                "pedido": state["pedido"],
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


def rota_pos_config_ambientes(state: EstadoProjeto) -> str:
    return _aplicar(state, rotas.pos_config_ambientes(state))


def no_veredito_do_ramo(state: EstadoProjeto) -> EstadoProjeto:
    """Fim do ramo: fecha o veredito e devolve o controle a quem o chamou.

    Registrado sob o nome `aprovacao_humana` de propósito. É para lá que as
    rotas do domínio mandam quando o ramo acaba — por convergência ou por teto
    de circuit breaker — e mudar o nome obrigaria a mexer em `dominio/rotas.py`,
    que é justamente o que a extração do subgrafo não deve exigir. O gate
    humano de verdade está no maestro, uma camada acima, e vê os N ramos de uma
    vez.

    `pronto` separa dois desfechos que hoje se confundiriam num booleano só:
    o ramo que convergiu e o que parou por orçamento. Só o primeiro é
    publicável, e é essa distinção que permite publicar os serviços verdes e
    reter o vermelho.
    """
    pronto = bool(
        state.get("testes_ok")
        and state.get("cobertura_ok")
        and state.get("aprovado")
        and state.get("pentest_ok", True)
        and state.get("visual_ok", True)
    )
    servico = state.get("servico") or "entrega"
    print(f">>> Ramo '{servico}': {'pronto para publicar' if pronto else 'RETIDO'}.")
    return {"pronto": pronto}


# ---------- montagem do grafo ----------

def construir_subgrafo_servico(entrada: str = "triagem"):
    """O pipeline de um serviço, do pedido ao veredito.

    `entrada` é o primeiro nó. Fora da triagem, só a reexecução a partir de um
    nó usa (graph/reexecucao.py): o estado chega pronto, copiado do checkpoint
    de outra thread.

    É o grafo que sempre existiu, menos as duas pontas que deixaram de ser dele:
    o gate humano e o deploy subiram para o maestro. A razão é o paralelismo —
    com N serviços, N gates fariam o humano aprovar um serviço antes de saber
    que outro reprovou, e N deploys precisam esperar essa decisão única.

    Sem checkpointer próprio: quem o compõe passa o dele, e é isso que faz o
    checkpoint do pai guardar o progresso de cada ramo separadamente.
    """
    g = StateGraph(EstadoProjeto)

    g.add_node("triagem", no_triagem)
    g.add_node("planejamento", no_planejamento)
    g.add_node("validacao_spec", no_validacao_spec)
    g.add_node("desenvolvimento", no_desenvolvimento)
    g.add_node("escrever_testes", no_escrever_testes)
    g.add_node("validacao_testes", no_validacao_testes)
    g.add_node("executar_testes", no_executar_testes)
    g.add_node("config_ambientes", no_config_ambientes)
    g.add_node("revisao", no_revisao)
    g.add_node("pentest", no_pentest)
    g.add_node("visual", no_visual)
    # Nome de rota, não de comportamento: ver `no_veredito_do_ramo`.
    g.add_node("aprovacao_humana", no_veredito_do_ramo)

    g.add_edge(START, entrada)
    g.add_edge("triagem", "planejamento")
    g.add_edge("planejamento", "validacao_spec")
    g.add_conditional_edges("validacao_spec", rota_pos_validacao_spec)
    g.add_conditional_edges("desenvolvimento", rota_pos_desenvolvimento)
    g.add_edge("escrever_testes", "validacao_testes")
    g.add_conditional_edges("validacao_testes", rota_pos_validacao_testes)
    g.add_conditional_edges("executar_testes", rota_pos_testes)
    g.add_conditional_edges("config_ambientes", rota_pos_config_ambientes)
    g.add_conditional_edges("revisao", rota_pos_revisao)
    g.add_conditional_edges("pentest", rota_pos_pentest)
    g.add_conditional_edges("visual", rota_pos_visual)
    g.add_edge("aprovacao_humana", END)

    return g.compile()


def _checkpointer():
    """Checkpointer persistente: sobrevive a quedas do processo, permitindo
    retomar do último nó concluído (ex.: falha 503 do provedor).

    WAL e `busy_timeout` não são afinação: com ramos paralelos, dois nós
    concluem ao mesmo tempo e o SQLite em modo padrão recusa a segunda escrita
    com `database is locked` — matando um ramo por uma disputa de arquivo, não
    por mérito da entrega.
    """
    if SqliteSaver is None:
        return MemorySaver()
    conn = sqlite3.connect("checkpoints.sqlite", check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return SqliteSaver(conn)


def construir_grafo(entrada: str = "triagem"):
    """O maestro: o que é da execução inteira, não de um serviço.

    Hoje ele conduz um ramo só, e o resultado é igual ao de sempre. A forma é
    que mudou: gate e deploy passaram a viver aqui, onde adiante vão ver os N
    serviços de uma vez.
    """
    g = StateGraph(EstadoProjeto)

    g.add_node("servico", construir_subgrafo_servico(entrada))
    g.add_node("aprovacao_humana", no_aprovacao_humana)
    g.add_node("deploy", no_deploy)

    g.add_edge(START, "servico")
    g.add_edge("servico", "aprovacao_humana")
    g.add_edge("aprovacao_humana", "deploy")
    g.add_edge("deploy", END)

    return g.compile(checkpointer=_checkpointer())
