# Squad de Engenharia — CrewAI + LangGraph

Esqueleto de um time de engenharia de software composto por agentes especialistas,
usando **LangGraph** como orquestrador (estado, checkpoints, gates humanos) e
**CrewAI** para a colaboração especialista dentro de cada nó do grafo.

## Arquitetura

```
Triagem → Crew planejamento → Guard de aderência → Crew desenvolvimento → Crew qualidade → Aprovação humana → Deploy
              ↑______↻ spec incoerente (máx. 2)__|        ↑__________↻ correções (máx. 3)__|
```

O **guard de aderência** é uma chamada única de LLM (barata) que confere se a
spec produzida trata mesmo do pedido antes de gastar tokens com o
desenvolvimento — proteção contra alucinação do planejamento. Spec reprovada
volta ao planejamento; após 2 replanejamentos sem sucesso, o grafo interrompe
com erro explícito.

- **LangGraph** decide *quando* avançar, repetir ou parar (arestas condicionais,
  checkpointing, human-in-the-loop antes do deploy).
- **CrewAI** decide *como* cada etapa é feita (agentes com papéis, tarefas e ferramentas).

## Estrutura

```
squad-engenharia/
├── main.py                     # ponto de entrada
├── requirements.txt
├── .env.example
└── src/squad/
    ├── config/
    │   ├── agents.yaml         # definição dos agentes (papéis, goals, backstories)
    │   └── tasks.yaml          # definição das tarefas de cada crew
    ├── crews/
    │   ├── planejamento.py     # analista + arquiteto
    │   ├── desenvolvimento.py  # tech lead + dev backend + dev frontend
    │   └── qualidade.py        # qa + revisor de código
    └── graph/
        ├── state.py            # estado compartilhado do grafo
        └── workflow.py         # grafo LangGraph (nós, arestas, checkpoints)
```

## Como rodar

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # preencha sua chave de API
python main.py "Criar endpoint de cadastro de usuários com validação de e-mail"
```

O grafo pausa no nó de **aprovação humana** (interrupt do LangGraph). Para aprovar
e seguir ao deploy, retome a execução com o mesmo `thread_id` respondendo `sim`.

## Documentação adicional

- [docs/RESILIENCIA.md](docs/RESILIENCIA.md) — problemas reais encontrados
  (autenticação, alucinação, quedas de provedor, loops) com causa raiz,
  solução e o princípio de arquitetura por trás de cada um.
- [docs/DESENVOLVIMENTO-REAL.md](docs/DESENVOLVIMENTO-REAL.md) — roadmap de
  evolução de simulação para desenvolvimento real (workspace, QA que executa
  testes, deploy real) e os padrões de arquitetura que o projeto usa.

## Decisões de projeto

- Checkpointer padrão é `MemorySaver` (dev). Em produção, troque por
  `SqliteSaver`/`PostgresSaver` para retomada real após falha de processo.
- O laço de correções tem limite de 3 tentativas — evita loop infinito de tokens.
- O tech lead usa processo hierárquico do CrewAI: coordena e delega, sem ferramentas.

## Provedor de LLM: OpenCode Zen

A squad usa o [OpenCode Zen](https://opencode.ai/docs/zen) como provedor,
via API compatível com OpenAI (`https://opencode.ai/zen/v1`). Configure no `.env`:

```
OPENCODE_API_KEY=sua-chave
MODEL=openai/kimi-k2.7-code
```

Todos os agentes compartilham o mesmo LLM por padrão (`src/squad/llm.py`),
mas você pode passar modelos diferentes por agente — ex.: um modelo de código
(`openai/gpt-5.1-codex`) para os devs e um mais barato para triagem — chamando
`zen_llm("openai/<modelo>")` no `build_agent`. Os nós do LangGraph em si são
determinísticos e não consomem tokens; só as crews usam o LLM.
