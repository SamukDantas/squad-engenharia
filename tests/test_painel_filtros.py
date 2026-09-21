"""Filtro e paginação da tela inicial do painel.

O histórico cresce com cada execução, e a tela inicial devolvia tudo de uma
vez, reagregando cada arquivo a cada abertura.
"""
import json
import os

import pytest

from src.squad import painel


def _gravar(diretorio, tid, dia, pedido="Calculadora", stack="nextjs", desfecho="deploy", teto=False):
    eventos = [
        {"evento": "inicio_execucao", "quando": f"{dia}T10:00:00+00:00",
         "pedido": pedido, "stack": stack},
        {"evento": "planejamento", "quando": f"{dia}T10:01:00+00:00",
         "inicio": f"{dia}T10:00:00+00:00", "duracao_s": 60.0},
    ]
    if teto:
        eventos.append({"evento": "teto_atingido", "quando": f"{dia}T10:02:00+00:00",
                        "laco": "correcao"})
    fim = {"evento": "fim_execucao", "quando": f"{dia}T10:03:00+00:00", "desfecho": desfecho}
    if desfecho == "deploy":
        eventos.append({"evento": "deploy", "quando": f"{dia}T10:02:30+00:00",
                        "inicio": f"{dia}T10:02:00+00:00", "duracao_s": 30.0,
                        "deploy_ok": True})
    eventos.append(fim)
    (diretorio / f"{tid}.json").write_text(json.dumps(eventos), encoding="utf-8")


@pytest.fixture
def historico(tmp_path):
    painel._LINHAS.clear()
    for i in range(25):
        _gravar(tmp_path, f"calc-{i:02d}", f"2026-09-{i + 1:02d}")
    _gravar(tmp_path, "conv-01", "2026-08-15", pedido="Conversor de temperaturas",
            stack="python", desfecho="erro", teto=True)
    return tmp_path


def test_primeira_pagina_e_a_mais_recente(historico):
    r = painel.consultar_execucoes(historico)
    assert r["total"] == r["total_geral"] == 26
    assert (r["pagina"], r["paginas"], r["por_pagina"]) == (1, 2, 20)
    assert len(r["itens"]) == 20
    assert r["itens"][0]["thread_id"] == "calc-24"


def test_ultima_pagina_traz_o_resto(historico):
    r = painel.consultar_execucoes(historico, {"pagina": "2"})
    assert len(r["itens"]) == 6 and r["itens"][-1]["thread_id"] == "conv-01"


def test_pagina_fora_do_intervalo_vira_a_ultima(historico):
    assert painel.consultar_execucoes(historico, {"pagina": "99"})["pagina"] == 2
    assert painel.consultar_execucoes(historico, {"pagina": "abc"})["pagina"] == 1


def test_por_pagina_tem_teto(historico):
    assert painel.consultar_execucoes(historico, {"por_pagina": "5000"})["por_pagina"] == 100


def test_busca_no_pedido_e_no_id(historico):
    assert painel.consultar_execucoes(historico, {"busca": "conversor"})["total"] == 1
    assert painel.consultar_execucoes(historico, {"busca": "CALC-0"})["total"] == 10


def test_filtros_de_desfecho_stack_teto_e_periodo(historico):
    assert painel.consultar_execucoes(historico, {"desfecho": "erro"})["total"] == 1
    assert painel.consultar_execucoes(historico, {"stack": "python"})["total"] == 1
    assert painel.consultar_execucoes(historico, {"com_teto": "1"})["total"] == 1
    r = painel.consultar_execucoes(historico, {"desde": "2026-09-01", "ate": "2026-09-10"})
    assert r["total"] == 10 and r["total_geral"] == 26


def test_resumo_e_do_conjunto_filtrado(historico):
    r = painel.consultar_execucoes(historico, {"stack": "nextjs"})
    assert r["resumo"]["execucoes"] == r["resumo"]["deploy"] == 25
    assert r["resumo"]["teto"] == 0


def test_opcoes_vem_do_historico(historico):
    opcoes = painel.consultar_execucoes(historico, {"stack": "python"})["opcoes"]
    assert opcoes == {"desfechos": ["deploy", "erro"], "stacks": ["nextjs", "python"]}


def test_linha_recalculada_so_quando_o_arquivo_muda(historico, monkeypatch):
    painel.listar_execucoes(historico)
    chamadas = []
    original = painel.detalhar
    monkeypatch.setattr(painel, "detalhar", lambda d, t: chamadas.append(t) or original(d, t))
    painel.listar_execucoes(historico)
    assert chamadas == []

    arquivo = historico / "calc-03.json"
    _gravar(historico, "calc-03", "2026-09-04", pedido="Calculadora alterada")
    os.utime(arquivo, ns=(arquivo.stat().st_atime_ns, arquivo.stat().st_mtime_ns + 1_000_000))
    linhas = {linha["thread_id"]: linha for linha in painel.listar_execucoes(historico)}
    assert chamadas == ["calc-03"]
    assert linhas["calc-03"]["pedido"] == "Calculadora alterada"


def test_busca_ignora_acento(tmp_path):
    painel._LINHAS.clear()
    _gravar(tmp_path, "c1", "2026-09-20", pedido="Criar um modulo de conversao de temperaturas")
    assert painel.consultar_execucoes(tmp_path, {"busca": "Conversão"})["total"] == 1
