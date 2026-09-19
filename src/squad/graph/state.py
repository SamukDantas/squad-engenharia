"""Estado compartilhado do grafo — a 'memória de trabalho' que atravessa os nós."""
from typing import TypedDict


class EstadoProjeto(TypedDict, total=False):
    pedido: str          # demanda original do usuário
    thread_id: str       # id da execução; as funções de rota não recebem config
    stack: str           # perfil da stack da entrega (python, nextjs, java)
    servico: str         # nome do microsserviço deste ramo ("" na entrega única)
    contratos: str       # contrato entre serviços, congelado antes do fan-out
    pronto: bool         # o ramo terminou em condição de ser publicado?
    workspace: str       # caminho absoluto de workspace/<thread_id>/
    spec: str            # saída da crew de planejamento
    spec_coerente: bool  # veredito do guard de aderência ao pedido
    spec_tentativas: int # contador do laço de replanejamento
    arquivos: list[str]  # caminhos relativos dos arquivos reais no workspace
    codigo: str          # dump consolidado dos arquivos, lido do disco (p/ revisor)
    testes_aderentes: bool   # guard de critérios: a suíte testa o que a spec exige?
    testes_tentativas: int   # contador do laço de reescrita de testes
    testes_ok: bool      # veredito de execução: exit code do pytest == 0
    cobertura: float     # % de cobertura agregada da entrega
    cobertura_pior: float        # % do módulo menos coberto
    cobertura_pior_arquivo: str  # qual é esse módulo
    cobertura_ok: bool   # passou nos dois pisos (agregado e por módulo)
    saida_testes: str    # saída real (stdout+stderr) do pytest
    relatorio_qa: str    # saída do revisor de código
    revisao_anterior: str    # relatório da revisão anterior (memória entre rodadas)
    feedback_qa: str     # correções pedidas quando reprovado (stack trace ou revisão)
    origem_feedback: str # quem motivou a rodada: "testes" | "revisao" | "pentest" | ""
    aprovado: bool       # veredito do revisor
    tentativas: int      # contador do laço de correções
    revisao_tentativas: int  # contador só das reprovações de revisão
    pentest_ok: bool     # veredito de execução: nenhuma vuln >= piso de bloqueio
    vulnerabilidades: list[dict]  # achados do pentest (ferramenta, endpoint, severidade)
    pentest_tentativas: int  # contador só das reprovações de pentest
    visual_ok: bool      # veredito de execução: entrega legível nos dois temas
    problemas_visuais: list[dict]  # achados da renderização (arquivo, tema, razão)
    visual_tentativas: int   # contador só das reprovações de verificação visual
    ambientes_ok: bool   # veredito de leitura: dev/hml/prod configurados e sãos
    achados_ambientes: list[dict]  # achados da configuração (tipo, ambiente, detalhe)
    ambientes_tentativas: int      # contador só das reprovações de configuração
    deploy_ok: bool      # resultado do nó de deploy
    deploy_ref: str      # onde a entrega foi publicada (repo@branch ou commit local)
