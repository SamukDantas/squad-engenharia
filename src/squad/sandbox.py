"""Execução dos testes: a jaula onde código gerado por LLM roda (Fase 6).

Código escrito por modelo pode conter qualquer coisa — um `rm` mal colocado,
um loop infinito, uma dependência maliciosa alucinada. O runner padrão é um
container Docker efêmero: entrega montada read-only, sem rede, com limites de
CPU/memória e teto de tempo.

Não há fallback silencioso para o host: perder a jaula justamente quando ela
falha é o pior momento para rodar código não confiável na máquina. Se o Docker
não estiver disponível, o nó falha alto e nomeia as duas saídas.
"""
import json
import os
import subprocess
from pathlib import Path

IMAGEM = "squad-sandbox:latest"
TIMEOUT_TESTES = 120     # segundos; estourou = reprova (loop infinito etc.)
LIMITE_SAIDA = 8_000     # chars da saída guardados no estado
MEMORIA = "512m"
CPUS = "1"

# Subdiretório de trabalho da squad dentro do workspace (fora da entrega).
DIR_SQUAD = ".squad"
COVERAGERC = f"{DIR_SQUAD}/coveragerc"
DIR_SAIDA = f"{DIR_SQUAD}/out"

_PYTEST_ARGS = [
    "tests", "--tb=short", "-q",
    "--cov=.", f"--cov-config={COVERAGERC}",
]

# Sem omitir os próprios testes, eles inflam a cobertura: um arquivo de teste
# executado é ~100% coberto, e a métrica passaria a medir a si mesma.
_COVERAGERC = """[run]
omit =
    tests/*
    .squad/*
    */site-packages/*
"""


def _preparar(workspace: str) -> Path:
    raiz = Path(workspace)
    (raiz / DIR_SQUAD).mkdir(parents=True, exist_ok=True)
    (raiz / COVERAGERC).write_text(_COVERAGERC, encoding="utf-8")
    saida = raiz / DIR_SAIDA
    saida.mkdir(parents=True, exist_ok=True)
    # Relatório da rodada anterior não pode ser confundido com o desta.
    (saida / "coverage.json").unlink(missing_ok=True)
    return raiz


def _cobertura(workspace: str) -> dict:
    """Cobertura total e a do **pior módulo**.

    O agregado sozinho engana: módulos bem testados puxam a média e escondem
    justamente o arquivo central do pedido. Quem decide precisa dos dois
    números.
    """
    vazio = {"cobertura": 0.0, "cobertura_pior": 0.0, "cobertura_pior_arquivo": ""}
    arquivo = Path(workspace) / DIR_SAIDA / "coverage.json"
    if not arquivo.is_file():
        return vazio
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
        total = round(float(dados["totals"]["percent_covered"]), 1)
    except (ValueError, KeyError):
        return vazio

    pior, pior_arquivo = None, ""
    for nome, info in (dados.get("files") or {}).items():
        resumo = info.get("summary", {})
        # Arquivo sem instruções (ex.: __init__.py vazio) reporta 100% e não
        # diz nada sobre a qualidade da suíte.
        if not resumo.get("num_statements"):
            continue
        pct = round(float(resumo.get("percent_covered", 0.0)), 1)
        if pior is None or pct < pior:
            pior, pior_arquivo = pct, nome.replace("\\", "/")

    return {
        "cobertura": total,
        "cobertura_pior": total if pior is None else pior,
        "cobertura_pior_arquivo": pior_arquivo,
    }


def _checar_docker() -> None:
    try:
        r = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "Docker não encontrado no PATH. Instale o Docker Desktop ou use "
            "TEST_RUNNER=host no .env (executa os testes sem jaula)."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Docker não respondeu em 30s — o daemon está saudável?")
    # `docker info` sai com 0 mesmo com o daemon morto (imprime o erro no
    # lugar da versão) — confiar só no exit code deixaria passar um daemon
    # inexistente e a falha apareceria depois, disfarçada de erro do pytest.
    versao = (r.stdout or "").strip()
    if r.returncode != 0 or not versao or "error" in versao.lower():
        detalhe = versao or (r.stderr or "").strip()
        raise RuntimeError(
            "Daemon do Docker não está respondendo (Docker Desktop parado ou "
            "sem backend). Suba o Docker Desktop ou use TEST_RUNNER=host no "
            f".env.\n{detalhe[:300]}"
        )

    r = subprocess.run(
        ["docker", "image", "inspect", IMAGEM],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"Imagem {IMAGEM} não encontrada. Construa uma vez com:\n"
            "  docker build -f Dockerfile.sandbox -t squad-sandbox:latest .\n"
            "Ou use TEST_RUNNER=host no .env."
        )


