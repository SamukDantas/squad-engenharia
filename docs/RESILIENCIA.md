# Resiliência e Lições de Engenharia — Squad CrewAI + LangGraph

Registro dos problemas reais encontrados durante a construção e validação da
squad, com causa raiz, solução aplicada e o princípio de arquitetura por trás.
Todos ocorreram em execuções reais (jul–ago/2026) usando OpenCode Zen como
provedor. Os itens 1–10 vêm da squad em modo simulação; os itens 11–14
apareceram na evolução para desenvolvimento real (Fases 1–5), quando os
agentes ganharam disco, ferramentas e processos externos; os itens 15–16
surgiram ao mover a execução dos testes para uma jaula Docker (Fase 6); os
itens 17–24 vieram das execuções com um pedido **complexo** (API REST com
persistência), em voltas sucessivas do mesmo ciclo: rodar encontrou defeitos
nas defesas calibradas para um arquivo (17–19); medir a correção encontrou os
defeitos dela (20–21); a correção seguinte encontrou os defeitos de si mesma
(22–23); e a primeira execução que produziu uma entrega boa revelou que o
parser do veredito contrariava a lição que ele implementava (24). O padrão é o
próprio método — cada volta só apareceu porque a anterior foi executada e
observada, não argumentada.

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

*Continua no item 23: a primeira versão desta defesa classificou por sintoma
e piorou o caso que motivou sua criação.*

---

## 23. O retry que triplicou o custo da falha

**Sintoma:** com o retry do item 22 ativo, o nó de escrita de testes falhou em
**2.694 segundos** (45 min, 3 tentativas) — contra 844 segundos (14 min) da
mesma falha sem retry. As três tentativas terminaram no mesmo erro.

**Causa raiz:** dois erros de julgamento na mesma linha de código.

1. **Classificação por sintoma, não por causa.** O `TypeError: 'NoneType'
   object is not subscriptable` aparecia em dois fenômenos distintos: um
   soluço real do provedor (chamada de 27s, que se recuperou) e a
   **saturação de contexto** do item 5 — o modelo estoura e devolve vazio,
   o CrewAI quebra ao acessar. Saturação é determinística: o contexto não
   encolhe entre tentativas, então repetir só troca um erro rápido por um
   erro lento.
2. **Retry sem noção de custo.** Repetir um guard de 20s é barato; repetir um
   kickoff de crew de 800s custa mais que a própria falha. O mesmo mecanismo
   que protegia um caso arruinava o outro.

**Solução:** classificar saturação (`invalid response from llm`, `none or
empty`) como **permanente**, com mensagem que nomeia a causa e aponta a saída
(reduzir artefatos ou trocar de modelo); e `caro=True` nos kickoffs de crew,
que passam a ter uma única tentativa. Guards curtos mantêm as três.

**Princípio:** classificar falha pelo **texto do sintoma** é frágil quando
causas diferentes produzem o mesmo sintoma — e aqui o sintoma era idêntico
para um problema efêmero e um determinístico. Toda política de repetição
precisa responder duas perguntas, não uma: *isso pode melhorar sozinho?* e
*quanto custa descobrir que não?* Sem a segunda, a defesa vira amplificador
de custo — e o item 20 já tinha avisado que métrica nova precisa de
regressão nos dois sentidos.

---

## 24. A implementação que violou a própria lição

**Sintoma:** execução com entrega boa — testes verdes, 100% de cobertura em
todos os módulos, testes de endpoint reais — em que o revisor escreveu
**APROVADO** e o grafo registrou `aprovado: False`, encaminhando o trabalho ao
gate humano como reprovado.

**Causa raiz:** o parser do veredito exigia o token na **última linha**:

```python
aprovado = "APROVADO" in texto.upper().splitlines()[-1]
```

O revisor concluiu com `**APROVADO**` e então fechou com um parágrafo
("Nenhuma correção necessária. O código é legível..."). A última linha não
continha o token, e a aprovação virou reprova. O item 7 deste documento diz
para nunca acoplar roteamento condicional a formato exato de saída de LLM — e
a implementação dele fazia exatamente isso, só que uma linha mais abaixo.

**Solução:** procurar o veredito nas **últimas linhas** (8), valendo o último
encontrado, com `REPROVADO` explicitamente antes de `APROVADO` na checagem e
nada reconhecível contando como reprova. Verificado contra os textos reais das
duas rodadas desta execução — a que aprovou e a que reprovou — mais seis
formatos sintéticos.

