"""Repartição do contexto enviado ao LLM — RESILIENCIA.md, itens 18, 29 e 33.

O defeito original: o teto era conferido **depois** de acrescentar o bloco,
então não limitava nada. Medido nas 14 entregas em disco, o dump chegou a 43.918
chars com o teto em 15.000, e quem sumia era sempre a suíte — em 8 dos 11 casos
truncados. O revisor aprovava código que não tinha visto inteiro.
"""
from src.squad.dominio.orcamento import (
    blocos_com_orcamento,
    e_binario,
    ordenar_para_dump,
)


def e_teste(caminho: str) -> bool:
    return caminho.startswith("tests/")


# ---------- o teto tem que limitar de verdade ----------

def test_respeita_o_teto_com_arquivos_grandes():
    conteudos = {"a.py": "x" * 40_000, "b.py": "y" * 40_000}
    saida = blocos_com_orcamento(conteudos, ["a.py", "b.py"], 15_000, "Entrega")
    assert len(saida) <= 15_000


def test_nenhum_arquivo_some_do_manifesto_ao_truncar():
    """Quem lê precisa saber que a amostra é amostra: o manifesto lista todos os
    nomes, inclusive os que entraram truncados."""
    conteudos = {f"m{i}.py": "z" * 20_000 for i in range(5)}
    saida = blocos_com_orcamento(conteudos, list(conteudos), 5_000, "Entrega")
    for nome in conteudos:
        assert f"- {nome}" in saida, nome


def test_arquivo_truncado_diz_que_foi_truncado():
    saida = blocos_com_orcamento({"a.py": "x" * 30_000}, ["a.py"], 2_000, "Entrega")
    assert "truncado aqui" in saida
    assert "30000 chars no total" in saida


def test_o_pequeno_cabe_inteiro_e_o_grande_absorve_a_sobra():
    """Água em copos: o menor leva só o que precisa, e o saldo é redividido."""
    conteudos = {"pequeno.py": "a" * 50, "grande.py": "b" * 30_000}
    saida = blocos_com_orcamento(conteudos, ["pequeno.py", "grande.py"], 6_000, "Entrega")
    assert "a" * 50 in saida, "o arquivo pequeno deveria caber inteiro"
    assert len(saida) <= 6_000


def test_tudo_cabendo_ninguem_e_truncado():
    conteudos = {"a.py": "print(1)", "b.py": "print(2)"}
    saida = blocos_com_orcamento(conteudos, ["a.py", "b.py"], 10_000, "Entrega")
    assert "truncado" not in saida
    assert "print(1)" in saida and "print(2)" in saida


def test_lista_vazia_devolve_texto_vazio():
    assert blocos_com_orcamento({}, [], 1_000, "Entrega") == ""


def test_teto_minusculo_nao_quebra():
    """Cabeçalho e molduras podem sozinhos estourar o teto; o saldo vai a zero
    em vez de virar cota negativa."""
    saida = blocos_com_orcamento({"a.py": "x" * 100}, ["a.py"], 10, "Entrega")
    assert isinstance(saida, str)


# ---------- ordem: fonte antes de teste ----------

def test_fonte_vem_antes_de_teste_na_fila():
    """O revisor já recebe a saída dos testes em separado, então perder trecho
    de suíte custa menos que perder o módulo que a spec descreve."""
    arquivos = ["tests/test_app.py", "app.py", "tests/conftest.py", "models.py"]
    assert ordenar_para_dump(arquivos, e_teste) == [
        "app.py", "models.py", "tests/test_app.py", "tests/conftest.py",
    ]


def test_binario_fica_de_fora_da_fila():
    """`tarefas.db` (12.303 chars) foi enviado inteiro ao revisor — 56% do dump
    de uma entrega, em ruído que não informa nada."""
    assert ordenar_para_dump(["app.py", "tarefas.db", "logo.png"], e_teste) == ["app.py"]


# ---------- classificação de binário ----------

def test_reconhece_extensoes_binarias():
    for caminho in ("dados.db", "a/b/foto.PNG", "pacote.zip", "modelo.pkl"):
        assert e_binario(caminho) is True, caminho


def test_texto_nao_e_binario():
    for caminho in ("app.py", "README.md", "src/index.ts", "Main.java"):
        assert e_binario(caminho) is False, caminho


def test_arquivo_sem_extensao_nao_e_binario():
    assert e_binario("Makefile") is False
    assert e_binario("src/Dockerfile") is False


def test_arquivo_oculto_sem_extensao_nao_e_binario():
    """`.gitignore` tem ponto na posição 0 — não é extensão."""
    assert e_binario(".gitignore") is False
