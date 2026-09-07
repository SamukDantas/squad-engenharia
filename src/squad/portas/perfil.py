"""Perfil de stack: tudo que muda quando a entrega deixa de ser Python.

Antes, estas quinze decisões estavam espalhadas por seis arquivos e
sincronizadas à mão por comentários — a imagem do sandbox no `sandbox.py`, o
prefixo `tests/` em seis lugares do `workflow.py`, o `.gitignore` da entrega no
`deploy.py`, a imagem alvo no `pentest.py`. Aqui elas ficam juntas, e uma stack
nova é um objeto destes, não uma cirurgia.

O perfil carrega **callables** de propósito. Parsear cobertura é código, não
configuração: o `coverage.py` devolve um JSON com `totals.percent_covered`, o
`vitest` devolve `total.lines.pct` e o JaCoCo devolve XML com `<counter>`. O que
é igual nas três é o contrato de saída — total, pior módulo, qual módulo —, e é
isso que o perfil promete.
"""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Cobertura:
    """O que todo parser de cobertura precisa devolver.

    O agregado sozinho engana: módulos bem testados puxam a média e escondem
    justamente o arquivo central do pedido. Quem decide precisa dos dois
    números, e do nome de quem ficou pior.
    """
    total: float = 0.0
    pior: float = 0.0
    pior_arquivo: str = ""

    def como_estado(self) -> dict:
        """No formato dos campos do `EstadoProjeto` — o grafo não muda de forma
        quando a stack muda."""
        return {
            "cobertura": self.total,
            "cobertura_pior": self.pior,
            "cobertura_pior_arquivo": self.pior_arquivo,
        }


@dataclass(frozen=True)
class PerfilStack:
    """Uma stack que a squad sabe entregar."""

    nome: str

    # ---- execução da suíte ----
    imagem_sandbox: str
    dockerfile_sandbox: str          # citado na mensagem de erro de imagem ausente
    runner: str                      # nome do comando, só para log ("pytest", "vitest")
    comando_container: Callable[[str], str]   # (dir de saída no container) -> sh -c
    comando_host: Callable[[str], list[str]]  # (dir de saída relativo) -> argv
    relatorio_cobertura: str         # nome do arquivo dentro do dir de saída
    ler_cobertura: Callable[[str], Cobertura]  # (texto do relatório) -> Cobertura
    preparar_workspace: Callable[[str], None]  # escreve config do runner, se houver

    # `TEST_RUNNER=host` roda a suíte no interpretador da própria squad. Isso só
    # existe no Python, e por acidente: fora dele o runtime do produto não é o
    # runtime do orquestrador. As demais stacks recusam em vez de tentar.
    permite_host: bool = True

    # Compilação, quando a stack tem uma. Roda ANTES da suíte, em container
    # próprio, e falha nele reprova a rodada sem chegar aos testes.
    #
    # Existe por uma entrega real: um `.module.css` com seletor de elemento
    # (`table {}`) é CSS válido e CSS Module inválido. A entrega passou por
    # 38 testes verdes, 87,4% de cobertura, revisão aprovada e deploy — e não
    # compilava. Os testes transformam TS/JSX sem construir, o revisor não
    # tem como suspeitar de CSS válido, e o nó visual pula em SPA. Nenhuma
    # das três camadas podia ver.
    comando_build: object = None     # Callable[[], str] | None

    # O que significa "compila" nesta stack. Roda no nó de deploy, logo antes
    # do push, e falha nele impede a publicação.
    #
    # Diferente de `comando_build`, este é obrigatório em toda stack: mesmo
    # onde não há build (Python), há como provar que o código é carregável.
    # E é separado dos testes de propósito — os tetos de circuit breaker
    # roteiam ao gate humano com a suíte vermelha, e um `sim` ali publicaria
    # o que não compila. Foi assim que uma entrega Next.js quebrada foi ao
    # GitHub depois de 38 testes verdes e revisão aprovada.
    comando_verificacao: object = None  # Callable[[], str]

    # ---- limites da jaula ----
    # 512m/120s não cobrem cold start de vitest com transpilação nem JVM+Maven,
    # então são do perfil, não constantes globais.
    timeout_testes: int = 120
    memoria: str = "512m"
    cpus: str = "1"

    # ---- convenções de árvore de arquivos ----
    # A convenção de teste é da stack: Python usa `tests/`, Java usa
    # `src/test/java/` e TypeScript costuma colocar o teste ao lado do módulo.
    e_teste: Callable[[str], bool] = lambda caminho: caminho.startswith("tests/")
    # Diretórios que não são entrega. Sem `node_modules` aqui, a varredura de um
    # projeto Next.js enumeraria dezenas de milhares de arquivos, todos entrando
    # no manifesto, no hash por arquivo e no orçamento do revisor.
    ignorar_no_workspace: frozenset = frozenset(
        {"__pycache__", ".pytest_cache", ".ruff_cache", ".squad", ".git"}
    )
    # Artefato compilado: existe no disco, não é entrega.
    extensoes_descartaveis: frozenset = frozenset({".pyc"})

    # ---- publicação e pentest ----
    gitignore_entrega: str = ""
    imagem_alvo: str = ""            # imagem que sobe a entrega para o pentest
    dockerfile_alvo: str = ""        # citado na mensagem de erro de imagem ausente

    # ---- prompts ----
    libs_permitidas: str = ""        # ambiente pré-provisionado, citado ao executor
    instrucoes_qa: str = ""          # onde e como o QA escreve a suíte

    # Campos reservados para o passo dos prompts; vazios não mudam nada hoje.
    instrucoes_executor: str = ""
    exemplo_run_json: str = ""
