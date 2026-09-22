# Modelos do Codex — Comparativo, Benchmarks e Cotas

Referência para escolher o modelo no seletor do Codex (plano **Plus**):
especificações, preços, benchmarks, velocidade, limites de uso e ciclo de
vida. Levantamento de **set/2026**, a partir do seletor com `Padrão`,
GPT-6 Astra, GPT-5.6 Sol, GPT-5.6 Terra, GPT-5.6 Luna e GPT-5.5.

## Procedência dos dados — leia antes de decidir

Os números abaixo **não foram lidos das páginas primárias**. O ambiente em que
este levantamento rodou tinha egress bloqueado para `openai.com`,
`artificialanalysis.ai`, `llm-stats.com`, `openrouter.ai` e demais domínios —
só a busca funcionou. Portanto a fonte efetiva são **resumos de resultados de
busca sobre fontes secundárias e agregadoras**, não documentação oficial
verificada.

Consequência prática para a squad: trate esta página como **mapa de decisão,
não como fonte da verdade**. Antes de fixar um modelo em `config.toml`, em
agente customizado ou em qualquer decisão com custo, confirme preço e cota na
página oficial. Onde não houve dado confiável, está escrito
`não encontrado` — esses campos não foram preenchidos por estimativa.

Links de todas as fontes no fim da página.

## O seletor

| Item no menu | Papel | Geração | Status em set/2026 |
|---|---|---|---|
| **Padrão** | Não é modelo — ponteiro para o conjunto recomendado pela OpenAI, que muda sozinho a cada lançamento | — | Aponta para Astra |
| **GPT-6 Astra** | Flagship | GPT-6 | Lançado 3/set/2026 |
| **GPT-5.6 Sol** | Topo da 5.6 | GPT-5.6 | GA 9/jul/2026 |
| **GPT-5.6 Terra** | Equilibrado | GPT-5.6 | GA 9/jul/2026 |
| **GPT-5.6 Luna** | Rápido e barato | GPT-5.6 | GA 9/jul/2026 |
| **GPT-5.5** | Geração anterior | GPT-5.5 | Lançado 23/abr/2026 — **sai do Codex em 14/out/2026** |

> **Prazo com ação necessária:** GPT-5.5 aposenta do ChatGPT, ChatGPT Work e
> Codex em **14/out/2026**, em todos os planos (consumer, Business,
> Enterprise, Edu). Continua disponível na API com chave própria. Se houver
> `gpt-5.5` fixado em config, agentes customizados ou tasks agendadas,
> migre antes dessa data.

## Especificações

| Especificação | GPT-6 Astra | 5.6 Sol | 5.6 Terra | 5.6 Luna | GPT-5.5 |
|---|---|---|---|---|---|
| Janela de contexto | 1.050.000 | 1.050.000 | 1.050.000 | 1.050.000 | 1M (API) |
| Saída máxima | 128.000 | 128.000 | 128.000 | 128.000 | 128.000 |
| Entrada | texto + imagem | texto + imagem | texto + imagem | texto + imagem | texto + imagem |
| Saída | texto | texto | texto | texto | texto |
| Knowledge cutoff | não encontrado | fev/2026 | fev/2026 | fev/2026 | não encontrado |
| Níveis de reasoning | none → max | none → max | none → max | none → max | — |
| Computer use | sim | sim (bug no Windows) | sim (bug) | sim (bug) | sim |

### O contexto real dentro do Codex

Apesar de 1,05M anunciado, uma sessão padrão do Codex reporta
**~258.400 tokens utilizáveis**: o Codex impõe teto de 272.000 tokens de
entrada e guarda ~5% de folga (272.000 × 0,95 ≈ 258.400). Vale para todos os
modelos da lista — planejar RAG ou empacotamento de contexto assumindo 1M
dentro do Codex produz truncamento silencioso.

**Conflito não resolvido:** uma fonte afirma que GPT-5.5 roda com 400K no
Codex (contra 1M na API); outra diz que todos os modelos atuais do Codex são
1,05M e trata 5.5 como geração anterior. Não foi possível confirmar qual está
certa — e a questão perde relevância com a aposentadoria de outubro.

