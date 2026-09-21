"""Quanto cada execução gasta da cota do Codex.

A conta que roda a squad é a mesma conta ChatGPT do Codex, com cota por
janela. Sem isto nas métricas, a pergunta "quantas execuções cabem no plano?"
só se respondia lendo arquivos de sessão do Codex à mão — e a squad roda com
`--ephemeral`, que nem grava esses arquivos.
"""
import json
import subprocess
import threading
from contextvars import copy_context
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.squad.adaptadores import codex_cli, consumo, metricas_json
from src.squad.adaptadores.metricas_json import gasto_da_cota, medir, resumo


def _turno(entrada, cache, saida, raciocinio=0):
    return json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": entrada, "cached_input_tokens": cache,
        "output_tokens": saida, "reasoning_output_tokens": raciocinio,
    }})


STREAM = "\n".join([
    '{"type":"thread.started","thread_id":"x"}',
    "texto para humano no meio do stream",
    _turno(13122, 9984, 5),
    _turno(100, 0, 20, 7),
])


# ---------- tokens ----------

def test_uso_soma_todos_os_turnos_e_ignora_o_resto():
    assert consumo.uso_do_stream(STREAM) == {
        "entrada": 13222, "entrada_cache": 9984, "saida": 25, "raciocinio": 7,
    }
    assert consumo.uso_do_stream("") == {}


@pytest.fixture
def metricas(tmp_path, monkeypatch):
    monkeypatch.setattr(metricas_json, "DIR_METRICAS", tmp_path)
    return lambda tid: json.loads((tmp_path / f"{tid}.json").read_text(encoding="utf-8"))


def test_no_grava_os_tokens_gastos_dentro_dele(metricas):
    with medir("t", "planejamento"):
        consumo.acumular({"entrada": 10, "saida": 2})
        consumo.acumular({"entrada": 5, "saida": 1})
    assert metricas("t")[0]["tokens"] == {"entrada": 15, "saida": 3, "chamadas": 2}


def test_no_sem_llm_nao_ganha_campo_vazio(metricas):
    with medir("t", "executar_testes"):
        pass
    assert "tokens" not in metricas("t")[0]


def test_no_que_quebrou_tambem_grava_o_que_gastou(metricas):
    with pytest.raises(RuntimeError):
        with medir("t", "desenvolvimento"):
            consumo.acumular({"entrada": 7})
            raise RuntimeError("caiu")
    assert metricas("t")[0]["tokens"]["entrada"] == 7


def test_medicao_aninhada_soma_nas_duas(metricas):
    with medir("t", "servico"):
        with medir("t", "planejamento"):
            consumo.acumular({"entrada": 4})
    interno, externo = metricas("t")
    assert interno["tokens"]["entrada"] == externo["tokens"]["entrada"] == 4


def test_gasto_de_um_no_paralelo_nao_vaza_para_o_outro(metricas):
    """No modo paralelo dois serviços rodam ao mesmo tempo; o contexto é
    copiado para a thread do nó, como o LangGraph faz."""
    pronto = threading.Barrier(2)

    def no(tid, gasto):
        with medir(tid, "desenvolvimento"):
            pronto.wait()
            consumo.acumular({"entrada": gasto})

    threads = [
        threading.Thread(target=copy_context().run, args=(no, tid, gasto))
        for tid, gasto in (("a", 100), ("b", 1))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert metricas("a")[0]["tokens"]["entrada"] == 100
    assert metricas("b")[0]["tokens"]["entrada"] == 1


def test_chamada_ao_codex_conta_mesmo_quando_falha(monkeypatch):
    monkeypatch.setattr(codex_cli.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=1, stdout=_turno(50, 0, 3), stderr="",
    ))
    with consumo.contar() as tokens:
        with pytest.raises(RuntimeError, match="exit 1"):
            codex_cli._rodar(["codex"], 10, "teste")
    assert tokens == {"entrada": 50, "entrada_cache": 0, "saida": 3,
                      "raciocinio": 0, "chamadas": 1}


# ---------- cota ----------

RESPOSTA = {"rateLimits": {
    "planType": "free",
    "primary": {"usedPercent": 61, "windowDurationMins": 43200, "resetsAt": 1791267907},
    "secondary": None,
}}


