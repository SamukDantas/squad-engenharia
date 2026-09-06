"""Domínio da squad: as decisões, sem nenhuma tecnologia.

Nada aqui importa langgraph, crewai, docker, subprocess ou pathlib. São as
regras que sobrevivem à troca de stack, de executor e de infraestrutura — e,
por serem puras, são as primeiras da squad que dá para testar sem subir um
container nem gastar um token.

A camada de fora (`graph/workflow.py`) lê disco, chama adaptadores e traduz o
que estes módulos devolvem em efeito: registrar métrica, imprimir aviso,
levantar erro. O domínio nunca faz nada disso — ele só decide.
"""
