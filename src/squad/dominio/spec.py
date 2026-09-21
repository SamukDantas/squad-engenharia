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
