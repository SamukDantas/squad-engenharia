"""Ponto de entrada da squad.

Uso:
  python main.py "Criar endpoint de healthcheck"        # nova execução
  python main.py --thread <id>                           # retomar execução interrompida
  python main.py --thread <id> --a-partir-de escrever_testes
                                                         # thread nova, dali em diante
  python main.py --thread <id> --a-partir-de escrever_testes --ate executar_testes
                                                         # e para depois do juiz que interessa

O thread id é impresso no início de cada execução. Com o checkpointer SQLite,
uma queda no meio (erro 503 do provedor, Ctrl+C, crash) não perde o progresso:
retome com --thread e o grafo continua do último nó concluído.
"""
import argparse
import os
import uuid

import truststore
from dotenv import load_dotenv

# Valida TLS pelo repositório de certificados do SO, não pelo bundle do
# certifi — em redes com inspeção TLS (proxy/antivírus corporativo), o bundle
# não conhece o emissor e toda chamada HTTPS falha com CERTIFICATE_VERIFY_FAILED.
truststore.inject_into_ssl()

load_dotenv(override=True)

from src.squad.adaptadores import codex_cli, perfis  # noqa: E402
from src.squad.adaptadores.metricas_json import eventos, registrar, resumo  # noqa: E402
from src.squad.dominio.rotas import retomada_chama_llm  # noqa: E402
from src.squad.graph import reexecucao  # noqa: E402
from src.squad.graph.maestro import construir_maestro  # noqa: E402
from src.squad.graph.workflow import DeployNegado, construir_grafo  # noqa: E402
from src.squad.llm import conferir_credencial, provedor  # noqa: E402


def _foi_paralela(thread_id: str) -> bool:
    """Se esta thread nasceu paralela, lido do próprio histórico.

    Retomar exige o mesmo grafo com que a thread foi criada, e obrigar quem
    retoma a lembrar da flag transformaria um esquecimento numa execução que
    parece corrompida. O histórico já sabe.
    """
    for e in eventos(thread_id):
        if e.get("evento") == "inicio_execucao":
            return bool(e.get("paralelo"))
    return False


def _entrada_da_thread(thread_id: str) -> str:
    """O primeiro nó com que a thread foi criada: `triagem`, ou o nó de onde
    uma reexecução partiu. Retomar exige o mesmo grafo, como no paralelo."""
    for e in eventos(thread_id):
        if e.get("evento") == "inicio_execucao":
            return (e.get("reexecucao") or {}).get("a_partir_de") or "triagem"
    return "triagem"


def _reexecutar(origem: str, no: str, ate: str | None = None) -> tuple[str, dict]:
    """Cria a thread nova a partir do estado de `origem` antes de `no`.

    Devolve o id novo e a entrada do grafo (reexecucao.py).
    """
    if no not in reexecucao.NOS_REEXECUTAVEIS:
        raise SystemExit(
            f"Nó '{no}' não serve de ponto de partida. Use um destes: "
            f"{', '.join(reexecucao.NOS_REEXECUTAVEIS)}."
        )
    if _foi_paralela(origem):
        raise SystemExit(
            "Reexecução a partir de um nó ainda não existe para thread paralela: "
            "cada serviço tem o próprio ramo."
        )
    estado = reexecucao.estado_antes_do_no(construir_grafo().checkpointer, origem, no)
    if not estado:
        raise SystemExit(
            f"A thread {origem} nunca chegou a '{no}': não há estado de onde partir."
        )
    perfil = perfis.obter(estado.get("stack"))
    from src.squad.graph.workflow import _arquivos_do_workspace
    agora = _arquivos_do_workspace(estado["workspace"], perfil)
    novo = str(uuid.uuid4())
    entrada, removidos = reexecucao.preparar(
        estado, novo, agora, reexecucao.pastas_fora_da_copia(perfil.ignorar_no_workspace)
    )
    print(f"Reexecução de {origem} a partir de '{no}'.")
    print(f"Thread id desta execução: {novo}")
    print("(guarde para retomar com: python main.py --thread " + novo + ")")
    if removidos:
        print(f"Arquivos que ainda não existiam naquele ponto, fora da cópia: {', '.join(removidos)}")
    registrar(
        novo, "inicio_execucao",
        pedido=estado.get("pedido", ""), stack=estado.get("stack"), paralelo=False,
        reexecucao={"thread_origem": origem, "a_partir_de": no, **({"ate": ate} if ate else {})},
        **_cota(),
    )
    return novo, entrada