**Custo do defeito:** um falso REPROVADO gasta uma rodada inteira do laço —
desenvolvimento, escrita de testes, guard, execução e revisão de novo. Nesta
execução o teto já havia sido atingido, então nada foi desperdiçado; numa
aprovação de primeira rodada, teriam sido dois ciclos completos.

**Princípio:** a regra escrita e a regra implementada divergem com o tempo, e
o lugar mais perigoso para essa divergência é dentro da própria defesa que a
regra criou. Ao escrever um parser tolerante, teste-o contra **saídas reais do
modelo**, não contra o formato que o prompt pediu — o prompt pede, o modelo
decide.

---

## 25. Nove horas paradas: a falha que nenhuma das três camadas pega

**Sintoma:** execução iniciada às 13:08 encontrada às 22:08 ainda "rodando" —
**nove horas** sem uma linha de log. O processo consumira 60 segundos de CPU
no período (0,2%). O grafo tinha concluído triagem e planejamento e estava
dentro do guard de aderência, esperando uma resposta HTTP que nunca chegou.

**Causa raiz:** todo subprocesso do projeto tem teto de tempo — OpenCode
1800s, pytest 120s, git 60s — mas as **chamadas de LLM não tinham nenhum**. E
o mais instrutivo: nenhuma das três camadas de resiliência ajuda aqui, porque
todas pressupõem que a chamada *retorna*:

| Camada | Precisa de | Um travamento oferece |
|---|---|---|
| Retry (item 22) | uma exceção | nada — não há erro |
| Checkpoint (item 6) | o processo morrer | nada — o processo está vivo |
| Circuit breaker (item 8) | a rodada terminar | nada — a rodada não avança |

Travamento não é uma quarta escala de tempo: é **duração infinita**, e por
isso escapa de um conjunto de defesas que parecia completo.

**Solução:** `timeout` por requisição (`TIMEOUT_LLM`, padrão 300s). Duas
descobertas na medição, ambas registradas no código: o SDK por baixo repete
internamente, então o tempo real até desistir é ~**5,5x** o teto (300s ≈ 27
min); e `num_retries=0` **não** desliga isso — o CrewAI repassa kwargs ao SDK
da OpenAI, que rejeita o parâmetro e quebra toda chamada. A mensagem do
timeout (`Request timed out`) também precisou entrar em `_TRANSITORIAS`: a
lista tinha `"timeout"`, que não casa com `"timed out"` — o item 23 batendo
pela terceira vez.

**Princípio:** um inventário de defesas dá sensação de cobertura que a
realidade não confirma. Ao desenhar resiliência, pergunte de cada camada
**o que ela precisa que aconteça para agir** — e procure a falha que não
oferece nenhum desses gatilhos. Aqui, três camadas dependiam de um evento
(exceção, morte do processo, fim da rodada) e o silêncio não produz nenhum.

---

## 26. Um status, dois significados opostos

**Sintoma:** execução interrompida por cota mensal esgotada
(`429 GoUsageLimitError — Monthly usage limit reached. Resets in 8 days.`), e
o retry insistiu **três vezes** com backoff antes de desistir.

**Causa raiz:** a lista de transitórias casava `"429"` e `"rate limit"`. Mas
429 carrega dois significados opostos:

| 429 significando | Natureza | Ação certa |
|---|---|---|
| "devagar aí, tente em 3s" | transitória | repetir com backoff |
| "cota do mês acabou, volta em 8 dias" | **permanente** | falhar na hora |

O status HTTP é o mesmo; só o corpo distingue. Classificar pelo código dava o
veredito errado para metade dos casos.

**Solução:** `usage limit reached`, `usagelimiterror` e `quota exceeded` em
`_PERMANENTES`, **antes** das transitórias na ordem de checagem — como a
função testa permanentes primeiro, a frase específica vence o `"429"`
genérico. Verificado nos dois sentidos: cota esgotada falha em 1 tentativa,
rate limit real continua sendo repetido.

**Princípio:** código de status classifica o *transporte*, não a *causa*. Onde
o mesmo código cobre condições com respostas opostas — repetir versus desistir
—, a decisão tem que olhar o corpo. É a terceira vez que este documento
registra a mesma lição (itens 23 e 25): classificar por conteúdo só funciona
se o conteúdo real de cada provedor estiver na lista, e cada provedor novo
traz frases novas.

---

## 27. O revisor sem memória oscila o código entre dois pólos

