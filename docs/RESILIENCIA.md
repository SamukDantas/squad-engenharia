# Resiliência e Lições de Engenharia — Squad CrewAI + LangGraph

Registro dos problemas reais encontrados durante a construção e validação da
squad, com causa raiz, solução aplicada e o princípio de arquitetura por trás.
Todos ocorreram em execuções reais (jul–ago/2026) usando OpenCode Zen como
provedor. Os itens 1–10 vêm da squad em modo simulação; os itens 11–14
apareceram na evolução para desenvolvimento real (Fases 1–5), quando os
agentes ganharam disco, ferramentas e processos externos; os itens 15–16
surgiram ao mover a execução dos testes para uma jaula Docker (Fase 6); os
itens 17–19 vieram da primeira execução com um pedido **complexo** (API REST
com persistência), que é quando as defesas dimensionadas para um arquivo
encontraram um projeto de verdade; e os itens 20–21 vieram da execução
seguinte, que mediu o efeito dessas correções — e encontrou os defeitos delas.

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

## 11. Inspeção TLS na rede: o bundle do certifi não conhece o emissor

**Sintoma:** `Failed to connect to OpenAI API: Connection error` em toda
chamada de LLM, e `git push` falhando com `unable to get local issuer
certificate`. A chave estava correta e o endpoint, no ar.

**Causa raiz:** a rede tem inspeção TLS (proxy/antivírus corporativo). O
certificado apresentado é reassinado por um interceptador cuja CA raiz existe
apenas no repositório de certificados do **sistema operacional** — não no
bundle próprio de cada ferramenta (`certifi` no Python, `ca-bundle.crt` do
OpenSSL no Git).

**Solução:** validar pela cadeia de confiança do SO, sem afrouxar a
verificação — `truststore.inject_into_ssl()` no início do `main.py` e
`git config --global http.sslBackend schannel`.

**Princípio:** em rede com inspeção TLS, o dono da confiança é o sistema
operacional; ferramentas com bundle próprio ficam cegas. A saída é apontar
para o trust store do SO — **nunca** desabilitar a verificação
(`verify=False`, `sslVerify false`), que troca um problema de configuração
por um buraco de segurança permanente.

---

## 12. Dar ferramentas aos agentes muda o requisito de modelo (HTTP 400)

**Sintoma:** após a Fase 2 (devs com ferramentas de arquivo), toda chamada da
crew de desenvolvimento passou a falhar com
`400 — DFLASH speculative decoding does not support grammar-constrained
decoding yet`. O mesmo modelo funcionava perfeitamente antes.

**Causa raiz:** o modelo gratuito configurado não suporta *grammar-constrained
decoding*, a técnica que o CrewAI usa para tool calling estruturado. Enquanto
os agentes só produziam texto livre, a limitação era invisível; ao ganharem
ferramentas, ela virou falha total.

**Solução:** trocar para um modelo com suporte a tool calling e documentar o
requisito no `.env.example` — a escolha de modelo passou a ser função do
**modo de uso**, não só do catálogo.

**Princípio:** capacidade de tool calling é dependência funcional, não
detalhe de performance. Mudanças de arquitetura reprecificam o requisito de
modelo: valide o modelo contra o *como ele vai ser usado* (complementa o
item 2, que trata apenas da existência do ID no catálogo).

---

## 13. Argumento multilinha truncado pelo shim `.CMD` (falha silenciosa)

**Sintoma:** o OpenCode CLI respondeu "não encontrei nenhuma especificação
técnica na sua mensagem" e terminou com **exit 0**. A spec havia sumido no
caminho, sem erro algum.

**Causa raiz:** no Windows, `shutil.which("opencode")` resolve o shim
`opencode.CMD` instalado pelo npm; os argumentos passam por cmd.exe, que
**trunca na primeira quebra de linha**. Prompt de uma linha funcionava,
multilinha não. Specs longas ainda esbarrariam no limite de tamanho de
argumento (~8 mil caracteres).

**Solução:** as instruções passaram a ser escritas em `.squad/tarefa.md`
dentro do workspace, com o prompt do CLI reduzido a uma linha única apontando
para o arquivo. O diretório `.squad/` é excluído da varredura do manifesto e
do `.gitignore` da entrega — instrução da gerência não é artefato de entrega.

**Princípio:** falha silenciosa é pior que erro — o executor seguiu feliz com
metade do prompt e o pipeline teria "funcionado" produzindo a coisa errada.
Ao integrar um processo externo, passe payload grande por **arquivo**, não
por argumento: limites de shell variam por SO e degradam sem avisar.

---

## 14. Subprocesso herdando stdin engoliu a aprovação humana

