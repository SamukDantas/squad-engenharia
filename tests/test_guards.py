"""Os dois guards de entrega — RESILIENCIA.md, itens 26, 28 e 32.

Cada caso é uma execução real em que o executor saiu com exit code 0 e a squad
seguiu pagando trabalho em cima do nada.
"""
from src.squad.dominio.guards import conferir_correcao, conferir_entrega

WS = "/workspace/abc"


# ---------- entrega vazia ----------

def test_entrega_com_conteudo_passa():
    assert conferir_entrega({"app.py": True, "tests/test_app.py": True}, WS) is None


def test_workspace_vazio_reprova():
    """O executor abortou (permissão negada / saldo) e saiu com 0. Sem este
    guard, a squad pagou 855s de QA em cima do vazio."""
    falha = conferir_entrega({}, WS)
    assert falha and "nada" in falha


def test_so_arquivos_de_teste_nao_e_entrega():
    """A suíte é do QA, e o executor é proibido de tocá-la — então ela não prova
    que o desenvolvimento produziu algo."""
    falha = conferir_entrega({"tests/test_app.py": True, "tests/conftest.py": True}, WS)
    assert falha and "apenas 2 arquivo(s) de teste" in falha


def test_arquivo_vazio_nao_conta_como_trabalho():
    """A primeira versão do guard contava qualquer caminho fora da suíte, e o
    executor deixou um `app/__init__.py` de 0 bytes — estrutura sem conteúdo,
    que passou batido."""
    falha = conferir_entrega({"app/__init__.py": False}, WS)
    assert falha and "todos vazios ou só de teste" in falha


def test_entrega_valida_mesmo_com_arquivos_vazios_ao_lado():
    assert conferir_entrega({"app/__init__.py": False, "app/main.py": True}, WS) is None


def test_a_mensagem_nomeia_o_workspace_para_quem_for_investigar():
    assert WS in conferir_entrega({}, WS)


def test_convencao_de_teste_e_injetavel():
    """Com teste colocado (`src/app.test.ts`), a convenção `tests/` faria o
    guard contar teste como entrega e parar de disparar."""
    manifesto = {"src/app.test.ts": True}
    assert conferir_entrega(manifesto, WS) is None, "convenção padrão não reconhece"
    falha = conferir_entrega(manifesto, WS, e_teste=lambda c: c.endswith(".test.ts"))
    assert falha and "apenas 1 arquivo(s) de teste" in falha


# ---------- rodada de correção que não corrigiu ----------

def test_correcao_que_mudou_algo_passa():
    assert conferir_correcao({"app.py": "aaa"}, {"app.py": "bbb"}, "revisao") is None


def test_correcao_que_nao_mudou_nada_reprova():
    """Thread `ac0c7d4e`: a rodada rodou 109,5s, não escreveu um único arquivo,
    o pytest devolveu cobertura idêntica ao dígito e a segunda revisão repetiu
    o apontamento palavra por palavra — porque o código era o mesmo."""
    impressao = {"app.py": "aaa", "models.py": "bbb"}
    falha = conferir_correcao(impressao, dict(impressao), "revisao")
    assert falha
    assert "2 arquivo(s), todos byte a byte idênticos" in falha
    assert "origem: revisao" in falha


def test_arquivo_novo_conta_como_mudanca():
    assert conferir_correcao({"app.py": "aaa"}, {"app.py": "aaa", "novo.py": "ccc"}, "visual") is None


def test_arquivo_removido_conta_como_mudanca():
    assert conferir_correcao({"app.py": "aaa", "velho.py": "x"}, {"app.py": "aaa"}, "pentest") is None


def test_a_origem_aparece_na_mensagem():
    """Quem lê o erro precisa saber qual guard cobrou a rodada."""
    for origem in ("revisao", "pentest", "visual", "testes"):
        falha = conferir_correcao({"a": "1"}, {"a": "1"}, origem)
        assert f"origem: {origem}" in falha
