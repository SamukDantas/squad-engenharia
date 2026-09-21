"""A causa de uma rodada vermelha fica nas métricas.

Três execuções seguidas da calculadora de juros tiveram o build quebrado na
primeira rodada; a rodada seguinte corrigia, e como a saída fica no estado e é
sobrescrita, não havia mais o que ler quando alguém foi diagnosticar.
"""
from pathlib import Path

from src.squad.dominio.vereditos import trecho_da_falha
from src.squad.graph import workflow
from src.squad.portas.perfil import Cobertura
from src.squad.portas.testes import ResultadoTestes

SAIDA_NEXT = """   ▲ Next.js 15.1.3
   Creating an optimized production build ...
 ✓ Compiled successfully
   Linting and checking validity of types ...
Failed to compile.

./app/page.test.tsx:8:1
Type error: Property 'IS_REACT_ACT_ENVIRONMENT' does not exist on type 'typeof globalThis'.
Next.js build worker exited with code: 1"""


def test_trecho_de_build_next_leva_arquivo_e_mensagem():
    trecho = trecho_da_falha(SAIDA_NEXT)
    assert "./app/page.test.tsx:8:1" in trecho  # o arquivo vem na linha de cima
    assert "Type error: Property 'IS_REACT_ACT_ENVIRONMENT'" in trecho
    assert "Failed to compile" in trecho
    assert "Creating an optimized" not in trecho  # ruído fica de fora


def test_sem_linha_de_erro_fica_o_fim_da_saida():
    saida = "x" * 50 + "\nresumo final"
    assert trecho_da_falha(saida, limite=20).endswith("resumo final")


def test_trecho_respeita_o_limite():
    saida = "\n".join(f"error {i}" for i in range(2000))
    assert len(trecho_da_falha(saida, limite=300)) <= 300


def test_rodada_vermelha_grava_o_trecho_nas_metricas(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(workflow, "executar_testes", lambda ws, tid, perfil: ResultadoTestes(
        testes_ok=False, saida=SAIDA_NEXT, cobertura=Cobertura(), falha_de_build=True,
    ))
    estado = {"workspace": str(tmp_path), "thread_id": "t", "stack": "nextjs"}
    workflow.no_executar_testes(estado, {"configurable": {"thread_id": "t"}})

    from src.squad.painel import detalhar
    barra = [b for b in detalhar(Path("metrics"), "t")["barras"] if b["evento"] == "executar_testes"][0]
    assert "IS_REACT_ACT_ENVIRONMENT" in barra["trecho_falha"]


def test_rodada_verde_nao_grava_trecho(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(workflow, "executar_testes", lambda ws, tid, perfil: ResultadoTestes(
        testes_ok=True, saida="ok", cobertura=Cobertura(total=100.0, pior=100.0),
    ))
    estado = {"workspace": str(tmp_path), "thread_id": "t", "stack": "nextjs"}
    workflow.no_executar_testes(estado, {"configurable": {"thread_id": "t"}})

    from src.squad.painel import detalhar
    barra = [b for b in detalhar(Path("metrics"), "t")["barras"] if b["evento"] == "executar_testes"][0]
    assert "trecho_falha" not in barra
