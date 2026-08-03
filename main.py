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

from src.squad.graph.workflow import construir_grafo  # noqa: E402


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
        entrada = None  # None = continuar de onde parou
    else:
        pedido = " ".join(args.pedido) or "Criar endpoint de healthcheck"
        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}
        print(f"Thread id desta execução: {thread_id}")
        print("(guarde para retomar com: python main.py --thread " + thread_id + ")")
        entrada = {"pedido": pedido}

    try:
        _rodar(grafo, entrada, config)
    except Exception as e:
        print(f"\nExecução interrompida: {e}")
        print(f"Progresso salvo. Retome com: python main.py --thread {thread_id}")
        raise SystemExit(1)

    estado = grafo.get_state(config)
    if estado.next:  # pausado no gate humano
        print("\nGrafo pausado aguardando aprovação humana.")
        resposta = input("Autorizar deploy? (sim/nao): ")
        from langgraph.types import Command
        _rodar(grafo, Command(resume=resposta), config)

    final = grafo.get_state(config).values
    print("\nDeploy ok:", final.get("deploy_ok", False))
    if final.get("deploy_ref"):
        print("Entrega publicada em:", final["deploy_ref"])


if __name__ == "__main__":
    main()
