"""Verificação visual da entrega: a contraparte de execução do que se vê (Fase 9).

Terceira camada de veredito por execução, ao lado do pytest e do pentest. O
pytest prova que o código funciona; o pentest, que não é trivialmente
explorável; este nó, que a entrega é legível na tela de quem abrir.

Existe porque há uma classe inteira de defeito que nenhuma das outras camadas
alcança: o revisor LLM lê `color: #1f2937` e não sabe o que aparece atrás, e o
pytest não pinta pixel. Medido numa entrega real da squad — uma página que
declarava cor de texto e nenhum fundo passou por 42 testes verdes e por um
revisor que aprovou, e estava ilegível no tema escuro do sistema.

A jaula é a do pytest, não a do pentest: `--network none`. Abrir HTML gerado
por LLM num navegador é executar código de terceiro, mas ao contrário do
pentest não há alvo a alcançar — as páginas são abertas por `file://`.

**Escopo, dito na cara:** renderiza a entrega estática. O que só aparece depois
de um `fetch` (linhas de tabela, gráficos com dados) não é medido, porque sem
rede o fetch não completa. Cobre o esqueleto da página — fundo, títulos,
cabeçalhos de tabela, rótulos, cartões —, que é onde mora o defeito de tema e
contraste. Não substitui olho humano em layout.
"""
import json
import os
import subprocess
from pathlib import Path

IMAGEM = "squad-visual:latest"

# Renderizar é rápido (duas passadas por página num Chromium já quente), mas o
# primeiro start do navegador é lento. Como no pentest, estourar mata a run e
# encerra tudo em vez de travar o grafo.
TIMEOUT_VISUAL = int(os.getenv("TIMEOUT_VISUAL", "180"))
MEMORIA = "1g"   # Chromium não sobe em 512m
CPUS = "1"
LIMITE_FEEDBACK = 12  # achados listados no brief de correção

# Diretórios que não são a entrega vista pelo usuário.
_IGNORAR = {"tests", ".squad", "node_modules", "__pycache__", ".git"}


def habilitado() -> bool:
    """Só `1` liga. O nó existe sempre no grafo; desligado, curto-circuita com
    veredito verde — quem ainda não construiu a imagem não muda de comportamento.
    Mesmo contrato do pentest."""
    return (os.getenv("VISUAL_HABILITADO") or "0").strip() == "1"


def _checar_docker_e_imagem() -> None:
    try:
        r = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        raise RuntimeError(
            "Verificação visual exige Docker e ele não respondeu. Suba o Docker, "
            "ou desligue com VISUAL_HABILITADO=0 no .env."
        )
    if r.returncode != 0:
        raise RuntimeError(
            f"Verificação visual exige Docker: {(r.stderr or r.stdout).strip()[:200]}"
        )

    r = subprocess.run(
        ["docker", "image", "inspect", IMAGEM],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"Imagem {IMAGEM} não encontrada. Construa uma vez:\n"
            f"  docker build -f Dockerfile.visual -t squad-visual:latest .\n"
            "Ou desligue com VISUAL_HABILITADO=0 no .env."
        )


def _paginas(workspace: str) -> list[str]:
    raiz = Path(workspace)
    return sorted(
        str(p.relative_to(raiz)).replace("\\", "/")
        for p in raiz.rglob("*.html")
        if not _IGNORAR.intersection(p.relative_to(raiz).parts)
    )


def _formatar_feedback(problemas: list[dict]) -> str:
    """Brief de correção determinístico, no mesmo formato do pentest: cada
    achado vira um item com o seletor, o número medido e o exigido. Sem LLM no
    caminho — o feedback é a medição, não uma paráfrase dela."""
    linhas = [
        "A verificação visual reprovou a entrega. Os problemas abaixo foram "
        "medidos num navegador real, com o tema do sistema emulado nos dois "
        "modos. Corrija no CSS da própria página:",
        "",
    ]
    for i, p in enumerate(problemas[:LIMITE_FEEDBACK], 1):
        alvo = f"{p['arquivo']} (tema {p['tema']})"
        if p["tipo"] == "fundo_nao_declarado":
            linhas.append(
                f"{i}. [{p['tipo']}] {p['arquivo']} — {p['detalhe']}. Declare "
                "background e color no body, e defina as cores como variáveis "
                "redefinidas dentro de @media (prefers-color-scheme: dark)."
            )
        elif p["tipo"] == "pagina_nao_renderiza":
            linhas.append(f"{i}. [{p['tipo']}] {alvo} — {p['detalhe']}")
        else:
            linhas.append(
                f"{i}. [contraste] {alvo} — `{p['seletor']}` "
                f"(\"{p['texto']}\"): {p['razao']}:1, exigido {p['exigido']}:1. "
                f"Texto {p['cor']} sobre {p['fundo']}"
                + ("" if p.get("fundo_declarado") else " (fundo herdado do navegador)")
                + "."
            )
    if len(problemas) > LIMITE_FEEDBACK:
        linhas.append(f"... e mais {len(problemas) - LIMITE_FEEDBACK} achado(s).")
    return "\n".join(linhas)


def executar_visual(workspace: str, thread_id: str) -> dict:
    """Renderiza a entrega e devolve veredito objetivo.

    visual_ok = nenhum problema de contraste ou de fundo em nenhum dos temas.
    Entrega sem HTML passa: não há o que renderizar, e reprovar por ausência
    transformaria o nó num requisito de front-end que a spec pode não ter.
    """
    paginas = _paginas(workspace)
    if not paginas:
        print(">>> Visual: entrega sem HTML — nada a renderizar.")
        return {"visual_ok": True, "problemas": [], "feedback_visual": "", "paginas": 0}

    _checar_docker_e_imagem()
    raiz = Path(workspace).resolve()
    print(f">>> Visual: renderizando {len(paginas)} página(s) em jaula sem rede...")

    try:
        r = subprocess.run(
            [
                "docker", "run", "--rm", "--name", f"visual-{thread_id}",
                "--network", "none",
                "--memory", MEMORIA, "--cpus", CPUS,
                # A entrega entra read-only: renderizar não pode alterar o que
                # está sendo julgado.
                "-v", f"{raiz.as_posix()}:/entrega:ro",
                IMAGEM, "/entrega",
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=TIMEOUT_VISUAL,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Verificação visual excedeu {TIMEOUT_VISUAL}s. Aumente TIMEOUT_VISUAL "
            "ou investigue a página (script travando o load)."
        )

    if r.returncode != 0:
        raise RuntimeError(
            f"Verificação visual falhou (exit {r.returncode}):\n"
            f"{(r.stderr or r.stdout).strip()[:1000]}"
        )

    try:
        medida = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError(
            "Verificação visual não devolveu JSON — saída do container:\n"
            f"{(r.stdout or r.stderr).strip()[:1000]}"
        )

    problemas = medida.get("problemas", [])
    aprovado = not problemas
    if aprovado:
        print(f">>> Visual: {medida.get('paginas', 0)} página(s) sem achado — verde.")
    else:
        print(f">>> Visual: {len(problemas)} achado(s) — vermelho.")

    return {
        "visual_ok": aprovado,
        "problemas": problemas,
        "feedback_visual": "" if aprovado else _formatar_feedback(problemas),
        "paginas": medida.get("paginas", 0),
    }
