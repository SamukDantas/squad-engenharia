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
from ..llm import zen_llm
from ..metricas import medir
from ..opencode import executar_opencode
from ..pentest import executar_pentest, habilitado as pentest_habilitado
from ..resiliencia import com_retry
from ..sandbox import executar_testes
from .state import EstadoProjeto

MAX_TENTATIVAS = 3
MAX_REPLANEJAMENTOS = 2
MAX_TESTES = 2                # reescritas da suíte por rodada de desenvolvimento
MAX_REVISOES = 2              # rodadas que a opinião do revisor pode custar
MAX_PENTEST = 2              # rodadas que uma reprovação de pentest pode custar
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


def _tem_conteudo(workspace: str, rel: str) -> bool:
    """Arquivo com algo dentro — 0 byte ou só espaço em branco não conta."""
    caminho = Path(workspace) / rel
    try:
        if caminho.stat().st_size == 0:
            return False
        return bool(caminho.read_text(encoding="utf-8", errors="replace").strip())
    except OSError:
        return False


def _conferir_entrega(arquivos: list[str], workspace: str) -> None:
    """Guard de entrega vazia: o nó de desenvolvimento não pode concluir sem
    ter produzido nada.

    O executor é externo e sinaliza sucesso pelo exit code, que classifica o
    processo — não o trabalho. Medido duas vezes em execução real: o OpenCode
    CLI abortou (permissão negada numa rodada, saldo insuficiente na outra),
    saiu com 0, e a squad seguiu pagando 855s de QA, dois guards de critérios
    e um pytest em cima de um workspace vazio, ainda entrando em outra rodada
    de desenvolvimento.

    Falha alto em vez de rotear de volta: não há o que corrigir sem código, e
    a causa é sempre de configuração (modelo sem tool calling, credencial,
    permissão) — repetir a mesma rodada só repete a mesma falha (item 26). O
    checkpoint preserva o progresso: `main.py --thread <id>` retoma.

    Arquivos em `tests/` não contam como entrega: são do QA, e o executor é
    proibido de tocá-los. Arquivo **vazio** também não: a primeira versão
    deste guard contava qualquer caminho fora de `tests/`, e o mesmo executor
    que não escreveu nada numa rodada deixou um `app/__init__.py` de 0 bytes
    na seguinte — estrutura sem conteúdo, que teria passado batido. Existir
    arquivo não é existir trabalho, e o critério é o mesmo que `_cobertura`
    já usa ao ignorar módulo sem instruções.
    """
    entregues = [
        a for a in arquivos
        if not a.startswith("tests/") and _tem_conteudo(workspace, a)
    ]
    if entregues:
        return
    if not arquivos:
        achado = "nada"
    elif all(a.startswith("tests/") for a in arquivos):
        achado = f"apenas {len(arquivos)} arquivo(s) de teste"
    else:
        achado = f"{len(arquivos)} arquivo(s), todos vazios ou só de teste"
    raise RuntimeError(
        f"O executor de desenvolvimento concluiu sem escrever a entrega: {achado} "
        f"em {workspace}. Exit code 0 não prova trabalho feito — confira se o "
        "modelo do executor suporta tool calling, se as credenciais têm saldo e "
        "se a saída acima registra permissão negada. Nada a corrigir sem código: "
        "o grafo para aqui em vez de pagar QA e pytest em cima do vazio."
    )


def _impressao_entrega(workspace: str) -> dict[str, str]:
    """Hash do conteúdo de cada arquivo da entrega, para comparar rodadas.

    Só a entrega: `tests/` é do QA e o executor é proibido de tocar, então
    mudança lá não prova que a correção pedida foi feita. Compara conteúdo e
    não mtime — executor que reescreve o arquivo idêntico não corrigiu nada.
    """
    raiz = Path(workspace)
    impressao: dict[str, str] = {}
    for rel in _arquivos_do_workspace(workspace):
        if rel.startswith("tests/"):
            continue
        try:
            impressao[rel] = hashlib.sha256((raiz / rel).read_bytes()).hexdigest()
        except OSError:
            continue
    return impressao


