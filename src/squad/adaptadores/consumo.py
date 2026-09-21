"""Quanto cada nó gastou do provedor, somado sem o nó precisar saber.

O Codex fecha cada `codex exec --json` com um `turn.completed` que traz o uso
de tokens da chamada. Quem chama o Codex (executor, QA, agentes de texto)
entrega esse uso aqui; quem mede (`metricas_json.medir`) abre uma contagem ao
redor do nó e grava o total no evento.

A contagem vive num `ContextVar`, não numa variável global: no modo paralelo
vários nós rodam ao mesmo tempo, e um contador global misturaria o gasto de um
serviço com o de outro. O LangGraph copia o contexto para a thread que roda o
nó, então as chamadas feitas lá dentro chegam à contagem certa. Chamada feita
numa thread criada sem copiar o contexto não é contada — o total fica menor,
nunca atribuído ao nó errado.
"""
import json
from contextlib import contextmanager
from contextvars import ContextVar

# Pilha de contagens abertas: um `medir` dentro de outro soma nos dois.
_abertas: ContextVar[tuple[dict, ...]] = ContextVar("consumo_aberto", default=())

# Nome no `turn.completed` do Codex → nome gravado nas métricas.
_CAMPOS = {
    "input_tokens": "entrada",
    "cached_input_tokens": "entrada_cache",
    "output_tokens": "saida",
    "reasoning_output_tokens": "raciocinio",
}


def uso_do_stream(saida: str) -> dict:
    """Soma o uso de todos os `turn.completed` de uma saída `--json`."""
    total: dict = {}
    for linha in (saida or "").splitlines():
        linha = linha.strip()
        if not linha.startswith("{"):
            continue
        try:
            evento = json.loads(linha)
        except ValueError:
            continue
        if evento.get("type") != "turn.completed":
            continue
        for origem, destino in _CAMPOS.items():
            valor = (evento.get("usage") or {}).get(origem)
            if isinstance(valor, int):
                total[destino] = total.get(destino, 0) + valor
    return total


def acumular(uso: dict) -> None:
    """Soma `uso` em todas as contagens abertas neste contexto."""
    if not uso:
        return
    for contagem in _abertas.get():
        for campo, valor in uso.items():
            contagem[campo] = contagem.get(campo, 0) + valor
        contagem["chamadas"] = contagem.get("chamadas", 0) + 1


@contextmanager
def contar():
    """Abre uma contagem; o dicionário devolvido é preenchido até o fim do bloco."""
    contagem: dict = {}
    token = _abertas.set(_abertas.get() + (contagem,))
    try:
        yield contagem
    finally:
        _abertas.reset(token)
