"""Painel de métricas: a leitura das execuções que o resumo do terminal não dá.

Read-only por construção. O escritor único de `metrics/` continua sendo o
`metricas.py`; este módulo abre arquivo apenas para leitura, nunca para escrita.
A separação é a mesma que o grafo já usa entre quem produz e quem julga.

Serve execução viva e histórico morto pelo mesmo caminho, porque a fonte é o
disco e não o processo do grafo: dá para abrir o painel no meio de uma execução,
ou em agosto do ano que vem.

Uso (a partir da raiz do projeto):
    python -m src.squad.painel                    # http://127.0.0.1:4949
    python -m src.squad.painel --porta 8080
    python -m src.squad.painel --json <thread_id> # o mesmo dado, sem servidor
"""
import argparse
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DIR_PADRAO = Path("metrics")
PORTA_PADRAO = 4949
ARQUIVO_HTML = Path(__file__).with_name("painel.html")

# Campo booleano que carrega o veredito de cada nó — é o que colore a barra.
VEREDITO_DO_NO = {
    "validacao_spec": "spec_coerente",
    "validacao_testes": "testes_aderentes",
    "executar_testes": "testes_ok",
    "revisao": "aprovado",
    "pentest": "pentest_ok",
    "visual": "visual_ok",
    "deploy": "deploy_ok",
}

# Campos que já viram coluna própria e não precisam repetir no detalhe do marco.
_CAMPOS_ESTRUTURAIS = {"evento", "quando", "inicio", "duracao_s"}

# Último conteúdo válido lido de cada arquivo. `registrar` reescreve o arquivo
# inteiro a cada evento, então um GET pode pegar JSON truncado no meio da
# escrita. Devolver o último snapshot bom faz o painel manter a tela; devolver
# vazio a faria piscar a cada poll.
_ULTIMO_BOM: dict[str, list[dict]] = {}


def _eventos(diretorio: Path, thread_id: str) -> list[dict]:
    arquivo = diretorio / f"{thread_id}.json"
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _ULTIMO_BOM.get(thread_id, [])
    if isinstance(dados, list):
        _ULTIMO_BOM[thread_id] = dados
        return dados
    return _ULTIMO_BOM.get(thread_id, [])


