"""Deploy real da entrega (Fase 4): git commit + push atrás do gate humano.

Determinístico, sem LLM. O workspace vira um repositório git próprio; a
entrega é commitada e, se DEPLOY_REPO (owner/repo) estiver configurado,
publicada na branch entrega/<thread_id> do repositório alvo. Sem
DEPLOY_REPO, degrada para commit local com aviso.

Falha de qualquer comando (exit != 0 ou timeout) levanta RuntimeError com o
stderr real — o checkpoint preserva o progresso e a retomada com --thread
reexecuta apenas o deploy.
"""
import os
import subprocess
from pathlib import Path

TIMEOUT_GIT = 60  # segundos por comando

_GITIGNORE_ENTREGA = (
    "__pycache__/\n.pytest_cache/\n.ruff_cache/\n.squad/\n*.pyc\n"
)


def _git(workspace: str, *args: str) -> str:
    comando = ["git", *args]
    try:
        r = subprocess.run(
            comando,
            cwd=workspace,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,  # git nunca deve pedir input interativo
            timeout=TIMEOUT_GIT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Deploy falhou: 'git {' '.join(args)}' excedeu {TIMEOUT_GIT}s."
        )
    if r.returncode != 0:
        raise RuntimeError(
            f"Deploy falhou em 'git {' '.join(args)}' (exit {r.returncode}):\n"
            f"{(r.stderr or r.stdout).strip()}"
        )
    return r.stdout.strip()


def executar_deploy(workspace: str, thread_id: str, pedido: str) -> dict:
    raiz = Path(workspace)

    # A entrega não leva lixo de execução (caches do pytest/CPython).
    (raiz / ".gitignore").write_text(_GITIGNORE_ENTREGA, encoding="utf-8")

    if not (raiz / ".git").is_dir():
        _git(workspace, "init")

    _git(workspace, "add", "-A")
    status = _git(workspace, "status", "--porcelain")
    if status:
        _git(workspace, "commit", "-m", f"entrega: {pedido} (thread {thread_id})")
    else:
        print(">>> Deploy: nada novo a commitar (re-deploy sem mudanças).")

    commit = _git(workspace, "rev-parse", "HEAD")

    repo_alvo = (os.getenv("DEPLOY_REPO") or "").strip()
    if not repo_alvo:
        print(
            ">>> Deploy: DEPLOY_REPO não configurado — entrega commitada apenas "
            f"localmente ({commit[:10]}). Configure DEPLOY_REPO=owner/repo no "
            ".env para publicar."
        )
        return {"deploy_ok": True, "deploy_ref": f"commit local {commit[:10]}"}

    ref = f"entrega/{thread_id}"
    url = f"https://github.com/{repo_alvo}.git"
    print(f">>> Deploy: publicando {ref} em {repo_alvo}...")
    _git(workspace, "push", url, f"HEAD:refs/heads/{ref}")
    return {"deploy_ok": True, "deploy_ref": f"{repo_alvo}@{ref}"}