**Sintoma:** execução abandonada pelo custo. Três rodadas do laço, ~39 min de
nós, para uma API de tarefas cujo **pytest ficou verde na rodada 1** — tudo
que veio depois foi opinião do revisor.

| Nó | Rodada 1 | Rodada 2 | Rodada 3 |
|---|---|---|---|
| desenvolvimento | 303s | 347s | 375s |
| **escrever_testes** | **493s** | **381s** | interrompido |
| validacao_testes | 46s | 20s | — |
| pytest (docker) | 3,5s ✅ | 3,2s ✅ | — |
| revisao | 105s ❌ | 77s ❌ | — |

**Causa raiz:** três defeitos que só aparecem juntos.

O primeiro é de roteamento: reprovação de revisão voltava ao desenvolvimento e
caía na aresta estática para `escrever_testes`. A suíte inteira era reescrita —
o nó mais caro do grafo — por um apontamento sobre inicialização do Flask, que
não tinha relação nenhuma com os testes.

O segundo é de critério: o revisor reprovava listando itens que ele próprio
marcava como "opcional" e "estilístico". Não havia distinção entre bloqueante e
sugestão, então preferência de estilo custava uma rodada de desenvolvimento.

O terceiro é o que impedia a convergência. O revisor julga cada rodada do zero,
sem ver o próprio veredito anterior:

| Rodada | Apontamento | Efeito |
|---|---|---|
| 1 | "`init_db()` executado no import de `app.py`" | dev tira a inicialização do import |
| 2 | "o app global não inicializa o banco" (o oposto) | dev devolve a inicialização |
| 3 | código volta a `app = create_app()` no módulo | **reimplementou o que a rodada 1 reprovou** |

Duas rodadas caras para voltar ao ponto de partida. Não é indecisão do modelo:
cada revisão está certa isoladamente, porque o trade-off tem dois lados
defensáveis e nada no laço registra qual deles já foi arbitrado.

**Solução:** roteamento seletivo — rodada nascida de reprovação de revisão vai
do desenvolvimento **direto ao pytest**, sem reescrever a suíte nem repagar o
guard de critérios (o código mudou; os testes precisam *rodar* de novo, não ser
*escritos* de novo). Mais três freios: veredito bloqueante restrito a defeito
documentado, falha de segurança ou violação da spec, com o resto numa seção não
bloqueante; o veredito anterior devolvido ao revisor, proibido de mandar
desfazer o que exigiu antes; e `MAX_REVISOES = 2`, teto próprio para as rodadas
que a opinião pode custar. As redes que tornam o atalho seguro já existiam:
correção que quebra a suíte deixa o pytest vermelho, correção que adiciona
código sem teste cai no piso de cobertura.

**Princípio:** sinal caro e subjetivo não pode ter o mesmo poder de roteamento
que sinal barato e determinístico — nem o mesmo orçamento de rodadas. E um
juiz sem memória do próprio veredito não converge: **oscila**. Onde um laço
consulta um LLM mais de uma vez sobre o mesmo artefato, o histórico da decisão
é parte da entrada, não contexto opcional — a mesma lição do item 10, agora do
lado de quem julga em vez de quem produz.

**Medido depois** (`9e288cb8`, mesmo pedido, mesmo modelo `deepseek-v4-pro`,
mesmo endpoint, mesma máquina — só o laço mudou):

| | `896917be` (antes) | `9e288cb8` (depois) |
|---|---|---|
| planejamento | 204,0s | 94,1s |
| desenvolvimento | 303,5s | 424,2s |
| escrever_testes | 492,6s | 363,3s |
| pytest | ✅ 98,5% | ✅ 100% |
| revisão | ❌ reprovado | ✅ **aprovado** |
| rodadas | 3, interrompida | **1, concluída** |
| total | **39,4 min** | **17,3 min** |

Com uma ressalva que importa mais que o número: **o roteamento seletivo não foi
exercitado**. Ele só entra depois de uma reprovação de revisão, e não houve
nenhuma. Quem produziu o corte foi o outro freio — o critério de bloqueante vs.
sugestão. O relatório da revisão aprovada abre com "## Apontamentos bloqueantes
— Nenhum." e segue com duas sugestões da mesma natureza das que, na execução
anterior, custaram duas rodadas inteiras.