def _instante(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def _janela(evento: dict) -> tuple[float, float]:
    """(início, fim) do evento em epoch.

    Usa o `inicio` explícito quando existe. Nos eventos gravados antes de o
    campo passar a ser escrito, reconstrói do fim menos a duração — que é uma
    aproximação (mistura wall clock com monotônico), mas mantém legível o
    histórico anterior à mudança em vez de descartá-lo.
    """
    fim = _instante(evento["quando"])
    if evento.get("inicio"):
        return _instante(evento["inicio"]), fim
    return fim - float(evento.get("duracao_s", 0.0)), fim


def _veredito(evento: dict) -> bool | None:
    """True/False para o nó que emite veredito, None para os que não emitem.

    Um nó que quebrou não tem veredito: o erro é o próprio desfecho, e pintá-lo
    de vermelho o confundiria com uma reprova legítima.
    """
    if evento.get("erro"):
        return None
    campo = VEREDITO_DO_NO.get(evento["evento"])
    if campo is None:
        return None
    valor = evento.get(campo)
    return bool(valor) if valor is not None else None


def _desfecho(marcos: list[dict], barras: list[dict]) -> str:
    """Como a execução terminou, pela melhor evidência disponível.

    O último `fim_execucao` não basta: uma thread atravessa vários processos, e
    uma retomada **depois** de um fim ressuscita a execução. Medido numa
    execução real — o planejamento morreu num 429, o `fim_execucao` gravou
    `erro`, a chave foi trocada e a thread seguiu por mais meia hora; o painel
    continuava anunciando `erro` sobre uma execução viva. Daí percorrer os
    marcos em ordem em vez de olhar só o último de cada tipo.

    Sem marco nenhum — o histórico anterior à instrumentação, que é a maior
    parte do arquivo — um nó de deploy concluído ainda prova que a execução
    chegou ao fim. O resto é honestamente indeterminado: chamar de "em_curso"
    faria a taxa de conclusão mentir na primeira tela.
    """
    estado = ""
    for marco in marcos:
        if marco["evento"] in {"inicio_execucao", "retomada"}:
            estado = "em_curso"
        elif marco["evento"] == "fim_execucao":
            estado = marco["detalhe"].get("desfecho", "?")
            # Histórico anterior ao desfecho `negado`: a recusa no gate era
            # gravada como `erro` com a frase no campo. Reconhecer isso aqui
            # mantém a série honesta sem reescrever medição já gravada — o
            # arquivo de métricas é registro, não rascunho.
            legado = str(marco["detalhe"].get("erro", ""))
            if estado == "erro" and "negado pelo aprovador" in legado:
                estado = "negado"
            # O único `input()` do pipeline é o do gate, então EOFError ali
            # significa processo sem terminal — trabalho pronto, decisão
            # pendente. Um `fim_execucao` posterior sobrescreve isto, porque os
            # marcos são percorridos em ordem: thread que depois foi aprovada
            # continua contando como deploy.
            elif estado == "erro" and legado.startswith("EOFError"):
                estado = "aguardando_gate"
    if estado:
        return estado
    if any(b["evento"] == "deploy" and not b["erro"] for b in barras):
        return "deploy"
    return "indeterminado"


def detalhar(diretorio: Path, thread_id: str) -> dict | None:
    """Eventos crus mais os derivados que as telas consomem."""
    eventos = _eventos(diretorio, thread_id)
    if not eventos:
        return None

    inicio_run = min(_janela(e)[0] for e in eventos)
    fim_run = max(_janela(e)[1] for e in eventos)

    # A preparação é tudo até o primeiro `desenvolvimento`; daí em diante, uma
    # rodada por execução do nó, rotulada pela origem que a motivou. É esse
    # corte que separa trabalho novo de retrabalho.
    rodadas: list[dict] = [
        {"n": 0, "origem": "preparacao", "duracao_s": 0.0,
         "chars_contexto": 0, "inicio_s": 0.0}
    ]
    barras: list[dict] = []
    marcos: list[dict] = []

    for evento in eventos:
        ini, _fim = _janela(evento)
        medido = "duracao_s" in evento

        if evento["evento"] == "desenvolvimento":
            rodadas.append({
                "n": len(rodadas),
                # `origem` só existe nos eventos recentes. Na primeira rodada a
                # origem é sempre a demanda; nas demais, sem o campo, não há
                # como saber qual guard cobrou — e dizer "inicial" seria mentir.
                "origem": evento.get("origem")
                or ("inicial" if len(rodadas) == 1 else "indeterminada"),
                "duracao_s": 0.0,
                "chars_contexto": 0,
                "inicio_s": round(ini - inicio_run, 1),
            })

        item = {
            "evento": evento["evento"],
            "rodada": rodadas[-1]["n"],
            "inicio_s": round(ini - inicio_run, 1),
            "duracao_s": round(float(evento.get("duracao_s", 0.0)), 1),
            "veredito": _veredito(evento),
            "erro": evento.get("erro", ""),
        }
        if medido:
            item["chars_contexto"] = int(evento.get("chars_contexto") or 0)
            # Por que a rodada ficou vermelha, gravado pelo nó de testes — a
            # saída em si é sobrescrita pela rodada seguinte.
            if evento.get("trecho_falha"):
                item["trecho_falha"] = evento["trecho_falha"]
            barras.append(item)
            rodadas[-1]["duracao_s"] += item["duracao_s"]
            rodadas[-1]["chars_contexto"] += item["chars_contexto"]
        else:
            item["detalhe"] = {
                k: v for k, v in evento.items() if k not in _CAMPOS_ESTRUTURAIS
            }
            marcos.append(item)

    tempo_nos = round(sum(b["duracao_s"] for b in barras), 1)
    wall = round(fim_run - inicio_run, 1)
    # Trabalho novo é a preparação mais a primeira rodada; o resto é retrabalho.
    retrabalho = round(
        sum(r["duracao_s"] for r in rodadas
            if r["origem"] not in {"preparacao", "inicial"}),
        1,
    )
    inicios = [m for m in marcos if m["evento"] == "inicio_execucao"]
    desfecho = _desfecho(marcos, barras)

    return {
        "thread_id": thread_id,
        "pedido": inicios[-1]["detalhe"].get("pedido", "") if inicios else "",
        "desfecho": desfecho,
        "inicio": datetime.fromtimestamp(inicio_run).isoformat(timespec="seconds"),
        "wall_s": wall,
        "tempo_nos_s": tempo_nos,
        # Tempo entre nós: gate humano, retomadas, o intervalo em que ninguém
        # estava trabalhando. Clamp em zero porque as durações são arredondadas.
        "fora_dos_nos_s": round(max(0.0, wall - tempo_nos), 1),
        "retrabalho_s": retrabalho,
        "chars_contexto": sum(b["chars_contexto"] for b in barras),
        "rodadas": rodadas,
        "barras": barras,
        "marcos": marcos,
        "tetos": [m for m in marcos if m["evento"] == "teto_atingido"],
        "retomadas": sum(1 for m in marcos if m["evento"] == "retomada"),
    }


def _cobertura_final(eventos: list[dict]) -> float | None:
    testes = [e for e in eventos if e["evento"] == "executar_testes"]
    return testes[-1].get("cobertura") if testes else None


def listar_execucoes(diretorio: Path) -> list[dict]:
    """Uma linha por execução, mais recente primeiro. É a tela que responde
    'a mudança melhorou?' — a que o resumo, olhando uma thread só, não pode."""
    linhas = []
    for arquivo in diretorio.glob("*.json"):
        thread_id = arquivo.stem
        detalhe = detalhar(diretorio, thread_id)
        if not detalhe:
            continue
        eventos = _eventos(diretorio, thread_id)
        linhas.append({
            "thread_id": thread_id,
            "pedido": detalhe["pedido"],
            "inicio": detalhe["inicio"],
            "desfecho": detalhe["desfecho"],
            "wall_s": detalhe["wall_s"],
            "tempo_nos_s": detalhe["tempo_nos_s"],
            "retrabalho_s": detalhe["retrabalho_s"],
            "rodadas": len(detalhe["rodadas"]) - 1,
            "tetos": [t["detalhe"].get("laco", "?") for t in detalhe["tetos"]],
            "retomadas": detalhe["retomadas"],
            "cobertura": _cobertura_final(eventos),
        })
    return sorted(linhas, key=lambda linha: linha["inicio"], reverse=True)


# ---------- servidor ----------

class _Painel(BaseHTTPRequestHandler):
    diretorio = DIR_PADRAO
    # Conexão aberta e muda (preconnect especulativo do navegador) desiste
    # sozinha em vez de prender uma thread para sempre.
    timeout = 10

    def _responder(self, corpo: bytes, tipo: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        # O painel lê arquivo a cada requisição; cache do navegador aqui só
        # atrasaria a tela de uma execução viva.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corpo)

    def _json(self, dados, status: int = 200) -> None:
        self._responder(
            json.dumps(dados, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def do_GET(self) -> None:  # noqa: N802 (assinatura da stdlib)
        rota = self.path.split("?", 1)[0]

        if rota in {"/", "/index.html", "/painel.html"}:
            try:
                corpo = ARQUIVO_HTML.read_bytes()
            except OSError:
                self._json({"erro": f"{ARQUIVO_HTML.name} não encontrado"}, 500)
                return
            self._responder(corpo, "text/html; charset=utf-8")
            return

        if rota == "/api/execucoes":
            self._json(listar_execucoes(self.diretorio))
            return

        if rota.startswith("/api/execucao/"):
            pedido = rota[len("/api/execucao/"):]
            # Só ids que existem como arquivo no diretório de métricas. Casar
            # contra a listagem, em vez de montar o caminho com o que veio na
            # URL, fecha travessia de diretório sem depender de sanitização.
            conhecidos = {a.stem for a in self.diretorio.glob("*.json")}
            if pedido not in conhecidos:
                self._json({"erro": "execução desconhecida"}, 404)
                return
            self._json(detalhar(self.diretorio, pedido))
            return

        self._json({"erro": "rota desconhecida"}, 404)

    def log_message(self, formato, *args) -> None:
        """Silencia o log de acesso: com poll de 3s ele inunda o terminal em
        que o grafo está rodando."""


def servir(diretorio: Path, porta: int) -> None:
    _Painel.diretorio = diretorio
    # Só loopback: o painel expõe o pedido e os relatórios de uma execução, e
    # não tem autenticação nenhuma.
    #
    # Com threads: o HTTPServer atende uma conexão por vez, e o navegador abre
    # conexões especulativas que ficam mudas. Uma delas prendia o servidor
    # esperando a linha de requisição, a fila enchia e o Windows passava a
    # recusar toda conexão nova — o painel "parava" com a porta ainda aberta.
    servidor = ThreadingHTTPServer(("127.0.0.1", porta), _Painel)
    servidor.daemon_threads = True  # Ctrl+C não espera conexão pendurada
    print(f"Painel em http://127.0.0.1:{porta}  (lendo {diretorio}/, Ctrl+C para sair)")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nPainel encerrado.")
    finally:
        servidor.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Painel de métricas da squad")
    parser.add_argument("--dir", default=str(DIR_PADRAO), help="diretório de métricas")
    parser.add_argument("--porta", type=int, default=PORTA_PADRAO)
    parser.add_argument("--json", metavar="THREAD_ID",
                        help="imprime o detalhe da execução e sai")
    args = parser.parse_args()

    diretorio = Path(args.dir)
    if not diretorio.is_dir():
        raise SystemExit(f"Diretório de métricas não encontrado: {diretorio}")

    if args.json:
        detalhe = detalhar(diretorio, args.json)
        if detalhe is None:
            raise SystemExit(f"Sem eventos para a thread {args.json}")
        print(json.dumps(detalhe, ensure_ascii=False, indent=2))
        return

    servir(diretorio, args.porta)


if __name__ == "__main__":
    main()
