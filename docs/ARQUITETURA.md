# Arquitetura — Diagrama de Sequência

Fluxo completo de uma execução após as Fases 1–7 do
[roadmap](DESENVOLVIMENTO-REAL.md): o disco (workspace) vira um ator, a
qualidade se divide em quatro participantes (crew de testes, guard de
critérios, pytest e crew de revisão) e o laço de correções é roteado pelo
**exit code do pytest** e pela **cobertura**, executados numa jaula Docker —
vereditos vêm de execução, não de opinião.

```mermaid
---
config:
  theme: redux-dark-color
---
sequenceDiagram
    autonumber
    actor U as Usuário
    participant G as Grafo LangGraph
    participant CK as Checkpointer SQLite
    participant P as Crew Planejamento (analista → arquiteto)
    participant V as Guard de Aderência (chamada única LLM + nome do repositório)
    participant D as Executor de Desenvolvimento (OpenCode CLI, Codex CLI ou crews CrewAI)
    participant T as Crew Testes (QA escreve pytest)
    participant GT as Guard de Critérios (chamada única LLM)
    participant WS as Workspace (workspace/thread_id)
    participant PT as pytest em sandbox Docker (sem rede, timeout 120s)
    participant R as Crew Revisão (revisor LLM)
    participant PN as Pentest em sandbox isolado (rede --internal, sem egress)
    participant DP as Deploy
    participant LP as Provedor LLM (LLM_PROVEDOR: Codex CLI | gateway corporativo on-prem | OpenCode Zen)
    participant KC as Keycloak do gateway corporativo (token em ~/.config/opencode)

    U->>G: python main.py "pedido"
    G->>WS: triagem: valida entrada e cria o workspace
    G->>CK: salva checkpoint (thread_id)

    opt LLM_PROVEDOR=gateway
        G->>KC: token_valido(): relê o arquivo e, faltando menos de 5 min, renova sob o lock compartilhado com o plugin
        alt token válido
            KC-->>G: access_token (cache do processo)
        else sem token ou refresh recusado
            KC-->>G: RuntimeError: faça o login uma vez no opencode (falha antes de gastar o nó)
        end
    end

    loop máx. 2 replanejamentos
        G->>P: kickoff(pedido)
        P->>LP: chat/completions (Bearer renovado por request pelo interceptor)
        LP-->>P: resposta
        P-->>G: spec técnica
        G->>CK: salva checkpoint
        G->>V: spec trata do pedido? (SIM/NAO)
        alt spec coerente
            V-->>G: SIM → segue
            G->>V: nome curto do repositório (uma vez, gravado no estado)
            V-->>G: nome_repo, ex. conversor-temperatura (resposta inválida: regra determinística)
        else spec incoerente
            V-->>G: NAO → replaneja
            Note over G: 3ª falha: RuntimeError (interrompe com erro explícito)
        end
    end

    loop máx. 3 tentativas de correção
        G->>D: executa(spec, feedback_qa) conforme DEV_EXECUTOR
        D->>LP: opencode run -m gateway/gateway (plugin keycloak injeta o Bearer)
        D->>WS: escreve arquivos .py REAIS (não escreve em tests/)
        G->>WS: varre o disco: manifesto + dump do código
        G->>CK: salva checkpoint
        Note over V,R: guards, QA e revisão usam o mesmo squad_llm() → LP<br/>com Zen: chave OPENCODE_API_KEY + header x-opencode-session
        opt rodada nascida de revisão, pentest, visual ou teste vermelho (asserção ou build)
            Note over G,PT: roteamento seletivo: suíte preservada —<br/>vai direto ao pytest, sem reescrever testes nem repagar o guard
        end
        loop máx. 2 reescritas da suíte
            G->>T: kickoff(spec, arquivos, feedback_qa)
            T->>WS: escreve testes reais em tests/
            G->>GT: os testes cobrem os critérios de aceite? (SIM/NAO)
            alt NAO e ainda há tentativas
                GT-->>G: NAO → QA reescreve a suíte
            else SIM ou teto atingido
                GT-->>G: segue para execução (sinal fraco registrado)
            end
        end
        G->>PT: executa pytest na jaula (entrega read-only, nó sem LLM)
        PT-->>G: exit code + saída real + cobertura
        G->>CK: salva checkpoint
        alt exit ≠ 0 (falha, nenhum teste coletado ou timeout)
            Note over G,D: feedback_qa = stack trace real do pytest<br/>volta ao desenvolvimento (3ª tentativa: circuit breaker → gate humano)
        else verde, mas cobertura abaixo do mínimo
            Note over G,T: problema do teste, não do código<br/>QA reescreve a suíte sem nova rodada de desenvolvimento
        else verde e cobertura suficiente
            G->>R: kickoff(codigo, spec, saida_testes, revisao_anterior)
            R-->>G: bloqueantes + sugestões + veredito APROVADO/REPROVADO
            alt APROVADO (verdes E aprovado)
                Note over G,PN: segue para o pentest (se PENTEST_HABILITADO=1)
                G->>PN: sobe a entrega em sandbox isolado e ataca (nó sem LLM)
                PN-->>G: pentest_ok + vulnerabilidades por severidade
                alt sem vuln bloqueante
                    Note over G: sai do laço → gate humano
                else vuln >= piso de severidade
                    Note over G,D: feedback_qa = brief de correção das vulns<br/>volta ao desenvolvimento e daí direto ao pytest<br/>(2ª reprova: teto de pentest → gate humano com relatório)
                end
            else REPROVADO (só apontamento bloqueante reprova)
                Note over G,D: feedback_qa = apontamentos da revisão<br/>volta ao desenvolvimento e daí direto ao pytest<br/>(2ª reprova: teto de opinião → gate humano)
            end
        end
    end

    G->>U: interrupt( ) Destino DEPLOY_OWNER/nome_repo — Autorizar deploy? (sim/nao)
    Note over G,CK: execução pausada e persistida —<br/>sobrevive a queda do processo
    U->>G: sim
    G->>DP: git commit + push em DEPLOY_OWNER/nome_repo (repo próprio por projeto, nó determinístico)
    DP-->>G: deploy_ok = true, deploy_ref
    G->>CK: checkpoint final
    G-->>U: Deploy ok: True + entrega publicada em deploy_ref
```