Ou seja: nos dois casos o código estava bom desde a primeira rodada (pytest
verde nas duas). O que mudou foi o que o revisor fez com isso. A economia do
roteamento seletivo continua sendo projeção, não medição — só aparece numa
execução com reprovação legítima. **Registrar o que a medição não provou é
parte da medição**; o contrário é atribuir o ganho à mudança de que mais se
gosta.

---

## 28. O nó que entregou nada e disse que deu certo

**Sintoma:** duas execuções seguidas em que o nó de desenvolvimento concluiu
normalmente com o **workspace vazio**, e a squad seguiu adiante como se
houvesse entrega — 855s de QA, dois guards de critérios, um pytest e ainda
outra rodada de desenvolvimento, tudo em cima de zero arquivo.

Causas diferentes, sintoma idêntico:

| Execução | O que o executor fez | Exit code |
|---|---|---|
| `748d0fe4` | tentou escrever em `/tmp` (no Windows, fora do workspace); a jaula do próprio CLI auto-rejeitou e a run abortou | **0** |
| `9ae8cde0` | morreu em 22,5s com saldo insuficiente na conta do provedor | **0** |

**Causa raiz:** o nó confiava no exit code de um processo externo como prova
de trabalho feito. Exit code classifica **o processo**, não **o trabalho** — o
CLI terminou de forma ordenada em ambos os casos, e "terminou bem" não é
"produziu entrega". A varredura do disco já existia e já devolvia lista vazia;
ninguém perguntava a ela. É o item 15 outra vez, agora do lado do executor em
vez do guard.

Pior: a falha era *silenciosa e cara*. Sem código, o QA escreveu testes para
módulos inexistentes, o guard de critérios reprovou a suíte que não existia, o
pytest ficou vermelho por não coletar nada — e cada camada interpretou o vazio
como um problema da sua alçada, produzindo feedback plausível sobre a coisa
errada.

**Solução:** guard determinístico em `no_desenvolvimento`, depois da varredura:
sem nenhum arquivo fora de `tests/`, o grafo **para com erro explícito** em vez
de rotear de volta. Não há o que corrigir sem código, e a causa é sempre de
configuração (modelo sem tool calling, credencial, permissão) — repetir a
rodada só repete a falha (item 26). O checkpoint preserva tudo: `--thread`
retoma quando a configuração estiver certa. Em `executar_opencode`, as frases
com que o CLI aborta devolvendo 0 (`the user rejected permission...`,
`auto-rejecting`) passam a levantar erro nomeando a causa, em vez de deixá-la
para ser descoberta três nós adiante.

**Emenda, descoberta ao varrer os workspaces antigos:** a primeira versão deste
guard tinha o mesmo defeito uma camada abaixo. Ela contava **qualquer** caminho
fora de `tests/`, e o mesmo executor que não escreveu nada numa rodada deixou um
`app/__init__.py` de **0 bytes** na seguinte (thread `748d0fe4`, segunda
rodada). Estrutura sem conteúdo: o guard teria aprovado, e a squad seguiria
pagando QA em cima de um pacote vazio. Existir arquivo não é existir trabalho —
que é o mesmo critério que `_cobertura` já aplicava ao ignorar módulo sem
instruções, e que eu não apliquei ao escrever o guard. Corrigido: só conta
arquivo com conteúdo não vazio.

**Princípio:** **sucesso de processo não é sucesso de trabalho.** Onde um nó
delega a um executor externo, o veredito tem que vir do artefato — o disco —,
nunca do código de saída de quem deveria tê-lo produzido. É a mesma regra que
o projeto já aplica ao veredito dos testes (roteia por pytest, não por opinião)
aplicada uma etapa antes: se a camada seguinte só faz sentido com entrega, a
existência da entrega é pré-condição, não suposição.

---

## 29. O teto que cortava a metade que interessava

**Sintoma:** nenhum — e esse é o ponto. A saída do pytest era guardada no
estado por `saida[-8000:]`, corte cego pela cauda, sem aviso de que houve
corte. Descoberto ao avaliar a adoção de um compressor externo, não por
falha observada.

**Medição** (18 amostras de `saida_testes` nos checkpoints, e reexecução do
pytest nos workspaces reais para recuperar o bruto que o estado não guarda):

| | |
|---|---|
| amostras abaixo do teto | 16 de 18 (310 a 2.438 chars — suítes verdes) |
| amostras no teto de 8.000 | 2 |
| maior saída bruta real | **19.422 chars** |
| descartado pela cauda | **11.422 chars (59%)** |
| linhas de falha distintas | 21 |
| linhas de falha que chegavam ao dev | **19** |

