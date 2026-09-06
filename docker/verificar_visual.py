"""Verificador de contraste da entrega, rodado DENTRO do container isolado.

Abre cada página HTML da entrega num Chromium headless, uma vez por tema
(claro e escuro), e mede o contraste real entre cada texto e o fundo que
efetivamente aparece atrás dele — o do próprio elemento ou, se transparente, o
do primeiro ancestral que declara um.

Mede em vez de opinar: as razões saem de `getComputedStyle` num motor de
layout de verdade, pelo mesmo motivo que o veredito dos testes sai do exit code
do pytest. Um revisor LLM lê `color: #1f2937` e não sabe o que aparece atrás.

Emite JSON no stdout. Nada aqui fala com a rede: o container roda com
`--network none` e as páginas são abertas por `file://`.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

TEMAS = ("light", "dark")
# Fundo que o Chromium pinta quando a página não declara nenhum. É o valor que
# a checagem `fundo_nao_declarado` denuncia; entra aqui só para a razão de
# contraste desses elementos não ficar indefinida.
CANVAS_PADRAO = {"light": (255, 255, 255), "dark": (18, 18, 18)}
LIMITE_ACHADOS = 25       # por página e tema — o brief de correção precisa caber
IGNORAR = {"tests", ".squad", "node_modules", "__pycache__", ".git"}

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


def verificar(raiz: Path) -> dict:
    paginas = _paginas(raiz)
    if not paginas:
        return {"paginas": 0, "problemas": []}

    problemas: list[dict] = []
    with sync_playwright() as pw:
        navegador = pw.chromium.launch(args=["--no-sandbox"])
        try:
            for pagina in paginas:
                rel = str(pagina.relative_to(raiz)).replace("\\", "/")
                sem_fundo_em = []
                for tema in TEMAS:
                    ctx = navegador.new_context(color_scheme=tema)
                    aba = ctx.new_page()
                    try:
                        aba.goto(pagina.as_uri(), wait_until="load", timeout=15_000)
                        # A entrega pode preencher a página por fetch; sem rede o
                        # fetch falha rápido, e este respiro deixa o que é
                        # síncrono assentar antes de medir.
                        aba.wait_for_timeout(300)
                        medida = aba.evaluate(_JS_MEDIR, CANVAS_PADRAO[tema])
                    except Exception as e:  # página que nem abre é achado, não crash
                        problemas.append({
                            "tipo": "pagina_nao_renderiza", "arquivo": rel, "tema": tema,
                            "detalhe": f"{type(e).__name__}: {e}"[:200],
                        })
                        continue
                    finally:
                        ctx.close()

                    if not medida["fundo_declarado"]:
                        sem_fundo_em.append(tema)
                    for achado in medida["achados"][:LIMITE_ACHADOS]:
                        problemas.append({**achado, "arquivo": rel, "tema": tema})

                # Uma vez por página: a falta de fundo não é um defeito por tema,
                # é o defeito que faz os dois temas divergirem.
                if sem_fundo_em:
                    problemas.append({
                        "tipo": "fundo_nao_declarado", "arquivo": rel, "tema": "ambos",
                        "detalhe": (
                            "a página não declara background-color no body nem na "
                            "raiz, então herda o canvas do navegador e inverte com "
                            "o tema do sistema enquanto as cores de texto ficam paradas"
                        ),
                    })
        finally:
            navegador.close()

    return {"paginas": len(paginas), "problemas": _dedup(problemas)}


def main() -> None:
    raiz = Path(sys.argv[1] if len(sys.argv) > 1 else "/entrega")
    print(json.dumps(verificar(raiz), ensure_ascii=False))


if __name__ == "__main__":
    main()
