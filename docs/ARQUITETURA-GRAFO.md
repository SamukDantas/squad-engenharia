# Arquitetura do Grafo — Ramos, Gates e Tetos

Mapa em Mermaid do grafo de execução do **maestro** (modo paralelo) e do
**ramo de serviço** (subgrafo), com os gates (decisões condicionais) e os
tetos (`Teto`/circuit breaker) que devolvem o fluxo a um nó anterior.

Fonte da verdade: `src/squad/graph/workflow.py` e `src/squad/graph/maestro.py`;
os destinos de cada gate e os tetos exatos em `src/squad/dominio/rotas.py`.

```mermaid
---
config:
  theme: redux-dark-color
---
flowchart TD
    subgraph MAESTRO["Maestro — modo paralelo"]
        direction TB
        M_START(("START"))
        decomp["decomposicao"]
        contratos["contratos"]
        integracao["integracao"]
        gate[("gate")]
        deploy_servico["deploy_servico"]
        M_END(("END"))

        M_START --> decomp
        decomp -- "rota_pos_decomposicao" --> contratos
        contratos -- "fan_out_servicos (N)" --> integracao
        integracao --> gate
        gate -- "fan_out_deploy" --> deploy_servico
        gate -- "fan_out_deploy" --> M_END
        deploy_servico --> M_END
    end

    subgraph RAMO["Ramo — subgrafo de serviço (1 por serviço)"]
        direction TB
        R_START(("START"))
        triagem["triagem"]
        planejamento["planejamento"]
        validacao_spec["validacao_spec"]
        desenvolvimento["desenvolvimento"]
        escrever_testes["escrever_testes"]
        validacao_testes["validacao_testes"]
        executar_testes["executar_testes"]
        config_ambientes["config_ambientes"]
        aprovacao_humana["aprovacao_humana"]
        R_END(("END"))

        R_START --> triagem
        triagem --> planejamento
        planejamento --> validacao_spec

        validacao_spec -- "spec_coerente" --> desenvolvimento
        validacao_spec -- "teto plano (2) ou tentativas" --> aprovacao_humana

        desenvolvimento -- "rota_pos_desenvolvimento" --> escrever_testes
        desenvolvimento -- "origem preserva suite" --> executar_testes

        escrever_testes --> validacao_testes

        validacao_testes -- "testes_aderentes" --> executar_testes
        validacao_testes -- "teto testes (2)" --> escrever_testes

        executar_testes -- "testes_ok" --> config_ambientes
        executar_testes -- "teto correcao (3)" --> aprovacao_humana
        executar_testes -- "falha" --> desenvolvimento
        executar_testes -- "teto testes (2)" --> escrever_testes

        config_ambientes -- "ambientes_ok" --> aprovacao_humana
        config_ambientes -- "teto ambientes (2)" --> desenvolvimento

        aprovacao_humana --> R_END
    end
```

## Legenda dos limites

| Limite | Constante | Valor | Efeito ao estourar |
| --- | --- | --- | --- |
| Plano/spec | `MAX_REPLANEJAMENTOS` | 2 | `validacao_spec` → `aprovacao_humana` (teto `planejamento`) |
| Testes escritos | `MAX_TESTES` | 2 | `validacao_testes`/`executar_testes` → `escrever_testes` (teto `escrever_testes`) |
| Correção de código | `MAX_TENTATIVAS` | 3 | `executar_testes` → `aprovacao_humana` (teto `correcao`) |
| Ambientes | `MAX_AMBIENTES` | 2 | `config_ambientes` → volta ao desenvolvimento |
| Visual | `MAX_VISUAL` | 2 | `visual` → segue para a `revisao` com os achados no estado |
| Revisão, pentest | `MAX_REVISOES`, `MAX_PENTEST` | 2 cada | cada gate → `aprovacao_humana` com relatório |
| Deploy (maestro) | — | — | `gate` global (`no_gate`, "PLACAR DA EXECUÇÃO") bloqueia o `fan_out_deploy` |

Nota: depois de `config_ambientes`, a ordem é `visual` → `revisao` →
`pentest` → `aprovacao_humana`. A verificação visual vem antes da revisão
desde a thread `af83302f`: é o juiz mais barato (determinístico, sem token), e
por último a reprova dela chegava com o orçamento de correção esgotado.
