"""Subir a entrega como servidor, numa rede isolada.

Dois nós precisam disto e pela mesma razão: julgar a entrega **de pé**, não em
repouso. O pentest a ataca; a verificação visual a renderiza. O que muda é o que
se faz com o alvo no ar — subir o alvo é igual nos dois.

A contenção é uma bridge Docker `--internal`: sem gateway para a internet. O
alvo é código gerado por LLM, e quem fala com ele (atacante ou navegador)
também roda em container. É o mesmo princípio do `--network none` do sandbox,
aplicado à fronteira externa em vez de a toda a rede — aqui o alvo *precisa*
estar alcançável, então a rede existe e o que não existe é a saída.
"""
import json
import os
import shlex
import subprocess
from pathlib import Path

DIR_SQUAD = ".squad"
ARQUIVO_RUN = f"{DIR_SQUAD}/run.json"

MEMORIA = "512m"
CPUS = "1"


def shquote(arg: str) -> str:
    """Escapa um argumento para o `sh -c` que roda dentro do container Linux.
    `shlex.quote` é POSIX — vale para o shell do container, não para o host."""
    return shlex.quote(str(arg))


def docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, encoding="utf-8",
        errors="replace", stdin=subprocess.DEVNULL, timeout=timeout,
    )


def ler_run(workspace: str, exigido_por: str, como_desligar: str) -> dict:
    """Manifesto de subida declarado pelo executor.

    É a resposta ao problema "a entrega precisa subir e o comando varia": em vez
    de adivinhar o entrypoint a partir do README em prosa, o executor grava o
    comando exato. As mensagens de erro citam quem exigiu o manifesto, porque
    dois nós diferentes chegam aqui.
    """
    arquivo = Path(workspace) / ARQUIVO_RUN
    if not arquivo.is_file():
        raise RuntimeError(
            f"{exigido_por} exige {ARQUIVO_RUN}, que não existe em {workspace}. O "
            "executor deve declarar como subir a entrega (comando, porta, health "
            "path) — sem isso não há como pôr o alvo no ar. Confira se a instrução "
            f"de gravar o run.json chegou ao executor, ou {como_desligar}."
        )
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{ARQUIVO_RUN} não é JSON válido ({e}).")
    cmd = dados.get("cmd")
    porta = dados.get("port")
    if not isinstance(cmd, list) or not cmd or not isinstance(porta, int):
        raise RuntimeError(
            f"{ARQUIVO_RUN} inválido: precisa de 'cmd' (lista não vazia) e 'port' "
            f"(inteiro). Recebido: {dados!r}"
        )
    return {"cmd": cmd, "port": porta, "health_path": dados.get("health_path", "/")}


def checar_docker(exigido_por: str, como_desligar: str) -> None:
    try:
        r = docker("info", "--format", "{{.ServerVersion}}", timeout=30)
    except FileNotFoundError:
        raise RuntimeError(
            f"Docker não encontrado no PATH. {exigido_por} exige Docker (a entrega "
            f"sobe num sandbox isolado). Instale o Docker Desktop, ou {como_desligar}."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Docker não respondeu em 30s — o daemon está saudável?")
    # `docker info` sai com 0 mesmo com o daemon morto (imprime o erro no lugar
    # da versão): confiar só no exit code deixaria a falha aparecer depois,
    # disfarçada de erro da entrega.
    versao = (r.stdout or "").strip()
    if r.returncode != 0 or not versao or "error" in versao.lower():
        detalhe = versao or (r.stderr or "").strip()
        raise RuntimeError(f"Daemon do Docker não está respondendo.\n{detalhe[:300]}")


def checar_imagem(imagem: str, dockerfile: str, como_desligar: str) -> None:
    r = docker("image", "inspect", imagem, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(
            f"Imagem {imagem} não encontrada. Construa uma vez com:\n"
            f"  docker build -f {dockerfile} -t {imagem} .\n"
            f"Ou {como_desligar}."
        )


def derrubar(rede: str, *containers: str) -> None:
    """Teardown incondicional: mata os containers e remove a rede.

    Chamado no `finally` — perder a limpeza deixa um alvo no ar e uma rede
    órfã, e matar o cliente docker não mata o container (mesma lição do
    `docker kill` do sandbox).
    """
    for nome in containers:
        for acao in (("kill", nome), ("rm", "-f", nome)):
            try:
                docker(*acao, timeout=30)
            except (subprocess.SubprocessError, OSError):
                pass
    try:
        docker("network", "rm", rede, timeout=30)
    except (subprocess.SubprocessError, OSError):
        pass


def subir(workspace: str, rede: str, nome_alvo: str, perfil, run: dict) -> None:
    """Cria a rede interna e põe a entrega no ar dentro dela.

    A entrega entra read-only e é copiada para um diretório gravável antes de
    subir — o mesmo padrão do sandbox, para o app poder escrever seu SQLite sem
    tocar o que está no host. `exec` no fim para o servidor virar o PID 1 do
    container e receber o sinal de kill do teardown.

    `preparo_alvo` roda antes do comando: `next start` sem `next build` não sobe,
    e `java -cp target/classes` sem `mvn compile` não acha classe nenhuma. Quem
    declara o preparo é o perfil, porque é a stack que sabe do que precisa.
    """
    cr = docker("network", "create", "--internal", rede)
    if cr.returncode != 0:
        raise RuntimeError(f"Falha ao criar a rede interna: {cr.stderr.strip()}")

    raiz = Path(workspace).resolve()
    cmd_str = " ".join(shquote(a) for a in run["cmd"])
    r = docker(
        "run", "-d", "--name", nome_alvo, "--network", rede,
        "-v", f"{raiz.as_posix()}:/src:ro",
        "--memory", os.getenv("ALVO_MEMORIA", MEMORIA),
        "--cpus", os.getenv("ALVO_CPUS", CPUS),
        perfil.imagem_alvo, "sh", "-c",
        f"cp -r /src /app/projeto && cd /app/projeto && "
        f"{perfil.preparo_alvo}exec {cmd_str}",
    )
    if r.returncode != 0:
        raise RuntimeError(f"Falha ao subir o alvo: {r.stderr.strip()}")


def logs(nome_alvo: str, limite: int = 2_000) -> str:
    """Saída do container do alvo — é o que explica um alvo que não sobe."""
    try:
        r = docker("logs", "--tail", "80", nome_alvo, timeout=30)
    except (subprocess.SubprocessError, OSError):
        return ""
    return ((r.stdout or "") + "\n" + (r.stderr or "")).strip()[-limite:]
