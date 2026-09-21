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

from . import vereditos

# Orçamento de rodadas. `tentativas` é o teto geral, compartilhado; cada sinal
# caro tem o seu, para não consumir sozinho o que é do pytest.
MAX_TENTATIVAS = 3
MAX_REPLANEJAMENTOS = 2
MAX_DECOMPOSICOES = 2         # rodadas que uma decomposição inválida pode custar
MAX_TESTES = 2                # reescritas da suíte por rodada de desenvolvimento
MAX_REVISOES = 2              # rodadas que a opinião do revisor pode custar
MAX_PENTEST = 2               # rodadas que uma reprovação de pentest pode custar
MAX_VISUAL = 2                # rodadas que uma reprovação de renderização pode custar
MAX_AMBIENTES = 2             # rodadas que uma configuração de ambiente errada pode custar
MAX_REVISOES_SUITE = 1        # revisões da suíte pelo QA quando o dev não muda nada

# Origens de feedback que reprovam **código**, não suíte: a correção muda o
# código, então os testes precisam rodar de novo, não ser reescritos.
ORIGENS_QUE_PRESERVAM_A_SUITE = frozenset(
    {"revisao", "pentest", "visual", "ambientes"}
)

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
    # Rodada de correção que não mudou nada, com o vermelho vindo de uma
    # asserção: o erro pode ser do teste (guards.destino_da_correcao_inerte).
    inerte = state.get("correcao_inerte")
    if inerte == "revisar_suite":
        return Decisao(
            destino="escrever_testes",
            aviso=(
                ">>> Correção sem mudança num vermelho de asserção: o teste pode "
                "estar errado. A suíte volta ao QA para revisão."
            ),
        )
    if inerte == "aprovacao_humana":
        return Decisao(
            destino="aprovacao_humana",
            teto=Teto("suite_contestada", MAX_REVISOES_SUITE),
            aviso=(
                ">>> Impasse entre desenvolvimento e QA sobre o mesmo teste: "
                "gate humano com o diagnóstico."
            ),
        )

    origem = state.get("origem_feedback")
    tem_suite = any(e_teste(a) for a in state.get("arquivos", []))
    if origem in ORIGENS_QUE_PRESERVAM_A_SUITE and tem_suite:
        return Decisao(
            destino="executar_testes",
            aviso=f">>> Correção de {origem}: suíte preservada, indo direto ao pytest.",
        )
    if origem == "testes" and tem_suite and _suite_foi_o_juiz(state):
        return Decisao(
            destino="executar_testes",
            aviso=">>> Correção de teste vermelho: suíte preservada, indo direto ao pytest.",
        )
    return Decisao(destino="escrever_testes")


def _suite_foi_o_juiz(state: Estado) -> bool:
    """A suíte rodou e julgou o código — então ela sobrevive à correção.

    Reescrever a suíte a cada rodada nascida de teste vermelho era o maior
    custo medido do grafo: na calculadora de juros (thread `00b92498`), três
    passadas do QA somaram 52 dos 73 min de nós, e só a primeira escreveu algo
    que faltava. O código mudou; a suíte precisa *rodar* de novo, não ser
    *escrita* de novo — o mesmo argumento do item 29, que valia para revisão e
    não tinha sido estendido ao pytest porque um teste errado não tinha rota de
    volta ao QA. Agora tem: se a correção não mexer no código, a suíte
    contestada volta ao QA (guards.destino_da_correcao_inerte).

    Vale para asserção e para build quebrado. Import e coleta ficam de fora: aí
    o teste costuma apontar para o que não existe, e quem conserta é o QA.
    """
    return bool(state.get("falha_de_build")) or vereditos.falha_de_assercao(
        state.get("saida_testes", "")
    )


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
        # A mesma asserção sobreviveu a uma correção: o teste é o suspeito, e
        # outra rodada de desenvolvimento só repetiria a tentativa
        # (guards.falha_repetida). Vai ao QA mesmo com o orçamento de correção
        # no fim: a revisão da suíte não gasta rodada de desenvolvimento.
        if state.get("suite_contestada"):
            return Decisao(
                destino="escrever_testes",
                aviso=(
                    ">>> Os mesmos testes falharam antes e depois da correção: "
                    "a suíte volta ao QA para revisão."
                ),
            )
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
    # Antes da revisão, e não depois, porque é o juiz mais barato que ainda não
    # falou: lê arquivo, sem container e sem LLM. Reprovar aqui poupa uma
    # revisão inteira sobre uma entrega que voltaria ao dev de qualquer jeito.
    return Decisao(destino="config_ambientes")


