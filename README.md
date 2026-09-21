# Squad de Engenharia — CrewAI + LangGraph

Esqueleto de um time de engenharia de software composto por agentes especialistas,
usando **LangGraph** como orquestrador (estado, checkpoints, gates humanos) e
**CrewAI** para a colaboração especialista dentro de cada nó do grafo.

## Arquitetura

```
Triagem → Planejamento → Guard aderência → Desenvolvimento → Testes → pytest → Revisão → Pentest → Visual → Aprovação humana → Deploy
              ↑__↻ spec incoerente (máx. 2)_|   ↑___↻ testes vermelhos (máx. 3) / revisão reprovada (máx. 2) / vuln bloqueante (máx. 2) / contraste reprovado (máx. 2)___↻_|
```

Princípio central: **vereditos vêm de execução, não de opinião**. O executor
de desenvolvimento escreve arquivos Python reais em `workspace/<thread_id>/`,
o QA escreve testes pytest reais e um nó **determinístico** executa o pytest —
o laço de correções é roteado pelo **exit code**, não pela palavra de um LLM.
O revisor LLM só roda com testes verdes e cobre o que execução não pega
(legibilidade, segurança, aderência à spec).

A segurança tem também uma camada de **execução**, não só de opinião: com
`PENTEST_HABILITADO=1`, um nó determinístico sobe a entrega como servidor num
sandbox Docker isolado (rede `--internal`, sem egress) e a ataca com um toolset
ofensivo (nuclei, nikto, sqlmap, ffuf). Vulnerabilidade acima do piso de
severidade reabre o laço de desenvolvimento com um brief de correção, com
orçamento próprio; abaixo do piso, informa o gate humano. É a mesma régua do
pytest — veredito por ataque real — aplicada à segurança.

O que se **vê** também tem camada de execução. Com `VISUAL_HABILITADO=1`, um
nó determinístico abre cada página da entrega num Chromium headless isolado
(`--network none`), uma vez por tema do sistema, e mede o contraste real entre
cada texto e o fundo que aparece atrás dele. Abaixo do piso WCAG AA, a rodada
volta ao desenvolvimento com as razões medidas. É a classe de defeito que as
outras camadas não alcançam por construção: o revisor LLM lê `color: #1f2937` e
não sabe o que aparece atrás, e o pytest não pinta pixel — uma entrega real da
squad passou por 42 testes verdes e por um revisor que aprovou, estando
ilegível no tema escuro (1,28:1 medido, exigido 4,5:1).

O nó de desenvolvimento é **intercambiável** (`DEV_EXECUTOR`): por padrão usa
o **Codex CLI** (`codex exec`) em modo headless como mão de obra, com a squad
no papel de gerência (guard, QA real e gate governando o executor); `opencode`
usa o **OpenCode CLI** do mesmo jeito; `crews` mantém o caminho com as crews
CrewAI, sem dependência externa. A tarefa que os dois CLIs recebem é a mesma
(`adaptadores/tarefa_executor.py`) — o que muda é a jaula de cada um, e a
governança do grafo não muda em nenhum caso. Do mesmo jeito, o
**provedor de LLM** é escolhido por `LLM_PROVEDOR` (Codex, o padrão; gateway
gateway corporativo on-premise ou OpenCode Zen), sem mudar nada na governança do grafo.

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
├── pytest.ini                  # a suíte da squad é só tests/, nunca as entregas
├── tests/                      # testes do domínio (rotas, guards, vereditos, orçamento)
├── .env.example
├── workspace/<thread_id>/      # entrega real de cada execução (gitignored)
└── src/squad/
    ├── config/
    │   ├── agents.yaml         # definição dos agentes (papéis, goals, backstories)
    │   └── tasks.yaml          # definição das tarefas de cada crew
    ├── portas/                 # as formas: perfil de stack, testes, executor, métricas
    ├── adaptadores/            # implementações: perfil python, runner de testes, opencode, codex, métricas
    │   ├── tarefa_executor.py  # a tarefa que todo executor recebe (texto e .squad/tarefa.md)
    │   └── keycloak_token.py   # token do gateway corporativo: lê e renova o login do OpenCode
    ├── dominio/                # as decisões, sem nenhuma tecnologia
    │   ├── rotas.py            # para onde ir depois de cada nó, e os tetos
    │   ├── guards.py           # entrega vazia e rodada que não corrigiu nada
    │   ├── vereditos.py        # leitura de SIM/NAO e APROVADO/REPROVADO
    │   └── orcamento.py        # repartição do contexto enviado ao LLM
    ├── llm.py                  # LLM da squad: provedor Codex, gateway corporativo ou Zen (LLM_PROVEDOR)
    ├── tools.py                # ferramentas de arquivo confinadas ao workspace
    ├── alvo.py                 # sobe a entrega como servidor em rede isolada
    ├── visual.py               # renderiza a entrega e mede contraste (Docker)
    ├── painel.py               # painel read-only sobre metrics/ (servidor + agregação)
    ├── painel.html             # as três telas do painel (sem build, sem CDN)
    ├── deploy.py               # deploy real (git commit + push da entrega)
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

