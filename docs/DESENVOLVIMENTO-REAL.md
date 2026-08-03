# Roadmap: de Simulação a Desenvolvimento Real

**Objetivo do projeto:** construir desenvolvimento real, não aparentar a
simulação de uma squad. Agentes devem escrever arquivos de verdade, executar
testes reais e fazer deploy real.

## O princípio que organiza tudo

> **Vereditos devem vir de execução, não de opinião.**

Hoje, o roteamento do grafo depende de um LLM dizer "APROVADO" — o QA
*imagina* o resultado dos testes que escreveu, e o revisor revisa código que
nunca rodou. Na versão real, o roteamento depende do **exit code do pytest**.
Essa é a diferença entre simular engenharia e praticá-la: um agente com acesso
ao mundo real supera cinco agentes conversando sobre ele.

## As cinco fases

> **Status (ago/2026):** Fases 1–7 **implementadas**. Os testes gerados por
> LLM rodam em **sandbox Docker** (`TEST_RUNNER=docker`, padrão; `host` é a
> saída para quem não tem Docker), com ferramentas de arquivo confinadas ao
> workspace como camada adicional. Entregas são em **Python** (o pytest é o
> juiz) e as dependências do código gerado vêm de um **ambiente
> pré-provisionado** (requirements.txt e a imagem do sandbox) — sem pip
> install em runtime. O deploy publica a entrega na branch
> `entrega/<thread_id>` do repositório configurado em `DEPLOY_REPO` (sem a
> variável, commit local apenas).

### Fase 1 — Workspace real por execução
Uma pasta `workspace/<thread_id>/` onde o código vive como **arquivos**, não
como string gigante no estado do grafo. O estado passa a carregar caminhos e
metadados. Efeito colateral: resolve pela raiz o problema de contexto inflado
(item 5 do RESILIENCIA.md).

### Fase 2 — Devs que escrevem no disco
Os agentes de desenvolvimento ganham ferramentas de arquivo
(`FileWriterTool`/`FileReadTool` do `crewai_tools`) e materializam módulos
reais no workspace, em vez de descrever código em markdown.

### Fase 3 — QA que executa (a mudança mais importante)
O nó de qualidade vira dois passos:
1. **Determinístico**: roda `pytest` de verdade — veredito objetivo
   (exit code + relatório de falhas), hoje dentro do **sandbox Docker**
   (ver seção "CrewAI vs Docker" abaixo, implementada na Fase 6);
2. **Revisor LLM**: continua existindo para o que execução não pega —
   legibilidade, segurança, aderência à spec.

Aprovação passa a exigir **testes verdes E revisão aprovada**. O feedback do
laço de correção vira o stack trace real — infinitamente mais acionável para
o dev do que crítica textual.

### Fase 4 — Deploy real
O nó de deploy deixa de ser um `print` e executa `git commit` + `push` (ou
build de container), atrás do mesmo gate humano.

*Desenho implementado:* o workspace vira repositório git próprio; a entrega
é commitada (com `.gitignore` para caches de execução) e publicada na branch
`entrega/<thread_id>` do repositório alvo fixo (`DEPLOY_REPO=owner/repo` no
`.env`). Sem `DEPLOY_REPO`, degrada para commit local com aviso. Falha de
git (exit != 0 ou timeout) levanta erro explícito — o checkpoint preserva o
progresso e `--thread` retoma reexecutando só o deploy.

### Fase 5 — OpenCode como músculo
O nó de desenvolvimento invoca o OpenCode CLI em modo headless
(`opencode run`) apontado para o workspace. A assinatura vira a mão de obra;
a squad vira a **gerência**: guard, QA real e gate governando um executor que
já é excelente em escrever código.

*Desenho implementado:* executor **intercambiável** via `DEV_EXECUTOR`
(`opencode`, padrão, ou `crews`) — a governança do grafo é idêntica nos dois
casos, porque o nó lê o disco, não o texto do executor. As instruções vão
para `.squad/tarefa.md` dentro do workspace e o prompt do CLI é uma linha
única apontando para o arquivo: no Windows o binário é o shim `opencode.CMD`
do npm, e argumentos multilinha são truncados na primeira quebra de linha por
cmd.exe (specs longas também esbarrariam no limite de tamanho de argumento).
O executor é proibido de escrever em `tests/` — quem escreve os testes é o QA
da squad, mantendo a separação entre quem implementa e quem valida.