## Preços de API (USD por 1M tokens)

| Modelo | Entrada | Cache hit | Cache write | Saída |
|---|---|---|---|---|
| **GPT-6 Astra** | $10,00 | $1,00 | $12,50 | $50,00 |
| **5.6 Sol** | $5,00 | não encontrado | não encontrado | $30,00 |
| **5.6 Terra** | $2,00 | não encontrado | não encontrado | $12,00 |
| **5.6 Luna** | $0,20 | não encontrado | não encontrado | $1,20 |
| **GPT-5.5** | $5,00 | não encontrado | não encontrado | $30,00 |
| GPT-5.5 Pro | $30,00 | — | — | $180,00 |

Terra e Luna tiveram corte em **30/jul/2026**: Terra caiu 20% (de $2,50/$15)
e Luna caiu 80% (de $1,00/$6,00).

Spread de entrada: Astra custa 2,5× Sol, 5× Terra e **50× Luna**. Na saída,
41× Luna.

### A armadilha do contexto longo

Passando de **272.000 tokens de entrada**, a tarifa maior incide sobre o
**pedido inteiro**, não apenas sobre o excedente:

| Modelo | Entrada | Saída |
|---|---|---|
| Astra | $10 → **$20** (2×) | $50 → **$75** (1,5×) |
| Sol | $5 → **$10** (2×) | $30 → **$45** (1,5×) |

Para Astra o cache hit também dobra ($1 → $2) e o cache write vai a $25.
Como o Codex já limita a entrada em 272K, dentro do Codex isso raramente é
atingido — na API é fácil, e o salto é de 2× sem aviso.

## Benchmarks

> **Armadilha de leitura:** os números de Terminal-Bench circulam em
> **versões incomparáveis** (2.0, 2.1, 4.0). A 4.0 é drasticamente mais
> difícil — Sol marca 37,3% nela e 88,8% na 2.1. Comparar 57,7%
> (Astra/TB 4.0) com 88,8% (Sol/TB 2.1) inverte a conclusão. As tabelas
> abaixo estão separadas por versão.

### Astra contra Sol — mesma bateria

| Benchmark | GPT-6 Astra | 5.6 Sol | Δ |
|---|---|---|---|
| **Terminal-Bench 4.0** (agente em terminal) | **57,7%** | 37,3% | +20,4 pts |
| **FrontierMath Tier 4 (v2)** | **97,6%** | 83,0% | +14,6 pts |
| **OSWorld 2.0** (computer use) | **72,6%** | 65,7% | +6,9 pts |
| **DeepSWE v1.1** (113 tarefas de coding agêntico) | **74,1%** | 72,7% | +1,4 pts |
| **GPQA Diamond** (ciência nível pós-graduação) | **96,0%** | 94,6% | +1,4 pts |
| **BrowseComp** (navegação) | **91,5%** | 90,4% | +1,1 pts |
| **AA Intelligence Index** (max effort) | **53** | 47 | +6 |
| **GDPval-AA v2** (tarefas de valor econômico) | pior | **melhor** | **−45 Elo** |
| Terminal-Bench Science | 64,6% | não encontrado | — |
| ARC-AGI-3 | 99,9% | não encontrado | — |

**A regressão importa.** Astra é ~45 pontos Elo **pior** que Sol em
GDPval-AA v2 — único benchmark em que Sol ganha, e justamente o que mede
trabalho profissional de valor econômico. Para knowledge work em vez de
coding agêntico, esse dado contraria a escolha óbvia.

**Eficiência de turnos:** Astra usou 24 turnos por tarefa no GDPval em max
effort contra 45 de Sol — quase metade. Em OSWorld, ~40 min por tarefa
contra 75 min de Sol (−47%).

### Sol, Terra e Luna

| Benchmark | Sol | Terra | Luna |
|---|---|---|---|
| **AA Coding Agent Index** | **80,0** | 77,4 | 74,6 |
| **SWE-Bench Pro** | **64,6%** | 63,4% | não encontrado |
| **Terminal-Bench 2.1** | **88,8%** | 87,4% | 84,7% |
| **GPQA Diamond** | 94,6% | >92% | 92,3% |
| **MRCR v2, 256K–512K** | **91,5%** | não encontrado | 41,3% |
| **MRCR v2, 512K–1M** | **73,8%** | 72,5% | 41,3% |

