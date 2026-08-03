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
    participant V as Guard de Aderência (chamada única LLM)
    participant D as Executor de Desenvolvimento (OpenCode CLI ou crews CrewAI)
    participant T as Crew Testes (QA escreve pytest)
    participant GT as Guard de Critérios (chamada única LLM)
    participant WS as Workspace (workspace/thread_id)
    participant PT as pytest em sandbox Docker (sem rede, timeout 120s)
    participant R as Crew Revisão (revisor LLM)
    participant DP as Deploy

    U->>G: python main.py "pedido"
    G->>WS: triagem: valida entrada e cria o workspace
    G->>CK: salva checkpoint (thread_id)

    loop máx. 2 replanejamentos
        G->>P: kickoff(pedido)
        P-->>G: spec técnica
        G->>CK: salva checkpoint
        G->>V: spec trata do pedido? (SIM/NAO)
        alt spec coerente
            V-->>G: SIM → segue
        else spec incoerente
            V-->>G: NAO → replaneja
            Note over G: 3ª falha: RuntimeError (interrompe com erro explícito)
        end
    end

    loop máx. 3 tentativas de correção
        G->>D: executa(spec, feedback_qa) conforme DEV_EXECUTOR
        D->>WS: escreve arquivos .py REAIS (não escreve em tests/)
        G->>WS: varre o disco: manifesto + dump do código
        G->>CK: salva checkpoint
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
            G->>R: kickoff(codigo, spec, saida_testes)
            R-->>G: revisão + veredito APROVADO/REPROVADO
            alt APROVADO (verdes E aprovado)
                Note over G: sai do laço → gate humano
            else REPROVADO
                Note over G,D: feedback_qa = apontamentos da revisão<br/>volta ao desenvolvimento (3ª tentativa: circuit breaker → gate humano)
            end
        end
    end

    G->>U: interrupt( ) Autorizar deploy? (sim/nao)
    Note over G,CK: execução pausada e persistida —<br/>sobrevive a queda do processo
    U->>G: sim
    G->>DP: git commit + push entrega/thread_id (nó determinístico)
    DP-->>G: deploy_ok = true, deploy_ref
    G->>CK: checkpoint final
    G-->>U: Deploy ok: True + entrega publicada em deploy_ref
```

## Pontos estruturais

1. **Triagem toca o disco**: `workspace/<thread_id>/` é criado antes de
   qualquer LLM rodar; a entrega vive como arquivos, não como string no
   estado do grafo.
2. **Executor de desenvolvimento intercambiável**: `DEV_EXECUTOR` escolhe
   entre o OpenCode CLI (padrão) e as crews CrewAI. A governança é a mesma
   nos dois casos — o grafo lê o disco, não o texto do executor.
3. **Qualidade em quatro participantes**: a crew de testes escreve pytest
   real, o guard de critérios confere se a suíte testa o que a spec exige, o
   pytest executa em nó determinístico (sem LLM) e o revisor LLM cobre o que
   execução não pega — legibilidade, segurança, aderência à spec.
4. **Dois níveis de decisão no laço**: primeiro o exit code (vermelho volta
   ao desenvolvimento com o stack trace real, sem gastar token de revisão);
   só com verde o revisor roda. Aprovação automática = testes verdes **E**
   revisão aprovada.
5. **Separação entre implementar e validar**: quem escreve o código não
   escreve os testes — o executor é proibido de tocar em `tests/`.
6. **Quem vigia os testes**: verdes não provam correção se a suíte for fraca.
   Duas checagens complementares — o **guard de critérios** (semântico, antes
   de executar) e a **cobertura** (determinística, depois de executar) —
   devolvem ao QA num laço curto, sem pagar outra rodada de desenvolvimento.
7. **Jaula de execução**: código gerado por LLM roda em container efêmero,
   com a entrega montada read-only, sem rede e com limites de recursos —
   `.squad/out` é a única superfície de escrita (relatório de cobertura).
8. **O grafo lê o disco**: manifesto de arquivos e dump de código vêm de
   varredura determinística do workspace, injetados nas tarefas que decidem
   roteamento (RESILIENCIA.md, item 10).
9. **Circuit breakers**: 2 replanejamentos (depois erro explícito), 3
   rodadas de correção (depois o gate humano decide) e 2 reescritas da
   suíte (depois segue com o sinal fraco registrado no estado).
10. **Métricas por execução**: cada nó registra duração e veredito em
   `metrics/<thread_id>.json`, com resumo impresso ao final — sem isso não
   há como saber se uma mudança melhorou o resultado.
