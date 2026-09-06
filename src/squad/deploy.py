"""Deploy real da entrega (Fase 4): git commit + push atrás do gate humano.

Determinístico, sem LLM. Um projeto, um repositório: o workspace vira um
repositório git próprio e é publicado num repositório **dele**, criado no remoto
se ainda não existir. Sem `DEPLOY_OWNER`, degrada para commit local com aviso.

A versão anterior empurrava toda entrega como a branch `entrega/<thread_id>` de
um único repositório fixo. Isso concentrava todas as entregas num ponto de perda
só: quando `SamukDantas/squad-entregas` foi apagado, as três entregas de agosto
de 2026 foram junto, e a squad só descobriu o sumiço no último nó — depois de
pagar a execução inteira.

Falha de qualquer comando (exit != 0 ou timeout) levanta RuntimeError com o
stderr real — o checkpoint preserva o progresso e a retomada com --thread
reexecuta apenas o deploy.
"""
import os
import re
import subprocess
import unicodedata
from pathlib import Path

TIMEOUT_GIT = 60  # segundos por comando git
TIMEOUT_GH = 60   # segundos por comando gh
LIMITE_SLUG = 50  # chars do nome do repositório derivado do pedido

_GITIGNORE_ENTREGA = (
    "__pycache__/\n.pytest_cache/\n.ruff_cache/\n.squad/\n*.pyc\n"
    # Rastro de comandos rodados pelo executor: não é entrega.
    "*.log\n*_output.txt\n*_result.txt\n*_results.txt\n"
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


def _gh(*args: str) -> subprocess.CompletedProcess:
    """Roda o gh CLI e devolve o resultado cru — quem chama decide o que é falha.

    Criar repositório é a única coisa que a squad faz fora da própria máquina, e
    `gh repo view` falhando é informação normal (o repositório ainda não existe),
    não erro.
    """
    try:
        return subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT_GH,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "Deploy falhou: o GitHub CLI (gh) não está no PATH, e é ele que cria "
            "o repositório da entrega. Instale-o, ou deixe DEPLOY_OWNER vazio no "
            ".env para a squad commitar apenas localmente."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Deploy falhou: 'gh {' '.join(args)}' excedeu {TIMEOUT_GH}s.")


def _conta_ativa() -> str:
    """Conta com que o gh vai agir. Só para compor mensagem de erro útil: a
    máquina pode ter várias contas autenticadas, e a ativa não é
    necessariamente a dona do repositório alvo."""
    r = _gh("auth", "status", "--active")
    for linha in (r.stdout + r.stderr).splitlines():
        achado = re.search(r"account (\S+)", linha)
        if achado:
            return achado.group(1)
    return "(desconhecida)"


def _dono() -> str:
    """Dono dos repositórios de entrega, de `DEPLOY_OWNER`.

    Aceita `DEPLOY_REPO` (owner/repo) do formato antigo como fallback, lendo só
    o dono: quem já tinha o `.env` configurado continua publicando sem editar
    nada, no lugar de cair silenciosamente para commit local.
    """
    dono = (os.getenv("DEPLOY_OWNER") or "").strip()
    if dono:
        return dono
    antigo = (os.getenv("DEPLOY_REPO") or "").strip()
    if antigo:
        dono = antigo.split("/", 1)[0]
        print(
            f">>> Deploy: DEPLOY_OWNER não configurado; usando o dono de "
            f"DEPLOY_REPO ({dono}). Um projeto agora vira um repositório próprio, "
            "então DEPLOY_REPO virou só o dono — troque por DEPLOY_OWNER no .env."
        )
        return dono
    return ""


def _slug(pedido: str, thread_id: str) -> str:
    """Nome do repositório a partir do pedido.

    O `thread_id` é um UUID opaco e não diz nada a quem abre a lista de
    repositórios; o pedido diz. Corta na fronteira de palavra para o nome não
    terminar no meio de uma, e cai no thread_id quando o pedido não sobra nada
    utilizável (só pontuação, ou outro alfabeto).
    """
    texto = unicodedata.normalize("NFKD", pedido)
    texto = texto.encode("ascii", "ignore").decode("ascii").lower()
    texto = re.sub(r"[^a-z0-9]+", "-", texto).strip("-")
    if len(texto) > LIMITE_SLUG:
        cortado = texto[:LIMITE_SLUG].rsplit("-", 1)[0]
        texto = cortado or texto[:LIMITE_SLUG]
    return texto.strip("-") or f"entrega-{thread_id[:8]}"


def _avisar_conta(dono: str) -> None:
    """Aviso cedo quando a conta gh ativa não é a dona das entregas.

    Não é erro: a conta ativa pode ser colaboradora, ou administrar a org dona.
    Mas quando não é nenhuma das duas, tudo daqui para a frente falha com
    'Repository not found' — que parece repositório inexistente e é, na verdade,
    repositório invisível. Medido nesta máquina: com 'IversoSolucao' ativa, nem
    o gh nem o git resolvem os repositórios privados de 'SamukDantas', mesmo
    estando as duas contas autenticadas.
    """
    ativa = _conta_ativa()
    if ativa != dono:
        print(
            f">>> Deploy: a conta gh ativa é '{ativa}', e o dono das entregas é "
            f"'{dono}'. Se '{ativa}' não administra '{dono}', o repositório dele "
            f"é invisível daqui — rode 'gh auth switch --user {dono}' antes."
        )


def _repo_existe(alvo: str) -> bool:
    """Se o repositório é visível para a conta gh ativa.

    Falso não prova que não existe: um repositório privado de outra conta
    responde o mesmo 'Could not resolve to a Repository' que um inexistente, e o
    gh não distingue os dois. Quem chama trata isso tentando criar — e a falha
    de criação carrega a conta ativa na mensagem, que é o que resolve o caso.
    """
    return _gh("repo", "view", alvo, "--json", "name").returncode == 0


def _criar_repo(alvo: str, pedido: str) -> None:
    """Cria o repositório da entrega no remoto.

    Privado por padrão: a entrega é código que ninguém revisou publicamente
    ainda, e tornar público é decisão que se toma uma vez, não um efeito
    colateral do deploy. `DEPLOY_VISIBILIDADE=public` inverte.
    """
    visibilidade = (os.getenv("DEPLOY_VISIBILIDADE") or "private").strip().lower()
    if visibilidade not in {"private", "public"}:
        raise RuntimeError(
            f"DEPLOY_VISIBILIDADE inválida: '{visibilidade}'. Use 'private' ou 'public'."
        )
    descricao = " ".join(pedido.split())[:250]
    print(f">>> Deploy: criando repositório {alvo} ({visibilidade})...")
    r = _gh("repo", "create", alvo, f"--{visibilidade}", "-d", descricao)
    if r.returncode != 0:
        saida = (r.stderr or r.stdout).strip()
        raise RuntimeError(
            f"Deploy falhou ao criar {alvo}:\n{saida}\n"
            f"A conta gh ativa é '{_conta_ativa()}'. Só dá para criar repositório "
            f"sob a própria conta ou sob organização que ela administre — se o "
            f"dono for outra conta autenticada, rode "
            f"'gh auth switch --user {alvo.split('/', 1)[0]}' e retome a thread."
        )


def _ref_de_push(workspace: str, url: str, thread_id: str) -> str:
    """Para onde empurrar dentro do repositório do projeto.

    Repositório novo (sem branch nenhuma) recebe a entrega em `main`, que é o
    caso normal de um projeto novo. Repositório que já tem commits recebe em
    `entrega/<thread_id>`: cada execução tem workspace próprio e portanto
    histórico git sem ancestral comum, então empurrar para `main` seria rejeitado
    por unrelated history — e forçar apagaria a entrega anterior.
    """
    if _git(workspace, "ls-remote", "--heads", url).strip():
        return f"entrega/{thread_id}"
    return "main"


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

    dono = _dono()
    if not dono:
        print(
            ">>> Deploy: DEPLOY_OWNER não configurado — entrega commitada apenas "
            f"localmente ({commit[:10]}). Configure DEPLOY_OWNER=<conta> no .env "
            "para publicar."
        )
        return {"deploy_ok": True, "deploy_ref": f"commit local {commit[:10]}"}

    _avisar_conta(dono)
    alvo = f"{dono}/{_slug(pedido, thread_id)}"
    url = f"https://github.com/{alvo}.git"

    # Idempotente de propósito: retomar a thread reexecuta este nó inteiro, e o
    # repositório criado na tentativa anterior não pode virar erro na seguinte.
    if not _repo_existe(alvo):
        _criar_repo(alvo, pedido)
    else:
        print(f">>> Deploy: repositório {alvo} já existe, reaproveitando.")

    ref = _ref_de_push(workspace, url, thread_id)
    print(f">>> Deploy: publicando {ref} em {alvo}...")
    _git(workspace, "push", url, f"HEAD:refs/heads/{ref}")
    return {"deploy_ok": True, "deploy_ref": f"{alvo}@{ref}"}