**Sintoma:** `EOFError: EOF when reading a line` exatamente no gate humano,
depois do pipeline inteiro ter rodado — planejamento, guard, implementação,
testes e laço de correções, todos concluídos.

**Causa raiz:** `subprocess.run` sem `stdin=` **herda o stdin do processo
pai**. O OpenCode CLI consumiu a resposta que estava no buffer de entrada
destinada ao gate. Além de quebrar a aprovação, um subprocesso interativo
poderia travar o pipeline esperando input que nunca chegaria.

**Solução:** `stdin=subprocess.DEVNULL` em todos os subprocessos — CLI, git e
pytest. A execução afetada foi recuperada com `--thread` a partir do
checkpoint, sem repetir nenhum nó pago (item 6 provando seu valor de novo).

**Princípio:** processo filho herda mais do que se imagina. Todo subprocesso
não interativo deve declarar stdin fechado — senão compete pela entrada do
processo pai. O agravante é o local da falha: quanto mais tarde no pipeline,
mais caro o retrabalho — e o gate humano é o último nó.

---

## 15. Exit code 0 que significa falha: o guard que não guardava

**Sintoma:** com o Docker Desktop parado, `docker info` **sai com código 0** e
imprime `Error response from daemon: Docker Desktop is unable to start` no
lugar da versão do servidor. O guard do sandbox, que checava apenas o exit
code, dava o daemon como saudável.

**Causa raiz:** convenção de exit code não é contrato. O cliente Docker
considera que *ele* funcionou — conseguiu falar com o que havia — e reporta o
problema no conteúdo da resposta, não no status. O mesmo enganou o laço que
esperava o daemon subir: ele terminou instantaneamente, com falso positivo.

**Solução:** o guard passou a exigir uma **resposta válida**, não apenas
status de sucesso — versão não vazia e sem sinal de erro no texto. Sem isso, a
falha só apareceria mais tarde, disfarçada de erro do pytest, no meio do laço
de correções.

**Princípio:** ao integrar ferramenta externa, valide a **resposta**, não só o
status. Onde há sinal ambíguo, prefira falhar cedo e alto: um guard que
aprova o que deveria barrar é pior que guard nenhum, porque desloca a falha
para longe da causa. Vale para exit codes como o item 7 vale para vereditos de
LLM — normalize e desconfie.

---

## 16. `pytest` vs `python -m pytest`: o mesmo teste, dois `sys.path`

**Sintoma:** a suíte que rodava verde no host quebrou inteira ao migrar para o
sandbox Docker — `ModuleNotFoundError: No module named 'app'` na **coleta**,
5 erros antes de qualquer teste executar. Mesmo código, mesma versão de
Python, mesmas dependências.

**Causa raiz:** dentro do container o comando era `pytest ...`; no host,
`python -m pytest ...`. Só a forma com `-m` insere o diretório atual no
`sys.path` — sem ela, `from app import app` não resolve.

**Solução:** padronizar `python -m pytest` nos dois runners. A diferença de
invocação era o único delta real entre os ambientes.

**Princípio:** ambientes de execução equivalentes precisam ser invocados de
forma idêntica, senão a paridade é ilusão — e o sintoma aparece longe da
causa (um erro de import parece problema do código gerado, não da forma de
chamar o pytest). Ao migrar uma etapa para outro ambiente, o primeiro
suspeito de qualquer divergência é a **linha de comando**, não o código.

---

## 17. Cobertura agregada esconde o módulo que importa

**Sintoma:** primeira execução com pedido complexo (API REST de tarefas com
CRUD, SQLite, filtro e paginação). Cobertura de **89,4%**, folgada acima do
piso de 70% — e **nenhum teste exercitava os endpoints HTTP**. A API, objeto
do pedido, estava praticamente sem teste.

**Causa raiz:** por arquivo, `repository.py`, `models.py` e `schemas.py`
estavam em 100%, enquanto `routers/tasks.py` — as rotas — ficava em **51,7%**.
Módulos fáceis e bem testados puxam a média e mascaram o difícil. O piso
agregado media a suíte inteira, não o que a suíte deixou de fora.

**Agravante:** o guard de critérios reprovou nas seis tentativas, com razão.
Mas ele é consultivo: estourado o `MAX_TESTES`, o pipeline segue. O sinal
certo existiu, foi registrado e nenhuma camada acima dele reagiu.

**Solução:** piso **por módulo** (`COBERTURA_MINIMA_MODULO`, padrão 60%)
calculado sobre o arquivo menos coberto, lido do mesmo `coverage.json`.
Reprovar devolve ao QA com o arquivo nomeado no feedback. Um sinal semântico
que ninguém obedece vira um sinal determinístico que bloqueia.

