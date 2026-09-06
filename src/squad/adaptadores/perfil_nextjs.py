"""Perfil da stack Next.js/TypeScript: vitest, cobertura V8, React.

Duas diferenças de fundo em relação ao Python, e as duas vêm do `--network none`:

- as dependências não podem ser instaladas em runtime, então `node_modules` vem
  assado na imagem, num nível acima do projeto. O resolvedor do Node sobe a
  árvore de diretórios procurando `node_modules`, então a entrega em
  `/app/projeto` enxerga `/app/node_modules` sem nenhum symlink;
- o `npx` consultaria o registry para um pacote ausente. O binário do vitest é
  invocado por caminho absoluto, que falha alto se faltar em vez de tentar
  baixar.

A suíte fica **ao lado do módulo** (`src/app.test.ts`), que é a convenção da
comunidade — e é exatamente o caso que quebrava três guards quando o prefixo
`tests/` estava fixo no grafo.
"""
import json

from ..portas.perfil import Cobertura, PerfilStack

RELATORIO = "coverage-summary.json"

# Diretório onde a imagem instala as dependências, um nível acima do projeto.
NODE_MODULES = "/app/node_modules"
VITEST = f"{NODE_MODULES}/.bin/vitest"

_SUFIXOS_TESTE = (".test.ts", ".test.tsx", ".test.js", ".test.jsx",
                  ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx")

_LIBS = "next, react, react-dom"

_GITIGNORE = (
    "node_modules/\n.next/\ndist/\nbuild/\ncoverage/\n.squad/\n"
    "*.tsbuildinfo\n*.log\n"
)


def _e_teste(caminho: str) -> bool:
    """Teste colocado ao lado do módulo, ou dentro de `tests/`.

    As duas convenções convivem em projetos reais, e reconhecer só uma faria o
    guard de entrega vazia contar teste como entrega.
    """
    return caminho.startswith("tests/") or caminho.endswith(_SUFIXOS_TESTE)


def _preparar(workspace: str) -> None:
    """Nada a escrever: a configuração do vitest vai por flag de linha de
    comando, para a entrega não precisar carregar um arquivo de config que o
    executor poderia sobrescrever."""


def _flags_cobertura(dir_saida: str) -> str:
    # `clean=false` é obrigatório, não preferência: o diretório de saída é um
    # bind mount, e a limpeza padrão do vitest tenta `rmdir` nele — o que falha
    # com EACCES e derruba a execução antes do primeiro teste. A limpeza também
    # seria redundante: o runner já apaga o relatório da rodada anterior.
    return (
        "--coverage.enabled --coverage.provider=v8 "
        "--coverage.reporter=json-summary --coverage.clean=false "
        f"--coverage.reportsDirectory={dir_saida}"
    )


def _comando_container(dir_saida: str) -> str:
    return (
        f"cp -r /src /app/projeto && cd /app/projeto && "
        f"{VITEST} run --root . {_flags_cobertura(dir_saida)}"
    )


def _comando_host(dir_saida: str) -> list[str]:
    # Inalcançável: `permite_host=False`. Existe para o perfil ser completo e
    # para o erro vir da checagem explícita, não de um AttributeError.
    return ["vitest", "run", "--root", ".", *_flags_cobertura(dir_saida).split()]


def _ler_cobertura(texto: str) -> Cobertura:
    """Schema do `coverage-summary.json` (istanbul/V8).

    O arquivo traz uma chave `total` com os agregados e uma chave por arquivo,
    cada uma com `lines.pct`. Usamos linhas, e não statements, para a régua ser
    a mesma do coverage.py.
    """
    try:
        dados = json.loads(texto)
        total = round(float(dados["total"]["lines"]["pct"]), 1)
    except (ValueError, KeyError, TypeError):
        return Cobertura()

    pior, pior_arquivo = None, ""
    for nome, info in dados.items():
        if nome == "total" or not isinstance(info, dict):
            continue
        linhas = info.get("lines") or {}
        # Arquivo sem linha executável reporta 100% e não diz nada sobre a
        # suíte — mesma exclusão que o perfil Python faz por `num_statements`.
        if not linhas.get("total"):
            continue
        pct = round(float(linhas.get("pct", 0.0)), 1)
        if pior is None or pct < pior:
            # O relatório traz caminho absoluto do container; o que interessa a
            # quem lê é o caminho dentro da entrega.
            pior, pior_arquivo = pct, nome.replace("\\", "/").split("/projeto/")[-1]

    return Cobertura(
        total=total,
        pior=total if pior is None else pior,
        pior_arquivo=pior_arquivo,
    )


PERFIL = PerfilStack(
    nome="nextjs",
    imagem_sandbox="squad-sandbox-nextjs:latest",
    dockerfile_sandbox="Dockerfile.sandbox-nextjs",
    runner="vitest",
    comando_container=_comando_container,
    comando_host=_comando_host,
    relatorio_cobertura=RELATORIO,
    ler_cobertura=_ler_cobertura,
    preparar_workspace=_preparar,
    # O runner de host roda a suíte no interpretador da própria squad, que é
    # Python. Não existe equivalente aqui.
    permite_host=False,
    # Cold start do vitest inclui transpilação; 120s/512m não cobrem.
    timeout_testes=300,
    memoria="1g",
    e_teste=_e_teste,
    ignorar_no_workspace=frozenset(
        {"node_modules", ".next", "dist", "build", "coverage", ".squad", ".git"}
    ),
    extensoes_descartaveis=frozenset({".tsbuildinfo", ".map"}),
    gitignore_entrega=_GITIGNORE,
    imagem_alvo="squad-target-nextjs:latest",
    dockerfile_alvo="Dockerfile.target-nextjs",
    libs_permitidas=_LIBS,
    instrucoes_qa=(
        "Escreva testes vitest ao lado de cada módulo, com sufixo "
        "`.test.ts` ou `.test.tsx`. Eles serão executados pelo vitest a "
        "partir da raiz do workspace."
    ),
    instrucoes_executor=(
        "- TypeScript/React apenas, usando somente estas libs já instaladas: "
        f"{_LIBS}. Não há `npm install` em runtime — import de qualquer outra "
        "dependência quebra a suíte.\n"
        "- NÃO escreva testes: nenhum `*.test.ts(x)`, `*.spec.ts(x)` nem nada "
        "dentro de `tests/`. A suíte é escrita por outro agente da equipe de "
        "qualidade.\n"
        "- NÃO crie configuração de vitest nem scripts de teste no "
        "`package.json`: a suíte é executada pelo pipeline por linha de comando."
    ),
    exemplo_run_json=(
        '{"cmd": ["/app/node_modules/.bin/next", "start", "-p", 3000], '
        '"port": 3000, "health_path": "/"}'
    ),
)