**O penhasco do Luna.** Em recuperação de contexto longo, Luna despenca para
41,3% — menos da metade de Sol, e igual nas duas faixas. Terra praticamente
empata com Sol em 512K–1M (72,5% contra 73,8%). Regra operacional: para
**trajetórias agênticas multi-turno longas, Terra é o piso viável; Luna não
serve**. Luna é para tarefas curtas, repetitivas e de volume.

O resto da diferença entre os três é pequeno: Terra fica 1,2 pt atrás de Sol
em SWE-Bench Pro e 1,4 pt em Terminal-Bench 2.1, gastando **menos tokens por
conclusão** e custando 40% do preço.

### GPT-5.5 — referência histórica

| Benchmark | GPT-5.5 |
|---|---|
| τ²-Bench | 93,9% |
| GPQA Diamond | 93,5% |
| GDPval | 84,9% |
| Terminal-Bench **2.0** | 82,7% |
| SWE-Bench Pro | 58,6% |
| Coding Index | 74,9% |

Sol supera 5.5 em SWE-Bench Pro por 6 pts (64,6% contra 58,6%). Mesmo Luna
(Coding Index 74,6) empata com 5.5 (74,9) por uma fração do preço — e 5.5
sai do Codex em outubro. **Não há motivo para escolher 5.5 hoje.**

## Velocidade e latência

Resultado contraintuitivo: **Astra gera tokens mais devagar que Sol.**

| Configuração | Tokens/s | Time to first token |
|---|---|---|
| Sol (medium) | **67,7** | 6,20 s |
| Astra (medium) | 62,1 | 6,25 s |
| Sol (max) | **75,3** | 120,80 s |
| Astra (max) | 70,8 | **256,13 s** |
| Astra (low) | não encontrado | **2,56 s** |
| Sol (high) | não encontrado | 9,84 s |

**Astra em `max` tem TTFT de 4min16s** — mais que o dobro de Sol em `max`.
Inviável para trabalho interativo; serve para batch.

### A configuração que resolve isso

A orientação da própria OpenAI é tratar **Astra em `low` como substituto de
Sol em `high`**. Nesse par, Astra responde em 2,56s contra 9,84s de Sol —
**3,8× mais rápido** — e ainda entrega mais inteligência. Quem vinha de
Sol/high satisfeito deve **descer** para `low` ou `medium` no Astra, não
subir.

Níveis: `low` → `medium` → `high` → `xhigh` → `max`. Comece em low/medium;
suba só depois de uma falha documentada e específica. `high` faz sentido com
incerteza genuína (debug difícil, arquitetura desconhecida, abordagens
concorrentes). `xhigh` para satisfação de restrições densa ou planejamento de
longo horizonte com taxa de falha medida em `high`. `max` só para prompts de
altíssimo valor onde a avaliação mostre ganho real sobre `xhigh`.

## Cotas no plano Plus — a tabela que mais afeta o dia a dia

| Modelo | Mensagens locais / janela de 5h | Relativo ao Astra |
|---|---|---|
| **GPT-6 Astra** | **5 – 45** | 1× |
| 5.6 Sol | 10 – 100 | ~2× |
| 5.6 Terra | 25 – 200 | ~5× |
| 5.6 Luna | 250 – 2.000 | **~50×** |

- **Astra e Sol dividem a mesma cota** de Work + Codex. Não há quota
  separada: gastar em um consome o outro.
- Aplicam-se **duas janelas** — 5 horas **e** semanal. É preciso saldo nas
  duas para continuar.
- A janela de 5h começa na primeira mensagem após o fim da anterior; não é
  horário fixo.
- Plus permite **comprar créditos** para uso adicional de Astra.
- Pro 5x ($100) ≈ 5× a cota; Pro 20x ($200) ≈ 20×.