**Princípio:** métrica agregada é média, e média esconde exatamente o que se
quer encontrar. Quando um número decide roteamento, meça também o **pior
caso** — e prefira transformar um veredito consultivo repetidamente ignorado
em regra executável, em vez de aumentar a severidade do aviso.

---

## 18. Guard que julga por amostra sem saber que é amostra

**Sintoma:** o guard de critérios reprovava sistematicamente a suíte de um
projeto multi-arquivo (6 de 6 tentativas).

**Causa raiz:** o prompt recebia os primeiros 8.000 caracteres da suíte
concatenada — **48%** dos 16.506 reais. A ordem é alfabética, então arquivos
no fim ficavam invisíveis: um `test_routers.py` existente seria julgado como
inexistente. Neste caso o veredito estava certo por outro motivo (não havia
mesmo testes de endpoint), o que é pior — o acerto acidental esconderia o
defeito por muito tempo.

**Solução:** amostra com **todos os arquivos representados** — manifesto
completo da suíte mais uma fatia de cada arquivo, com o orçamento dividido
entre eles e marcação explícita de truncagem por arquivo.

**Princípio:** truncar contexto é inevitável; truncar **enviesado** não.
Se o julgamento é sobre um conjunto, a amostra precisa cobrir o conjunto —
cortar pelo total elimina sempre os mesmos elementos e produz um veredito
confiante sobre o que o modelo nunca viu.

---

## 19. Rastro do executor virando entrega

**Sintoma:** a entrega continha `test_output.txt`, `full_test_result.txt`,
`install_output.txt`, `test_result.txt` e um `pywhere.txt` vazio, na raiz do
projeto — tudo a caminho do deploy.

**Causa raiz:** o executor roda comandos no workspace e salva a saída em
arquivos. Como a varredura do manifesto trata o workspace inteiro como
entrega, o rastro entrou no dump enviado ao revisor (gastando contexto) e
seria publicado.

**Solução em duas camadas:** instrução explícita ao executor (nada de arquivos
de rastro; transitório vai para `.squad/`) e filtro por padrão de nome
(`*.log`, `*_output.txt`, `*_result.txt`) no manifesto e no `.gitignore` da
entrega. O filtro é deliberadamente conservador: não descarta por tamanho,
porque arquivo vazio pode ser legítimo (`py.typed`, `__init__.py`) — e por
isso um nome arbitrário como `pywhere.txt` ainda passa. A prevenção no prompt
é a defesa principal; o filtro é a rede.

**Princípio:** quem trabalha num diretório deixa rastro. Separe cedo o que é
**entrega** do que é **subproduto do trabalho** — e prefira prevenir na
instrução a adivinhar por heurística depois, porque heurística sobre nome de
arquivo erra nos dois sentidos.

---

## 20. A régua nova mediu a coisa errada: script auxiliar sequestrando o piso

**Sintoma:** na execução seguinte à criação do piso por módulo (item 17), o
"pior módulo" passou a ser `run_tests.py` com **0%** — um script de
conveniência que o executor criou para chamar o pytest e que nenhum teste
importa. Com testes verdes, o piso teria bloqueado a entrega mandando o QA
escrever testes **para um runner de testes**.

**Causa raiz:** a régua nova não distinguia *módulo de entrega sem teste* — o
caso real que ela existe para pegar — de *script auxiliar naturalmente sem
teste*. Ambos aparecem no `coverage.json` com o mesmo formato, e o script,
nunca importado, marca 0% e ganha o pior lugar por construção.

**Solução em duas camadas, a mesma forma do item 19:** instrução ao executor
para não criar scripts de execução de testes (o pipeline já roda a suíte) e
uma lista conservadora de nomes convencionais (`run_tests.py`, `manage.py`,
`setup.py`…) no `omit` do coveragerc. Verificado nos dois sentidos: o falso
positivo sumiu e o caso original (`routers/tasks.py` a 51,7%) continua sendo
detectado.

**Princípio:** toda métrica nova precisa de um teste de regressão nos **dois
sentidos** — que ela pegue o caso que motivou sua criação e que não pegue o
caso parecido que é legítimo. Uma defesa que dispara errado gasta rodadas
caras perseguindo um problema que não existe, e ensina a equipe a ignorá-la.

---

## 21. Teto dimensionado para a primeira rodada

**Sintoma:** a terceira rodada de desenvolvimento estourou os 900s do
`TIMEOUT_OPENCODE` e derrubou a execução de um projeto multi-arquivo.

**Causa raiz:** o teto foi calibrado observando a primeira rodada de um
pedido simples. Mas rodadas de **correção** ficam progressivamente mais
caras: o código cresce, o feedback acumula e o executor precisa ler o que já
existe antes de mudar. Medido: 497s, 476s, e a terceira acima de 900s.

