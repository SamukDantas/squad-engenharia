# Squad de Engenharia — CrewAI + LangGraph

Esqueleto de um time de engenharia de software composto por agentes especialistas,
usando **LangGraph** como orquestrador (estado, checkpoints, gates humanos) e
**CrewAI** para a colaboração especialista dentro de cada nó do grafo.

## Arquitetura

```
Triagem → Crew planejamento → Guard de aderência → Crew desenvolvimento → Crew testes → pytest → Crew revisão → Aprovação humana → Deploy
              ↑____↻ spec incoerente (máx. 2)_|      ↑____________↻ testes vermelhos / revisão reprovada (máx. 3)____↻_|
```

Princípio central: **vereditos vêm de execução, não de opinião**. Os devs
escrevem arquivos Python reais em `workspace/<thread_id>/`, o QA escreve
testes pytest reais e um nó **determinístico** executa o pytest — o laço de
correções é roteado pelo **exit code**, não pela palavra de um LLM. O revisor
LLM só roda com testes verdes e cobre o que execução não pega (legibilidade,
segurança, aderência à spec).

O **guard de aderência** é uma chamada única de LLM (barata) que confere se a
spec produzida trata mesmo do pedido antes de gastar tokens com o
desenvolvimento — proteção contra alucinação do planejamento. Spec reprovada
volta ao planejamento; após 2 replanejamentos sem sucesso, o grafo interrompe
com erro explícito.

- **LangGraph** decide *quando* avançar, repetir ou parar (arestas condicionais,
  checkpointing, human-in-the-loop antes do deploy) e executa os testes.
- **CrewAI** decide *como* cada etapa é feita (agentes com papéis, tarefas e
  ferramentas de arquivo confinadas ao workspace).

## Estrutura

```
squad-engenharia/
├── main.py                     # ponto de entrada
├── requirements.txt            # deps da squad + ambiente pré-provisionado p/ código gerado
├── .env.example
├── workspace/<thread_id>/      # entrega real de cada execução (gitignored)
└── src/squad/
    ├── config/
    │   ├── agents.yaml         # definição dos agentes (papéis, goals, backstories)
    │   └── tasks.yaml          # definição das tarefas de cada crew
    ├── tools.py                # ferramentas de arquivo confinadas ao workspace
    ├── crews/
    │   ├── planejamento.py     # analista + arquiteto
    │   ├── desenvolvimento.py  # dev backend + dev integração + tech lead
    │   └── qualidade.py        # crew_testes (QA escreve) + crew_revisao (revisor)
    └── graph/
        ├── state.py            # estado compartilhado do grafo
        └── workflow.py         # grafo LangGraph (nós, arestas, pytest, checkpoints)
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
A entrega fica em `workspace/<thread_id>/` — arquivos Python reais, testes em
`tests/` e um README de execução.

O código gerado usa apenas a stdlib e as libs **pré-provisionadas** no
`requirements.txt` (fastapi, flask, httpx, requests): não há `pip install`
em runtime — import fora da lista quebra o pytest e vira reprova com stack
trace real.

## Documentação adicional

- [docs/RESILIENCIA.md](docs/RESILIENCIA.md) — problemas reais encontrados
  (autenticação, alucinação, quedas de provedor, loops) com causa raiz,
  solução e o princípio de arquitetura por trás de cada um.
- [docs/DESENVOLVIMENTO-REAL.md](docs/DESENVOLVIMENTO-REAL.md) — roadmap de
  evolução de simulação para desenvolvimento real (workspace, QA que executa
  testes, deploy real) e os padrões de arquitetura que o projeto usa.

## Decisões de projeto

- Aprovação automática exige **testes verdes E revisão aprovada**; o pytest
  roda em subprocess com timeout de 120s (timeout = reprova). O sandbox
  Docker desta etapa é migração futura (ver docs/DESENVOLVIMENTO-REAL.md).
- O laço de correções tem limite de 3 tentativas — ao estourar, o gate humano
  decide o que fazer com o trabalho reprovado.
- As ferramentas de arquivo são customizadas e **confinadas ao workspace**
  (caminho absoluto ou `..` que escape é recusado) — a jaula mínima sem Docker.
- Checkpointer padrão é SQLite (`checkpoints.sqlite`), com fallback para
  `MemorySaver` se o pacote opcional não estiver instalado.
- A crew de desenvolvimento é sequencial (backend → integração → tech lead
  consolida), sem delegação dinâmica — mais determinística e barata.

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