**Relato de queima agressiva:** a issue
[openai/codex#42987](https://github.com/openai/codex/issues/42987) reporta
Astra em `medium` esgotando 100% da cota de 5h do Plus em **dois turnos
curtos**. Com piso de 5 mensagens, o limite inferior é severo. No Plus,
trate Astra como recurso escasso.

## Recursos exclusivos do Astra

| Recurso | O que faz | Ressalva |
|---|---|---|
| **Notas pesquisáveis / nova compactação** | Em vez de resumir repetidamente, salva notas que atravessam janelas de contexto; janelas anteriores ficam pesquisáveis, **incluindo tool calls**, não só mensagens | **Experimental, desligado por padrão**, exige task nova. Não garante que toda nota esteja atual nem que a sessão gaste menos tokens |
| **Pergunta assíncrona** | Levanta a dúvida e **continua trabalhando** no que não depende da resposta; bloqueia só em decisões consequentes | — |
| **Computer use no Windows** | Controla o desktop pela mesma instalação do ChatGPT Desktop | Sol, Terra e Luna reportam "nenhum app disponível" — issue [openai/codex#44174](https://github.com/openai/codex/issues/44174) |
| **Alinhamento** | Em simulação com **mais de 54.000 tarefas internas do Codex**, Astra recebeu cerca de **metade** dos flags de comportamento desalinhado de severidade alta em relação a Sol | — |

> **Classificação de segurança:** Astra é o **primeiro modelo que a OpenAI
> classificou como "Critical" para cibersegurança** — com ferramentas e
> acesso adequados, localiza vulnerabilidades inéditas e desenvolve exploits
> funcionais contra sistemas endurecidos. Isso implica salvaguardas e gating
> adicionais no deploy.

## Recomendação por situação

| Situação | Modelo | Por quê |
|---|---|---|
| Loop de desenvolvimento diário | **Terra** | 97% do Terminal-Bench de Sol, 5× mais mensagens no Plus, 40% do preço, contexto longo intacto |
| Bug difícil, arquitetura nova, refactor longo | **Astra em `low`/`medium`** | Bate Sol/high em qualidade e é 3,8× mais rápido no TTFT — mas queima cota |
| Gate de produção, revisão final | **Sol** | Melhor em GDPval; cota 2× a do Astra |
| Volume, tarefas curtas repetitivas, subagentes | **Luna** | ~2.000 msgs/5h, $0,20/M — **nunca** para sessões de contexto longo |
| Qualquer coisa hoje | ~~GPT-5.5~~ | Perde para Sol, empata com Luna e sai do Codex em 14/out |
| Não quer decidir | **Padrão** | Segue a recomendação corrente da OpenAI automaticamente |

**Estratégia de cota no Plus:** deixe `Padrão`/Terra como default de trabalho
e reserve Astra para os problemas em que Terra já falhou. Com piso de 5
mensagens por 5 horas, Astra como default queima a janela antes do almoço.

## Lacunas conhecidas

Dois pontos que **não foi possível verificar** e que devem ser confirmados na
fonte oficial antes de qualquer decisão com custo:

1. **Knowledge cutoff do Astra** — não encontrado em nenhuma fonte.
2. **Janela do GPT-5.5 no Codex** — conflito entre 400K e 1,05M. Irrelevante
   para quem migrar antes de outubro.

Além disso, todos os campos marcados `não encontrado` nas tabelas de preço
(cache hit e cache write de Sol, Terra, Luna e 5.5) seguem abertos.

## Fontes

Oficiais (não lidas diretamente — bloqueadas pelo proxy; chegaram via resumo
de busca):

- [GPT-6 Astra: A new generation of intelligence](https://openai.com/index/gpt-6-astra/)
- [GPT-6 Astra System Card — Deployment Safety Hub](https://deploymentsafety.openai.com/gpt-6-astra)
- [GPT-5.6: Frontier intelligence that scales with your ambition](https://openai.com/index/gpt-5-6/)
- [Advancing the price-performance frontier with GPT-5.6](https://openai.com/index/advancing-the-price-performance-frontier-with-gpt-5-6/)
- [Introducing GPT-5.5](https://openai.com/index/introducing-gpt-5-5/)
- [Managing usage with GPT-6 Astra in Work and Codex](https://help.openai.com/en/articles/20001516-managing-usage-with-gpt-6-astra-in-work-and-codex)
- [Codex Models — Recommended models](https://developers.openai.com/codex/models.md)
- [Deprecations | OpenAI API](https://developers.openai.com/api/docs/deprecations)

Benchmarks independentes:

- [Benchmarking GPT-6 Astra | Artificial Analysis](https://artificialanalysis.ai/articles/benchmarking-gpt-6-astra)
- [GPT-6 Astra (max) vs GPT-5.6 Sol (max)](https://artificialanalysis.ai/models/comparisons/gpt-6-astra-vs-gpt-5-6-sol)
- [GPT-6 Astra (medium) vs GPT-5.6 Sol (medium)](https://artificialanalysis.ai/models/comparisons/gpt-6-astra-medium-vs-gpt-5-6-sol-medium)
- [GPT-6 Astra (low) vs GPT-5.6 Sol (high)](https://artificialanalysis.ai/models/comparisons/gpt-6-astra-low-vs-gpt-5-6-sol-high)
- [GPT-5.6 benchmarks across Intelligence, Speed and Cost](https://artificialanalysis.ai/articles/gpt-5-6-has-landed)
- [GPT-5.6 Benchmark Review: Sol, Terra, Luna | LayerLens](https://layerlens.ai/blog/gpt-5-6-benchmark-review-sol-terra-luna)
- [GPT-6 Astra Benchmarks Explained | Vellum](https://www.vellum.ai/blog/gpt-6-astra-benchmarks-explained)
- [GPT-5.6 Sol vs Terra vs Luna | Vellum](https://www.vellum.ai/blog/gpt-5-6-sol-terra-luna-explained)

Preço, cota e operação no Codex:

- [GPT-6 Astra: Features, Benchmarks, and Pricing | DataCamp](https://www.datacamp.com/blog/gpt-6-astra)
- [Past 272,000 Tokens It Bills You Double](https://medium.com/codetodeploy/gpt-6-astra-has-a-1-05-million-token-context-window-past-272-000-tokens-it-bills-you-double-e46fb60d2ec1)
- [Configuring GPT-6 Astra in Codex CLI](https://codex.danielvaughan.com/2026/09/03/gpt-6-astra-codex-cli-configuration-context-notes-safety/)
- [GPT-6 Astra in Codex CLI: 1.05M Context, Critical Cyber Threshold](https://codex.danielvaughan.com/2026/09/04/gpt-6-astra-codex-cli-integration-guide-critical-cyber-threshold/)
- [Codex Context Window: 1M Setting & How to Check Usage](https://getunblocked.com/blog/codex-context-window/)
- [GPT-6 Astra Usage Limits Explained](https://christopheralarcon.com/blog/gpt-6-astra-usage-limits-explained)
- [GPT-6 Astra Codex Usage Limits: Quota, Plus vs Pro & Resets](https://www.codexusage.dev/limits/astra)
- [GPT-5.6 Luna Price Cut 80%](https://www.explainx.ai/blog/openai-gpt-5-6-luna-terra-price-cuts-july-2026)
- [GPT-5.5 Retires from Codex Oct 14](https://www.orcarouter.ai/blog/gpt-5-5-remains-available-openai-api-codex-api-key)
- [Moving From GPT-5.6 Sol to GPT-6 Astra: Set It to Medium](https://ilikekillnerds.com/2026/09/06/gpt-5-6-sol-to-gpt-6-astra-reasoning-effort/)
- [GPT-6 Astra gated behind a 'Critical' Cyber Threshold | MarkTechPost](https://www.marktechpost.com/2026/09/03/openai-releases-gpt-6-astra-a-1-05m-context-computer-use-model-gated-behind-a-critical-cyber-threshold/)

Issues do Codex:

- [Astra medium esgotou 100% da cota de 5h do Plus em dois turnos · #42987](https://github.com/openai/codex/issues/42987)
- [Computer Use funciona no Astra mas não em Sol, Terra e Luna · #44174](https://github.com/openai/codex/issues/44174)
