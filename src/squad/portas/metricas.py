"""Porta do histórico de execução: onde os eventos são gravados e lidos.

Uma implementação hoje (um JSON por thread em `metrics/`), e é o candidato mais
provável a ganhar a segunda: o volume cresce por execução, o painel já lê o
histórico inteiro para montar a série, e um banco resolveria as duas coisas.

O contrato tem duas metades e um invariante entre elas: **quem grava é um só**.
O painel lê pela mesma porta e nunca escreve — a mesma separação que o grafo já
faz entre quem produz e quem julga.
"""
from typing import Protocol


class RepositorioDeMetricas(Protocol):
    def registrar(self, thread_id: str, evento: str, **dados) -> None:
        """Acrescenta um evento ao histórico da execução."""
        ...

    def ler(self, thread_id: str) -> list[dict]:
        """Histórico completo, na ordem em que foi gravado.

        Devolve lista vazia quando não há nada — nunca levanta. Uma leitura que
        falha no meio de uma escrita é normal (o arquivo é reescrito inteiro a
        cada evento) e não é motivo para derrubar quem lê.
        """
        ...
