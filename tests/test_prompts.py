"""Os prompts das crews interpolam sem faltar variável.

Existe por uma falha real: a tarefa de arquitetura passou a citar `{stack}`, mas
só as crews de desenvolvimento e de QA recebiam esse campo. O CrewAI só levanta
na interpolação — a execução morreu depois da triagem, com o planejamento já
pago, e a mensagem (`Template variable 'stack' not found`) não dizia qual crew.

A checagem que existia antes era fraca do jeito certo de enganar: conferia se o
placeholder tinha fonte *em algum lugar*, não se a crew que roda aquela tarefa a
fornece.
"""
import re
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parents[1]

# Campos que os nós do **ramo** montam na hora. Todas as tarefas do pipeline de
# um serviço rodam em crews que também recebem `_do_perfil()`.
POR_TAREFA = {
    "pedido", "spec", "arquivos", "feedback_qa", "codigo", "saida_testes",
    "revisao_anterior",
}

# As crews do maestro são a exceção: rodam antes de existir stack de ramo, e
# recebem só o que o nó delas monta. Declaradas aqui exatamente como o
# `graph/maestro.py` as chama — divergir daqui é o erro que este teste pega.
DO_MAESTRO = {
    "decompor_servicos": {"pedido", "stack", "stacks_disponiveis", "max_servicos"},
    "redigir_contratos": {"pedido", "servicos"},
}


def _placeholders(texto: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", texto or ""))


def _tasks() -> dict:
    return yaml.safe_load((RAIZ / "src/squad/config/tasks.yaml").read_text(encoding="utf-8"))


def _do_perfil() -> set[str]:
    """As chaves que `_do_perfil` distribui a todas as crews, lidas do código."""
    from src.squad.adaptadores import perfis
    from src.squad.graph.workflow import _do_perfil as helper

    return set(helper(perfis.obter("python")))


def _fontes(nome: str) -> set[str]:
    """O que a crew que roda ESTA tarefa de fato fornece.

    Por tarefa e não global: a checagem antiga perguntava se o placeholder tinha
    fonte em algum lugar da squad, e por isso deixou passar a tarefa de
    arquitetura citando `{stack}` numa crew que não o recebia.
    """
    if nome in DO_MAESTRO:
        return DO_MAESTRO[nome]
    return POR_TAREFA | _do_perfil()


def test_todo_placeholder_de_tarefa_tem_quem_o_preencha():
    faltando = {
        (nome, ph)
        for nome, tarefa in _tasks().items()
        for campo in ("description", "expected_output")
        for ph in _placeholders(tarefa.get(campo, ""))
        if ph not in _fontes(nome)
    }
    assert not faltando, f"placeholders sem fonte: {sorted(faltando)}"


def test_as_crews_do_maestro_nao_recebem_campos_de_perfil():
    """Elas rodam antes de existir ramo, e portanto antes de existir stack de
    entrega. Uma tarefa do maestro que passe a citar `{runner}` amanhã morre na
    interpolação — este teste é quem avisa antes."""
    for nome, fontes in DO_MAESTRO.items():
        assert not (fontes & (_do_perfil() - {"stack"})), nome


def test_o_maestro_passa_exatamente_o_que_as_tarefas_dele_pedem():
    """Declarar a mais também é defeito: uma chave que ninguém usa é sinal de
    que o prompt mudou e a declaração ficou para trás."""
    from src.squad.graph import maestro  # noqa: F401  (garante que importa)

    tarefas = _tasks()
    for nome, fontes in DO_MAESTRO.items():
        usados = set()
        for campo in ("description", "expected_output"):
            usados |= _placeholders(tarefas[nome].get(campo, ""))
        assert usados <= fontes, (nome, usados - fontes)


def test_campos_do_perfil_vao_para_todas_as_crews():
    """Distribuir tudo para todas é o que remove a classe de erro: uma tarefa
    que passe a citar `{stack}` amanhã já tem quem a preencha, em qualquer crew."""
    assert _do_perfil() == {
        "stack", "runner", "libs_permitidas", "instrucoes_qa",
        "exemplo_run_json",
    }


def test_todo_kickoff_espalha_os_campos_do_perfil():
    """Lido do fonte: um `kickoff` novo que esqueça `**_do_perfil(...)` reabre
    exatamente o buraco que derrubou a execução da stack nextjs."""
    fonte = (RAIZ / "src/squad/graph/workflow.py").read_text(encoding="utf-8")
    kickoffs = re.findall(r"\.kickoff\(\s*\n?\s*inputs=\{(.*?)\}", fonte, re.DOTALL)
    # Entradas montadas antes numa variável (o QA monta uma vez e usa nos dois
    # caminhos, CrewAI e Codex): a variável tem de espalhar o perfil.
    for nome in re.findall(r"\.kickoff\(\s*\n?\s*inputs=(\w+)", fonte):
        definicao = re.search(rf"{nome} = \{{(.*?)\}}", fonte, re.DOTALL)
        assert definicao, f"inputs={nome} sem definição literal no fonte"
        kickoffs.append(definicao.group(1))
    assert len(kickoffs) == 4, f"esperava 4 crews, achei {len(kickoffs)}"
    for i, corpo in enumerate(kickoffs):
        assert "_do_perfil(" in corpo, f"kickoff #{i + 1} não espalha o perfil"


def test_nenhuma_tarefa_cita_stack_fixa_no_texto():
    """O texto das tarefas não pode voltar a nomear pytest, fastapi ou Python:
    a stack vem do perfil, e um resquício aqui reapareceria só na execução de
    outra stack — calado, como instrução errada ao agente."""
    proibidos = ("pytest", "fastapi", "flask", "ARQUIVOS PYTHON")
    achados = [
        (nome, campo, termo)
        for nome, tarefa in _tasks().items()
        for campo in ("description", "expected_output")
        for termo in proibidos
        if termo.lower() in (tarefa.get(campo) or "").lower()
    ]
    assert not achados, f"stack fixa no tasks.yaml: {achados}"


def test_qa_confere_cada_criterio_antes_de_entregar():
    """Nas threads `47339386` e `d1b5175b` o guard de critérios reprovou a
    primeira suíte por falta de teste da página (mensagens de erro, valores
    exibidos, atualização após digitar). O QA recebe a mesma régua do guard
    e fecha o manifesto com a matriz critério → teste."""
    descricao = _tasks()["escrever_testes"]["description"]
    assert "critério → teste" in descricao
    assert "cada mensagem de" in descricao and "erro de cada entrada inválida" in descricao
