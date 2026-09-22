"""Uma validação parcial termina com veredito, e o guard converge.

`eed26930`, `dd2d9d0b` e `eb149520` terminaram como "parcial": a primeira
estava verde e aparecia cinza como as outras, que falharam no guard de
critérios. E o guard, a cada reavaliação, achava lacunas novas.
"""
import inspect

from src.squad.adaptadores.metricas_json import veredito_da_validacao
from src.squad.dominio import spec
from src.squad.graph import workflow


def _juiz(evento, **campos):
    return {"evento": evento, "duracao_s": 1.0, **campos}


def test_suite_verde_com_cobertura_e_validada_de_primeira():
    """A `eed26930`: só a suíte, verde."""
    v = veredito_da_validacao([
        {"evento": "inicio_execucao"},
        _juiz("executar_testes", testes_ok=True, cobertura_ok=True),
    ])
    assert v == {"ok": True, "de_primeira": True, "reprovas_no_caminho": [], "motivos": []}


def test_guard_reprovado_e_teto_reprova_a_validacao():
    """A `eb149520`: suíte verde, mas o guard reprovou até o teto."""
    v = veredito_da_validacao([
        _juiz("escrever_testes"),
        _juiz("validacao_testes", testes_aderentes=False),
        _juiz("escrever_testes"),
        _juiz("validacao_testes", testes_aderentes=False),
        {"evento": "teto_atingido", "laco": "escrever_testes"},
        _juiz("executar_testes", testes_ok=True, cobertura_ok=True),
    ])
    assert not v["ok"]
    assert "teto de escrever_testes atingido" in v["motivos"]
    assert "validacao_testes terminou reprovado (testes_aderentes)" in v["motivos"]


def test_reprova_corrigida_valida_mas_nao_de_primeira():
    v = veredito_da_validacao([
        _juiz("validacao_testes", testes_aderentes=False),
        _juiz("validacao_testes", testes_aderentes=True),
        _juiz("executar_testes", testes_ok=True, cobertura_ok=True),
    ])
    assert v["ok"] and not v["de_primeira"]
    assert v["reprovas_no_caminho"] == ["validacao_testes"]


def test_cobertura_abaixo_do_piso_reprova_mesmo_verde():
    v = veredito_da_validacao([_juiz("executar_testes", testes_ok=True, cobertura_ok=False)])
    assert not v["ok"] and "executar_testes terminou reprovado (cobertura_ok)" in v["motivos"]


def test_no_que_quebrou_reprova():
    v = veredito_da_validacao([_juiz("escrever_testes", erro="RuntimeError: caiu")])
    assert not v["ok"] and any("quebrou" in m for m in v["motivos"])


def test_sem_juiz_nao_ha_o_que_validar():
    v = veredito_da_validacao([{"evento": "inicio_execucao"}])
    assert not v["ok"] and v["motivos"] == ["nenhum juiz rodou"]


def test_painel_mostra_o_veredito_das_parciais_antigas(tmp_path):
    import json
    from src.squad import painel
    eventos = [
        {"evento": "inicio_execucao", "quando": "2026-09-21T10:00:00+00:00", "pedido": "p"},
        {"evento": "executar_testes", "quando": "2026-09-21T10:01:00+00:00",
         "inicio": "2026-09-21T10:00:30+00:00", "duracao_s": 30.0, "testes_ok": True},
        {"evento": "fim_execucao", "quando": "2026-09-21T10:02:00+00:00", "desfecho": "parcial"},
    ]
    (tmp_path / "t.json").write_text(json.dumps(eventos), encoding="utf-8")
    assert painel.detalhar(tmp_path, "t")["desfecho"] == "validado"


# ---------- o guard converge ----------

def test_primeira_avaliacao_nao_tem_memoria():
    assert spec.reavaliacao_do_guard("") == ""


def test_reavaliacao_confere_o_que_foi_cobrado():
    """`eb149520`: a primeira reprova pediu "tabelas" e "data de fechamento";
    depois de o QA cobrir, a segunda pediu "funil vazio"."""
    trecho = spec.reavaliacao_do_guard("- Não verifica os indicadores em tabelas.")
    assert "REAVALIAÇÃO" in trecho and "indicadores em tabelas" in trecho
    assert "Não procure" in trecho and "lacunas novas" in trecho


def test_guard_recebe_as_lacunas_e_as_guarda():
    fonte = inspect.getsource(workflow.no_validacao_testes)
    assert "reavaliacao_do_guard(state.get('lacunas_criterios', ''))" in fonte
    assert '"lacunas_criterios": justificativa(veredito)' in fonte
    assert '"lacunas_criterios": ""' in fonte  # aprovado: memória limpa


def test_rodada_de_desenvolvimento_zera_a_memoria_do_guard():
    """Código novo, suíte nova: o guard começa do zero, como o laço de testes."""
    assert '"lacunas_criterios": ""' in inspect.getsource(workflow.no_desenvolvimento)