**Causa raiz:** o teto foi dimensionado para suíte verde, que gasta 300–2.400
chars. Suíte vermelha e verbosa gasta uma ordem de grandeza a mais — e é
exatamente o caso em que o texto deixa de ser registro e vira **instrução de
conserto**. O corte pela cauda preserva o rodapé (`FAILED ...`) e joga fora a
seção de tracebacks, onde está a causa. É o item 21 outra vez: régua calibrada
na primeira rodada, que arrebenta quando o trabalho cresce.

**Solução:** teto para 20.000, cobrindo o maior caso observado com folga. Não
foi eliminado: contexto gigante satura o modelo e devolve resposta vazia
(item 5), então o limite continua sendo uma defesa — só que dimensionada pelo
que a execução realmente produz. O custo é ~3.200 tokens a mais, e só em
rodada vermelha; o dump de código que o revisor recebe em *toda* revisão já
custa mais que isso.

Considerada e **descartada**: comprimir a saída (por ferramenta externa ou
compressor próprio). Um compressor determinístico trivial reduz esses 19.422
chars a 6.215 preservando as 21 falhas — mas resolve por perda um problema que
uma constante resolve sem perda, e a inspeção do resultado mostrou o preço:
deduplicação global apagava a linha `E ...Error` de blocos de falha seguintes,
deixando o cabeçalho da falha sem o erro. Comprimir o artefato que instrui o
conserto só se justifica quando ele não couber — e ele cabe.

**Princípio:** todo corte de artefato precisa ser **dimensionado pela medição
do artefato**, não pelo palpite de quem escreveu a constante. E corte
silencioso é a pior variante: sem instrumentação, um `[-8000:]` nunca reclama —
ele entrega metade do sinal com a mesma cara de quem entregou tudo. Antes de
adotar máquina nova para caber no orçamento, confira se o orçamento é o certo.

---

## 30. A jaula que o executor nunca teve

**Sintoma:** nenhum — e é esse o ponto. O README descreve o nó de
desenvolvimento como confinado ao workspace, e nada em execução contradizia
isso. A auditoria só apareceu quando medimos o que o CLI enxerga de dentro de
um workspace da squad:

```
opencode debug config   → 19 servidores MCP declarados, 15 habilitados
opencode debug skill    → 623 skills carregadas do config global
opencode debug agent    → 639 entradas de permissão, com "*" → allow
```

Entre os 15: `windows-mcp` (controle do sistema operacional), `docker` (que
gerencia o próprio `squad-sandbox` que julga os testes), `github` com token de
push, `supabase`, `ngrok`, `grafana`. E ~37,7k tokens só de nome e descrição
das skills entrando no prompt **antes da spec**, em toda rodada.

**Causa raiz:** `executar_opencode` passa `--dir <workspace>`, e ficou a
suposição de que isso isolava a execução. Não isola: `--dir` troca o diretório
de trabalho, enquanto a **configuração** continua vindo do `opencode.json`
global do usuário, mesclada. A jaula de [`tools.py`](../src/squad/tools.py) é
real, mas só vale para `DEV_EXECUTOR=crews` — o caminho **padrão** nunca passou
por ela. Duas superfícies com o mesmo nome no README e garantias opostas.

Pior, o problema cresce sozinho: qualquer MCP que o usuário instale na máquina
para outro projeto passa a enxergar o workspace da squad na execução seguinte,
sem nenhuma alteração no código.

**Isto também reclassifica o item 28.** O aborto com exit 0 do `748d0fe4` foi
atribuído ao `/tmp` do Windows. A causa real é o modelo de permissão: a chave
`external_directory` tem default `ask`, e em headless não há quem responda —
o CLI auto-rejeita e mata a run. O `/tmp` foi o gatilho; qualquer caminho fora
do workspace produziria o mesmo. A correção do item 28 (falhar nomeando a
causa) continua certa, mas tratava o sintoma.

**Solução:** `_config_escopo` monta um config por execução e o entrega ao CLI
por `OPENCODE_CONFIG_CONTENT`, que o OpenCode mescla por chave — fecha o
escopo sem tocar na máquina do usuário:

- **MCP**: todos desligados por nome, menos os de `OPENCODE_MCP_PERMITIDOS`
  (padrão: nenhum). A lista desligada é a **união** dos servidores conhecidos
  com os declarados no config global, para que um servidor novo não entre
  calado — a correção não pode envelhecer em silêncio.
