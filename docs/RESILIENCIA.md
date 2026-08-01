# Resiliência e Lições de Engenharia — Squad CrewAI + LangGraph

Registro dos problemas reais encontrados durante a construção e validação da
squad, com causa raiz, solução aplicada e o princípio de arquitetura por trás.
Todos ocorreram em execuções reais (jul/2026) usando OpenCode Zen como provedor.

---

## 1. Autenticação: variável de ambiente global vencia o `.env` (HTTP 401)

**Sintoma:** `AuthError: Invalid API key` em todas as chamadas, mesmo com a
chave correta no `.env`.

**Causa raiz:** o Windows tinha uma `OPENCODE_API_KEY` global (do setup do
OpenCode CLI) com uma chave antiga. Por padrão, `load_dotenv()` **não
sobrescreve** variáveis já existentes no sistema — a chave global vencia a do
projeto silenciosamente.

**Solução:** `load_dotenv(override=True)` no `main.py`. O `.env` do projeto
passa a ser a fonte de verdade em qualquer máquina.

**Princípio:** configuração de projeto deve ser determinística e autocontida;
nunca depender do estado do ambiente da máquina.

---

## 2. Catálogo de modelos: IDs inexistentes no provedor

**Sintoma:** risco de `model not found` — o modelo padrão configurado
(`qwen3-coder`) não existia no catálogo da conta.

**Causa raiz:** catálogos de gateways de LLM mudam com frequência; nomes de
modelos decorados ficam obsoletos.

**Solução:** validar o catálogo real via `GET /zen/v1/models` antes de fixar o
`MODEL`, e documentar o comando de verificação no `.env.example`.

**Princípio:** trate o catálogo do provedor como dependência externa
verificável, não como constante.

---

## 3. Delegação hierárquica: manager como dono da própria tarefa

**Sintoma:** `Executor is already running. Cannot invoke the same executor
instance concurrently` na crew de desenvolvimento, seguido de falha total.

**Causa raiz:** no processo hierárquico do CrewAI, a tarefa `implementar`
estava atribuída ao tech lead, que também era o `manager_agent`. Ao delegar,
a delegação recaía nele mesmo — invocação recursiva do mesmo executor.
Agravante: a delegação dinâmica exige modelos fortes em uso de ferramentas.

**Solução:** crew de desenvolvimento convertida para **processo sequencial**
com tarefas explícitas e encadeamento de contexto:
backend implementa → frontend integra → tech lead consolida (sem delegação).

**Princípio:** delegação dinâmica é a fonte nº 1 de falhas em crews; prefira
fluxos sequenciais determinísticos, reservando hierarquia para quando a
decisão de "quem faz o quê" for genuinamente dinâmica.

---

## 4. Alucinação de tema no planejamento → Guard de aderência

**Sintoma:** pedido "criar endpoint de healthcheck"; o arquiteto produziu uma
spec completa de **serviço de checkout e pagamento** (Stripe, PIX, PCI DSS),
ignorando o contexto real. O pipeline teria implementado o sistema errado.

**Causa raiz:** prompt genérico ("a partir dos requisitos levantados...") +
modelo gratuito fraco = o modelo descartou o contexto e regurgitou uma spec
decorada do treino.

**Solução em duas camadas:**
1. *Prompt ancorado*: a tarefa do arquiteto passou a citar o pedido
   explicitamente via placeholder `{pedido}`, proibir requisitos inventados e
   limitar a spec a 150 linhas.
2. *Guard de aderência* (nó `validacao_spec`): uma única chamada barata de LLM
   confere se a spec trata do pedido antes de liberar o desenvolvimento.
   Reprovada → volta ao planejamento (máx. 2 replanejamentos; depois o grafo
   interrompe com erro explícito).

**Princípio:** o grafo pode estar perfeito e o resultado errado — valide a
**coerência semântica** entre etapas, não apenas a execução delas. Falhar alto
e cedo custa centavos; implementar a spec errada custa a execução inteira.

---

## 5. Resposta vazia do LLM em contexto gigante

**Sintoma:** `Invalid response from LLM call - None or empty` na crew de
desenvolvimento.

**Causa raiz:** a spec do arquiteto (sem limite de tamanho) viajava inteira
para cada tarefa seguinte; o modelo gratuito saturava e devolvia vazio.

**Solução:** limite de 150 linhas na spec (item 4) + truncamento defensivo no
guard (8 mil caracteres). Efeito colateral positivo: custo por etapa caiu.

**Princípio:** controle o tamanho dos artefatos que atravessam o estado do
grafo; contexto crescente é custo composto e risco de falha.

---

## 6. Queda do provedor (HTTP 503) → Checkpointing persistente