## Pontos estruturais

1. **Triagem toca o disco**: `workspace/<thread_id>/` é criado antes de
   qualquer LLM rodar; a entrega vive como arquivos, não como string no
   estado do grafo.
2. **Executor de desenvolvimento intercambiável**: `DEV_EXECUTOR` escolhe
   entre o Codex CLI (padrão), o OpenCode CLI e as crews CrewAI. A governança
   é a mesma nos três casos — o grafo lê o disco, não o texto do executor. A
   tarefa é compartilhada (`adaptadores/tarefa_executor.py`) para a troca de
   executor medir o executor, e não a diferença entre dois prompts.
3. **Qualidade em quatro participantes**: a crew de testes escreve pytest
   real, o guard de critérios confere se a suíte testa o que a spec exige, o
   pytest executa em nó determinístico (sem LLM) e o revisor LLM cobre o que
   execução não pega — legibilidade, segurança, aderência à spec.
4. **Dois níveis de decisão no laço**: primeiro o exit code (vermelho volta
   ao desenvolvimento com o stack trace real, sem gastar token de revisão);
   só com verde o revisor roda. Aprovação automática = testes verdes **E**
   revisão aprovada **E** (quando `PENTEST_HABILITADO=1`) pentest sem vuln
   bloqueante — segurança também por execução, não só opinião do revisor.
5. **Separação entre implementar e validar**: quem escreve o código não
   escreve os testes — o executor é proibido de tocar em `tests/`.
6. **Quem vigia os testes**: verdes não provam correção se a suíte for fraca.
   O **guard de critérios** (semântico, antes de executar) e a **cobertura**
   (determinística, depois) devolvem ao QA num laço curto, sem pagar outra
   rodada de desenvolvimento. A cobertura tem dois pisos — agregado e **por
   módulo** —, porque a média esconde justamente o arquivo central do pedido.
7. **Roteamento seletivo por origem do feedback**: correção nascida de
   revisão, pentest, visual ou **teste vermelho** (asserção ou build) não
   reescreve a suíte — volta ao desenvolvimento e segue direto ao pytest. O
   código mudou, então os testes precisam *rodar* de novo, não ser *escritos*
   de novo; `escrever_testes` é o nó mais caro do grafo. Vermelho de import
   ou coleta volta ao QA, porque aí o teste costuma apontar para o que não
   existe. Quando o QA precisa agir com a suíte já no disco, ele recebe o
   **modo ajuste** — mexer só no que o retorno aponta — e o guard de
   critérios diz o que falta, em vez de mandar reescrever. O revisor recebe o
   próprio veredito anterior e só reprova por apontamento bloqueante, porque
   sinal caro e subjetivo não pode comandar o laço (RESILIENCIA.md, item 27).
8. **Jaula de execução**: código gerado por LLM roda em container efêmero,
   com a entrega montada read-only, sem rede e com limites de recursos —
   `.squad/out` é a única superfície de escrita (relatório de cobertura).
9. **O grafo lê o disco**: manifesto de arquivos e dump de código vêm de
   varredura determinística do workspace, injetados nas tarefas que decidem
   roteamento (RESILIENCIA.md, item 10).
10. **Circuit breakers**: 2 replanejamentos (depois erro explícito), 3
   rodadas de correção (depois o gate humano decide), 2 reescritas da
   suíte (depois segue com o sinal fraco registrado no estado) e 2 rodadas
   por reprovação de revisão (depois o gate humano decide).
11. **Métricas por execução**: cada nó registra duração e veredito em
   `metrics/<thread_id>.json` — incluindo a origem de cada rodada de
   desenvolvimento (inicial, testes ou revisão) —, com resumo impresso ao
   final. Sem isso não há como saber se uma mudança melhorou o resultado.
   Com o Codex, cada nó grava também os tokens gastos, e os marcos da
   execução gravam a cota da conta, lida pelo `codex app-server` sem chamar
   modelo — a diferença entre o primeiro e o último marco é quanto a execução
   custou do plano.
12. **Provedor de LLM intercambiável**: `LLM_PROVEDOR` escolhe entre o
   Codex CLI (`codex`, padrão), o gateway gateway corporativo on-premise (`gateway`) e o
   OpenCode Zen (`zen`), como `DEV_EXECUTOR` escolhe o executor — a
   governança do grafo não depende de quem responde. A configuração em uso é
   **Codex com `gpt-5.6-luna`** nos agentes e no executor: no mesmo pedido
   (calculadora de juros em Next.js), a média caiu de 70,8 min com o OpenCode
   e o gateway corporativo, nunca verde no gate, para 21,4 min, com as execuções mais
   recentes verdes sem intervenção — a medição e as ressalvas estão no item 38
   do [RESILIENCIA.md](RESILIENCIA.md). Com o gateway corporativo a autenticação é
   Keycloak: login interativo uma única vez no OpenCode, depois refresh
   automático sob o mesmo lock do plugin, e o `Authorization` é trocado **por
   request** (um nó longo não fica com token vencido). O token nunca entra no
   repo nem no `.env`. As três superfícies de modelo (`MODEL`,
   `MODEL_FERRAMENTAS`, `OPENCODE_RUN_MODEL`) seguem o provedor; o rollback
   entre provedores é uma linha.
