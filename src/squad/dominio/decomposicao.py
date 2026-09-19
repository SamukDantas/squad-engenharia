"""Quebrar o pedido em serviços: o que é uma decomposição válida.

É a decisão que abre o paralelismo, e por isso é a mais cara de errar. Um nome
repetido faz dois ramos escreverem no mesmo workspace; um ciclo de dependências
torna impossível congelar o contrato numa passada só; uma lista de vinte
serviços afoga a máquina em containers antes de qualquer um deles compilar.
Nenhum desses erros aparece na hora — todos aparecem no meio da execução, depois
de o planejamento já ter sido pago.

Funções puras. Quem lê a saída do LLM e monta os dicionários é o nó do grafo;
aqui só se julga o que chegou.
"""
import json
import re
from dataclasses import dataclass, field

# Teto de largura do fan-out. Não é limite de arquitetura, é limite de máquina:
# cada ramo mantém um container de build e um de teste, e a stack Java pede
# 2,5 GB por jaula. Uma decomposição mais larga que isto é sinal de que o pedido
# precisa ser quebrado em execuções, não de que a squad deve tentar.
MAX_SERVICOS = 6

# Nome que sobrevive a virar diretório, nome de container, branch e repositório.
_NOME_VALIDO = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

LIMITE_NOME = 40


@dataclass(frozen=True)
class Servico:
    """Um ramo do fan-out."""
    nome: str
    responsabilidade: str
    stack: str
    depende_de: tuple[str, ...] = field(default=())


def _normalizar_nome(bruto: object) -> str:
    """Aceita `Room Service`, `room_service` e `ROOM-SERVICE` como o mesmo nome.

    Normalizar em vez de reprovar porque a fonte é um LLM: recusar uma
    decomposição correta pelo uso de maiúscula custaria uma rodada inteira de
    planejamento para corrigir um hífen.
    """
    texto = str(bruto or "").strip().lower()
    texto = re.sub(r"[\s_]+", "-", texto)
    texto = re.sub(r"[^a-z0-9-]+", "", texto)
    return re.sub(r"-{2,}", "-", texto).strip("-")[:LIMITE_NOME]


def normalizar(bruto: object, stacks_validas: frozenset[str]) -> tuple[list[Servico], list[str]]:
    """Converte a decomposição crua em serviços, com os erros que impedem seguir.

    Devolve `(servicos, erros)`. Lista de erros vazia é aprovação; com erro, os
    serviços devolvidos servem só para compor a mensagem, não para executar.
    """
    erros: list[str] = []
    if not isinstance(bruto, list) or not bruto:
        return [], ["a decomposição não trouxe serviço nenhum"]
    if len(bruto) > MAX_SERVICOS:
        erros.append(
            f"a decomposição trouxe {len(bruto)} serviços; o teto é "
            f"{MAX_SERVICOS} — quebre o pedido em execuções"
        )

    servicos: list[Servico] = []
    vistos: set[str] = set()
    for i, item in enumerate(bruto, 1):
        if not isinstance(item, dict):
            erros.append(f"o serviço {i} não é um objeto")
            continue
        nome = _normalizar_nome(item.get("nome") or item.get("name"))
        if not nome or not _NOME_VALIDO.match(nome):
            erros.append(f"o serviço {i} não tem nome utilizável ({item.get('nome')!r})")
            continue
        if nome in vistos:
            # Dois ramos com o mesmo nome dividiriam workspace, containers e
            # arquivo de métricas — e um sobrescreveria o outro em silêncio.
            erros.append(f"o nome '{nome}' aparece mais de uma vez")
            continue
        vistos.add(nome)

        stack = str(item.get("stack") or "").strip().lower()
        if stack not in stacks_validas:
            erros.append(
                f"o serviço '{nome}' pede a stack '{stack}', que não existe "
                f"(disponíveis: {', '.join(sorted(stacks_validas))})"
            )
            continue

        depende = item.get("depende_de") or item.get("depends_on") or []
        if isinstance(depende, str):
            depende = [depende]
        servicos.append(Servico(
            nome=nome,
            responsabilidade=" ".join(str(item.get("responsabilidade") or "").split()),
            stack=stack,
            depende_de=tuple(
                d for d in (_normalizar_nome(x) for x in depende) if d
            ),
        ))

    conhecidos = {s.nome for s in servicos}
    for s in servicos:
        for alvo in s.depende_de:
            if alvo == s.nome:
                erros.append(f"o serviço '{s.nome}' depende de si mesmo")
            elif alvo not in conhecidos:
                erros.append(
                    f"o serviço '{s.nome}' depende de '{alvo}', que não foi decomposto"
                )

    if servicos and ordem_topologica(servicos) is None:
        erros.append(
            "as dependências entre os serviços formam um ciclo — sem uma ordem, "
            "não há como congelar o contrato antes de construir"
        )

    if not servicos and not erros:
        erros.append("a decomposição não trouxe serviço nenhum utilizável")
    return servicos, erros