**Solução:** teto configurável (`TIMEOUT_DESENVOLVIMENTO`, padrão 1800s). O
propósito do teto é matar CLI travado, não trabalho legítimo demorado — e o
checkpoint garante que estourar custa uma rodada, não a execução.

**Princípio:** teto calibrado no caso mais barato vira falha do caso normal.
Dimensione limites pelo **pior caso plausível** da operação, não pelo
primeiro que você mediu — e prefira torná-los configuráveis a fixá-los na
constante que pareceu razoável no dia.

---

## 22. Soluço do provedor matando o nó inteiro

**Sintoma:** três interrupções na mesma sequência de execuções complexas — um
401 por saldo, e dois `TypeError: 'NoneType' object is not subscriptable`
vindos de dentro do CrewAI/LiteLLM ao acessar uma resposta malformada do
provedor. A pior delas derrubou o nó de escrita de testes **após 844
segundos** de trabalho.

**Causa raiz:** o grafo tinha defesa para laço semântico (circuit breakers) e
para queda de processo (checkpoints), mas **nenhuma para o soluço de uma
chamada**. E o checkpoint salva nós *concluídos*, não trabalho parcial dentro
de um nó: com nós longos, a granularidade do checkpoint vira a unidade de
perda.

**Solução:** `com_retry` em volta de toda chamada de LLM dos nós — 3
tentativas com espera dobrando (5s, 10s). A decisão de repetir é por
**conteúdo do erro**, não por tipo: resposta malformada, 5xx, timeout e
limite de taxa são repetidos; saldo zerado, credencial inválida e modelo
incapaz (item 12) sobem na primeira ocorrência.

**Princípio:** resiliência tem camadas por *duração da falha*, e faltava a
mais curta. Repetir o que é transitório e **falhar rápido no que é
permanente** são a mesma disciplina: insistir em credencial inválida gasta
tempo e esconde a causa, tanto quanto desistir de um 503 desperdiça trabalho
já feito. A classificação é o coração do mecanismo — um retry que repete tudo
é quase tão ruim quanto não ter retry.

---

## Resumo da arquitetura de defesa em camadas

```
Camada 1 — Guard de aderência   (barato, automático)     → pega tema errado
Camada 2 — Guard de critérios   (barato, automático)     → pega suíte que não testa a spec
Camada 3 — pytest na jaula      (barato, determinístico) → pega defeito que executa errado
Camada 4 — Piso de cobertura    (barato, determinístico) → pega teste que não exercita a entrega
            agregado + por módulo                        → e o módulo que a média esconde
Camada 5 — Revisor LLM          (caro, automático)       → pega o que passa nos testes
Camada 6 — Gate humano          (manual)                 → segura ação irreversível
Transversal — Checkpoints SQLite + circuit breakers + parsers tolerantes
            + confinamento de ferramentas ao workspace + sandbox sem rede
            + métricas por execução + retry de falha transitória
```

Resiliência em três escalas de tempo, cada uma para uma duração de falha:

```
Soluço da chamada   → retry com backoff        (segundos)
Queda do processo   → checkpoints SQLite       (retomada por --thread)
Laço improdutivo    → circuit breakers         (rodadas)
```

Cada camada pega uma classe de erro que as outras não pegam; nenhuma sozinha
seria suficiente. Todos os itens deste documento foram descobertos e validados
em execuções reais — não são hipóteses de design.

Duas evoluções mudaram a natureza da defesa. Com as Fases 1–5, o sinal mais
caro do grafo deixou de ser opinião de LLM e virou **execução**: o laço de
correções é roteado pelo exit code do pytest, e o revisor só é acionado com
testes verdes. Com as Fases 6–7, a execução ganhou **jaula** (container sem
rede, entrega read-only) e a própria suíte passou a ser vigiada — porque
testes verdes escritos por quem também poderia escrevê-los fracos não provam
correção. O item 7 (parsers tolerantes) segue valendo onde LLM ainda decide:
os dois guards e o veredito da revisão.

Uma camada só serve para o que ela consegue ver. Os itens 15 e 16 mostram o
custo de confiar em sinal ambíguo — exit code que mente, invocação que muda o
`sys.path`: guarda que aprova o que deveria barrar desloca a falha para longe
da causa, e é justamente onde o depurador não vai procurar.

E os itens 17–19 mostram que **defesa se dimensiona com o trabalho**: as
mesmas camadas que aprovaram um healthcheck em uma rodada deixaram passar uma
API sem testes de endpoint, porque mediam o agregado e enxergavam metade da
suíte. O pedido complexo não quebrou o pipeline — ele revelou onde as réguas
tinham sido calibradas para um arquivo só.