def _destino_deploy(estado) -> str:
    """Para onde o `sim` vai publicar, dito antes de pedir o `sim`.

    Vazio quando não há nome gravado (maestro, que publica um repositório por
    serviço, ou thread anterior ao nome curto) ou quando o deploy é só local.
    """
    nome = (estado.values or {}).get("nome_repo")
    dono = (os.getenv("DEPLOY_OWNER") or "").strip()
    if not nome or not dono:
        return ""
    visibilidade = (os.getenv("DEPLOY_VISIBILIDADE") or "private").strip().lower()
    return f"{dono}/{nome} ({visibilidade})"


def _cota() -> dict:
    """A cota do Codex para gravar num marco, quando a squad usa o Codex.

    Lida nos marcos (início, retomada, fim) e não por nó: o servidor devolve o
    percentual inteiro, e o gasto de um nó cabe dentro de um ponto. A
    diferença entre o primeiro e o último marco é o gasto da execução
    (`metricas_json.gasto_da_cota`); o gasto por nó fica com os tokens.

    Nunca levanta: também roda no `except` que registra a queda, e lá um erro
    aqui esconderia o erro que se quer registrar.
    """
    try:
        usa_codex = (
            provedor() == "codex"
            or os.getenv("DEV_EXECUTOR", "codex").strip().lower() == "codex"
        )
    except ValueError:
        return {}
    if not usa_codex:
        return {}
    lida = codex_cli.cota()
    return {"cota_codex": lida} if lida else {}


def _exigir_credencial() -> None:
    """Credencial ausente ou sessão expirada vira a instrução de login aqui,
    e não `Connection error` no meio de um nó já pago
    (llm.conferir_credencial)."""
    try:
        conferir_credencial()
    except RuntimeError as e:
        # Traceback aqui não ajuda ninguém: a mensagem já diz o que fazer, e
        # a pilha só empurra a instrução para fora da tela.
        print(f"\n{e}")
        raise SystemExit(1)