def _conferir_correcao(antes: dict[str, str], depois: dict[str, str], origem: str) -> None:
    """Guard de rodada de correção: corrigir sem mudar nada não é corrigir.

    O guard de entrega vazia cobre a rodada **inicial** — se não há arquivo, o
    executor não trabalhou. Numa rodada de correção ele não cobre nada: o
    workspace já está cheio da rodada anterior, e um executor que ignorou o
    feedback por completo passa como nó concluído.

    Medido na thread `ac0c7d4e` (agendador de tarefas): o revisor reprovou por
    uma condição de corrida entre cancelamento e execução, a rodada de correção
    rodou 109,5s, **não escreveu um único arquivo**, o pytest seguinte devolveu
    cobertura idêntica ao dígito e a segunda revisão repetiu o apontamento
    palavra por palavra — porque o código era o mesmo. O orçamento de revisões
    acabou ali, sem que nenhuma tentativa de conserto tivesse existido.

    Falha alto, como o item 28: repetir a rodada que não mudou nada tende a não
    mudar nada de novo, e a causa costuma ser de configuração ou de feedback
    que o executor não conseguiu acionar. O checkpoint preserva tudo.
    """
    if antes != depois:
        return
    raise RuntimeError(
        f"A rodada de correção (origem: {origem}) terminou sem alterar a entrega: "
        f"{len(depois)} arquivo(s), todos byte a byte idênticos aos de antes. "
        "O executor recebeu o feedback e não o acionou — repetir a rodada tende "
        "a repetir o resultado, e o veredito seguinte vai reproduzir o mesmo "
        "apontamento sobre o mesmo código. Confira se o feedback chegou "
        "acionável ao executor e se o modelo suporta tool calling. O checkpoint "
        "preserva o progresso: `main.py --thread <id>` retoma."
    )


# Extensões cujo conteúdo não é texto de revisar. Binário lido com
# `errors="replace"` vira ruído no contexto do LLM sem informar nada: medido nas
# entregas já existentes, `tarefas.db` (12.303 chars) e `tasks.db` (16.397)
# foram enviados inteiros ao revisor — 56% do dump de uma delas. A varredura do
# workspace continua vendo esses arquivos (eles são entrega e vão ao deploy);
# o corte é só no que se manda para o modelo.
_EXTENSOES_BINARIAS = {
    ".db", ".sqlite", ".sqlite3", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".ico", ".svg", ".pdf", ".zip", ".gz", ".tar", ".whl", ".so", ".dll",
    ".exe", ".bin", ".pickle", ".pkl",
}


def _e_binario(rel: str) -> bool:
    return Path(rel).suffix.lower() in _EXTENSOES_BINARIAS


