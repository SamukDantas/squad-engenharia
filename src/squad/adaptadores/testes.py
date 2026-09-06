"""Execução da suíte: a jaula onde código gerado por LLM roda (Fase 6).

Código escrito por modelo pode conter qualquer coisa — um `rm` mal colocado, um
loop infinito, uma dependência maliciosa alucinada. O runner padrão é um
container Docker efêmero: entrega montada read-only, sem rede, com limites de
CPU/memória e teto de tempo.

Não há fallback silencioso para o host: perder a jaula justamente quando ela
falha é o pior momento para rodar código não confiável na máquina. Se o Docker
não estiver disponível, o nó falha alto e nomeia as duas saídas.

A jaula é a mesma para toda stack — o que muda (imagem, comando, formato do
relatório de cobertura, limites) vem do perfil. Este módulo não sabe o que é
pytest.
"""
import os
import subprocess
from pathlib import Path

from ..portas.perfil import Cobertura, PerfilStack
from ..portas.testes import ResultadoTestes

# Chars da saída guardados no estado. O corte é pela **cauda**, e a 8.000 ele
# mordia: medido numa suíte vermelha real, a saída tinha 19.422 chars e o corte
# descartava 11.422 (59%) — 2 das 21 linhas de falha distintas nunca chegavam ao
# dev, justamente quando o texto é a instrução de conserto. 20.000 cobre o maior
# caso já observado com folga. O teto continua existindo de propósito: contexto
# gigante satura o modelo e devolve resposta vazia (item 5).
LIMITE_SAIDA = 20_000

# Diretório de trabalho da squad dentro do workspace — convenção da squad, não
# da stack: é a única superfície de escrita montada no container.
DIR_SAIDA = ".squad/out"
DIR_SAIDA_CONTAINER = "/out"


def _preparar(workspace: str, perfil: PerfilStack) -> None:
    saida = Path(workspace) / DIR_SAIDA
    saida.mkdir(parents=True, exist_ok=True)
    # Relatório da rodada anterior não pode ser confundido com o desta.
    (saida / perfil.relatorio_cobertura).unlink(missing_ok=True)
    perfil.preparar_workspace(workspace)


def _cobertura(workspace: str, perfil: PerfilStack) -> Cobertura:
    arquivo = Path(workspace) / DIR_SAIDA / perfil.relatorio_cobertura
    if not arquivo.is_file():
        return Cobertura()
    try:
        texto = arquivo.read_text(encoding="utf-8")
    except OSError:
        return Cobertura()
    return perfil.ler_cobertura(texto)


def _checar_docker(perfil: PerfilStack) -> None:
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
    # `docker info` sai com 0 mesmo com o daemon morto (imprime o erro no lugar
    # da versão) — confiar só no exit code deixaria passar um daemon inexistente
    # e a falha apareceria depois, disfarçada de erro do runner.
    versao = (r.stdout or "").strip()
    if r.returncode != 0 or not versao or "error" in versao.lower():
        detalhe = versao or (r.stderr or "").strip()
        raise RuntimeError(
            "Daemon do Docker não está respondendo (Docker Desktop parado ou "
            "sem backend). Suba o Docker Desktop ou use TEST_RUNNER=host no "
            f".env.\n{detalhe[:300]}"
        )

    r = subprocess.run(
        ["docker", "image", "inspect", perfil.imagem_sandbox],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"Imagem {perfil.imagem_sandbox} não encontrada. Construa uma vez com:\n"
            f"  docker build -f {perfil.dockerfile_sandbox} "
            f"-t {perfil.imagem_sandbox} .\n"
            "Ou use TEST_RUNNER=host no .env."
        )


def _rodar_docker(
    workspace: str, thread_id: str, perfil: PerfilStack
) -> tuple[int, str]:
    _checar_docker(perfil)
    raiz = Path(workspace).resolve()
    nome = f"qa-{thread_id}"
    comando = [
        "docker", "run", "--rm", "--name", nome,
        # Entrega read-only: um rm -rf dentro do container não toca o host.
        "-v", f"{raiz.as_posix()}:/src:ro",
        # Única superfície de escrita, restrita ao diretório de trabalho da squad.
        "-v", f"{(raiz / DIR_SAIDA).as_posix()}:{DIR_SAIDA_CONTAINER}",
        "--network", "none",
        "--memory", perfil.memoria, "--cpus", perfil.cpus,
        perfil.imagem_sandbox,
        "sh", "-c", perfil.comando_container(DIR_SAIDA_CONTAINER),
    ]
    print(
        f">>> Executando {perfil.runner} em sandbox Docker "
        f"({perfil.imagem_sandbox}, sem rede)..."
    )
    try:
        r = subprocess.run(
            comando, capture_output=True, text=True, encoding="utf-8",
            errors="replace", stdin=subprocess.DEVNULL,
            timeout=perfil.timeout_testes,
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
            f"TIMEOUT: os testes excederam {perfil.timeout_testes}s — provável "
            f"loop infinito ou teste travado. Container encerrado.\n"
            f"Saída parcial:\n{parcial[-2_000:]}"
        )


def _rodar_host(workspace: str, perfil: PerfilStack) -> tuple[int, str]:
    if not perfil.permite_host:
        raise RuntimeError(
            f"TEST_RUNNER=host não existe para a stack '{perfil.nome}': o runner "
            "de host roda a suíte no interpretador da própria squad, o que só "
            "faz sentido quando a entrega também é Python. Use TEST_RUNNER=docker."
        )
    print(f">>> Executando {perfil.runner} no host (sem jaula — TEST_RUNNER=host)...")
    try:
        r = subprocess.run(
            perfil.comando_host(DIR_SAIDA), cwd=workspace, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL,  # teste que pede input falha, não trava
            timeout=perfil.timeout_testes,
        )
        return r.returncode, ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    except subprocess.TimeoutExpired as e:
        parcial = str(e.stdout or "")
        return 1, (
            f"TIMEOUT: os testes excederam {perfil.timeout_testes}s — provável "
            f"loop infinito ou teste travado.\nSaída parcial:\n{parcial[-2_000:]}"
        )


def executar_testes(
    workspace: str, thread_id: str, perfil: PerfilStack
) -> ResultadoTestes:
    """Roda a suíte no workspace e devolve veredito objetivo.

    Exit code 0 = verde; qualquer outro (inclusive 5, "nenhum teste coletado")
    = vermelho. Timeout = vermelho.
    """
    _preparar(workspace, perfil)
    runner = (os.getenv("TEST_RUNNER") or "docker").strip().lower()
    if runner == "docker":
        codigo, saida = _rodar_docker(workspace, thread_id, perfil)
    elif runner == "host":
        codigo, saida = _rodar_host(workspace, perfil)
    else:
        raise ValueError(f"TEST_RUNNER inválido: '{runner}'. Use 'docker' ou 'host'.")

    testes_ok = codigo == 0
    cobertura = _cobertura(workspace, perfil)
    detalhe_pior = (
        f" (pior: {cobertura.pior_arquivo} {cobertura.pior}%)"
        if cobertura.pior_arquivo
        else ""
    )
    print(
        f">>> {perfil.runner} exit code {codigo} — "
        f"{'verde' if testes_ok else 'vermelho'}"
        f" | cobertura {cobertura.total}%{detalhe_pior}"
    )
    return ResultadoTestes(testes_ok=testes_ok, saida=saida, cobertura=cobertura)