def _rodar(grafo, entrada, config) -> None:
    for evento in grafo.stream(entrada, config=config):
        print(f"--- nó concluído: {list(evento.keys())[0]} ---")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pedido", nargs="*", help="Pedido para a squad")
    parser.add_argument("--thread", help="Retomar a execução com este thread id")
    parser.add_argument(
        "--a-partir-de",
        metavar="NO",
        help="Com --thread: cria uma thread NOVA com o estado daquela antes do nó "
             "indicado e roda dali em diante. A thread de origem não muda.",
    )
    parser.add_argument(
        "--ate",
        metavar="NO",
        help="Com --a-partir-de: para logo depois deste nó, sem pagar os seguintes. "
             "Ex.: --a-partir-de escrever_testes --ate executar_testes valida uma "
             "correção do QA sem desenvolvimento, revisão nem visual.",
    )
    parser.add_argument(
        "--paralelo",
        action="store_true",
        help="Decompõe o pedido em microsserviços e os constrói em paralelo, "
             "cada um com seu repositório. Sem a flag, é uma entrega só.",
    )
    parser.add_argument(
        "--stack",
        help=f"Stack da entrega ({', '.join(perfis.nomes())}). "
             f"Sem a flag, usa STACK do .env; sem ela, '{perfis.PERFIL_PADRAO}'.",
    )
    args = parser.parse_args()

    if args.a_partir_de and not args.thread:
        parser.error("--a-partir-de exige --thread <id da thread de origem>")
    if args.ate and not args.a_partir_de:
        parser.error("--ate só vale numa reexecução, com --a-partir-de")
    if args.ate and args.ate not in reexecucao.NOS_REEXECUTAVEIS:
        parser.error(f"--ate: use um destes nós: {', '.join(reexecucao.NOS_REEXECUTAVEIS)}")

    no_de_entrada = "triagem"
    if args.a_partir_de:
        # Conferida antes de criar a thread, como numa execução nova: mesmo
        # partindo de um nó sem LLM, o laço volta ao desenvolvimento no vermelho.
        _exigir_credencial()
        no_de_entrada = args.a_partir_de
        thread_id, entrada = _reexecutar(args.thread, args.a_partir_de, args.ate)
        paralelo = False
        config = {"configurable": {"thread_id": thread_id}}
    elif args.thread:
        thread_id = args.thread
        no_de_entrada = _entrada_da_thread(thread_id)
        paralelo = _foi_paralela(thread_id)
        config = {"configurable": {"thread_id": thread_id}}
        print(f"Retomando thread {thread_id} do último checkpoint"
              f"{' (paralela)' if paralelo else ''}...")
        # Uma thread pode atravessar vários processos. Sem este marco, a única
        # pista de retomada é uma lacuna no relógio entre dois eventos — que
        # também é o que uma chamada lenta ao provedor parece.
        registrar(thread_id, "retomada", **_cota())
        entrada = None  # None = continuar de onde parou
    else:
        # Execução nova sempre começa por LLM (planejamento). Conferido antes de
        # criar a thread, pelo mesmo motivo da stack logo abaixo.
        _exigir_credencial()
        paralelo = args.paralelo
        pedido = " ".join(args.pedido) or "Criar endpoint de healthcheck"
        # Resolve antes de criar a thread: uma stack digitada errada não deve
        # deixar para trás um checkpoint e um arquivo de métricas órfãos.
        perfil = perfis.obter(args.stack or os.getenv("STACK"))
        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}
        print(f"Thread id desta execução: {thread_id}")
        print("(guarde para retomar com: python main.py --thread " + thread_id + ")")
        entrada = {"pedido": pedido, "stack": perfil.nome, "thread_id": thread_id}
        registrar(
            thread_id, "inicio_execucao",
            pedido=pedido, stack=perfil.nome, paralelo=paralelo, **_cota(),
        )
        if paralelo:
            print("Modo paralelo: o pedido será decomposto em microsserviços.")

    grafo = construir_maestro() if paralelo else construir_grafo(no_de_entrada, args.ate)
    if args.thread and not args.a_partir_de and retomada_chama_llm(grafo.get_state(config).next):
        _exigir_credencial()

    # O gate humano entra no try: negar o deploy levanta, e esse desfecho é tão
    # informativo quanto uma queda do provedor. Com os dois lados cobertos, um
    # histórico sem `fim_execucao` passa a significar uma coisa só — a execução
    # ainda está viva, ou o processo morreu sem chance de registrar.
    try:
        _rodar(grafo, entrada, config)
        estado = grafo.get_state(config)
        if args.ate and estado.next:
            # Parada pedida, não gate: o que interessava já rodou. A thread
            # continua retomável com --thread, sem o --ate, se valer a pena.
            registrar(thread_id, "fim_execucao", desfecho="parcial",
                      parado_depois=args.ate, **_cota())
            print(f"\nReexecução parada depois de '{args.ate}', como pedido.")
            print(f"Para seguir dali: python main.py --thread {thread_id}")
            print(resumo(thread_id))
            return
        if estado.next:  # pausado no gate humano
            print("\nGrafo pausado aguardando aprovação humana.")
            motivo = (estado.values or {}).get("motivo_gate")
            if motivo:
                print(f"\n{motivo}\n")
            destino = _destino_deploy(estado)
            if destino:
                print(f"Destino do deploy: {destino}")
            try:
                resposta = input("Autorizar deploy? (sim/nao): ")
            except EOFError:
                # Processo sem entrada interativa (shell de background, CI,
                # subprocesso). Não é falha: o trabalho está feito e aprovado,
                # falta só a decisão. Gravar como `erro` faria a thread parecer,
                # no painel, igual a uma que caiu no meio do caminho.
                registrar(
                    thread_id, "fim_execucao", desfecho="aguardando_gate",
                    motivo="processo sem entrada interativa no gate humano",
                    **_cota(),
                )
                print("\nSem entrada interativa para responder ao gate.")
                print("Nada foi publicado — o trabalho está salvo e aprovado.")
                print(f"Decida num terminal: python main.py --thread {thread_id}")
                raise SystemExit(1)
            from langgraph.types import Command
            _rodar(grafo, Command(resume=resposta), config)
    except DeployNegado as e:
        # Recusa é decisão, não falha: gravar como `erro` faria a thread parecer,
        # no histórico, igual a uma que caiu no meio — e contaminaria qualquer
        # leitura de taxa de conclusão.
        registrar(thread_id, "fim_execucao", desfecho="negado", motivo=str(e), **_cota())
        print(f"\nDeploy recusado no gate: {e}")
        print(f"Nada foi publicado. Para reabrir a decisão: python main.py --thread {thread_id}")
        raise SystemExit(1)
    except Exception as e:
        registrar(
            thread_id, "fim_execucao",
            desfecho="erro",
            erro=f"{type(e).__name__}: {e}"[:300],
            **_cota(),
        )
        print(f"\nExecução interrompida: {e}")
        print(f"Progresso salvo. Retome com: python main.py --thread {thread_id}")
        raise SystemExit(1)

    final = grafo.get_state(config).values
    registrar(
        thread_id, "fim_execucao",
        desfecho="deploy" if final.get("deploy_ok") else "sem_deploy",
        deploy_ref=final.get("deploy_ref", ""),
        **_cota(),
    )
    print("\nDeploy ok:", final.get("deploy_ok", False))
    if final.get("deploy_ref"):
        print("Entrega publicada em:", final["deploy_ref"])
    print(resumo(thread_id))


if __name__ == "__main__":
    main()
