"""Métricas por execução: como saber se uma mudança melhorou o resultado.

Sem isto, cada fase do roadmap é uma aposta — o pipeline "funciona", mas não
há como comparar duas execuções. Cada nó registra um evento com duração e
veredito em `metrics/<thread_id>.json`, e o `main.py` imprime o resumo ao
final.

Escrita append-and-rewrite (arquivo pequeno, uma execução por arquivo): o
histórico sobrevive a quedas do processo, como os checkpoints do grafo.
"""
import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DIR_METRICAS = Path("metrics")


def _arquivo(thread_id: str) -> Path:
    DIR_METRICAS.mkdir(parents=True, exist_ok=True)
    return DIR_METRICAS / f"{thread_id}.json"


def _carregar(thread_id: str) -> list[dict]:
    arquivo = _arquivo(thread_id)
    if not arquivo.is_file():
        return []
    try:
        return json.loads(arquivo.read_text(encoding="utf-8"))
    except ValueError:  # arquivo truncado por queda no meio da escrita
        return []


def registrar(thread_id: str, evento: str, **dados) -> None:
    """Acrescenta um evento ao histórico da execução."""
    eventos = _carregar(thread_id)
    eventos.append(
        {
            "evento": evento,
            "quando": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **dados,
        }
    )
    _arquivo(thread_id).write_text(
        json.dumps(eventos, ensure_ascii=False, indent=2), encoding="utf-8"
    )


@contextmanager
def medir(thread_id: str, evento: str, **dados):
    """Registra o evento com a duração do bloco.

    Falha do nó também é registrada (com `erro`) antes de propagar: uma
    execução que quebrou é justamente a que se quer analisar depois.

    Grava `inicio` além de `quando` (que é o fim do bloco). Reconstruir o
    início como `quando - duracao_s` mistura dois relógios: `quando` é wall
    clock e `duracao_s` vem do monotônico, que em algumas plataformas não
    conta suspensão do sistema. Numa execução longa a diferença desloca a
    barra na linha do tempo — e quem lê o histórico não deveria ter que
    reconstruir o início.
    """
    inicio = time.monotonic()
    inicio_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    extras: dict = {}
    try:
        yield extras
    except Exception as e:
        registrar(
            thread_id, evento,
            inicio=inicio_iso,
            duracao_s=round(time.monotonic() - inicio, 1),
            erro=f"{type(e).__name__}: {e}"[:300],
            **dados, **extras,
        )
        raise
    registrar(
        thread_id, evento,
        inicio=inicio_iso,
        duracao_s=round(time.monotonic() - inicio, 1),
        **dados, **extras,
    )


def resumo(thread_id: str) -> str:
    """Bloco legível com os indicadores da execução."""
    eventos = _carregar(thread_id)
    if not eventos:
        return ""

    def _dos(nome: str) -> list[dict]:
        return [e for e in eventos if e["evento"] == nome]

    testes = _dos("executar_testes")
    revisoes = _dos("revisao")
    linhas = [
        "",
        "=" * 62,
        f"MÉTRICAS DA EXECUÇÃO  ({thread_id})",
        "=" * 62,
        f"  Rodadas de desenvolvimento : {len(_dos('desenvolvimento'))}",
        f"  Rodadas de escrita de teste: {len(_dos('escrever_testes'))}",
        f"  Replanejamentos (guard spec): {len(_dos('planejamento'))}",
    ]
    if testes:
        historico = " → ".join(
            f"{'verde' if e.get('testes_ok') else 'vermelho'}/{e.get('cobertura', 0)}%"
            for e in testes
        )
        linhas.append(f"  Execuções de teste          : {historico}")
        ultimo = testes[-1]
        if ultimo.get("cobertura_pior_arquivo"):
            linhas.append(
                f"  Módulo menos coberto        : "
                f"{ultimo['cobertura_pior_arquivo']} ({ultimo.get('cobertura_pior', 0)}%)"
            )
    guardas = _dos("validacao_testes")
    if guardas:
        linhas.append(
            "  Guard de critérios          : "
            + " → ".join("aderente" if g.get("testes_aderentes") else "reprovado"
                         for g in guardas)
        )
    if revisoes:
        linhas.append(
            "  Revisão de código           : "
            + " → ".join("APROVADO" if r.get("aprovado") else "REPROVADO"
                         for r in revisoes)
        )
    pentests = _dos("pentest")
    if pentests:
        linhas.append(
            "  Pentest (execução ofensiva) : "
            + " → ".join(
                ("verde" if p.get("pentest_ok") else "vermelho")
                + f"/{p.get('vulns_bloqueantes', 0)}bloq"
                for p in pentests
            )
        )
    visuais = _dos("visual")
    if visuais:
        linhas.append(
            "  Verificação visual          : "
            + " → ".join(
                ("verde" if v.get("visual_ok") else "vermelho")
                + f"/{v.get('problemas', 0)}achados"
                for v in visuais
            )
        )

    tetos = _dos("teto_atingido")
    if tetos:
        linhas.append(
            "  Tetos atingidos             : "
            + ", ".join(f"{t.get('laco')} ({t.get('limite')}x)" for t in tetos)
        )

    total = sum(e.get("duracao_s", 0) for e in eventos)
    linhas.append(f"  Tempo total nos nós         : {total:.0f}s")

    # Contexto injetado nos nós que falam com o LLM: é o que se paga por token,
    # e o único jeito de comparar duas execuções depois de mexer nos limites.
    contexto = sum(e.get("chars_contexto", 0) for e in eventos)
    if contexto:
        maior = max(eventos, key=lambda e: e.get("chars_contexto", 0))
        linhas.append(
            f"  Contexto enviado ao LLM     : {contexto:,} chars"
            f" (maior: {maior['evento']} {maior.get('chars_contexto', 0):,})"
        )

    # Só os nós medidos entram no ranking: marcos sem duração (`teto_atingido`,
    # `fim_execucao`) apareceriam empatados em 0s e, numa execução curta,
    # ocupariam o topo da lista.
    medidos = [e for e in eventos if "duracao_s" in e]
    caro = sorted(medidos, key=lambda e: e["duracao_s"], reverse=True)[:3]
    if caro:
        linhas.append(
            "  Nós mais lentos             : "
            + ", ".join(f"{e['evento']} {e.get('duracao_s', 0):.0f}s" for e in caro)
        )
    deploy = _dos("deploy")
    if deploy:
        linhas.append(f"  Entrega publicada em        : {deploy[-1].get('deploy_ref', '-')}")
    linhas.append(f"  Histórico completo          : metrics/{thread_id}.json")
    linhas.append("=" * 62)
    return "\n".join(linhas)