- **Permissão**: `external_directory` explícito — workspace liberado, resto
  negado — no lugar do `ask` que vira auto-rejeição.
- **Skills**: interruptor `OPENCODE_SKILLS`, **desligado por padrão**. Este
  default só foi virado depois de medir, porque mexer no que o executor lê é
  mudança de qualidade e não só de custo. A primeira tentativa comparou
  durações do grafo inteiro e não concluiu nada: o nó determinístico de
  pytest, que não podia ter sido afetado, variou 171% entre execuções — se o
  controle se move mais que o tratamento, a régua é curta demais.

  A régua certa apareceu no export de sessão do OpenCode: a **primeira**
  chamada de cada run tem `cache read: 0`, ou seja, é o preâmbulo cru. Ele é
  determinístico — deu exatamente o mesmo valor nas 3 execuções de cada braço,
  enquanto a duração do mesmo nó oscilava entre 204s e 411s.

  | 3 execuções por braço | skills ligadas | skills desligadas |
  |---|---|---|
  | preâmbulo | 104.798 | **10.543** (−90%) |
  | custo por rodada | $0,1658 | **$0,0367** (−78%) |
  | entrega | 5 arquivos | 5 arquivos, idênticos |

  As 623 skills eram 90% do que o executor lia antes de chegar na spec, e o
  grafo pode gastar 3 rodadas. A estimativa que eu tinha feito antes de medir
  (~37,7k tokens, contando nome e descrição) errou por mais de 2x para baixo.

**Princípio:** **flag de diretório não é fronteira de confiança.** Quando um nó
delega a uma ferramenta externa, a superfície dela é o que a *configuração da
máquina* define, não o que o argumento da chamada sugere — e o default de toda
ferramenta madura é herdar o ambiente do usuário, porque é isso que serve ao
uso interativo. Isolamento que não foi declarado explicitamente não existe; e
diferente das outras falhas deste documento, esta não produz sintoma nenhum
até produzir o pior possível. Só medição encontra: o que a squad enxerga tem
que ser **auditável por comando**, não deduzido do código.

---

## 31. O nó que entregou tudo e morreu ao contar

**Sintoma:** thread `20051ccc`. O nó de desenvolvimento rodou 244s, o OpenCode
escreveu a entrega inteira (`main.py`, `db.py`, `repository.py`, `schemas.py`,
`README.md`) — e o grafo caiu:

```
UnicodeEncodeError: 'charmap' codec can't encode character '→'
```

**Causa raiz:** `print(saida[-LIMITE_SAIDA:])`. A saída do OpenCode traz setas
(`→`), ícones e box-drawing. Quando o stdout é **redirecionado** — arquivo,
pipe, CI —, o Python no Windows abandona a codepage do console e usa a local
(cp1252), que não tem esses caracteres. O mesmo pedido rodando num terminal
interativo passa; redirecionado, quebra. Por isso não aparecia: todas as
execuções anteriores tinham sido interativas.

O detalhe que dói é *onde* quebrou. O trabalho estava **feito e em disco**. O
nó não falhou executando — falhou **relatando**. E o guard do item 28 não pega
este caso, porque ele checa entrega vazia e a entrega estava cheia.

Havia um segundo alcance escondido: as mensagens de `RuntimeError` embutem o
mesmo trecho de saída, e quem as imprime é a `main`. Uma falha do executor com
seta na saída viraria `UnicodeEncodeError` **ao reportar a falha original** —
o modo de falha mais caro que existe, porque destrói a evidência do defeito
que se estava tentando diagnosticar.

**Solução:** `_relatavel()` recodifica pelo `sys.stdout.encoding` com
`errors="replace"` antes de qualquer uso — o eco no log e as duas mensagens de
erro. Caractere que a saída não suporta vira `?`; o relato sobrevive.

**Princípio:** **relatar não pode ser mais frágil do que fazer.** Todo texto
que vem de processo externo é bytes arbitrários, não string amigável, e o
caminho de relato é justamente o que roda *depois* do trabalho caro e *durante*
o diagnóstico de falhas. Uma camada de observabilidade que derruba o que
observa inverte o próprio propósito — e o ambiente que a expõe (stdout
redirecionado) é exatamente o de CI e automação, onde ninguém está olhando.

---

## 32. A rodada de correção que não corrigiu nada

