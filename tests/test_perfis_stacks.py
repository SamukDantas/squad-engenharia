"""Os perfis Next.js e Java: convenções de teste e parsers de cobertura.

O parser é a parte de cada stack que não é portável de graça — coverage.py,
vitest e JaCoCo não concordam em nada além do que significa "coberto". Estes
testes fixam os três formatos contra fixtures no schema real de cada ferramenta.
"""
import json

from src.squad.adaptadores import perfil_java, perfil_nextjs, perfis
from src.squad.portas.perfil import Cobertura


# ---------- Next.js: convenção de teste colocado ----------

def test_nextjs_reconhece_teste_ao_lado_do_modulo():
    """É o caso que quebrava três guards quando o prefixo `tests/` estava fixo
    no grafo: com teste colocado, o guard de entrega vazia contava teste como
    entrega e parava de disparar."""
    e_teste = perfis.obter("nextjs").e_teste
    for caminho in ("src/lib/funil.test.ts", "src/app/page.spec.tsx",
                    "components/Card.test.jsx", "tests/e2e.ts"):
        assert e_teste(caminho) is True, caminho


def test_nextjs_nao_confunde_modulo_com_teste():
    e_teste = perfis.obter("nextjs").e_teste
    for caminho in ("src/lib/funil.ts", "src/app/page.tsx", "next.config.js"):
        assert e_teste(caminho) is False, caminho


def test_nextjs_ignora_node_modules_na_varredura():
    """Sem isto, `_arquivos_do_workspace` enumeraria dezenas de milhares de
    arquivos, todos entrando no manifesto, no hash por arquivo e no orçamento
    de 15.000 chars do revisor."""
    ignorados = perfis.obter("nextjs").ignorar_no_workspace
    for pasta in ("node_modules", ".next", "dist", "coverage"):
        assert pasta in ignorados, pasta


def test_nextjs_recusa_o_runner_de_host():
    assert perfis.obter("nextjs").permite_host is False


def test_nextjs_invoca_o_vitest_por_caminho_absoluto():
    """`npx` consultaria o registry para um pacote ausente; sem rede isso vira
    espera até o timeout. O caminho absoluto falha alto na hora."""
    cmd = perfis.obter("nextjs").comando_container("/out")
    assert "/app/node_modules/.bin/vitest" in cmd
    assert "npx" not in cmd
    assert "--coverage.reportsDirectory=/out" in cmd


# ---------- Next.js: cobertura V8 / istanbul ----------

def _summary(total_pct, arquivos):
    dados = {"total": {"lines": {"total": 100, "covered": 1, "pct": total_pct}}}
    for nome, (n, pct) in arquivos.items():
        dados[nome] = {"lines": {"total": n, "covered": 1, "pct": pct}}
    return json.dumps(dados)


def test_nextjs_le_total_e_pior_modulo():
    cob = perfil_nextjs._ler_cobertura(_summary(85.0, {
        "/app/projeto/src/lib/funil.ts": (40, 50.0),
        "/app/projeto/src/lib/util.ts": (10, 100.0),
    }))
    assert cob.total == 85.0
    assert cob.pior == 50.0


def test_nextjs_encurta_o_caminho_absoluto_do_container():
    """O relatório traz o caminho de dentro do container; quem lê o feedback
    precisa do caminho dentro da entrega."""
    cob = perfil_nextjs._ler_cobertura(
        _summary(50.0, {"/app/projeto/src/lib/funil.ts": (10, 50.0)})
    )
    assert cob.pior_arquivo == "src/lib/funil.ts"


def test_nextjs_ignora_arquivo_sem_linha_executavel():
    cob = perfil_nextjs._ler_cobertura(_summary(80.0, {
        "/app/projeto/src/tipos.ts": (0, 100.0),
        "/app/projeto/src/main.ts": (20, 80.0),
    }))
    assert cob.pior_arquivo == "src/main.ts"


def test_nextjs_relatorio_ilegivel_zera():
    for texto in ("", "não é json", "{}", '{"total": {}}'):
        assert perfil_nextjs._ler_cobertura(texto) == Cobertura(), repr(texto)


