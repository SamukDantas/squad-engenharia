"""O contrato foi cumprido? A pergunta que só existe com N serviços.

O pipeline de um serviço não tinha como fazê-la: não havia com quem se
desencontrar. Com três times paralelos, há uma classe de defeito nova e
invisível para todos os outros juízes — `room-service` devolve `capacity`,
`event-service` espera `totalSeats`, cada um passa nos próprios testes, e a
integração quebra na primeira chamada real. Testes verdes, revisão aprovada,
pentest limpo, renderização legível. E o sistema não funciona.

O que se mede aqui é **nível de símbolo**: o nome que o contrato deu ao campo e
à rota aparece no código de quem o contrato disse que o usaria? Não é semântica
— não se verifica se o campo significa a mesma coisa dos dois lados. É barato,
determinístico, e pega o desencontro de nome, que é o caso comum. O sentido fica
com o revisor, que recebe o contrato como critério.

Funções puras: quem lê os workspaces é `adaptadores/contratos.py`.
"""
import re
from dataclasses import dataclass

# Rota HTTP citada no contrato: `GET /rooms/{id}`.
_ROTA = re.compile(
    r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[A-Za-z0-9_\-/{}.:]*)", re.IGNORECASE
)

# Nome de campo entre crases. Exige camelCase ou duas palavras coladas para não
# transformar cada `id` e cada `int` do texto num requisito — termo curto e
# genérico aparece em qualquer código e o achado seria sempre verde à toa.
_CAMEL = r"[a-z][a-z0-9]*(?:[A-Z][a-zA-Z0-9]*)+"
# Campo DEFINIDO: `nome: tipo`, o formato que o prompt do contrato pede para
# cada campo de requisição e de resposta. Só ele vira símbolo cobrado.
_CAMPO_DEFINIDO = re.compile(rf"`({_CAMEL})\s*:\s*[^`]+`")
# Qualquer camelCase entre crases: a regra antiga, que só vale como recuo para
# contrato que não define campo nenhum no formato acima. Medido na execução
# paralela `eaad3461`: ela pegou `camelCase` ("JSON com campos em `camelCase`")
# e `sessionId` ("`allocationId` — igual ao `sessionId`"), palavras da prosa,
# e reteve o event-service por dois campos que nenhum serviço devia ter.
_CAMPO = re.compile(rf"`({_CAMEL})`")

# Cabeçalho markdown: abre uma seção nova, e é onde os serviços da seção são
# nomeados com mais frequência.
_TITULO = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$", re.MULTILINE)

# Trecho variável de rota — `{id}` casa com qualquer coisa no código gerado.
_PARAMETRO = re.compile(r"\{[^}]*\}")

# O prompt do contrato manda dizer, por chamada, quem responde. Quando a linha
# existe, ela resolve a pergunta que a primeira versão errava: de QUEM cobrar o
# símbolo. Exigir de todos os serviços da seção reprovou um consumidor por não
# usar um campo da resposta que ele legitimamente ignora.
_QUEM_RESPONDE = re.compile(
    r"quem\s+responde\s*:\s*`?([A-Za-z0-9][A-Za-z0-9-]*)`?", re.IGNORECASE
)


@dataclass(frozen=True)
class Simbolo:
    """Um termo do contrato, os serviços da seção e quem deve expô-lo.

    `provedor` é quem o contrato disse que responde. Quando existe, é dele que o
    símbolo é cobrado — e só dele. Sem ele, basta que **algum** dos serviços da
    seção use o termo: cobrar de todos transformaria cada campo de resposta num
    requisito para o consumidor, que pode ignorá-lo sem violar nada.
    """
    termo: str
    tipo: str            # "rota" | "campo"
    servicos: tuple[str, ...]
    provedor: str | None = None


@dataclass(frozen=True)
class Achado:
    tipo: str
    termo: str
    servico: str
    detalhe: str


def _secoes(texto: str) -> list[str]:
    """Quebra o contrato em seções por cabeçalho markdown.

    A seção é a unidade de atribuição: o prompt do contrato pede uma por chamada
    entre serviços, dizendo quem chama e quem responde. Sem cabeçalho nenhum, o
    documento inteiro é uma seção — e aí os símbolos valem para todos os
    serviços citados nele.
    """
    cortes = [m.start() for m in _TITULO.finditer(texto)]
    if not cortes:
        return [texto] if texto.strip() else []
    partes = []
    if cortes[0] > 0:
        partes.append(texto[:cortes[0]])
    for i, inicio in enumerate(cortes):
        fim = cortes[i + 1] if i + 1 < len(cortes) else len(texto)
        partes.append(texto[inicio:fim])
    return [p for p in partes if p.strip()]


