"""Roteamento do grafo: para onde ir depois de cada nó, e por quê.

São as decisões caras do pipeline — avançar, repetir, parar, chamar humano — e
por isso moram aqui, não dentro de um agente. Todas são funções puras de um
mapeamento de estado para uma `Decisao`.

O efeito fica de fora de propósito. Antes, as rotas registravam métrica,
imprimiam no terminal e levantavam erro no meio da decisão; com isso, testar
"a revisão estourou o teto?" exigia disco e captura de stdout. Agora elas
**descrevem** o que deve acontecer e quem chama executa:

    decisao = rotas.pos_revisao(state)
    # -> Decisao(destino="aprovacao_humana", teto=Teto("revisao", 2), aviso="...")

A ordem em que o chamador aplica importa e é a de antes: registrar o teto,
imprimir o aviso, levantar o erro.
"""
from dataclasses import dataclass
from typing import Any, Mapping

# Orçamento de rodadas. `tentativas` é o teto geral, compartilhado; cada sinal
# caro tem o seu, para não consumir sozinho o que é do pytest.
MAX_TENTATIVAS = 3
MAX_REPLANEJAMENTOS = 2
MAX_TESTES = 2                # reescritas da suíte por rodada de desenvolvimento
MAX_REVISOES = 2              # rodadas que a opinião do revisor pode custar
MAX_PENTEST = 2               # rodadas que uma reprovação de pentest pode custar
MAX_VISUAL = 2                # rodadas que uma reprovação de renderização pode custar

# Origens de feedback que reprovam **código**, não suíte: a correção muda o
# código, então os testes precisam rodar de novo, não ser reescritos.
ORIGENS_QUE_PRESERVAM_A_SUITE = frozenset({"revisao", "pentest", "visual"})

Estado = Mapping[str, Any]


@dataclass(frozen=True)
class Teto:
    """Circuit breaker disparado: o laço parou por orçamento, não por mérito."""
    laco: str
    limite: int


@dataclass(frozen=True)
class Decisao:
    destino: str | None = None   # próximo nó; None quando `erro` está preenchido
    teto: Teto | None = None     # a registrar antes de seguir
    aviso: str | None = None     # linha para o operador ver no terminal
    erro: str | None = None      # condição fatal: o chamador levanta


def e_teste_padrao(caminho: str) -> bool:
    """Convenção Python: suíte em `tests/`.

    Parâmetro em vez de constante porque a convenção é da stack, não do
    domínio — Java põe em `src/test/java/` e TypeScript costuma colocar o teste
    ao lado do módulo.
    """
    return caminho.startswith("tests/")


def pos_validacao_spec(state: Estado) -> Decisao:
    if state.get("spec_coerente"):
        return Decisao(destino="desenvolvimento")
    if state.get("spec_tentativas", 0) > MAX_REPLANEJAMENTOS:
        return Decisao(
            teto=Teto("planejamento", MAX_REPLANEJAMENTOS),
            erro=(
                "Planejamento produziu specs incoerentes com o pedido "
                f"{MAX_REPLANEJAMENTOS + 1} vezes seguidas. Interrompendo para "
                "evitar desperdício de tokens — revise o modelo ou o prompt."
            ),
        )
    return Decisao(destino="planejamento")


def pos_desenvolvimento(state: Estado, e_teste=e_teste_padrao) -> Decisao:
    """Roteamento seletivo: a rodada nascida de reprovação de revisão não
    reescreve a suíte.

    O código mudou, então os testes precisam *rodar* de novo — não ser
    *escritos* de novo. `escrever_testes` é o nó mais caro do grafo (8 min
    medidos), e repagá-lo por um apontamento de legibilidade foi metade do
    custo da execução que motivou esta mudança. Ir direto ao pytest também
    pula o guard de critérios, que julgaria uma suíte inalterada.

    As redes já existentes cobrem o risco: se a correção quebrar a suíte, o
    pytest fica vermelho e a rodada volta ao desenvolvimento com o stack trace;
    se adicionar código sem teste, o piso de cobertura devolve ao QA.
    """
    origem = state.get("origem_feedback")
    tem_suite = any(e_teste(a) for a in state.get("arquivos", []))
    if origem in ORIGENS_QUE_PRESERVAM_A_SUITE and tem_suite:
        return Decisao(
            destino="executar_testes",
            aviso=f">>> Correção de {origem}: suíte preservada, indo direto ao pytest.",
        )
    return Decisao(destino="escrever_testes")