No modo padrão (`DEV_EXECUTOR=codex`), o executor é o **Codex CLI** (`npm i -g
@openai/codex`, e `codex` uma vez para o login — a conta ChatGPT serve, sem
chave de API). O pacote da Microsoft Store **não** serve: o binário dele fica
numa pasta protegida e responde "Acesso negado". O escopo por execução sai de
graça, com flag em vez de config injetada (`--sandbox workspace-write`,
`--ignore-user-config`, `--ignore-rules`, `--ephemeral`), e `--json` dá a
falha explícita (`turn.failed`) em vez de caça-frases na saída.

No Windows há um detalhe que precisa estar declarado: o modo do sandbox nativo
vem do `config.toml`, que o `--ignore-user-config` ignora junto. A squad manda
`CODEX_SANDBOX_WINDOWS` (padrão `unelevated`; `elevated` é mais forte e exige
instalação como administrador). Sem isso, a política recusa **todo** comando,
inclusive leitura, e a run termina com exit 0 sem ter feito nada.

O mesmo `--ignore-user-config` vale para o **modelo**: o `model` do seu
`config.toml` não chega ao executor. Por isso a squad manda sempre `--model`:
`CODEX_RUN_MODEL`, ou `gpt-5.6-luna` quando a variável está vazia. Com login por conta ChatGPT só valem os modelos liberados para o plano —
medido nesta máquina: `gpt-5.6-terra`, `gpt-5.6-luna` e `gpt-5.5` respondem, e
qualquer outro nome (inclusive o `gpt-5.6-sol` que a documentação cita) volta
`400 not supported when using Codex with a ChatGPT account`. O catálogo da sua
conta está em `~/.codex/models_cache.json`.