**Sintoma:** thread `ac0c7d4e` (agendador de tarefas). O revisor reprovou por
um defeito real — condição de corrida entre `cancel_task` e a submissão em
`_check_due_tasks`, com `_run_task` sem revalidar o status. A rodada de
correção rodou 109,5s, o pytest seguinte voltou verde, e a segunda revisão
repetiu o apontamento **palavra por palavra**.

Palavra por palavra porque o código era o mesmo:

```
rodada de correção:    16:41:34 → 16:43:23
arquivo mais recente:  tests/test_main.py  16:38:06   ← anterior à rodada
```

Nenhum arquivo da entrega tem mtime dentro da janela. A cobertura seguinte
bateu idêntica ao dígito (99,5% / 98,4%). O executor não escreveu nada, e o
grafo registrou o nó como concluído.

**Causa raiz:** o guard de entrega vazia (item 28) faz a pergunta certa para a
rodada **inicial** — "existe arquivo?" — e a pergunta errada para uma rodada de
correção, onde o workspace já está cheio da rodada anterior. Um executor que
ignora o feedback por completo passa pelos dois critérios: a entrega existe e
tem conteúdo. O que ninguém perguntava era se ela havia **mudado**.

O custo foi o orçamento inteiro de revisões gasto sem que nenhuma tentativa de
conserto tivesse existido: `MAX_REVISOES` estourou com o defeito intacto, e o
gate humano recebeu trabalho reprovado como se duas correções tivessem sido
tentadas e falhado. A diferença importa — "tentou e não conseguiu" e "não
tentou" pedem decisões opostas de quem revisa.

**Solução:** `_impressao_entrega` tira um hash SHA-256 por arquivo da entrega
antes e depois de toda rodada de correção, e `_conferir_correcao` falha alto
quando os dois conjuntos são idênticos. Compara **conteúdo, não mtime**:
executor que reescreve o mesmo arquivo byte a byte não corrigiu nada. Ignora
`tests/`, que é do QA e que o executor é proibido de tocar — mudança lá não
prova que a correção pedida foi feita. Na rodada inicial o guard não roda: lá
não há "antes", e o item 28 já é o critério certo.

**Princípio:** **cada guard responde à pergunta da sua rodada.** A mesma camada
que prova trabalho numa etapa pode ser vazia na seguinte, porque o que conta
como evidência mudou — na primeira rodada a evidência é o artefato existir, na
segunda é ele ter mudado. Guard herdado de outra fase sem revisar a pergunta dá
a sensação de cobertura sem a cobertura: ele continua passando, e é justamente
por continuar passando que ninguém percebe que parou de medir.

---

## 33. O teto que não era teto, e o revisor que julgava por amostra

**Sintoma:** nenhum, de novo. O revisor aprovava e reprovava normalmente, os
testes rodavam, o grafo fechava. A auditoria só apareceu ao medir o que cada nó
manda para o LLM, nas 14 entregas já em disco:

```
LIMITE_DUMP_CODIGO = 15.000
dump real em 126d542c → 43.918 chars   (193% acima do teto declarado)
dump truncado em      → 10 de 14 entregas (71%)
arquivo onde o corte caiu → tests/*, em 8 dos 11 casos
```

E dentro do dump, lido como texto e enviado ao modelo: `tarefas.db` (12.303
chars) e `tasks.db` (16.397) — SQLite binário, 56% do dump de uma das entregas.

**Causa raiz:** três defeitos empilhados na mesma função, `_dump_codigo`.

O primeiro é de ordem: o laço acrescentava o bloco e **depois** conferia o
total. Um arquivo grande passava inteiro, então o teto não limitava nada — só
avisava, tarde, que já tinha sido ultrapassado.

O segundo é de fila: `sorted()` põe `tests/` por último, então o corte sempre
caía na suíte. O revisor recebia o código e perdia os testes, sem que o texto
dissesse quais arquivos faltavam. Ele julgava por amostra sem saber que era
amostra — exatamente a falha que `_amostra_testes` já descrevia na própria
docstring e já tinha corrigido, três funções abaixo, para o guard de critérios.
A correção existia no arquivo e não tinha sido aplicada ao vizinho.

O terceiro é de filtro: `_arquivos_do_workspace` só exclui `.pyc`. Qualquer
binário que a entrega gere — e uma API com persistência gera — ia como texto
para o contexto, gastando orçamento e devolvendo ruído.