def test_cota_lida_do_app_server(monkeypatch):
    monkeypatch.setattr(codex_cli, "_cota_crua", lambda _t: RESPOSTA)
    assert codex_cli.cota() == {
        "plano": "free", "usado_pct": 61, "janela_h": 720.0,
        "zera_em": "2026-10-06T06:25:07+00:00",
    }


def test_cota_com_janela_secundaria(monkeypatch):
    resposta = json.loads(json.dumps(RESPOSTA))
    resposta["rateLimits"]["secondary"] = {
        "usedPercent": 12, "windowDurationMins": 300, "resetsAt": None,
    }
    monkeypatch.setattr(codex_cli, "_cota_crua", lambda _t: resposta)
    assert codex_cli.cota()["secundaria"] == {
        "usado_pct": 12, "janela_h": 5.0, "zera_em": None,
    }


@pytest.mark.parametrize("falha", [
    lambda _t: None,
    lambda _t: {"rateLimits": {"primary": None}},
    lambda _t: (_ for _ in ()).throw(FileNotFoundError("codex")),
    lambda _t: (_ for _ in ()).throw(subprocess.SubprocessError()),
])
def test_medir_a_cota_nunca_derruba_a_execucao(monkeypatch, falha):
    monkeypatch.setattr(codex_cli, "_cota_crua", falha)
    assert codex_cli.cota() is None


def _leitura(pct, zera="2026-10-06T06:25:07+00:00"):
    return {"plano": "free", "usado_pct": pct, "janela_h": 720.0, "zera_em": zera}


def test_gasto_e_do_primeiro_ao_ultimo_marco():
    eventos = [
        {"evento": "inicio_execucao", "cota_codex": _leitura(61)},
        {"evento": "planejamento"},
        {"evento": "fim_execucao", "cota_codex": _leitura(64)},
        {"evento": "retomada"},  # leitura que falhou: fica sem o campo
        {"evento": "fim_execucao", "cota_codex": _leitura(66)},
    ]
    assert gasto_da_cota(eventos) == {
        "plano": "free", "antes": 61, "depois": 66, "gasto_pct": 5,
        "zera_em": "2026-10-06T06:25:07+00:00",
    }


def test_janela_que_zerou_no_meio_nao_vira_gasto():
    eventos = [
        {"evento": "inicio_execucao", "cota_codex": _leitura(98)},
        {"evento": "fim_execucao", "cota_codex": _leitura(3, zera="2026-11-05T06:25:07+00:00")},
    ]
    assert gasto_da_cota(eventos) is None


def test_uma_leitura_so_nao_e_gasto():
    assert gasto_da_cota([{"evento": "inicio_execucao", "cota_codex": _leitura(61)}]) is None


def test_resumo_mostra_tokens_e_cota(metricas):
    metricas_json.registrar("t", "inicio_execucao", cota_codex=_leitura(61))
    with medir("t", "planejamento"):
        consumo.acumular({"entrada": 13000, "entrada_cache": 9000, "saida": 500})
    metricas_json.registrar("t", "fim_execucao", cota_codex=_leitura(65))
    texto = resumo("t")
    assert "13,000 de entrada (9,000 em cache), 500 de saída em 1 chamadas" in texto
    assert "61% → 65% (+4 pts, plano free, zera em 2026-10-06)" in texto


def test_painel_mostra_tokens_e_cota(tmp_path, monkeypatch):
    monkeypatch.setattr(metricas_json, "DIR_METRICAS", tmp_path)
    metricas_json.registrar("t", "inicio_execucao", pedido="p", cota_codex=_leitura(61))
    with medir("t", "planejamento"):
        consumo.acumular({"entrada": 1000, "entrada_cache": 800, "saida": 50})
    with medir("t", "desenvolvimento"):
        consumo.acumular({"entrada": 2000, "saida": 100})
    metricas_json.registrar("t", "fim_execucao", desfecho="deploy", cota_codex=_leitura(63))

    from src.squad.painel import detalhar, listar_execucoes
    d = detalhar(Path(tmp_path), "t")
    assert d["tokens"] == 3150  # entrada + saída; o cache é parte da entrada
    assert d["cota"]["gasto_pct"] == 2
    assert [r["tokens"] for r in d["rodadas"]] == [1050, 2100]
    assert d["marcos"][0]["detalhe"]["cota_codex"] == "61% (free, zera 2026-10-06)"
    linha = listar_execucoes(Path(tmp_path))[0]
    assert (linha["tokens"], linha["cota_pts"]) == (3150, 2)