Os agentes (planejamento, QA, revisão, guards) seguem `LLM_PROVEDOR`, que por
padrão também é o Codex — ver [Provedor de LLM](#provedor-de-llm-codex-gateway-corporativo-ou-opencode-zen).

Com `DEV_EXECUTOR=opencode`, é preciso ter o **OpenCode CLI** instalado e
autenticado (`npm i -g opencode-ai`). Para rodar sem CLI nenhum, use
`DEV_EXECUTOR=crews` no `.env`.

O `--dir` do CLI troca o diretório de trabalho, mas **não isola a
configuração**: sem intervenção, o executor herda o `opencode.json` global da
sua máquina — servidores MCP e skills incluídos. Numa instalação real isso
significava 15 MCP habilitados (entre eles controle do SO, Docker e GitHub com
token de push) e 623 skills. A squad fecha esse escopo por execução, e duas
variáveis do `.env` controlam o que fica de pé:

| Variável | Padrão | O que faz |
|---|---|---|
| `OPENCODE_MCP_PERMITIDOS` | vazio | MCP visíveis ao executor; vazio = nenhum |
| `OPENCODE_SKILLS` | `0` | `1` carrega as skills globais no prompt |

O padrão das skills veio de medição — 3 execuções por braço, mesmo pedido,
entrega idêntica nas 6:

| | skills ligadas | skills desligadas |
|---|---|---|
| preâmbulo por rodada | 104.798 tokens | **10.543** |
| custo por rodada | $0,1658 | **$0,0367** |

As skills instaladas eram 90% do que o executor lia antes de chegar na spec.

Auditar o que o executor enxerga, de dentro de um workspace:

```bash
opencode debug config
```

Detalhes e causa raiz em [docs/RESILIENCIA.md](docs/RESILIENCIA.md), item 30.

Os testes gerados rodam em **sandbox Docker** (`TEST_RUNNER=docker`, padrão).
Construa a imagem uma vez:

```bash
docker build -f Dockerfile.sandbox-python -t squad-sandbox-python:latest .
```

Sem Docker, use `TEST_RUNNER=host` — os testes passam a rodar direto na sua
máquina, **sem jaula**.

## Verificação visual da entrega (opcional)

Ligue com `VISUAL_HABILITADO=1` e construa a imagem uma vez:

```bash
docker build -f Dockerfile.visual -t squad-visual:latest .
```

O nó abre cada `.html` da entrega num Chromium headless com `--network none`,
duas vezes por página (`prefers-color-scheme` claro e escuro), e mede o
contraste de cada texto contra o fundo efetivo — subindo a árvore até achar um
`background-color` opaco. Abaixo do piso WCAG AA (4,5:1 para texto normal, 3:1
para texto grande), o achado vira item de um brief de correção determinístico e
a rodada volta ao desenvolvimento, com orçamento próprio (`MAX_VISUAL`).

Há uma checagem de causa raiz separada: página que não declara
`background-color` no `body` nem na raiz herda o canvas do navegador e inverte
junto com o tema do sistema enquanto as cores de texto ficam paradas. É o
defeito exato que motivou o nó.

**Dois modos, e quem escolhe é a entrega, não a configuração:**

- **HTML estático no workspace** → aberto por `file://`, com `--network none`.
  É o caso de entregas Python que servem uma página pronta.
- **Entrega que só existe servida** (Next.js e afins, que não deixam `.html` no
  workspace) → a squad **sobe a aplicação** numa bridge Docker `--internal`, sem
  rota para a internet, e renderiza o que o servidor devolve. Mede a raiz e um
  nível de links da mesma origem: a raiz costuma ser landing, e numa entrega
  real o dashboard morava em `/dashboard`.

A jaula muda de forma, não de princípio — é a mesma contenção do pentest, e pela
mesma razão: o alvo *precisa* estar alcançável, então o que se corta é a saída.
A infraestrutura de subir o alvo é compartilhada pelos dois nós
([`src/squad/alvo.py`](src/squad/alvo.py)).

**Só o que é HTML é julgado.** Uma entrega de API responde JSON, e medir
contraste num corpo JSON reprovaria toda API por ruído. Sem nenhuma rota HTML, o
nó passa por ausência de objeto — não por aprovação. Entrega sem HTML e sem
`run.json` passa direto, sem sequer chamar o Docker.

## Testes da squad

O domínio — as decisões — mora em `src/squad/dominio/`, sem nenhum import de
langgraph, crewai, docker ou pathlib. É o que sobrevive à troca de stack, de
executor e de infraestrutura, e é o que dá para testar sem subir container nem
gastar token:

```bash
pytest
```

Entre o domínio e a tecnologia há **portas**, e elas existem só onde há variação
real: o perfil da stack, quem executa a suíte, quem escreve o código e onde as
métricas são gravadas. Publicação, pentest e LLM ficaram de fora de propósito —
têm uma implementação só, e uma porta para uma implementação é fiação sem ganho.

A stack da entrega é um **perfil**: imagem do sandbox, comando de teste, parser
de cobertura, convenção de diretório de teste, `.gitignore` da entrega. Escolhida
por execução e gravada no checkpoint:

```bash
python main.py --stack python "Criar endpoint de healthcheck"
python main.py --stack nextjs "Dashboard de indicadores do funil comercial"
python main.py --stack java   "API de reserva de salas com autenticação"
```

Cada stack tem sandbox próprio, construído uma vez:

| stack | runner | cobertura | build | imagem |
|---|---|---|---|---|
| `python` | pytest | coverage.py | — | `Dockerfile.sandbox-python` |
| `nextjs` | vitest | V8 / istanbul | `next build` | `Dockerfile.sandbox-nextjs` |
| `java` | maven | JaCoCo | no `mvn test` | `Dockerfile.sandbox-java` |

Stack que compila roda o **build antes da suíte**, em container próprio, e falha
nele reprova a rodada sem chegar aos testes — com o erro do compilador e a linha
exata no brief de correção. Isso existe por uma entrega real que passou por 38
testes verdes, 87,4% de cobertura, revisão aprovada e deploy, e **não
compilava**: um `.module.css` com seletor de elemento (`table {}`) é CSS válido
e CSS Module inválido. Os testes transformam TS/JSX sem construir, o revisor não
tem como suspeitar de CSS válido, e o nó visual pula em SPA — nenhuma das três
camadas podia ver.

Tudo roda com `--network none`, então o ambiente vem assado na imagem: o
`node_modules` fica um nível acima do projeto (o resolvedor do Node sobe a
árvore e o encontra), e o `~/.m2` do Java é populado no build rodando um projeto
semente de verdade — `dependency:go-offline` sozinho não traz os plugins que só
são acionados durante o ciclo. Por isso o `pom.xml` da entrega Java não é livre:
o executor recebe no prompt exatamente o [pom de
referência](docker/pom-referencia.xml) que semeou a imagem.

A camada de fora (`graph/workflow.py`) lê disco, chama adaptadores e traduz o
que o domínio devolve em efeito. As rotas, por exemplo, não registram métrica
nem imprimem: devolvem uma `Decisao` com destino, teto e aviso, e o grafo aplica
na ordem de sempre — registrar o teto, imprimir o aviso, levantar o erro.

Os casos da suíte não são inventados: cada um fixa uma lição já paga em execução
real e documentada no [RESILIENCIA.md](docs/RESILIENCIA.md) — o revisor que
escrevia `**APROVADO**` e fechava com um parágrafo (item 27), o executor que
deixou um arquivo de 0 bytes (item 28), a rodada de correção que não mudou um
byte (item 32), o teto do dump que cortava sempre a suíte (item 33).

## Painel de métricas

Cada execução grava um histórico de eventos em `metrics/<thread_id>.json`, e o
`main.py` imprime o resumo ao final. O painel lê os mesmos arquivos e mostra o
que o resumo, olhando uma thread só, não consegue mostrar:

```bash
python -m src.squad.painel        # http://127.0.0.1:4949
```

Três telas:

- **Série de execuções** — uma linha por thread: wall-clock, share de retrabalho,
  rodadas, cobertura final, desfecho e tetos atingidos. É a tela que responde
  "a mudança melhorou?", comparando execuções em vez de descrever uma.
- **Linha do tempo** — uma faixa por nó no eixo do tempo real, colorida pelo
  veredito, com separadores de rodada. Os vãos entre as barras são tempo **fora**
  dos nós: gate humano, queda do provedor, retomada manual.
- **Repartição por rodada** — quanto do tempo foi trabalho novo e quanto foi
  retrabalho, separado pela origem que cobrou a rodada (testes, revisão, pentest).

Read-only por construção: o escritor único de `metrics/` continua sendo o
`metricas.py`, e o painel só abre arquivo para leitura. Serve execução viva
(atualiza sozinho a cada 3s) e histórico antigo pelo mesmo caminho, porque a
fonte é o disco e não o processo do grafo. Sobe só em `127.0.0.1` — não tem
autenticação e expõe o pedido e os vereditos da execução.

Sem servidor, o mesmo dado agregado sai em JSON:

```bash
python -m src.squad.painel --json <thread_id>
```

Execuções anteriores à instrumentação aparecem como `indeterminado`: elas não
têm o marco de fim, e chamá-las de "em curso" faria a taxa de conclusão mentir.

## Pentest da entrega (opcional)

Testes verdes provam que o código funciona; não provam que ele resiste a
ataque. Com `PENTEST_HABILITADO=1`, depois da revisão aprovada, um nó
determinístico sobe a entrega como servidor e a ataca de verdade. Requer duas
imagens, construídas uma vez:

```bash
docker build -f Dockerfile.target-python -t squad-target-python:latest .
docker build -f Dockerfile.pentest -t squad-pentest:latest .
```

O `Dockerfile.pentest` baixa e **pina** o toolset (nuclei, nikto, sqlmap, ffuf)
e os templates do nuclei no build — em runtime nada se atualiza. O `atacar.sh`
em `docker/` orquestra as ferramentas contra o alvo e grava os relatórios.

O sandbox de ataque é isolado por execução: alvo e atacante rodam numa rede
Docker `--internal` (sem rota para a internet), ambos não-root e com limites de
CPU/memória; a entrega é montada read-only e escreve só em `tmpfs`. O alvo é
código gerado por LLM e o atacante é uma caixa de ferramentas ofensivas —
nenhum dos dois pode ter egress. É o `--network none` do pytest aplicado à
fronteira externa.

Para o nó saber subir a entrega, o executor grava `.squad/run.json` com o
comando de subida, a porta e um caminho de health. Um achado com severidade
`>= PENTEST_SEVERIDADE_BLOQUEIO` (padrão `high`) reabre o laço de
desenvolvimento com um brief de correção, com orçamento próprio (`MAX_PENTEST`);
ao estourar, o gate humano decide com o relatório em mãos. Detalhes e causa raiz
em [docs/RESILIENCIA.md](docs/RESILIENCIA.md), item 34.

Para publicar as entregas aprovadas, configure `DEPLOY_OWNER=<conta>` no
`.env`. **Um projeto, um repositório:** o deploy cria
`<DEPLOY_OWNER>/<nome curto>` no GitHub se ele ainda não existir, e publica
a entrega ali. Sem a variável, o deploy commita apenas localmente.

**A squad só publica o que compila.** Antes do push, o nó de deploy compila a
entrega no ambiente da própria stack — `next build`, `mvn compile`,
`compileall` — e falha ali interrompe a publicação com o erro do compilador.
Não é redundante com o passo de build da suíte: os tetos de circuit breaker
roteiam ao gate humano **com a suíte vermelha**, de propósito, e um `sim` ali
publicaria o que não compila. Foi exatamente assim que uma entrega Next.js
quebrada chegou ao GitHub depois de 38 testes verdes e revisão aprovada. A
verificação roda contra o disco, não contra estado guardado: thread retomada
dias depois prova de novo.

O nome do repositório é **curto** e diz o que o projeto é — `conversor-temperatura`,
não `criar-um-modulo-python-de-conversao-de`. Uma chamada barata ao LLM o
decide logo depois que a spec é aprovada, e o nome fica gravado no estado:
retomar a thread publica no mesmo repositório, e o gate mostra o destino
(`Destino do deploy: <DEPLOY_OWNER>/<nome> (private)`) antes de pedir o `sim`.
Resposta fora do formato, ou provedor fora do ar, cai numa regra
determinística (as primeiras palavras úteis do pedido, como
`modulo-python-conversao`). No modo paralelo, cada repositório é o nome do
serviço. A criação é idempotente: retomar a thread
reexecuta o nó de deploy inteiro, e um repositório já criado é reaproveitado.
Repositório novo recebe a entrega em `main`; repositório que já tem commits
recebe em `entrega/<thread_id>`, porque cada execução tem workspace próprio e
portanto histórico git sem ancestral comum.

Requer o **GitHub CLI** (`gh`) autenticado, com a conta ativa sendo o
`DEPLOY_OWNER` ou alguém que administre a org dona. Com mais de uma conta
autenticada, os repositórios privados das outras são invisíveis e o erro que
aparece é `Repository not found` — `gh auth switch --user <conta>` resolve.

## Documentação adicional

- [docs/ARQUITETURA.md](docs/ARQUITETURA.md) — diagrama de sequência completo
  do fluxo (workspace, pytest como juiz, laço de correções, gates e o
  provedor de LLM com o token Keycloak).
- [docs/RESILIENCIA.md](docs/RESILIENCIA.md) — problemas reais encontrados
  (autenticação, alucinação, quedas de provedor, loops) com causa raiz,
  solução e o princípio de arquitetura por trás de cada um.
- [docs/DESENVOLVIMENTO-REAL.md](docs/DESENVOLVIMENTO-REAL.md) — roadmap de
  evolução de simulação para desenvolvimento real (workspace, QA que executa
  testes, deploy real) e os padrões de arquitetura que o projeto usa.

## Decisões de projeto

- Aprovação automática exige **testes verdes E revisão aprovada**. O pytest
  roda em container efêmero — entrega montada read-only, `--network none`,
  512MB/1 CPU e timeout de 120s (timeout = reprova, com `docker kill`).
- Não há fallback silencioso do sandbox para o host: sem Docker, o nó falha
  alto e nomeia as saídas. Perder a jaula justamente quando ela falha é o
  pior momento para rodar código não confiável na máquina.
- Testes verdes não bastam: um **guard de critérios** (antes de executar) e
  **dois pisos de cobertura** — agregado e por módulo — devolvem suítes fracas
  ao QA num laço curto, sem pagar outra rodada de desenvolvimento. O piso por
  módulo existe porque a média esconde o arquivo central do pedido.
- Reprovação de revisão **não repaga a suíte**: a rodada volta ao
  desenvolvimento e segue direto ao pytest, porque o código mudou mas os testes
  não. Escrever a suíte é o nó mais caro do grafo, e reescrevê-la por um
  apontamento de legibilidade consumia metade da execução.
- O revisor só reprova por apontamento **bloqueante** (defeito documentado,
  falha de segurança, violação da spec) e recebe o próprio veredito da rodada
  anterior — sem essa memória ele manda desfazer o que exigiu antes, e o código
  oscila entre duas versões sem convergir.
- Cada execução grava `metrics/<thread_id>.json` com duração e veredito por nó,
  e imprime o resumo ao final — é o que permite comparar duas execuções.
- O deploy é real: após o gate humano, a entrega é commitada e publicada num
  repositório próprio do projeto, criado em `DEPLOY_OWNER` se não existir (sem
  a variável, commit local no workspace apenas).
- O laço de correções tem limite de 3 tentativas, das quais no máximo 2 podem
  ser gastas por reprovação de revisão — ao estourar qualquer um dos dois, o
  gate humano decide o que fazer com o trabalho reprovado. O orçamento do sinal
  determinístico (pytest) não é consumido pelo sinal subjetivo (revisor).
- As ferramentas de arquivo são customizadas e **confinadas ao workspace**
  (caminho absoluto ou `..` que escape é recusado) — a jaula mínima sem Docker.
- Checkpointer padrão é SQLite (`checkpoints.sqlite`), com fallback para
  `MemorySaver` se o pacote opcional não estiver instalado.
- A crew de desenvolvimento é sequencial (backend → integração → tech lead
  consolida), sem delegação dinâmica — mais determinística e barata.

## Provedor de LLM: Codex, gateway corporativo ou OpenCode Zen

`LLM_PROVEDOR` escolhe quem responde aos agentes. O fluxo, com a checagem do
token, está no diagrama de sequência do [ARQUITETURA.md](docs/ARQUITETURA.md).

### Codex (`LLM_PROVEDOR=codex`, padrão)

Todos os agentes rodam pelo **Codex CLI**, com o login da conta ChatGPT — sem
endpoint HTTP, sem chave de API e sem Keycloak. Combine com
`DEV_EXECUTOR=codex` para a squad inteira rodar num só provedor:

```
LLM_PROVEDOR=codex
DEV_EXECUTOR=codex
CODEX_RUN_MODEL=gpt-5.6-luna
```

- **Agentes que só respondem texto** (planejamento, revisão, decomposição,
  contratos, guards, nome do repositório) usam o `CodexLLM`, uma subclasse de
  `BaseLLM` do CrewAI: cada chamada é um `codex exec` em sandbox **read-only**,
  num diretório vazio, com o pedido pelo stdin — argumento multilinha é
  truncado pelo atalho do npm no Windows.
- **O QA**, que grava arquivos, roda como o executor: `codex exec` direto no
  workspace, com a mesma tarefa `escrever_testes` do `tasks.yaml`. Depois, o
  grafo desfaz qualquer arquivo que ele tenha mexido **fora da suíte** — o
  código é do executor, assim como a suíte é do QA.
- Agente com ferramenta pelo CrewAI não roda neste provedor: com
  `DEV_EXECUTOR=crews`, a squad falha na montagem, antes de pagar nó nenhum.

Medido nos mesmos pedidos, somando o tempo dos nós até o gate:

| Pedido | OpenCode + gateway corporativo | Codex + `gpt-5.6-luna` |
|---|---|---|
| Calculadora de juros (Next.js) | 73,3 e 68,3 min, nenhuma verde no gate | média 21,4 min (melhor: 14,7), as duas últimas verdes sem intervenção |
| Passada do QA | média 12,5 min (pior: 23,0) | média 2,3 min (pior: 3,7) |
| Conversor de temperaturas (Python) | 12,5 min | 8,0 e 4,3 min |

Com o Codex só no executor e os agentes ainda no gateway corporativo, o conversor levou
12,6 min: o ganho vem de tirar os agentes do gateway. Parte da melhora também
veio de correções no grafo feitas no meio do caminho; a análise completa está
no item 38 do [RESILIENCIA.md](docs/RESILIENCIA.md).

### Gateway corporativo de IA

Gateway on-premise cedido por um cliente, compatível com OpenAI
(`https://ai-gateway.example.com/v1`), com um único
modelo, `gateway`. Não autentica por chave: autentica por **token Keycloak**,
o mesmo que o plugin `keycloak-token.ts` do OpenCode obtém. Por isso há um
passo manual, **uma vez**: fazer o login Google no OpenCode, num terminal
interativo.

```powershell
$env:OPENCODE_CONFIG = "$HOME\.config\opencode\keys\gateway.json"; opencode
```

A partir daí a squad lê o token salvo pelo plugin
(`~/.config/opencode/.opencode/keycloak-token.json`) e o renova sozinha pelo
refresh token, sob o mesmo lock do plugin. O `Authorization` é trocado a cada
request, então um nó longo não fica com token vencido. Quando o refresh token
também expira, a execução falha **antes** de gastar o nó, com a instrução de
login. No `.env`:

```
LLM_PROVEDOR=gateway
MODEL=openai/gateway
OPENCODE_RUN_MODEL=          # vazio = gateway/gateway
```

Nenhum segredo vai para o `.env`. Os caminhos e URLs têm padrão e podem ser
trocados por `GATEWAY_BASE_URL`, `GATEWAY_TOKEN_FILE`,
`GATEWAY_OPENCODE_PLUGIN`, `KEYCLOAK_ISSUER` e `KEYCLOAK_CLIENT_ID`.

### OpenCode Zen (`LLM_PROVEDOR=zen`)

O [OpenCode Zen](https://opencode.ai/docs/zen) é o provedor pago, via API
compatível com OpenAI (`https://opencode.ai/zen/v1`), e fica como plano B. O
rollback é trocar `LLM_PROVEDOR` e os modelos:

```
LLM_PROVEDOR=zen
OPENCODE_API_KEY=sua-chave
OPENCODE_BASE_URL=https://opencode.ai/zen/v1
MODEL=openai/kimi-k2.7-code
```

Atenção ao endpoint: `/zen/go/v1` é a assinatura **Go**, que não serve modelos
gratuitos e tem cota mensal própria; os gratuitos só existem em `/zen/v1`.

### As três superfícies de modelo

O requisito de cada uma vem do que ela precisa fazer, com qualquer provedor:

| Variável | Quem usa | Precisa de tool calling? |
|---|---|---|
| `MODEL` | guards, planejamento, revisão | não — cabe modelo gratuito |
| `MODEL_FERRAMENTAS` | QA que escreve os testes | **sim** |
| `OPENCODE_RUN_MODEL` | OpenCode CLI no desenvolvimento | **sim** |

No Zen, para experimentar o fluxo gastando pouco, ponha um gratuito em `MODEL`
(`openai/laguna-s-2.1-free`, `openai/deepseek-v4-flash-free`,
`openai/mimo-v2.5-free`) e mantenha as duas superfícies que escrevem em disco
num modelo com tool calling. Gratuito que não sustenta ferramenta não falha
alto: ele devolve o código como markdown na resposta e o nó conclui com o
workspace vazio (item 12 do RESILIENCIA.md).

Todos os agentes compartilham o mesmo LLM por padrão (`src/squad/llm.py`),
mas você pode passar modelos diferentes por agente — ex.: um modelo de código
(`openai/gpt-5.1-codex`) para os devs e um mais barato para triagem — chamando
`squad_llm("openai/<modelo>")` no `build_agent`. Os nós do LangGraph em si são
determinísticos e não consomem tokens; só as crews usam o LLM.