**Solução:** `_blocos_com_orcamento`, uma função só, usada pelo dump do revisor
e pela amostra do guard. O orçamento é conferido **antes** de acrescentar, e
cabeçalho de bloco, separador e marcador de truncagem saem dele — reservar só o
conteúdo deixaria o total estourar em silêncio de novo, que era o defeito
original. A repartição é água em copos: os arquivos entram em ordem crescente
de tamanho, cada um leva no máximo a sua cota, e o que sobra dos pequenos é
redividido entre os grandes. O manifesto lista **todos** os nomes, inclusive os
que entraram truncados, para que ninguém confunda ausência com omissão. Fonte
antes de teste na fila, porque o revisor já recebe `saida_testes` em separado.
E os binários são cortados **na fronteira do LLM**, não na varredura do
workspace: eles são entrega e continuam indo ao deploy.

Medido sobre as mesmas 14 entregas:

| | antes | depois |
|---|---:|---:|
| agregado dos dumps | 279.061 chars | **179.385** (−36%) |
| pior caso (`126d542c`) | 43.918 | **14.999** (−66%) |
| entregas acima do teto | 11 | **0** |
| arquivos invisíveis ao revisor | até 8 por rodada | **0** |

**A decisão de não adotar ferramenta externa vem daqui.** A alternativa
avaliada era plugar um compressor de contexto (Headroom) como proxy — o
`base_url` de [`llm.py`](../src/squad/llm.py) já é uma variável de ambiente, e
custaria zero linha. A medição matou a ideia: o proxy alcançaria as superfícies
do CrewAI, que são **29%** dos tokens da execução (o executor OpenCode é os
outros 71%), e a taxa que a ferramenta declara para agentes de código é 15–20%
— **5% do custo total**, meio centavo por execução, em troca de um daemon cuja
queda derruba toda chamada de LLM e de um `output shaper` que encurta a
conclusão do modelo justamente onde `_veredito_aprovado` procura o veredito
(item 7). Consertar os três defeitos acima cortou 36% do mesmo blob, de graça.
Pelo mesmo motivo ficaram de fora um indexador de grafo de código (ganho
declarado para 500+ arquivos; as entregas têm de 1 a 22) e um serviço de E2E na
nuvem (exigiria expor a entrega em rede, invertendo o `--network none` do
item 21).

**Princípio:** **limite que não é conferido antes de gastar não é limite, é
comentário.** E quando o corte precisa acontecer, *o que* se corta é decisão de
projeto tanto quanto *quanto*: truncar pelo total delega a escolha à ordem
alfabética, que não sabe nada sobre o que importa, e o custo aparece como
julgamento de pior qualidade — não como erro. Antes de comprar redução de
contexto de fora, vale medir o que o próprio código já desperdiça: aqui a
gordura era maior que o ganho da ferramenta, e removê-la tirou risco em vez de
adicionar.

---

## Resumo da arquitetura de defesa em camadas

```
Camada 0 — Guard de entrega     (grátis, determinístico) → pega executor que não produziu nada
            + guard de correção                          → e o que produziu o mesmo de antes
Camada 1 — Guard de aderência   (barato, automático)     → pega tema errado
Camada 2 — Guard de critérios   (barato, automático)     → pega suíte que não testa a spec
Camada 3 — pytest na jaula      (barato, determinístico) → pega defeito que executa errado
Camada 4 — Piso de cobertura    (barato, determinístico) → pega teste que não exercita a entrega
            agregado + por módulo                        → e o módulo que a média esconde
Camada 5 — Revisor LLM          (caro, automático)       → pega o que passa nos testes
            só bloqueante reprova, com memória do veredito anterior
Camada 6 — Gate humano          (manual)                 → segura ação irreversível
Transversal — Checkpoints SQLite + circuit breakers + parsers tolerantes
            + confinamento de ferramentas ao workspace + sandbox sem rede
            + métricas por execução + retry de falha transitória
```

Resiliência em três escalas de tempo, cada uma para uma duração de falha —
e cada uma barata o suficiente para o que protege (itens 22 e 23):

```
Chamada travada     → timeout por requisição   (duração infinita — item 25)
Soluço da chamada   → retry com backoff        (segundos, só em chamada barata)
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

O item 27 fecha o outro lado da mesma conta: **camada também tem custo, e o
custo precisa ser proporcional ao valor do sinal**. O revisor LLM é a camada
mais cara e a única subjetiva, e mesmo assim tinha o poder de mandar reescrever
a suíte inteira e de consumir sozinho o orçamento de rodadas reservado ao
pytest. Não adianta a régua estar certa se acioná-la custa mais do que o
defeito que ela pega.