# ---------- Java: convenção do Maven ----------

def test_java_separa_src_main_de_src_test():
    e_teste = perfis.obter("java").e_teste
    assert e_teste("src/test/java/com/ex/ReservaTest.java") is True
    assert e_teste("src/main/java/com/ex/Reserva.java") is False


def test_java_roda_maven_offline():
    """Sem `-o`, cada execução tenta baixar o mundo num container sem rota para
    fora e só descobre isso no timeout."""
    cmd = perfis.obter("java").comando_container("/out")
    assert "mvn -o -B test" in cmd
    assert "jacoco.xml" in cmd


def test_java_ignora_o_diretorio_de_build():
    assert "target" in perfis.obter("java").ignorar_no_workspace


# ---------- Java: cobertura JaCoCo ----------

JACOCO = """<?xml version="1.0" encoding="UTF-8"?>
<report name="entrega">
<package name="com/exemplo">
  <sourcefile name="Reserva.java">
    <counter type="BRANCH" missed="4" covered="0"/>
    <counter type="LINE" missed="8" covered="2"/>
  </sourcefile>
  <sourcefile name="App.java">
    <counter type="LINE" missed="0" covered="20"/>
  </sourcefile>
  <counter type="LINE" missed="8" covered="22"/>
</package>
<counter type="INSTRUCTION" missed="30" covered="70"/>
<counter type="LINE" missed="8" covered="22"/>
</report>
"""


def test_java_le_total_e_pior_classe():
    cob = perfil_java._ler_cobertura(JACOCO)
    assert cob.total == 73.3, "22 cobertas de 30 linhas"
    assert cob.pior == 20.0
    assert cob.pior_arquivo == "Reserva.java"


def test_java_ignora_contadores_que_nao_sao_de_linha():
    """O relatório traz INSTRUCTION, BRANCH, COMPLEXITY, METHOD e CLASS junto —
    ler o contador errado daria um número plausível e falso."""
    cob = perfil_java._ler_cobertura(JACOCO)
    assert cob.total != 70.0, "70% é a cobertura de INSTRUCTION, não de LINE"


def test_java_tolera_ordem_invertida_dos_atributos():
    """A ordem dos atributos não é garantida pelo XML."""
    invertido = JACOCO.replace(
        '<counter type="LINE" missed="8" covered="22"/>',
        '<counter covered="22" type="LINE" missed="8"/>',
    )
    assert perfil_java._ler_cobertura(invertido).total == 73.3


def test_java_relatorio_truncado_ou_ausente_zera():
    """O XML pode vir cortado se o build morrer no meio; o veredito de teste já
    veio do exit code, e cobertura zero reprova sozinha pelo piso."""
    for texto in ("", "<html>erro</html>", "<report name='x'>"):
        assert perfil_java._ler_cobertura(texto) == Cobertura(), repr(texto)


def test_java_sem_linha_executavel_zera():
    vazio = '<report name="x"><counter type="LINE" missed="0" covered="0"/></report>'
    assert perfil_java._ler_cobertura(vazio) == Cobertura()


# ---------- o que as três precisam ter em comum ----------

def test_as_tres_stacks_estao_registradas():
    assert perfis.nomes() == ["java", "nextjs", "python"]


def test_cada_stack_tem_imagem_e_dockerfile_proprios():
    """Duas stacks compartilhando imagem significaria que uma delas roda no
    ambiente errado — e o sintoma seria um teste vermelho inexplicável."""
    imagens = [perfis.obter(n).imagem_sandbox for n in perfis.nomes()]
    assert len(set(imagens)) == len(imagens), imagens
    alvos = [perfis.obter(n).imagem_alvo for n in perfis.nomes()]
    assert len(set(alvos)) == len(alvos), alvos


def test_so_o_python_permite_runner_de_host():
    """O runner de host roda a suíte no interpretador da própria squad."""
    assert [n for n in perfis.nomes() if perfis.obter(n).permite_host] == ["python"]
