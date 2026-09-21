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