def _rodar_docker(workspace: str, thread_id: str) -> tuple[int, str]:
    _checar_docker()
    raiz = Path(workspace).resolve()
    nome = f"qa-{thread_id}"
    pytest_cmd = " ".join(_PYTEST_ARGS)
    comando = [
        "docker", "run", "--rm", "--name", nome,
        # Entrega read-only: um rm -rf dentro do container não toca o host.
        "-v", f"{raiz.as_posix()}:/src:ro",
        # Única superfície de escrita, restrita ao diretório de trabalho da squad.
        "-v", f"{(raiz / DIR_SAIDA).as_posix()}:/out",
        "--network", "none",
        "--memory", MEMORIA, "--cpus", CPUS,
        IMAGEM,
        "sh", "-c",
        # `python -m pytest` (e não `pytest`) para paridade com o runner host:
        # só a forma com -m coloca o diretório atual no sys.path, sem a qual
        # `from app import ...` quebra na coleta dentro do container.
        f"cp -r /src /app/projeto && cd /app/projeto && "
        f"python -m pytest {pytest_cmd} --cov-report=json:/out/coverage.json",
    ]
    print(f">>> Executando pytest em sandbox Docker ({IMAGEM}, sem rede)...")
    try:
        r = subprocess.run(
            comando, capture_output=True, text=True, encoding="utf-8",
            errors="replace", stdin=subprocess.DEVNULL, timeout=TIMEOUT_TESTES,
        )
        return r.returncode, ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    except subprocess.TimeoutExpired as e:
        # Matar o cliente docker não mata o container: kill explícito.
        subprocess.run(
            ["docker", "kill", nome], capture_output=True,
            stdin=subprocess.DEVNULL, timeout=30,
        )
        parcial = (e.stdout or b"")
        if isinstance(parcial, bytes):
            parcial = parcial.decode("utf-8", errors="replace")
        return 1, (
            f"TIMEOUT: os testes excederam {TIMEOUT_TESTES}s — provável loop "
            f"infinito ou teste travado. Container encerrado.\n"
            f"Saída parcial:\n{parcial[-2_000:]}"
        )


def _rodar_host(workspace: str) -> tuple[int, str]:
    import sys

    print(">>> Executando pytest no host (sem jaula — TEST_RUNNER=host)...")
    comando = [sys.executable, "-m", "pytest", *_PYTEST_ARGS,
               f"--cov-report=json:{DIR_SAIDA}/coverage.json"]
    try:
        r = subprocess.run(
            comando, cwd=workspace, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL,  # teste que pede input falha, não trava
            timeout=TIMEOUT_TESTES,
        )
        return r.returncode, ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    except subprocess.TimeoutExpired as e:
        parcial = str(e.stdout or "")
        return 1, (
            f"TIMEOUT: os testes excederam {TIMEOUT_TESTES}s — provável loop "
            f"infinito ou teste travado.\nSaída parcial:\n{parcial[-2_000:]}"
        )


def executar_testes(workspace: str, thread_id: str) -> dict:
    """Roda a suíte no workspace e devolve veredito objetivo.

    Exit code 0 = verde; qualquer outro (inclusive 5, "nenhum teste coletado")
    = vermelho. Timeout = vermelho.
    """
    _preparar(workspace)
    runner = (os.getenv("TEST_RUNNER") or "docker").strip().lower()
    if runner == "docker":
        codigo, saida = _rodar_docker(workspace, thread_id)
    elif runner == "host":
        codigo, saida = _rodar_host(workspace)
    else:
        raise ValueError(f"TEST_RUNNER inválido: '{runner}'. Use 'docker' ou 'host'.")

    testes_ok = codigo == 0
    cobertura = _cobertura(workspace)
    detalhe_pior = (
        f" (pior: {cobertura['cobertura_pior_arquivo']} "
        f"{cobertura['cobertura_pior']}%)"
        if cobertura["cobertura_pior_arquivo"]
        else ""
    )
    print(
        f">>> pytest exit code {codigo} — {'verde' if testes_ok else 'vermelho'}"
        f" | cobertura {cobertura['cobertura']}%{detalhe_pior}"
    )
    return {**cobertura, "testes_ok": testes_ok, "saida_testes": saida[-LIMITE_SAIDA:]}
