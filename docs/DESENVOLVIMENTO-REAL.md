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
1. **Determinístico**: roda `pytest` de verdade em subprocess — veredito
   objetivo (exit code + relatório de falhas);
2. **Revisor LLM**: continua existindo para o que execução não pega —
   legibilidade, segurança, aderência à spec.

Aprovação passa a exigir **testes verdes E revisão aprovada**. O feedback do
laço de correção vira o stack trace real — infinitamente mais acionável para
o dev do que crítica textual.

### Fase 4 — Deploy real
O nó de deploy deixa de ser um `print` e executa `git commit` + `push` (ou
build de container), atrás do mesmo gate humano.

### Fase 5 — OpenCode como músculo (futuro)
O nó de desenvolvimento invoca o OpenCode CLI em modo headless
(`opencode run`) apontado para o workspace. A assinatura vira a mão de obra;
a squad vira a **gerência**: guard, QA real e gate governando um executor que
já é excelente em escrever código.

## O que NÃO muda

A arquitetura validada permanece idêntica: grafo, laços com circuit breakers,
checkpoints SQLite, guard de aderência e gate humano. O que muda é o que
acontece *dentro* dos nós:

| Hoje | Versão real |
|------|-------------|
| Código como texto no estado | Arquivos no workspace |
| QA opina sobre testes | pytest executa testes |
| Veredito = palavra do LLM | Veredito = exit code (+ revisão) |
| Deploy = print | Deploy = git push / build |

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