def ordem_topologica(servicos: list[Servico]) -> list[str] | None:
    """Ordem em que os contratos se resolvem: quem não depende de ninguém vem
    primeiro. `None` quando há ciclo.

    O paralelismo não precisa desta ordem para *executar* — os ramos rodam todos
    ao mesmo tempo. Ela existe porque o contrato precisa ser redigido numa
    passada só, e com ciclo isso é impossível: A não pode ser escrito sabendo de
    B enquanto B espera A.
    """
    pendentes = {s.nome: set(s.depende_de) & {x.nome for x in servicos} for s in servicos}
    ordem: list[str] = []
    while pendentes:
        # Empate resolvido pelo nome: a ordem precisa ser estável entre
        # execuções para que dois históricos sejam comparáveis.
        livres = sorted(n for n, deps in pendentes.items() if not deps)
        if not livres:
            return None
        for nome in livres:
            del pendentes[nome]
            ordem.append(nome)
        for deps in pendentes.values():
            deps.difference_update(livres)
    return ordem


def resumo(servicos: list[Servico]) -> str:
    """Linha por serviço, para o terminal e para o prompt do contrato."""
    linhas = []
    for s in servicos:
        deps = f" (depende de {', '.join(s.depende_de)})" if s.depende_de else ""
        linhas.append(f"- {s.nome} [{s.stack}]: {s.responsabilidade}{deps}")
    return "\n".join(linhas)


# Modelo enfeita a resposta: cerca o JSON em bloco de código, escreve um
# parágrafo antes, fecha com uma conclusão. O roteamento não pode depender do
# formato exato da saída (RESILIENCIA.md, item 7) — mas tolerar o enfeite não é
# aceitar qualquer coisa: o que não for uma lista JSON legível vira reprova.
_CERCA = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extrair_lista(bruto: object) -> list | None:
    """A lista de serviços dentro do texto que o modelo devolveu.

    `None` quando não há nada parseável — que quem chama trata como reprova, não
    como lista vazia. A diferença importa: lista vazia é uma decomposição que
    decidiu não decompor; `None` é uma resposta que não deu para ler.
    """
    texto = str(bruto or "").strip()
    if not texto:
        return None

    candidatos = [c.strip() for c in _CERCA.findall(texto)]
    candidatos.append(texto)
    # Último recurso: o maior trecho entre colchetes, para a resposta que traz o
    # JSON no meio de um parágrafo.
    inicio, fim = texto.find("["), texto.rfind("]")
    if 0 <= inicio < fim:
        candidatos.append(texto[inicio:fim + 1])

    for candidato in candidatos:
        try:
            dados = json.loads(candidato)
        except ValueError:
            continue
        if isinstance(dados, list):
            return dados
        # `{"servicos": [...]}` é uma forma que o modelo escolhe sozinho com
        # frequência, e recusá-la custaria uma rodada por uma chave a mais.
        if isinstance(dados, dict):
            for chave in ("servicos", "services", "microsservicos", "microservices"):
                if isinstance(dados.get(chave), list):
                    return dados[chave]
    return None


def pedido_do_servico(pedido: str, servico: "Servico", contratos: str) -> str:
    """O pedido que chega ao ramo: o original, o recorte dele, e o contrato.

    O original inteiro vai junto de propósito. Mandar só a responsabilidade
    ("dono do Agregado Salas") deixaria o ramo sem o domínio em que ela faz
    sentido, e a spec sairia genérica — o guard de aderência reprovaria, e a
    culpa pareceria do planejamento.

    O contrato entra marcado como congelado porque é a única parte que o ramo
    não pode renegociar: ele foi acordado com times que estão trabalhando neste
    exato momento e não têm como ser avisados.
    """
    partes = [pedido.strip(), "", "--- Seu escopo nesta execucao ---",
              f"Voce constroi APENAS o microsservico `{servico.nome}`."]
    if servico.responsabilidade:
        partes.append(f"Responsabilidade: {servico.responsabilidade}")
    if servico.depende_de:
        partes.append(
            "Ele consome: " + ", ".join(servico.depende_de)
            + ". Esses servicos estao sendo construidos por outros times AGORA, "
            "em paralelo. Nao os implemente: programe contra o contrato abaixo."
        )
    partes.append(
        "Os demais servicos do pedido NAO sao seus. Nao escreva codigo deles."
    )
    if contratos.strip():
        partes += ["", "--- Contrato entre os servicos (congelado) ---",
                   "Os nomes de campo e as rotas abaixo foram acordados antes de o "
                   "trabalho comecar e NAO podem ser alterados: os outros times ja "
                   "estao programando contra eles.", "", contratos.strip()]
    return "\n".join(partes)