def simbolos_do_contrato(texto: str, servicos: list[str]) -> list[Simbolo]:
    """Os termos que o contrato define, com os serviços de cada um.

    Um símbolo cujo trecho não nomeia serviço nenhum conhecido é descartado: sem
    saber de quem cobrar, o achado não teria a quem mandar corrigir.
    """
    envolvidos_por: dict[tuple[str, str], set[str]] = {}
    provedor_por: dict[tuple[str, str], str] = {}
    campos = _CAMPO_DEFINIDO if _CAMPO_DEFINIDO.search(texto or "") else _CAMPO
    for secao in _secoes(texto or ""):
        baixa = secao.lower()
        envolvidos = tuple(s for s in servicos if s.lower() in baixa)
        if not envolvidos:
            continue
        achado = _QUEM_RESPONDE.search(secao)
        provedor = None
        if achado:
            nome = achado.group(1).lower()
            provedor = next((s for s in servicos if s.lower() == nome), None)

        termos = [(rota.rstrip("/") or "/", "rota") for _, rota in _ROTA.findall(secao)]
        termos += [(campo, "campo") for campo in campos.findall(secao)]
        for chave in termos:
            envolvidos_por.setdefault(chave, set()).update(envolvidos)
            if provedor:
                provedor_por.setdefault(chave, provedor)

    return [
        Simbolo(
            termo=termo, tipo=tipo,
            servicos=tuple(sorted(quem)),
            provedor=provedor_por.get((termo, tipo)),
        )
        for (termo, tipo), quem in sorted(envolvidos_por.items())
    ]


def _segmentos_fixos(rota: str) -> list[str]:
    """Os pedaços literais de uma rota, sem as barras.

    Comparar `/rooms/` como substring foi o erro da primeira versão: o Spring
    parte a rota entre `@RequestMapping("/rooms")` na classe e
    `@PostMapping("/{roomId}/allocations")` no método, então `/rooms` existe no
    código e `/rooms/` não. Medido numa execução real, isso reprovou três
    serviços que honravam o contrato.
    """
    return [
        p for p in _PARAMETRO.sub("/", rota).split("/") if p and not p.startswith("?")
    ]


def _presente(termo: str, tipo: str, codigo: str) -> bool:
    if tipo != "rota":
        return termo in codigo
    # `/rooms/{id}` vira `{id}` no Spring, `:id` no Express, ou um parâmetro à
    # parte. O que se cobra são os segmentos fixos, cada um por si — é o que
    # sobrevive a como cada framework escreve a rota.
    fixos = _segmentos_fixos(termo)
    return all(seg in codigo for seg in fixos) if fixos else termo in codigo


def conferir(simbolos: list[Simbolo], codigo_por_servico: dict[str, str]) -> list[Achado]:
    """O contrato foi honrado por quem devia honrá-lo?

    Duas regras, e a diferença entre elas foi medida numa execução real:

    - com **provedor** identificado, o símbolo é cobrado só dele. Cobrar de todos
      os serviços da seção reprovava o consumidor por não usar um campo da
      resposta que ele legitimamente ignora;
    - sem provedor, basta que **algum** serviço da seção use o termo — o que
      ainda pega o caso de ninguém ter implementado, sem inventar violação.

    Lista vazia é aprovação. Serviço ausente do mapa é ignorado: numa execução
    com falha parcial, cobrar o contrato de quem nem foi construído produziria
    achados sobre um serviço que ninguém pode corrigir agora.
    """
    achados: list[Achado] = []
    for s in simbolos:
        presentes = {
            nome: _presente(s.termo, s.tipo, codigo_por_servico[nome])
            for nome in s.servicos
            if nome in codigo_por_servico
        }
        if not presentes:
            continue
        o_que = "a rota" if s.tipo == "rota" else "o campo"

        if s.provedor and s.provedor in presentes:
            if not presentes[s.provedor]:
                achados.append(Achado(
                    tipo=f"{s.tipo}_ausente", termo=s.termo, servico=s.provedor,
                    detalhe=(
                        f"o contrato diz que {s.provedor} responde {o_que} "
                        f"`{s.termo}`, mas ela não aparece no código dele"
                    ),
                ))
        elif not any(presentes.values()):
            achados.append(Achado(
                tipo=f"{s.tipo}_ausente", termo=s.termo,
                servico=sorted(presentes)[0],
                detalhe=(
                    f"o contrato define {o_que} `{s.termo}` entre "
                    f"{' e '.join(s.servicos)}, e ela não aparece no código de "
                    "nenhum deles"
                ),
            ))
    return achados


def formatar_feedback(achados: list[Achado], servico: str) -> str:
    """Brief de correção de um serviço só — é a ele que o feedback volta."""
    meus = [a for a in achados if a.servico == servico]
    if not meus:
        return ""
    linhas = [
        "A verificação de integração reprovou a entrega. O contrato entre os "
        "serviços foi congelado ANTES de o trabalho começar e os outros times "
        "já programaram contra ele — quem se ajusta é este serviço:",
        "",
    ]
    for i, a in enumerate(meus, 1):
        linhas.append(f"{i}. [{a.tipo}] {a.detalhe}.")
    return "\n".join(linhas)
