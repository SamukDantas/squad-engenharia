"""Estado compartilhado do grafo — a 'memória de trabalho' que atravessa os nós."""
from typing import TypedDict


class EstadoProjeto(TypedDict, total=False):
    pedido: str          # demanda original do usuário
    workspace: str       # caminho absoluto de workspace/<thread_id>/
    spec: str            # saída da crew de planejamento
    spec_coerente: bool  # veredito do guard de aderência ao pedido
    spec_tentativas: int # contador do laço de replanejamento
    arquivos: list[str]  # caminhos relativos dos arquivos reais no workspace
    codigo: str          # dump consolidado dos arquivos, lido do disco (p/ revisor)
    testes_ok: bool      # veredito de execução: exit code do pytest == 0
    saida_testes: str    # saída real (stdout+stderr) do pytest
    relatorio_qa: str    # saída do revisor de código
    feedback_qa: str     # correções pedidas quando reprovado (stack trace ou revisão)
    aprovado: bool       # veredito do revisor
    tentativas: int      # contador do laço de correções
    deploy_ok: bool      # resultado do nó de deploy
    deploy_ref: str      # onde a entrega foi publicada (repo@branch ou commit local)
