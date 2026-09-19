"""Ponto de entrada da squad.

Uso:
  python main.py "Criar endpoint de healthcheck"        # nova execução
  python main.py --thread <id>                           # retomar execução interrompida

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

from src.squad.adaptadores import perfis  # noqa: E402
from src.squad.adaptadores.metricas_json import eventos, registrar, resumo  # noqa: E402
from src.squad.graph.maestro import construir_maestro  # noqa: E402
from src.squad.graph.workflow import DeployNegado, construir_grafo  # noqa: E402


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


def _rodar(grafo, entrada, config) -> None:
    for evento in grafo.stream(entrada, config=config):
        print(f"--- nó concluído: {list(evento.keys())[0]} ---")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pedido", nargs="*", help="Pedido para a squad")
    parser.add_argument("--thread", help="Retomar a execução com este thread id")
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

    if args.thread:
        thread_id = args.thread
        paralelo = _foi_paralela(thread_id)
        config = {"configurable": {"thread_id": thread_id}}
        print(f"Retomando thread {thread_id} do último checkpoint"
              f"{' (paralela)' if paralelo else ''}...")
        # Uma thread pode atravessar vários processos. Sem este marco, a única
        # pista de retomada é uma lacuna no relógio entre dois eventos — que
        # também é o que uma chamada lenta ao provedor parece.
        registrar(thread_id, "retomada")
        entrada = None  # None = continuar de onde parou
    else:
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
            pedido=pedido, stack=perfil.nome, paralelo=paralelo,
        )
        if paralelo:
            print("Modo paralelo: o pedido será decomposto em microsserviços.")

    grafo = construir_maestro() if paralelo else construir_grafo()

    # O gate humano entra no try: negar o deploy levanta, e esse desfecho é tão
    # informativo quanto uma queda do provedor. Com os dois lados cobertos, um
    # histórico sem `fim_execucao` passa a significar uma coisa só — a execução
    # ainda está viva, ou o processo morreu sem chance de registrar.
    try:
        _rodar(grafo, entrada, config)
        estado = grafo.get_state(config)
        if estado.next:  # pausado no gate humano
            print("\nGrafo pausado aguardando aprovação humana.")
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
        registrar(thread_id, "fim_execucao", desfecho="negado", motivo=str(e))
        print(f"\nDeploy recusado no gate: {e}")
        print(f"Nada foi publicado. Para reabrir a decisão: python main.py --thread {thread_id}")
        raise SystemExit(1)
    except Exception as e:
        registrar(
            thread_id, "fim_execucao",
            desfecho="erro",
            erro=f"{type(e).__name__}: {e}"[:300],
        )
        print(f"\nExecução interrompida: {e}")
        print(f"Progresso salvo. Retome com: python main.py --thread {thread_id}")
        raise SystemExit(1)

    final = grafo.get_state(config).values
    registrar(
        thread_id, "fim_execucao",
        desfecho="deploy" if final.get("deploy_ok") else "sem_deploy",
        deploy_ref=final.get("deploy_ref", ""),
    )
    print("\nDeploy ok:", final.get("deploy_ok", False))
    if final.get("deploy_ref"):
        print("Entrega publicada em:", final["deploy_ref"])
    print(resumo(thread_id))


if __name__ == "__main__":
    main()
