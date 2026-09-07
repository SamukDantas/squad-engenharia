"""Registro das stacks que a squad sabe entregar.

Acrescentar uma stack é acrescentar uma linha aqui e um módulo de perfil ao
lado — não é mexer no grafo.
"""
from ..portas.perfil import PerfilStack
from . import perfil_java, perfil_nextjs, perfil_python

PERFIL_PADRAO = "python"

_PERFIS: dict[str, PerfilStack] = {
    p.nome: p
    for p in (perfil_python.PERFIL, perfil_nextjs.PERFIL, perfil_java.PERFIL)
}


def nomes() -> list[str]:
    return sorted(_PERFIS)


def obter(nome: str | None) -> PerfilStack:
    """Perfil pelo nome, com erro que diz o que existe.

    Stack desconhecida falha aqui, na triagem, e não lá no sandbox com uma
    imagem inexistente — é a mesma lógica do guard de entrega vazia: reprovar
    cedo custa centavos, reprovar tarde custa a execução.
    """
    escolhido = (nome or PERFIL_PADRAO).strip().lower()
    perfil = _PERFIS.get(escolhido)
    if perfil is None:
        raise ValueError(
            f"Stack desconhecida: '{escolhido}'. Disponíveis: {', '.join(nomes())}."
        )
    return perfil