def _blocos_com_orcamento(
    workspace: str, arquivos: list[str], limite: int, rotulo: str
) -> str:
    """Concatena arquivos dentro de um teto, **repartindo** o orçamento.

    Truncar pelo total (os primeiros N chars) faz quem lê julgar por amostra sem
    saber: os arquivos no fim da fila ficam invisíveis. Aqui cada arquivo tem
    cota, o que sobra de arquivo pequeno é redistribuído aos grandes (fila em
    ordem crescente de tamanho), e o manifesto lista todos os nomes — inclusive
    os que entraram truncados, para que ninguém confunda ausência com omissão.
    """
    if not arquivos:
        return ""

    conteudos = {
        rel: (Path(workspace) / rel).read_text(encoding="utf-8", errors="replace")
        for rel in arquivos
    }
    manifesto = "\n".join(f"- {a}" for a in arquivos)
    cabecalho = f"{rotulo} ({len(arquivos)}):\n{manifesto}\n\n"

    # O teto vale para o texto inteiro, então cabeçalho de bloco e separador
    # saem do orçamento antes de ele ser repartido: reservar só o conteúdo
    # deixaria o total estourar em silêncio de novo, que é o defeito original.
    moldura = {rel: len(f"### {rel}\n") + 1 for rel in arquivos}
    saldo = max(limite - len(cabecalho) - sum(moldura.values()), 0)

    # Água em copos: o menor primeiro leva só o que precisa, e o saldo restante
    # é redividido entre os que ainda não tiveram vez.
    fila = sorted(arquivos, key=lambda rel: len(conteudos[rel]))
    cotas: dict[str, int] = {}
    for i, rel in enumerate(fila):
        cotas[rel] = min(len(conteudos[rel]), saldo // (len(fila) - i))
        saldo -= cotas[rel]

    partes = []
    for rel in arquivos:
        conteudo = conteudos[rel]
        if len(conteudo) > cotas[rel]:
            # O marcador também ocupa a cota: sem descontá-lo, cada arquivo
            # truncado devolveria mais texto do que lhe foi orçado.
            marcador = (
                f"\n[... {rel} truncado aqui "
                f"({len(conteudo)} chars no total) ...]"
            )
            conteudo = conteudo[:max(cotas[rel] - len(marcador), 0)] + marcador
        partes.append(f"### {rel}\n{conteudo}")
    return cabecalho + "\n".join(partes)


def _dump_codigo(workspace: str, arquivos: list[str]) -> str:
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
    revisaveis = [a for a in arquivos if not _e_binario(a)]
    fonte = [a for a in revisaveis if not a.startswith("tests/")]
    testes = [a for a in revisaveis if a.startswith("tests/")]
    return _blocos_com_orcamento(
        workspace, fonte + testes, LIMITE_DUMP_CODIGO, "Arquivos da entrega"
    )


def _amostra_testes(workspace: str, arquivos: list[str]) -> str:
    """Amostra da suíte para o guard de critérios, com **todos** os arquivos
    representados — senão ele reprova por não ver testes que existem."""
    testes = [a for a in arquivos if a.startswith("tests/") and not _e_binario(a)]
    return _blocos_com_orcamento(
        workspace, testes, LIMITE_AMOSTRA_TESTES, "Arquivos de teste na suíte"
    )


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
        resultado = executar_pentest(state["workspace"], thread_id)
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
        m.update(chars_contexto=len(state["pedido"]) + len(state["spec"][:8000]))
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
    # Só numa rodada de correção há "antes" com que comparar; na inicial o
    # guard de entrega vazia já é o critério certo.
    antes = _impressao_entrega(state["workspace"]) if origem != "inicial" else None
    with medir(
        _tid(config), "desenvolvimento", executor=executor, origem=origem
    ) as m:
        m.update(chars_contexto=len(state["spec"]) + len(feedback))
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
    _conferir_entrega(arquivos, state["workspace"])
    if antes is not None:
        _conferir_correcao(antes, _impressao_entrega(state["workspace"]), origem)
    return {
        "arquivos": arquivos,
        "codigo": _dump_codigo(state["workspace"], arquivos),
        "tentativas": state["tentativas"] + 1,
        # Código novo, suíte nova: o laço de testes recomeça do zero.
        "testes_tentativas": 0,
    }


def no_escrever_testes(state: EstadoProjeto, config: RunnableConfig) -> EstadoProjeto:
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
        m.update(chars_contexto=len(state["spec"][:6000]) + len(testes))
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
    # Revisão e pentest reprovam código, não suíte: a correção muda o código,
    # então os testes precisam rodar de novo, não ser reescritos. O mesmo
    # raciocínio vale para os dois — vão direto ao pytest, que reancora o laço.
    if state.get("origem_feedback") in {"revisao", "pentest"} and tem_suite:
        print(
            f">>> Correção de {state['origem_feedback']}: suíte preservada, "
            "indo direto ao pytest."
        )
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
    # Aprovado pela revisão, o próximo juiz é a execução ofensiva — não o gate
    # humano direto. Testes verdes e revisão limpa não provam que a entrega
    # resiste a ataque.
    if state.get("aprovado"):
        return "pentest"
    if state.get("revisao_tentativas", 0) >= MAX_REVISOES:
        print(
            f">>> Revisão reprovou {MAX_REVISOES}x: teto de rodadas por opinião "
            "atingido, levando ao gate humano."
        )
        return "aprovacao_humana"
    if state["tentativas"] >= MAX_TENTATIVAS:
        return "aprovacao_humana"
    return "desenvolvimento"


def rota_pos_pentest(state: EstadoProjeto) -> str:
    """Teto próprio para o pentest, pela mesma razão do teto da revisão: o sinal
    mais caro não pode consumir sozinho o orçamento compartilhado com o pytest.

    Bloqueante e com orçamento — volta ao desenvolvimento com o brief de
    correção. Sem orçamento (de pentest ou do laço geral), o gate humano decide,
    com o relatório de vulnerabilidades em mãos: pode haver falha explorável que
    a auto-remediação não fechou, e publicar às cegas é o pior caminho.
    """
    if state.get("pentest_ok"):
        return "aprovacao_humana"
    if state.get("pentest_tentativas", 0) >= MAX_PENTEST:
        print(
            f">>> Pentest reprovou {MAX_PENTEST}x: teto de remediações atingido, "
            "levando ao gate humano com o relatório."
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
    g.add_node("pentest", no_pentest)
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
    g.add_edge("aprovacao_humana", "deploy")
    g.add_edge("deploy", END)

    # Checkpointer persistente: sobrevive a quedas do processo, permitindo
    # retomar a execução do último nó concluído (ex.: falha 503 do provedor).
    if SqliteSaver is not None:
        conn = sqlite3.connect("checkpoints.sqlite", check_same_thread=False)
        return g.compile(checkpointer=SqliteSaver(conn))
    return g.compile(checkpointer=MemorySaver())
