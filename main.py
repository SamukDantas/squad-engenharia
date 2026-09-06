"""Ponto de entrada da squad.

Uso:
  python main.py "Criar endpoint de healthcheck"        # nova execução
  python main.py --thread <id>                           # retomar execução interrompida

O thread id é impresso no início de cada execução. Com o checkpointer SQLite,
uma queda no meio (erro 503 do provedor, Ctrl+C, crash) não perde o progresso:
retome com --thread e o grafo continua do último nó concluído.
"""
import argparse
import uuid

import truststore
from dotenv import load_dotenv

# Valida TLS pelo repositório de certificados do SO, não pelo bundle do
# certifi — em redes com inspeção TLS (proxy/antivírus corporativo), o bundle
# não conhece o emissor e toda chamada HTTPS falha com CERTIFICATE_VERIFY_FAILED.
truststore.inject_into_ssl()

load_dotenv(override=True)

from src.squad.graph.workflow import DeployNegado, construir_grafo  # noqa: E402
from src.squad.metricas import registrar, resumo  # noqa: E402


def _rodar(grafo, entrada, config) -> None:
    for evento in grafo.stream(entrada, config=config):
        print(f"--- nó concluído: {list(evento.keys())[0]} ---")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pedido", nargs="*", help="Pedido para a squad")
    parser.add_argument("--thread", help="Retomar a execução com este thread id")
    args = parser.parse_args()

    grafo = construir_grafo()

    if args.thread:
        thread_id = args.thread
        config = {"configurable": {"thread_id": thread_id}}
        print(f"Retomando thread {thread_id} do último checkpoint...")
        # Uma thread pode atravessar vários processos. Sem este marco, a única
        # pista de retomada é uma lacuna no relógio entre dois eventos — que
        # também é o que uma chamada lenta ao provedor parece.
        registrar(thread_id, "retomada")
        entrada = None  # None = continuar de onde parou
    else:
        pedido = " ".join(args.pedido) or "Criar endpoint de healthcheck"
        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}
        print(f"Thread id desta execução: {thread_id}")
        print("(guarde para retomar com: python main.py --thread " + thread_id + ")")
        entrada = {"pedido": pedido}
        registrar(thread_id, "inicio_execucao", pedido=pedido)

    # O gate humano entra no try: negar o deploy levanta, e esse desfecho é tão
    # informativo quanto uma queda do provedor. Com os dois lados cobertos, um
    # histórico sem `fim_execucao` passa a significar uma coisa só — a execução
    # ainda está viva, ou o processo morreu sem chance de registrar.
    try:
        _rodar(grafo, entrada, config)
        estado = grafo.get_state(config)
        if estado.next:  # pausado no gate humano
            print("\nGrafo pausado aguardando aprovação humana.")
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
