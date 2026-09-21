"""O guard de critérios julga a suíte só contra os requisitos da spec.

Thread `05762b4b` (API de reserva de salas): o guard reprovou a primeira suíte
por não testar que o campo `usuario` do corpo é ignorado — linha da seção
"## Decisões de implementação", não do pedido. Uma passada inteira do QA
(~270 mil tokens) por uma sugestão do arquiteto. Conferido depois com a suíte
publicada sem os testes extras: spec inteira → NAO; só os requisitos → SIM.
"""
import inspect

from src.squad.crews.base import TASKS_CFG
from src.squad.dominio import spec
from src.squad.graph import workflow

SPEC = """## Requisitos
- Reserva sobreposta na mesma sala é rejeitada com 409.

## Decisões de implementação
- O usuário vem do token; o campo `usuario` do corpo é ignorado.
"""


def test_recorta_so_os_requisitos():
    recorte = spec.requisitos(SPEC)
    assert recorte.startswith("## Requisitos") and "409" in recorte
    assert "Decisões" not in recorte and "corpo" not in recorte


def test_requisitos_como_ultima_secao():
    texto = "## Contexto\nx\n\n## Requisitos\n- a\n- b\n"
    assert spec.requisitos(texto) == "## Requisitos\n- a\n- b"


def test_titulo_sem_diferenciar_caixa():
    assert "- a" in spec.requisitos("## REQUISITOS\n- a\n## Outra\n- b")


def test_spec_sem_a_secao_vale_inteira():
    """Spec anterior ao formato: julgar contra nada seria pior."""
    assert spec.requisitos("Criar um módulo que converte.") == "Criar um módulo que converte."
    assert spec.requisitos("") == ""


def test_guard_de_criterios_recebe_so_os_requisitos():
    fonte = inspect.getsource(workflow.no_validacao_testes)
    assert "spec_da_entrega.requisitos(state[\"spec\"])" in fonte
    assert "Requisitos da especificação" in fonte


def test_qa_trata_decisoes_como_sugestao():
    descricao = TASKS_CFG["escrever_testes"]["description"]
    assert '"## Decisões de implementação" é sugestão do arquiteto' in descricao


# ---------- thread af83302f (dashboard Next.js) ----------

def test_revisor_recebe_so_os_requisitos():
    """O revisor reprovou citando uma linha de "## Decisões de
    implementação" como exigência da spec. Refeita com o mesmo código, a
    revisão aprovou nas duas versões: a reprova era instável, e quem não vê
    a decisão não a cobra."""
    fonte = inspect.getsource(workflow.no_revisao)
    assert "spec_da_entrega.requisitos(state[\"spec\"])" in fonte
    assert '"spec": requisitos' in fonte


def test_qa_testa_conteudo_e_nao_a_organizacao_da_pagina():
    """O teste exigia 4 tabelas (número das decisões); o requisito só dizia
    "em tabelas". A rodada de correção reorganizou a página."""
    descricao = TASKS_CFG["escrever_testes"]["description"]
    assert "não a organização da página" in descricao


def test_executor_nextjs_declara_o_fundo_no_body():
    """O fundo foi num `.page` de CSS Module, e a verificação visual, que mede
    o body, reprovou por `fundo_nao_declarado` no fim da execução."""
    from src.squad.adaptadores import perfis
    instrucoes = perfis.obter("nextjs").instrucoes_executor
    assert "declarados no `body`" in instrucoes and "não conta" in instrucoes


def test_qa_nao_testa_aparencia():
    """Thread `3842268c`: com o fundo no `body` e a verificação visual verde,
    um teste que lia o CSS exigia `background` em `.page`, e a única rodada
    extra foi o executor duplicando o fundo para agradá-lo."""
    from src.squad.adaptadores import perfis
    receita = perfis.obter("nextjs").instrucoes_qa
    assert "NÃO escreva teste de aparência" in receita
    assert "(CSS, JSON)" not in receita  # a receita não sugere mais ler CSS
    descricao = TASKS_CFG["escrever_testes"]["description"]
    assert "Não leia" in descricao and "arquivo de estilo" in descricao