### Fase 6 — A jaula de execução
Os testes deixam de rodar no host e passam a rodar em container efêmero.
Detalhes na seção "CrewAI vs Docker", abaixo.

### Fase 7 — Quem vigia os testes, e como medir se algo melhorou
Duas lacunas que o próprio princípio deste roadmap deixou em aberto:

**Testes verdes não provam correção.** O exit code é objetivo, mas o
*conteúdo* do teste continua sendo opinião de quem o escreveu — o QA pode
produzir asserções triviais ou ignorar os critérios de aceite. Duas
checagens complementares, cada uma pegando o que a outra não pega:

1. **Guard de critérios** (semântico, antes de executar): espelha o guard de
   aderência — uma chamada barata que confere se a suíte testa mesmo o que a
   spec exige. Reprovar antes da execução custa centavos.
2. **Cobertura** (determinística, depois de executar): testes verdes com
   cobertura abaixo de `COBERTURA_MINIMA` indicam suíte que não exercita a
   entrega.

Os dois devolvem o trabalho ao **QA**, não ao desenvolvimento — problema do
teste não se corrige reescrevendo o código. É um laço curto, com teto
próprio (`MAX_TESTES`); ao estourar, o pipeline segue com o sinal fraco
registrado no estado, visível no gate humano e nas métricas: qualidade de
teste é sinal mais brando que teste vermelho.

**Não havia como saber se uma fase melhorou o resultado.** Cada nó passou a
registrar duração e veredito em `metrics/<thread_id>.json`, com resumo
impresso ao final: rodadas de desenvolvimento e de reescrita de testes,
histórico de verde/vermelho e cobertura por rodada, vereditos dos guards e
da revisão, nós mais lentos e onde a entrega foi publicada.

## O que NÃO muda

A arquitetura validada permanece idêntica: grafo, laços com circuit breakers,
checkpoints SQLite, guard de aderência e gate humano. O que muda é o que
acontece *dentro* dos nós:

| Hoje | Versão real |
|------|-------------|
| Código como texto no estado | Arquivos no workspace |
| QA opina sobre testes | pytest executa testes numa jaula |
| Veredito = palavra do LLM | Veredito = exit code + cobertura (+ revisão) |
| Ninguém vigia a qualidade do teste | Guard de critérios + piso de cobertura |
| Deploy = print | Deploy = git push / build |
| Sem saber se melhorou | Métricas por execução |

Todo o investimento em resiliência (RESILIENCIA.md) se transfere direto.

## E o padrão de arquitetura? (Clean Architecture?)

**Não — e a distinção importa.** O projeto tem *separação de
responsabilidades* em módulos (config / crews / graph), o que lembra
organização em camadas, mas **não implementa Clean Architecture**:

- Não há núcleo de domínio independente de framework: as crews dependem
  diretamente do CrewAI, o grafo depende diretamente do LangGraph;
- Não há inversão de dependência (a "regra da dependência" de apontar sempre
  para o domínio não é seguida);
- Não há camadas de entidades / casos de uso / adaptadores de interface.

Os padrões que o projeto **realmente** usa:

- **Orquestrador + trabalhadores** (orchestrator–workers): o grafo decide o
  fluxo; as crews executam o trabalho especialista;
- **Máquina de estados com persistência** (o grafo LangGraph com checkpointer);
- **Pipeline sequencial com laços de correção** e roteamento condicional;
- **Circuit breaker** nos dois laços;
- **Human-in-the-loop** como gate antes de ação irreversível;
- **Configuração declarativa** (agentes e tarefas em YAML, separados do código).

Adotar Clean Architecture de verdade só faria sentido se o projeto precisasse
trocar de framework de agentes sem reescrever o domínio (ex.: abstrair
"squad" para rodar sobre CrewAI *ou* MAF). É um investimento válido no futuro
da plataforma, mas prematuro agora — o valor atual está na governança do
processo, não na independência de framework.

## CrewAI vs Docker: camadas diferentes, não concorrentes

Pergunta recorrente: "qual a vantagem de usar CrewAI vs container Docker?"
A comparação tem uma pegadinha — **eles resolvem problemas de camadas
diferentes** e, neste projeto, trabalham juntos:

- **CrewAI** organiza *quem pensa*: agentes, papéis, tarefas, colaboração;
- **Docker** organiza *onde as coisas rodam*: processo isolado, com sistema
  de arquivos, dependências e limites de recursos próprios.