def pos_validacao_testes(state: Estado) -> Decisao:
    """Suíte que não adere aos critérios volta para o QA reescrever. Ao estourar
    o teto, segue mesmo assim: qualidade de teste é sinal mais brando que teste
    vermelho, e fica registrado no estado para o gate humano e as métricas."""
    if state.get("testes_aderentes"):
        return Decisao(destino="executar_testes")
    if state.get("testes_tentativas", 0) >= MAX_TESTES:
        return Decisao(
            destino="executar_testes",
            teto=Teto("escrever_testes", MAX_TESTES),
            aviso=">>> Guard de critérios: teto de reescritas atingido, seguindo assim mesmo.",
        )
    return Decisao(destino="escrever_testes")


def pos_testes(state: Estado) -> Decisao:
    if not state.get("testes_ok"):
        if state["tentativas"] >= MAX_TENTATIVAS:
            # Circuit breaker: humano decide o que fazer com o trabalho reprovado.
            return Decisao(
                destino="aprovacao_humana", teto=Teto("correcao", MAX_TENTATIVAS)
            )
        return Decisao(destino="desenvolvimento")
    # Verdes, mas sem exercitar a entrega: problema do teste — laço curto, só o
    # QA reescreve, sem pagar outra rodada de desenvolvimento.
    if not state.get("cobertura_ok") and state.get("testes_tentativas", 0) < MAX_TESTES:
        return Decisao(destino="escrever_testes")
    return Decisao(destino="revisao")


def pos_revisao(state: Estado) -> Decisao:
    """Teto próprio para a opinião do revisor.

    `tentativas` é orçamento compartilhado com falha de teste. Sem um teto
    separado, o sinal caro e subjetivo (revisão) consome sozinho as rodadas
    reservadas ao sinal barato e determinístico (pytest vermelho). Ao estourar,
    o gate humano decide — com o relatório em mãos.

    Aprovado pela revisão, o próximo juiz é a execução ofensiva, não o gate
    humano direto: testes verdes e revisão limpa não provam que a entrega
    resiste a ataque.
    """
    if state.get("aprovado"):
        return Decisao(destino="pentest")
    if state.get("revisao_tentativas", 0) >= MAX_REVISOES:
        return Decisao(
            destino="aprovacao_humana",
            teto=Teto("revisao", MAX_REVISOES),
            aviso=(
                f">>> Revisão reprovou {MAX_REVISOES}x: teto de rodadas por "
                "opinião atingido, levando ao gate humano."
            ),
        )
    if state["tentativas"] >= MAX_TENTATIVAS:
        return Decisao(
            destino="aprovacao_humana", teto=Teto("correcao", MAX_TENTATIVAS)
        )
    return Decisao(destino="desenvolvimento")


def pos_pentest(state: Estado) -> Decisao:
    """Teto próprio para o pentest, pela mesma razão do teto da revisão.

    Bloqueante e com orçamento — volta ao desenvolvimento com o brief de
    correção. Sem orçamento (de pentest ou do laço geral), o gate humano decide,
    com o relatório em mãos: pode haver falha explorável que a auto-remediação
    não fechou, e publicar às cegas é o pior caminho.
    """
    if state.get("pentest_ok"):
        return Decisao(destino="visual")
    if state.get("pentest_tentativas", 0) >= MAX_PENTEST:
        return Decisao(
            destino="aprovacao_humana",
            teto=Teto("pentest", MAX_PENTEST),
            aviso=(
                f">>> Pentest reprovou {MAX_PENTEST}x: teto de remediações "
                "atingido, levando ao gate humano com o relatório."
            ),
        )
    if state["tentativas"] >= MAX_TENTATIVAS:
        return Decisao(
            destino="aprovacao_humana", teto=Teto("correcao", MAX_TENTATIVAS)
        )
    return Decisao(destino="desenvolvimento")


def pos_visual(state: Estado) -> Decisao:
    """Teto próprio, pela mesma razão do teto do pentest.

    Ao estourar, o gate humano decide com os achados em mãos — contraste
    insuficiente é defeito real, mas não é motivo para queimar a execução
    inteira em rodadas de CSS.
    """
    if state.get("visual_ok"):
        return Decisao(destino="aprovacao_humana")
    if state.get("visual_tentativas", 0) >= MAX_VISUAL:
        return Decisao(
            destino="aprovacao_humana",
            teto=Teto("visual", MAX_VISUAL),
            aviso=(
                f">>> Visual reprovou {MAX_VISUAL}x: teto de correções atingido, "
                "levando ao gate humano com os achados."
            ),
        )
    if state["tentativas"] >= MAX_TENTATIVAS:
        return Decisao(
            destino="aprovacao_humana", teto=Teto("correcao", MAX_TENTATIVAS)
        )
    return Decisao(destino="desenvolvimento")
