"""Estado compartilhado do grafo — a 'memória de trabalho' que atravessa os nós."""
from typing import TypedDict


class EstadoProjeto(TypedDict, total=False):
    pedido: str          # demanda original do usuário
    spec: str            # saída da crew de planejamento
    spec_coerente: bool  # veredito do guard de aderência ao pedido
    spec_tentativas: int # contador do laço de replanejamento
    codigo: str          # saída da crew de desenvolvimento
    relatorio_qa: str    # saída da crew de qualidade
    feedback_qa: str     # correções pedidas quando reprovado
    aprovado: bool       # veredito da qualidade
    tentativas: int      # contador do laço de correções
    deploy_ok: bool      # resultado do nó de deploy
