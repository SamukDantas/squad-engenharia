"""Verificador de contraste da entrega, rodado DENTRO do container isolado.

Abre cada página HTML da entrega num Chromium headless, uma vez por tema
(claro e escuro), e mede o contraste real entre cada texto e o fundo que
efetivamente aparece atrás dele — o do próprio elemento ou, se transparente, o
do primeiro ancestral que declara um.

Mede em vez de opinar: as razões saem de `getComputedStyle` num motor de
layout de verdade, pelo mesmo motivo que o veredito dos testes sai do exit code
do pytest. Um revisor LLM lê `color: #1f2937` e não sabe o que aparece atrás.

Dois modos, escolhidos pelo argumento:

- **caminho**: entrega com HTML estático, aberta por `file://`, num container
  com `--network none`;
- **URL**: entrega que só existe servida (Next.js e afins). O container roda
  numa bridge interna, sem rota para a internet, e o alvo é a própria entrega
  no ar. Mede a raiz e um nível de links da mesma origem — a raiz costuma ser
  landing, e numa entrega real o dashboard morava em `/dashboard`.

Emite JSON no stdout.
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

TEMAS = ("light", "dark")
# Fundo que o Chromium pinta quando a página não declara nenhum. É o valor que
# a checagem `fundo_nao_declarado` denuncia; entra aqui só para a razão de
# contraste desses elementos não ficar indefinida.
CANVAS_PADRAO = {"light": (255, 255, 255), "dark": (18, 18, 18)}
LIMITE_ACHADOS = 25       # por página e tema — o brief de correção precisa caber
IGNORAR = {"tests", ".squad", "node_modules", "__pycache__", ".git"}

# Modo URL (SPA): a entrega não tem HTML estático, então é renderizada de pé.
ESPERA_ALVO = 90          # s até desistir de o servidor responder
LIMITE_ROTAS = 5          # rotas visitadas além da raiz

# WCAG 2.1 AA.
RAZAO_TEXTO_NORMAL = 4.5
RAZAO_TEXTO_GRANDE = 3.0

_JS_MEDIR = r"""
(canvasPadrao) => {
  const paraRGBA = (s) => {
    const m = String(s).match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(',').map(x => parseFloat(x.trim()));
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };

  const sobrepor = (frente, fundo) => ({
    r: frente.r * frente.a + fundo.r * (1 - frente.a),
    g: frente.g * frente.a + fundo.g * (1 - frente.a),
    b: frente.b * frente.a + fundo.b * (1 - frente.a),
    a: 1,
  });

  const luminancia = (c) => {
    const canal = (v) => {
      v = v / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * canal(c.r) + 0.7152 * canal(c.g) + 0.0722 * canal(c.b);
  };

  const razao = (a, b) => {
    const la = luminancia(a), lb = luminancia(b);
    const claro = Math.max(la, lb), escuro = Math.min(la, lb);
    return (claro + 0.05) / (escuro + 0.05);
  };

  const padrao = { r: canvasPadrao[0], g: canvasPadrao[1], b: canvasPadrao[2], a: 1 };

  // Fundo efetivo: sobe a árvore até achar um opaco, compondo os semi
  // transparentes no caminho. Sem nenhum até a raiz, sobra o canvas do
  // navegador — que é justamente o caso que quebra ao trocar de tema.
  const fundoDe = (el) => {
    const pilha = [];
    let no = el;
    while (no) {
      const cor = paraRGBA(getComputedStyle(no).backgroundColor);
      if (cor && cor.a > 0) {
        pilha.push(cor);
        if (cor.a >= 1) break;
      }
      no = no.parentElement;
    }
    let acumulado = padrao;
    for (let i = pilha.length - 1; i >= 0; i--) acumulado = sobrepor(pilha[i], acumulado);
    return { cor: acumulado, declarado: pilha.length > 0 };
  };

  const seletor = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) return s + '#' + el.id;
    const classe = (el.className || '').toString().trim().split(/\s+/)[0];
    return classe ? s + '.' + classe : s;
  };

  const visivel = (el) => {
    const e = getComputedStyle(el);
    if (e.display === 'none' || e.visibility === 'hidden' || parseFloat(e.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  // Fundo da própria página: a checagem de causa raiz. Sem fundo na raiz nem no
  // body, a página herda o canvas do navegador e inverte junto com o tema do
  // sistema, enquanto as cores de texto ficam paradas.
  const bgRaiz = paraRGBA(getComputedStyle(document.documentElement).backgroundColor);
  const bgBody = paraRGBA(getComputedStyle(document.body).backgroundColor);
  const fundoDeclarado = (bgRaiz && bgRaiz.a > 0) || (bgBody && bgBody.a > 0);

  const achados = [];
  for (const el of document.querySelectorAll('*')) {
    const temTextoProprio = Array.from(el.childNodes).some(
      (n) => n.nodeType === 3 && n.textContent.trim().length > 0
    );
    if (!temTextoProprio || !visivel(el)) continue;

    const estilo = getComputedStyle(el);
    const frente = paraRGBA(estilo.color);
    if (!frente || frente.a === 0) continue;

    const fundo = fundoDe(el);
    const corTexto = frente.a < 1 ? sobrepor(frente, fundo.cor) : frente;
    const valor = razao(corTexto, fundo.cor);

    const tamanho = parseFloat(estilo.fontSize);
    const peso = parseInt(estilo.fontWeight, 10) || 400;
    const grande = tamanho >= 24 || (tamanho >= 18.66 && peso >= 700);
    const exigido = grande ? 3.0 : 4.5;

    if (valor < exigido) {
      achados.push({
        tipo: 'contraste_insuficiente',
        seletor: seletor(el),
        texto: el.textContent.trim().slice(0, 60),
        cor: estilo.color,
        fundo: `rgb(${Math.round(fundo.cor.r)}, ${Math.round(fundo.cor.g)}, ${Math.round(fundo.cor.b)})`,
        fundo_declarado: fundo.declarado,
        razao: Math.round(valor * 100) / 100,
        exigido,
      });
    }
  }
  return { fundo_declarado: !!fundoDeclarado, achados };
}
"""


def _paginas(raiz: Path) -> list[Path]:
    return sorted(
        p for p in raiz.rglob("*.html")
        if not IGNORAR.intersection(p.relative_to(raiz).parts)
    )


def _dedup(achados: list[dict]) -> list[dict]:
    """Uma linha por (tema, seletor, tipo). Uma tabela com vinte células do
    mesmo `th` é um problema, não vinte."""
    vistos, unicos = set(), []
    for a in achados:
        chave = (a.get("tema"), a.get("seletor"), a["tipo"])
        if chave in vistos:
            continue
        vistos.add(chave)
        unicos.append(a)
    return unicos


def _medir(navegador, alvo: str, rotulo: str, problemas: list) -> tuple[list, bool]:
    """Abre `alvo` nos dois temas e acumula os achados.

    Devolve os temas sem fundo declarado e se a resposta era HTML. O segundo
    importa: uma entrega de API responde JSON, e medir contraste num corpo JSON
    reprovaria toda API por ruído. O que não é HTML não é julgado aqui — nem
    como achado, nem como página. Vale o mesmo para resposta fora do 2xx: página
    de erro é do framework, não da entrega.
    """
    sem_fundo, era_html = [], False
    for tema in TEMAS:
        ctx = navegador.new_context(color_scheme=tema)
        aba = ctx.new_page()
        try:
            resposta = aba.goto(alvo, wait_until="load", timeout=20_000)
            # Página de erro do framework não é a interface da entrega. Uma API
            # REST não tem mapping para `/`, então o Spring serve ali a sua
            # Whitelabel Error Page — HTML de verdade, com contraste de verdade,
            # e que a entrega não escreveu. Medido numa execução real: os 5
            # achados que reprovaram um serviço eram todos daquela página.
            #
            # A régua é o status, não o texto: o que não respondeu 2xx não é
            # interface a julgar. O servidor continua contando como no ar — quem
            # decide isso é `_esperar_alvo`, que aceita qualquer resposta.
            if resposta is not None and not (200 <= resposta.status < 300):
                continue
            tipo = (resposta.headers.get("content-type", "") if resposta else "")
            if "html" not in tipo.lower():
                continue
            era_html = True
            # A entrega pode preencher a página por fetch; este respiro deixa o
            # que é síncrono assentar antes de medir.
            aba.wait_for_timeout(400)
            medida = aba.evaluate(_JS_MEDIR, CANVAS_PADRAO[tema])
        except Exception as e:  # página que nem abre é achado, não crash
            problemas.append({
                "tipo": "pagina_nao_renderiza", "arquivo": rotulo, "tema": tema,
                "detalhe": f"{type(e).__name__}: {e}"[:200],
            })
            continue
        finally:
            ctx.close()

        if not medida["fundo_declarado"]:
            sem_fundo.append(tema)
        for achado in medida["achados"][:LIMITE_ACHADOS]:
            problemas.append({**achado, "arquivo": rotulo, "tema": tema})
    return sem_fundo, era_html


def _sem_fundo(rotulo: str) -> dict:
    return {
        "tipo": "fundo_nao_declarado", "arquivo": rotulo, "tema": "ambos",
        "detalhe": (
            "a página não declara background-color no body nem na raiz, então "
            "herda o canvas do navegador e inverte com o tema do sistema "
            "enquanto as cores de texto ficam paradas"
        ),
    }


def verificar(raiz: Path) -> dict:
    """Modo arquivo: entrega com HTML estático, aberta por file://."""
    paginas = _paginas(raiz)
    if not paginas:
        return {"paginas": 0, "problemas": []}

    problemas: list[dict] = []
    with sync_playwright() as pw:
        navegador = pw.chromium.launch(args=["--no-sandbox"])
        try:
            for pagina in paginas:
                rel = str(pagina.relative_to(raiz)).replace("\\", "/")
                sem_fundo, _ = _medir(navegador, pagina.as_uri(), rel, problemas)
                if sem_fundo:
                    problemas.append(_sem_fundo(rel))
        finally:
            navegador.close()
    return {"paginas": len(paginas), "problemas": _dedup(problemas)}


def _esperar_alvo(aba, base: str) -> None:
    """O servidor pode ainda estar subindo — `next build` sozinho leva dezenas
    de segundos. Repete o goto até responder, em vez de exigir um probe com
    curl numa imagem que não tem curl."""
    limite = time.monotonic() + ESPERA_ALVO
    ultimo = ""
    while time.monotonic() < limite:
        try:
            aba.goto(base, wait_until="load", timeout=10_000)
            return
        except Exception as e:
            ultimo = f"{type(e).__name__}: {e}"[:200]
            time.sleep(3)
    raise RuntimeError(
        f"O alvo não respondeu em {base} dentro de {ESPERA_ALVO}s. A entrega não "
        f"subiu — não é veredito visual. Último erro: {ultimo}"
    )


def _rotas(aba, base: str) -> list[str]:
    """Um nível de links da raiz, só da mesma origem.

    Existe porque a raiz costuma ser landing: numa entrega real o dashboard
    morava em `/dashboard`, e medir só `/` teria aprovado a página que ninguém
    olha. Um nível é o corte — crawler de verdade é outro problema.
    """
    encontrados = aba.evaluate(
        """() => Array.from(document.querySelectorAll('a[href]'))
                .map(a => a.href)
                .filter(h => h.startsWith(location.origin))"""
    )
    vistos, rotas = {base.rstrip("/")}, []
    for url in encontrados:
        limpo = url.split("#")[0].rstrip("/")
        if limpo and limpo not in vistos:
            vistos.add(limpo)
            rotas.append(limpo)
        if len(rotas) >= LIMITE_ROTAS:
            break
    return rotas


def verificar_url(base: str) -> dict:
    """Modo SPA: a entrega está de pé, e o que se mede é o que o servidor
    devolve. É o único caminho possível em Next.js, que não deixa HTML estático
    no workspace — ali o nó virava no-op silencioso justamente na stack onde
    seria mais útil."""
    problemas: list[dict] = []
    with sync_playwright() as pw:
        navegador = pw.chromium.launch(args=["--no-sandbox"])
        try:
            ctx = navegador.new_context()
            aba = ctx.new_page()
            try:
                _esperar_alvo(aba, base)
                rotas = _rotas(aba, base)
            finally:
                ctx.close()

            medidas = 0
            for url in [base] + rotas:
                rotulo = url[len(base.rstrip("/")):] or "/"
                sem_fundo, era_html = _medir(navegador, url, rotulo, problemas)
                if not era_html:
                    continue
                medidas += 1
                if sem_fundo:
                    problemas.append(_sem_fundo(rotulo))
        finally:
            navegador.close()

    # Nenhuma rota devolveu HTML: é uma API, não uma interface. Verde por
    # ausência de objeto, não por aprovação — reprovar aqui transformaria o nó
    # num requisito de front-end que a spec pode não ter.
    if medidas == 0:
        return {"paginas": 0, "problemas": []}
    return {"paginas": medidas, "problemas": _dedup(problemas)}


def main() -> None:
    alvo = sys.argv[1] if len(sys.argv) > 1 else "/entrega"
    # Um argumento só, com o modo implícito no formato: caminho abre por
    # file://, URL renderiza a entrega de pé.
    medida = verificar_url(alvo) if alvo.startswith("http") else verificar(Path(alvo))
    print(json.dumps(medida, ensure_ascii=False))


if __name__ == "__main__":
    main()