**Sintoma:** `503 — Inference is temporarily unavailable (failover_exhausted)`
no meio da execução. Com `MemorySaver`, todo o progresso (triagem,
planejamento, guard aprovado) foi perdido — e os tokens, pagos de novo.

**Causa raiz:** instabilidade típica de free tier + checkpoints apenas em
memória, que morrem com o processo.

**Solução:** checkpointer **SQLite** (`checkpoints.sqlite`) com fallback
automático para MemorySaver se o pacote não estiver instalado. O `main.py`
imprime o `thread_id` no início e, em caso de falha, o comando exato de
retomada (`python main.py --thread <id>`), que continua do último nó concluído.

**Princípio:** "execução durável" só existe se o estado sobreviver ao
processo. Checkpointing em memória é para desenvolvimento; qualquer execução
que custa dinheiro merece persistência.

---

## 7. Vereditos de LLM como sinal de roteamento

**Sintoma potencial:** modelos "enfeitam" respostas — "**Veredito: APROVADO**
✅" em vez da palavra exata — quebrando parsers ingênuos e causando loops
desnecessários.

**Solução:** parsers tolerantes nos dois pontos de decisão:
- QA: procura APROVADO/REPROVADO na última linha, case-insensitive.
- Guard: aceita variações de SIM/NAO/NÃO no início da resposta.

**Princípio:** nunca acople roteamento condicional a formato exato de saída de
LLM; normalize e tolere variações, e trate o caso indecifrável como reprova.

---

## 8. Circuit breakers em todos os laços

O grafo tem dois laços de correção, ambos com limite:

| Laço | Limite | Ao estourar |
|------|--------|-------------|
| Replanejamento (guard reprova spec) | 2 | Interrompe com `RuntimeError` explícito |
| Correções de QA (código reprovado) | 3 | Encaminha ao gate humano decidir |

**Princípio:** todo laço alimentado por LLM precisa de teto — sem ele, um
modelo ruim vira loop infinito de tokens. A escolha do comportamento ao
estourar é deliberada: replanejar 3x errado indica problema de modelo/prompt
(pare e avise); código reprovado 3x pode ainda ter valor parcial (humano
decide).

---

## 9. Gate humano antes de ação irreversível

O nó `aprovacao_humana` usa `interrupt()` do LangGraph: a execução pausa,
persiste no checkpoint e só continua com resposta humana explícita. O deploy
(ação com efeito no mundo real) nunca acontece sem aprovação.

**Princípio:** autonomia dos agentes onde ela agrega (criar, testar, revisar);
controle humano onde o erro é caro (publicar). O gate custa segundos e elimina
a classe inteira de "deploy errado automatizado".

---

## 10. Degradação do contexto encadeado em rodadas de correção

**Sintoma:** na segunda rodada do laço de QA, o revisor reprovou alegando
"não há código nem spec para revisar" — reprova por falta de material, não
por defeito real, desperdiçando uma rodada inteira do laço.

**Causa raiz:** a tarefa `revisar` dependia exclusivamente do contexto
encadeado (saída da tarefa `testar`). Com modelo fraco, esse elo degradou
(saída truncada/vazia) e o revisor — que decide o roteamento do grafo via
veredito APROVADO/REPROVADO — ficou às cegas.

**Solução:** injetar os artefatos críticos (`{codigo}` e `{spec}`)
diretamente na descrição da tarefa do revisor, como a tarefa de testes já
fazia. O contexto encadeado vira complemento, nunca fonte única.

**Princípio:** tarefas cuja saída decide **roteamento do grafo** não podem
depender apenas de elos de contexto entre tarefas — elos degradam com
modelos fracos e saídas truncadas. Injete os artefatos essenciais
diretamente; redundância de contexto é barata, uma rodada perdida do laço
não é.

**Validação em execução real da defesa em camadas:** nesta mesma execução,
as três camadas atuaram com casos reais — o guard aprovou o tema correto
(camada 1), o QA capturou um desvio fino de contrato (`latencyMs` na spec
vs `latency_ms` no código — camada 2) e o circuit breaker encaminhou a
decisão final ao humano (camada 3).

---

## Resumo da arquitetura de defesa em camadas

```
Camada 1 — Guard de aderência   (barato, automático)  → pega tema errado
Camada 2 — Laço de QA           (caro, automático)    → pega defeito de implementação
Camada 3 — Gate humano          (manual)              → segura ação irreversível
Transversal — Checkpoints SQLite + circuit breakers + parsers tolerantes
```

Cada camada pega uma classe de erro que as outras não pegam; nenhuma sozinha
seria suficiente. Todos os itens deste documento foram descobertos e validados
em execuções reais — não são hipóteses de design.
