# Arquitetura — Diagrama de Sequência

Fluxo completo de uma execução após as Fases 1–3 do
[roadmap](DESENVOLVIMENTO-REAL.md): o disco (workspace) vira um ator, a
qualidade se divide em três participantes (crew de testes, pytest e crew de
revisão) e o laço de correções é roteado pelo **exit code do pytest** —
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
    participant D as Crew Desenvolvimento (backend → integração → tech lead)
    participant T as Crew Testes (QA escreve pytest)
    participant WS as Workspace (workspace/thread_id)
    participant PT as pytest (subprocess, timeout 120s)
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
        G->>D: kickoff(spec, feedback_qa)
        D->>WS: escreve arquivos .py REAIS (ferramentas confinadas ao workspace)
        G->>WS: varre o disco: manifesto + dump do código
        G->>CK: salva checkpoint
        G->>T: kickoff(spec, arquivos)
        T->>WS: escreve testes reais em tests/
        G->>PT: executa pytest no workspace (nó sem LLM)
        PT-->>G: exit code + saída real
        G->>CK: salva checkpoint
        alt exit ≠ 0 (falha, nenhum teste coletado ou timeout)
            Note over G,D: feedback_qa = stack trace real do pytest<br/>volta ao desenvolvimento (3ª tentativa: circuit breaker → gate humano)
        else exit 0 (testes verdes)
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
    G->>DP: executa deploy (nó determinístico — real na Fase 4)
    DP-->>G: deploy_ok = true
    G->>CK: checkpoint final
    G-->>U: Deploy ok: True + entrega em workspace/thread_id/
```

## Pontos estruturais

1. **Triagem toca o disco**: `workspace/<thread_id>/` é criado antes de
   qualquer LLM rodar; a entrega vive como arquivos, não como string no
   estado do grafo.
2. **Qualidade em três participantes**: a crew de testes escreve pytest real,
   o pytest executa em nó determinístico (sem LLM) e o revisor LLM cobre o
   que execução não pega — legibilidade, segurança, aderência à spec.
3. **Dois níveis de decisão no laço**: primeiro o exit code (vermelho volta
   ao desenvolvimento com o stack trace real, sem gastar token de revisão);
   só com verde o revisor roda. Aprovação automática = testes verdes **E**
   revisão aprovada.
4. **O grafo lê o disco**: manifesto de arquivos e dump de código vêm de
   varredura determinística do workspace, injetados nas tarefas que decidem
   roteamento (RESILIENCIA.md, item 10).
5. **Circuit breakers**: 2 replanejamentos (depois erro explícito) e 3
   rodadas de correção (depois o gate humano decide).