Comparar os dois é comparar o organograma da equipe com o prédio onde ela
trabalha.

### O recorte em que a comparação existe

"Poderia implementar a squad como um conjunto de containers, sem framework
de agentes?" Poderia — cada agente vira um serviço, conversando por filas ou
HTTP. Trade-offs:

| Squad como containers (sem framework) | Squad como está (CrewAI em um processo) |
|---|---|
| Isolamento total entre agentes | Tudo compartilha o mesmo ambiente |
| Escala independente por agente | Escala como um processo único |
| Qualquer linguagem por agente | Python |
| Resiliência de infra madura (restart, healthcheck, K8s) | Resiliência via LangGraph (checkpoints, retomada) |
| Reimplementa na mão contexto, papéis e encadeamento | Tudo isso vem pronto do framework |
| Ainda precisa de orquestrador de processo* | LangGraph já orquestra o processo |

\* Kubernetes orquestra **containers**, não decide se a spec adere ao pedido —
o LangGraph continuaria necessário. Containerizar os agentes multiplica a
infraestrutura para o mesmo resultado lógico.

### Onde o Docker entra de verdade: a jaula de execução (Fase 6)

> **Implementado.** `src/squad/sandbox.py` com `TEST_RUNNER=docker` (padrão) e
> a imagem `squad-sandbox:latest` (`Dockerfile.sandbox`). Não há fallback
> silencioso para o host: se o daemon ou a imagem faltarem, o nó falha alto e
> nomeia as duas saídas — cair para fora da jaula justamente quando ela falha
> é o pior momento para rodar código não confiável na máquina.


A Fase 3 implica **executar código escrito por LLM** na máquina do operador.
Código gerado por modelo pode conter qualquer coisa — um `rm` mal colocado,
um loop que consome memória, uma dependência maliciosa alucinada. Executar
isso direto no host é risco inaceitável.

A resposta clássica é o sandbox Docker efêmero no nó de QA — mas o desenho
ingênuo (`docker run -v workspace:/app ... pip install && pytest`) falha em
três pontos:

1. **`pip install` com `--network none` é contraditório** — sem rede, nada é
   baixado. E dar rede ao sandbox só para o pip reabre a porta da dependência
   alucinada. Solução: uma **imagem pré-construída** (`squad-sandbox`) com
   pytest e as dependências comuns instaladas e pinadas; import que não
   existe na imagem vira reprova com stack trace, não download em runtime.
2. **Bind mount read-write atravessa a jaula** — um `rm -rf` dentro do
   container apaga os arquivos do host, exatamente o que a jaula deveria
   impedir. O workspace entra **read-only** (`:ro`) e os testes rodam sobre
   uma cópia interna ao container.
3. **Limite de recursos não é limite de tempo** — um loop infinito respeita
   `--memory` e `--cpus` e ainda assim trava o nó de QA para sempre. A
   invocação precisa de teto de tempo, e timeout estourado conta como
   reprova.

O desenho corrigido:

```bash
docker run --rm --name qa-<thread_id> \
  -v "<caminho absoluto>/workspace/<thread_id>:/src:ro" \
  --network none \
  --memory 512m --cpus 1 \
  squad-sandbox:latest \
  sh -c "cp -r /src /app && cd /app && pytest --tb=short"
```

E, no nó de QA, a invocação com teto de tempo:

```python
try:
    r = subprocess.run(comando_docker, capture_output=True, timeout=300)
    aprovado = r.returncode == 0  # exit 5 ("nenhum teste coletado") reprova
except subprocess.TimeoutExpired:
    subprocess.run(["docker", "kill", f"qa-{thread_id}"])
    aprovado = False  # matar o cliente não mata o container; kill explícito
```

Monta o workspace read-only, copia para dentro do container, roda o pytest
**sem rede**, com limites de CPU/memória e teto de tempo, coleta o exit code
e destrói o container. Sandbox descartável por execução. (O caminho do `-v`
deve ser absoluto — no Windows/Docker Desktop, o caminho completo do drive.)

### Síntese

| Camada | Peça | Papel |
|--------|------|-------|
| Colaboração | CrewAI | O cérebro: quem faz o quê |
| Processo | LangGraph | O maestro: quando, em que ordem, com que guardas |
| Execução | Docker | A jaula: onde código não confiável roda com segurança |

O Docker não substitui o CrewAI — ele é a peça que torna seguro transformar
opinião em exit code.
