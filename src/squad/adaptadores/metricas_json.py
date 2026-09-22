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

from . import consumo

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


def eventos(thread_id: str) -> list[dict]:
    """O histórico da execução, para quem precisa relê-lo.

    Público porque a retomada depende dele: `main.py` descobre aqui se a thread
    nasceu paralela, em vez de exigir que quem retoma lembre da flag — um
    esquecimento viraria uma execução que parece corrompida.
    """
    return _carregar(thread_id)


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

    Os tokens gastos com o provedor dentro do bloco entram como `tokens`
    (`consumo.py`), só quando houve gasto: nó sem LLM não ganha campo vazio.
    """
    inicio = time.monotonic()
    inicio_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    extras: dict = {}
    with consumo.contar() as tokens:
        try:
            yield extras
        except Exception as e:
            if tokens:
                extras["tokens"] = dict(tokens)
            registrar(
                thread_id, evento,
                inicio=inicio_iso,
                duracao_s=round(time.monotonic() - inicio, 1),
                erro=f"{type(e).__name__}: {e}"[:300],
                **dados, **extras,
            )
            raise
    if tokens:
        extras["tokens"] = dict(tokens)
    registrar(
        thread_id, evento,
        inicio=inicio_iso,
        duracao_s=round(time.monotonic() - inicio, 1),
        **dados, **extras,
    )


def somar_tokens(eventos: list[dict]) -> dict:
    """Os tokens de todos os nós da execução, campo a campo."""
    total: dict = {}
    for evento in eventos:
        for campo, valor in (evento.get("tokens") or {}).items():
            total[campo] = total.get(campo, 0) + valor
    return total


def gasto_da_cota(eventos: list[dict]) -> dict | None:
    """Da primeira à última leitura da cota do Codex nos marcos da execução.

    A cota é da conta, não da execução: uso do Codex fora da squad na mesma
    janela entra na conta. E, se a janela zerou no meio (`zera_em` mudou), a
    diferença não significa nada — nesse caso não há gasto a reportar.
    """
    leituras = [e["cota_codex"] for e in eventos if e.get("cota_codex")]
    if len(leituras) < 2:
        return None
    antes, depois = leituras[0], leituras[-1]
    if antes.get("zera_em") != depois.get("zera_em"):
        return None
    return {
        "plano": depois.get("plano"),
        "antes": antes["usado_pct"],
        "depois": depois["usado_pct"],
        "gasto_pct": depois["usado_pct"] - antes["usado_pct"],
        "zera_em": depois.get("zera_em"),
    }


# Campo que carrega o veredito de cada juiz, e o que ele exige a mais.
_JUIZES = {
    "validacao_spec": ("spec_coerente",),
    "validacao_testes": ("testes_aderentes",),
    "executar_testes": ("testes_ok", "cobertura_ok"),
    "revisao": ("aprovado",),
    "visual": ("visual_ok",),
    "pentest": ("pentest_ok",),
}


def veredito_da_validacao(eventos: list[dict]) -> dict:
    """Validada ou reprovada: o veredito de uma reexecução parcial.

    Uma reexecução com `--ate` para antes do gate, e o desfecho "parcial" não
    dizia se a validação passou — a `eed26930` estava verde e aparecia cinza
    como as que falharam. Validada exige três coisas: o ÚLTIMO veredito de
    cada juiz que rodou é positivo (inclusive a cobertura, quando gravada),
    nenhum teto foi atingido e nenhum nó quebrou. `de_primeira` diz se além
    disso nenhum juiz reprovou no caminho — é a régua de "100% bem sucedido".
    """
    ultimos: dict[str, dict] = {}
    reprovas: list[str] = []
    motivos: list[str] = []
    for evento in eventos:
        nome = evento.get("evento")
        if nome == "teto_atingido":
            motivos.append(f"teto de {evento.get('laco', '?')} atingido")
        if evento.get("erro") and "duracao_s" in evento:
            motivos.append(f"{nome} quebrou: {str(evento['erro'])[:120]}")
        if nome in _JUIZES:
            ultimos[nome] = evento
            if any(evento.get(c) is False for c in _JUIZES[nome]):
                reprovas.append(nome)
    for nome, evento in ultimos.items():
        falhos = [c for c in _JUIZES[nome] if evento.get(c) is False]
        if falhos:
            motivos.append(f"{nome} terminou reprovado ({', '.join(falhos)})")
    ok = not motivos and bool(ultimos)
    if not ultimos:
        motivos.append("nenhum juiz rodou")
    return {
        "ok": ok,
        "de_primeira": ok and not reprovas,
        "reprovas_no_caminho": reprovas,
        "motivos": motivos,
    }


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

    tokens = somar_tokens(eventos)
    if tokens:
        linhas.append(
            f"  Tokens do provedor          : {tokens.get('entrada', 0):,} de entrada"
            f" ({tokens.get('entrada_cache', 0):,} em cache),"
            f" {tokens.get('saida', 0):,} de saída em {tokens.get('chamadas', 0)} chamadas"
        )
    gasto = gasto_da_cota(eventos)
    if gasto:
        linhas.append(
            f"  Cota do Codex               : {gasto['antes']}% → {gasto['depois']}%"
            f" (+{gasto['gasto_pct']} pts, plano {gasto['plano']},"
            f" zera em {(gasto['zera_em'] or '?')[:10]})"
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
