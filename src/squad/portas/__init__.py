"""Portas: as formas que o domínio e o grafo enxergam da tecnologia.

Existem só onde há variação real — duas implementações plausíveis, não uma
hipótese. Hoje são quatro:

- `perfil`   a stack da entrega (Python, e depois Next.js e Java)
- `testes`   quem executa a suíte (Docker, host)
- `executor` quem escreve o código (OpenCode CLI, crews CrewAI)
- `metricas` onde o histórico da execução é gravado (JSON, e talvez um banco)

Publicação, pentest e LLM ficaram de fora de propósito: têm uma implementação
só, e uma porta para uma implementação é fiação sem ganho.
"""
