"""Leitura da spec: o que é requisito e o que é sugestão do arquiteto.

Desde o item 37 do RESILIENCIA.md, a spec tem duas seções: "## Requisitos",
rastreável a uma frase do pedido, e "## Decisões de implementação",
sugestões que a entrega pode não seguir. Quem julga a entrega precisa ver só a
primeira; ver as duas faz a sugestão virar cobrança.
"""
import re

_SECAO = re.compile(r"^##\s+(.+?)\s*$", re.M)


def requisitos(spec: str) -> str:
    """A seção "## Requisitos" da spec, ou a spec inteira se ela não existir.

    Sem a seção (spec anterior ao formato, ou arquiteto que não o seguiu), a
    spec inteira é o melhor que há: julgar contra nada seria pior.
    """
    texto = spec or ""
    titulos = list(_SECAO.finditer(texto))
    for i, titulo in enumerate(titulos):
        if titulo.group(1).strip().lower() == "requisitos":
            fim = titulos[i + 1].start() if i + 1 < len(titulos) else len(texto)
            return texto[titulo.start():fim].strip()
    return texto.strip()


def reavaliacao_do_guard(lacunas_anteriores: str) -> str:
    """O trecho do prompt do guard de critérios numa reavaliação.

    Sem memória, cada avaliação procurava lacunas do zero e achava outras: na
    validação `eb149520` (dashboard), a primeira reprova pediu "tabelas" e
    "data de fechamento"; o QA cobriu, e a segunda pediu "funil vazio". O QA
    perseguia um alvo que se movia até bater o teto. É o mesmo defeito que o
    revisor teve (RESILIENCIA.md, item 27), com o mesmo remédio: o juiz vê o
    que ele mesmo cobrou e confere ISSO.
    """
    if not (lacunas_anteriores or "").strip():
        return ""
    return (
        "\n\nESTA É UMA REAVALIAÇÃO. Na avaliação anterior você apontou estas "
        "lacunas, e o QA complementou a suíte para cobri-las:\n"
        f"{lacunas_anteriores.strip()}\n"
        "Responda SIM se ESSAS lacunas estão cobertas agora. Não procure "
        "lacunas novas nem refine o que já tem teste: só aponte algo além da "
        "lista acima se for um critério de aceite dos requisitos sem NENHUM "
        "teste. Mudar a exigência a cada avaliação faz a suíte nunca "
        "convergir."
    )


MAX_ITENS_CRITERIOS = 20


def pedido_da_lista_de_criterios(requisitos: str, ambiente_testes: str) -> str:
    """O prompt que faz o guard escrever, ANTES do QA, o que ele vai cobrar.

    O QA e o guard liam os mesmos requisitos de jeitos diferentes: o QA
    escolhia o que testar, o guard tratava cada item como verificação
    obrigatória, e a diferença só aparecia na reprova — uma passada inteira do
    QA (150 a 270 mil tokens). Nas 7 últimas execuções, o primeiro veredito do
    guard foi NAO em todas; os temas repetidos foram o requisito universal
    ("funil vazio devolve zeros em todos os indicadores", 4 de 7) e o formato
    dos dados ("oportunidade fechada tem data de fechamento", 3 de 7). Com a
    lista escrita antes, os dois trabalham sobre o mesmo contrato.
    """
    return (
        "Você é o verificador que vai julgar a suíte de testes desta entrega. "
        "ANTES de o QA escrever os testes, liste exatamente o que você vai "
        "cobrar: cada comportamento observável dos requisitos abaixo que um "
        "teste precisa verificar. Regras da lista:\n"
        "- um item por linha, numerado, curto e concreto: o que é exercitado "
        "(função, endpoint ou tela), com qual entrada, e o resultado esperado;\n"
        "- requisito universal (\"todos\", \"todas\", \"cada\", \"qualquer\") "
        "vira um item por elemento, com o nome de cada um;\n"
        "- regra sobre o formato dos dados (campo obrigatório, campo que só "
        "existe em certo estado) vira item só se for observável por teste;\n"
        "- só o que dá para testar com: "
        f"{ambiente_testes or 'o runner da stack'};\n"
        "- nada de aparência (cores, fundo, tema, contraste, layout: é da "
        "verificação visual), estrutura de código, estilo ou tipagem;\n"
        f"- no máximo {MAX_ITENS_CRITERIOS} itens. Responda só com a lista.\n\n"
        f"Requisitos:\n{requisitos}"
    )


def julgamento_pela_lista(criterios: str) -> str:
    """O trecho do prompt do guard que o prende à lista que ele mesmo fez."""
    if not (criterios or "").strip():
        return ""
    return (
        "\n\nANTES de o QA escrever, você definiu esta lista do que ia "
        "cobrar, e o QA a recebeu como obrigatória:\n"
        f"{criterios.strip()}\n"
        "Julgue a suíte contra ESSA lista: SIM se cada item tem um teste que "
        "o verifica com asserção sobre o resultado; NAO listando os itens sem "
        "teste. Não cobre nada que não esteja na lista."
    )
