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
