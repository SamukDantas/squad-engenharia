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

from . import alvo as infra

IMAGEM = "squad-visual:latest"

# Renderizar é rápido (duas passadas por página num Chromium já quente), mas o
# primeiro start do navegador é lento. Como no pentest, estourar mata a run e
# encerra tudo em vez de travar o grafo.
TIMEOUT_VISUAL = int(os.getenv("TIMEOUT_VISUAL", "180"))
MEMORIA = "1g"   # Chromium não sobe em 512m
CPUS = "1"
LIMITE_FEEDBACK = 12  # achados listados no brief de correção
LIMITE_LOG_ALVO = 4_000  # chars do log de boot devolvidos ao dev

# Diretórios que não são a entrega vista pelo usuário.
_IGNORAR = {"tests", ".squad", "node_modules", "__pycache__", ".git"}


def habilitado() -> bool:
    """Só `1` liga. O nó existe sempre no grafo; desligado, curto-circuita com
    veredito verde — quem ainda não construiu a imagem não muda de comportamento.
    Mesmo contrato do pentest."""
    return (os.getenv("VISUAL_HABILITADO") or "0").strip() == "1"


_DESLIGAR = "desligue com VISUAL_HABILITADO=0 no .env"


def _checar_docker_e_imagem(*imagens) -> None:
    infra.checar_docker("A verificação visual", _DESLIGAR)
    for imagem, dockerfile in imagens:
        infra.checar_imagem(imagem, dockerfile, _DESLIGAR)


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
    # O cabeçalho segue o achado: mandar "corrija no CSS" para quem tem um
    # servidor que não inicia aponta o dev para o lugar errado.
    if any(p["tipo"] == "alvo_nao_sobe" for p in problemas):
        linhas = [
            "A verificação visual reprovou a entrega porque ela não entrou "
            "no ar. O que segue veio do próprio processo, ao subir:",
            "",
        ]
    else:
        linhas = [
            "A verificação visual reprovou a entrega. Os problemas abaixo "
            "foram medidos num navegador real, com o tema do sistema emulado "
            "nos dois modos. Corrija no CSS da própria página:",
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
        elif p["tipo"] == "alvo_nao_sobe":
            linhas.append(
                f"{i}. [{p['tipo']}] A ENTREGA NÃO INICIA. Ela compila e passa "
                "nos testes, mas o servidor encerrou em vez de atender. Os "
                "testes não pegam isto porque dublam o que falta; o log de boot "
                "abaixo diz o que é. Corrija antes de qualquer outra coisa:\n"
                f"{p.get('log', '(sem log)')}"
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


def _parsear(saida: str, erro: str) -> dict:
    try:
        return json.loads(saida.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError(
            "Verificação visual não devolveu JSON — saída do container:\n"
            f"{(saida or erro).strip()[:1000]}"
        )


def _renderizar_arquivos(workspace: str, thread_id: str, paginas: list[str]) -> dict:
    """Modo arquivo: entrega com HTML estático, na jaula sem rede."""
    _checar_docker_e_imagem((IMAGEM, "Dockerfile.visual"))
    raiz = Path(workspace).resolve()
    print(f">>> Visual: renderizando {len(paginas)} página(s) em jaula sem rede...")
    r = subprocess.run(
        [
            "docker", "run", "--rm", "--name", infra.nome_de_rede("visual", thread_id),
            "--network", "none",
            "--memory", MEMORIA, "--cpus", CPUS,
            # A entrega entra read-only: renderizar não pode alterar o que está
            # sendo julgado.
            "-v", f"{raiz.as_posix()}:/entrega:ro",
            IMAGEM, "/entrega",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=TIMEOUT_VISUAL,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"Verificação visual falhou (exit {r.returncode}):\n"
            f"{(r.stderr or r.stdout).strip()[:1000]}"
        )
    return _parsear(r.stdout, r.stderr)


def _renderizar_servida(workspace: str, thread_id: str, perfil) -> dict:
    """Modo SPA: sobe a entrega e renderiza o que o servidor devolve.

    É o único caminho possível em Next.js e afins, que não deixam HTML estático
    no workspace — ali o nó virava no-op silencioso justamente na stack onde
    seria mais útil.

    A jaula muda de forma, não de princípio: em vez de `--network none`, uma
    bridge Docker `--internal` sem rota para a internet, com o alvo e o
    navegador dentro dela. É a mesma contenção do pentest, e pela mesma razão —
    o alvo *precisa* estar alcançável, então o que se corta é a saída.
    """
    _checar_docker_e_imagem(
        (IMAGEM, "Dockerfile.visual"),
        (perfil.imagem_alvo, perfil.dockerfile_alvo),
    )
    run = infra.ler_run(workspace, "A verificação visual de SPA", _DESLIGAR)
    rede = infra.nome_de_rede("squad-visual", thread_id)
    nome_alvo = infra.nome_de_rede("alvo-visual", thread_id)
    base = f"http://{nome_alvo}:{run['port']}/"

    print(f">>> Visual: subindo a entrega em rede interna isolada ({rede})...")
    try:
        infra.subir(workspace, rede, nome_alvo, perfil, run)
        print(f">>> Visual: renderizando {base} e um nível de links...")
        r = subprocess.run(
            [
                "docker", "run", "--rm", "--name", infra.nome_de_rede("visual", thread_id),
                "--network", rede,
                "--memory", MEMORIA, "--cpus", CPUS,
                IMAGEM, base,
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=TIMEOUT_VISUAL,
        )
        if r.returncode != 0:
            # Dois desfechos que o pipeline confundia, e que têm destinatários
            # diferentes: a entrega que NÃO INICIA é defeito do dev, e o log de
            # boot é a instrução de conserto; qualquer outra falha é da squad.
            #
            # Medido numa execução real: um serviço com 95,4% de cobertura,
            # revisão aprovada e ambientes verdes não subia por uma propriedade
            # Spring sem valor. O pipeline chamou isso de "verificação visual
            # falhou" e truncou o traceback em 300 chars na métrica — a causa
            # real ficou dentro do container e nunca chegou a ninguém.
            if infra.morreu(nome_alvo):
                return {
                    "paginas": 0,
                    "problemas": [{
                        "tipo": "alvo_nao_sobe",
                        "arquivo": "(a entrega inteira)",
                        "tema": "-",
                        "detalhe": "o servidor encerrou em vez de atender",
                        "log": infra.logs(nome_alvo, LIMITE_LOG_ALVO),
                    }],
                }
            raise RuntimeError(
                f"Verificação visual falhou (exit {r.returncode}):\n"
                f"{(r.stderr or r.stdout).strip()[:800]}\n"
                f"--- log do alvo ---\n{infra.logs(nome_alvo)}"
            )
        return _parsear(r.stdout, r.stderr)
    finally:
        infra.derrubar(rede, nome_alvo)


def executar_visual(workspace: str, thread_id: str, perfil) -> dict:
    """Renderiza a entrega e devolve veredito objetivo.

    visual_ok = nenhum problema de contraste ou de fundo em nenhum dos temas.

    Dois caminhos, e a escolha é do que a entrega é, não de configuração:
    HTML estático no workspace é aberto por `file://`; entrega que só existe
    servida sobe como alvo. Entrega sem HTML **e** sem manifesto de subida passa
    — não há o que renderizar, e reprovar por ausência transformaria o nó num
    requisito de front-end que a spec pode não ter.
    """
    paginas = _paginas(workspace)
    if paginas:
        medida = _renderizar_arquivos(workspace, thread_id, paginas)
    elif (Path(workspace) / infra.ARQUIVO_RUN).is_file():
        medida = _renderizar_servida(workspace, thread_id, perfil)
    else:
        print(">>> Visual: entrega sem HTML e sem run.json — nada a renderizar.")
        return {"visual_ok": True, "problemas": [], "feedback_visual": "", "paginas": 0}

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
