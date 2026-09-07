"""Perfil da stack Python: pytest, coverage.py, FastAPI/Flask.

É o perfil original da squad — tudo aqui já existia, espalhado por
`sandbox.py`, `workflow.py`, `deploy.py`, `pentest.py` e `opencode.py`. Reuni-lo
não muda comportamento nenhum; muda de onde as decisões vêm.
"""
import json
import sys
from pathlib import Path

from ..portas.perfil import Cobertura, PerfilStack

# Subdiretório de trabalho da squad dentro do workspace (fora da entrega).
DIR_SQUAD = ".squad"
COVERAGERC = f"{DIR_SQUAD}/coveragerc"
RELATORIO = "coverage.json"

_PYTEST_ARGS = [
    "tests", "--tb=short", "-q",
    "--cov=.", f"--cov-config={COVERAGERC}",
]

# Sem omitir os próprios testes, eles inflam a cobertura: um arquivo de teste
# executado é ~100% coberto, e a métrica passaria a medir a si mesma.
#
# Scripts de conveniência (um `run_tests.py` que o executor cria para chamar o
# pytest) nunca são importados, ficam em 0% e sequestram o piso por módulo — o
# QA seria mandado escrever testes para um runner de testes. A lista é
# conservadora e por nome convencional; a defesa principal é a instrução ao
# executor para não criar esses scripts.
_COVERAGERC = """[run]
omit =
    tests/*
    .squad/*
    */site-packages/*
    run_tests.py
    run.py
    manage.py
    setup.py
    conftest.py
"""

_LIBS = "fastapi, flask, httpx, requests"

_GITIGNORE = (
    "__pycache__/\n.pytest_cache/\n.ruff_cache/\n.squad/\n*.pyc\n"
    # Rastro de comandos rodados pelo executor: não é entrega.
    "*.log\n*_output.txt\n*_result.txt\n*_results.txt\n"
)


def _preparar(workspace: str) -> None:
    """Escreve a config do coverage.py dentro do workspace, fora da entrega."""
    raiz = Path(workspace)
    (raiz / DIR_SQUAD).mkdir(parents=True, exist_ok=True)
    (raiz / COVERAGERC).write_text(_COVERAGERC, encoding="utf-8")


def _comando_container(dir_saida: str) -> str:
    # `python -m pytest` (e não `pytest`) para paridade com o runner host: só a
    # forma com -m coloca o diretório atual no sys.path, sem a qual
    # `from app import ...` quebra na coleta dentro do container.
    args = " ".join(_PYTEST_ARGS)
    return (
        f"cp -r /src /app/projeto && cd /app/projeto && "
        f"python -m pytest {args} --cov-report=json:{dir_saida}/{RELATORIO}"
    )


def _comando_verificacao() -> str:
    """O mais perto de "compila" que Python tem: byte-compilação de tudo.

    Só `compileall`, sem importar os módulos. Importar executaria o código de
    topo de cada arquivo — e uma entrega que sobe servidor ou lê stdin travaria
    até o timeout, transformando a verificação num risco maior que o defeito que
    ela procura.

    O limite é conhecido e aceito: isto pega erro de sintaxe, não erro de
    import. Import quebrado já é pego antes, na coleta do pytest, que roda a
    suíte inteira contra os módulos reais.
    """
    return "cp -r /src /app/projeto && cd /app/projeto && python -m compileall -q ."


def _comando_host(dir_saida: str) -> list[str]:
    return [
        sys.executable, "-m", "pytest", *_PYTEST_ARGS,
        f"--cov-report=json:{dir_saida}/{RELATORIO}",
    ]


def _ler_cobertura(texto: str) -> Cobertura:
    """Schema do `coverage.json` do coverage.py.

    O pior módulo importa tanto quanto o total: o agregado engana quando a suíte
    testa muito o que é fácil e ignora o arquivo central do pedido — medido em
    execução real, 89% no total com 51% no módulo da API.
    """
    try:
        dados = json.loads(texto)
        total = round(float(dados["totals"]["percent_covered"]), 1)
    except (ValueError, KeyError, TypeError):
        return Cobertura()

    pior, pior_arquivo = None, ""
    for nome, info in (dados.get("files") or {}).items():
        resumo = info.get("summary", {})
        # Arquivo sem instruções (ex.: __init__.py vazio) reporta 100% e não diz
        # nada sobre a qualidade da suíte.
        if not resumo.get("num_statements"):
            continue
        pct = round(float(resumo.get("percent_covered", 0.0)), 1)
        if pior is None or pct < pior:
            pior, pior_arquivo = pct, nome.replace("\\", "/")

    return Cobertura(
        total=total,
        pior=total if pior is None else pior,
        pior_arquivo=pior_arquivo,
    )


PERFIL = PerfilStack(
    nome="python",
    imagem_sandbox="squad-sandbox-python:latest",
    dockerfile_sandbox="Dockerfile.sandbox-python",
    runner="pytest",
    comando_container=_comando_container,
    comando_verificacao=_comando_verificacao,
    comando_host=_comando_host,
    relatorio_cobertura=RELATORIO,
    ler_cobertura=_ler_cobertura,
    preparar_workspace=_preparar,
    permite_host=True,
    e_teste=lambda caminho: caminho.startswith("tests/"),
    ignorar_no_workspace=frozenset(
        {"__pycache__", ".pytest_cache", ".ruff_cache", ".squad", ".git"}
    ),
    extensoes_descartaveis=frozenset({".pyc"}),
    gitignore_entrega=_GITIGNORE,
    imagem_alvo="squad-target-python:latest",
    dockerfile_alvo="Dockerfile.target-python",
    libs_permitidas=_LIBS,
    instrucoes_qa=(
        "Escreva testes pytest no diretório `tests/`. Eles serão executados "
        "com pytest a partir da raiz do workspace."
    ),
    instrucoes_executor=(
        "- Python apenas, usando somente a biblioteca padrão e estas libs já "
        f"instaladas: {_LIBS}. Não use nenhuma outra dependência.\n"
        "- NÃO crie nem edite nada dentro de `tests/` — os testes são escritos "
        "por outro agente da equipe de qualidade.\n"
        "- NÃO crie scripts para rodar os testes (`run_tests.py` e afins): a "
        "suíte é executada pelo próprio pipeline."
    ),
    exemplo_run_json=(
        '{"cmd": ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", 8000], '
        '"port": 8000, "health_path": "/docs"}  '
        "(Flask usa `flask --app <mod> run --host 0.0.0.0 --port <p>`)"
    ),
)