def pos_revisao(state: Estado) -> Decisao:
    """Teto próprio para a opinião do revisor.

    `tentativas` é orçamento compartilhado com falha de teste. Sem um teto
    separado, o sinal caro e subjetivo (revisão) consome sozinho as rodadas
    reservadas ao sinal barato e determinístico (pytest vermelho). Ao estourar,
    o gate humano decide — com o relatório em mãos.

    Aprovado pela revisão, o próximo juiz é a execução ofensiva, não o gate
    humano direto: testes verdes e revisão limpa não provam que a entrega
    resiste a ataque. A verificação visual já falou antes da revisão.
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
        return Decisao(destino="aprovacao_humana")
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
    """A verificação visual vem ANTES da revisão.

    É o juiz mais barato que ainda não falou: determinístico, sem token, uns
    40 s. Rodava por último, depois da revisão e do pentest, e a reprova dela
    chegava com o orçamento de correção esgotado — medido no dashboard
    Next.js (thread `af83302f`): 3 rodadas, e o `fundo_nao_declarado` foi
    parar no gate. Antes da revisão, uma reprova visual também poupa uma
    revisão inteira sobre um CSS que vai mudar.

    Ao estourar o teto próprio, segue para a revisão, como os ambientes:
    contraste insuficiente é defeito real, mas não invalida o julgamento do
    revisor, e os achados ficam no estado para o gate humano ver.
    """
    if state.get("visual_ok"):
        return Decisao(destino="revisao")
    if state.get("visual_tentativas", 0) >= MAX_VISUAL:
        return Decisao(
            destino="revisao",
            teto=Teto("visual", MAX_VISUAL),
            aviso=(
                f">>> Visual reprovou {MAX_VISUAL}x: teto de correções atingido, "
                "seguindo para a revisão com os achados no estado."
            ),
        )
    if state["tentativas"] >= MAX_TENTATIVAS:
        return Decisao(
            destino="aprovacao_humana", teto=Teto("correcao", MAX_TENTATIVAS)
        )
    return Decisao(destino="desenvolvimento")


def pos_config_ambientes(state: Estado) -> Decisao:
    """Teto próprio, pela mesma razão do teto do visual.

    A stack que não exige configuração por ambiente chega aqui com
    `ambientes_ok=True` e passa direto — o nó existe sempre no grafo, como o
    pentest e o visual, e é o perfil que decide se ele tem algo a dizer.

    Ao estourar o teto segue adiante (visual, depois revisão) em vez de ir ao
    gate: diferente de teste vermelho, uma configuração errada não invalida o
    julgamento dos juízes seguintes, e o achado fica no estado para o gate
    humano ver.
    """
    if state.get("ambientes_ok", True):
        return Decisao(destino="visual")
    if state.get("ambientes_tentativas", 0) >= MAX_AMBIENTES:
        return Decisao(
            destino="visual",
            teto=Teto("ambientes", MAX_AMBIENTES),
            aviso=(
                f">>> Ambientes reprovaram {MAX_AMBIENTES}x: teto atingido, "
                "seguindo com os achados no estado."
            ),
        )
    if state["tentativas"] >= MAX_TENTATIVAS:
        return Decisao(
            destino="aprovacao_humana", teto=Teto("correcao", MAX_TENTATIVAS)
        )
    return Decisao(destino="desenvolvimento")


def pos_decomposicao(state: Estado) -> Decisao:
    """A decomposição precisa ser válida antes de virar N ramos.

    Reprovar aqui é barato — uma chamada de crew. Deixar passar não é: um nome
    repetido faz dois ramos escreverem no mesmo workspace, e o sintoma aparece
    como container recusado por nome duplicado, muito longe da causa.
    """
    if not state.get("erros_decomposicao"):
        return Decisao(destino="contratos")
    if state.get("servicos_tentativas", 0) > MAX_DECOMPOSICOES:
        return Decisao(
            teto=Teto("decomposicao", MAX_DECOMPOSICOES),
            erro=(
                f"A decomposição do pedido falhou {MAX_DECOMPOSICOES + 1} vezes "
                "seguidas. Interrompendo para evitar desperdício de tokens. "
                f"Último motivo: {state['erros_decomposicao']}"
            ),
        )
    return Decisao(
        destino="decomposicao",
        aviso=f">>> Decomposição inválida, refazendo: {state['erros_decomposicao']}",
    )


# Nós que retomam sem falar com o provedor: o gate espera um humano, e o
# deploy compila e publica. Uma thread parada ali não precisa de login.
NOS_SEM_LLM = frozenset({"aprovacao_humana", "deploy", "gate", "deploy_servico"})


def retomada_chama_llm(proximos) -> bool:
    """Se o que falta de uma thread retomada passa por nó que fala com o provedor.

    A checagem de credencial nasceu para uma execução não morrer no
    planejamento com `Connection error` quando a sessão expirou. Aplicada a
    toda retomada, ela barrava também a publicação de uma entrega já pronta,
    parada no gate — onde não há chamada de LLM nenhuma: medido ao publicar a
    thread `055e0f17`, com a sessão Keycloak expirada. Thread sem próximo nó
    (já terminada) também não precisa.
    """
    return bool(proximos) and not set(proximos) <= NOS_SEM_LLM
