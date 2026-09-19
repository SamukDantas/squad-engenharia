"""Os perfis Next.js e Java: convenções de teste e parsers de cobertura.

O parser é a parte de cada stack que não é portável de graça — coverage.py,
vitest e JaCoCo não concordam em nada além do que significa "coberto". Estes
testes fixam os três formatos contra fixtures no schema real de cada ferramenta.
"""
import json
from pathlib import Path

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


def test_nextjs_descarta_node_modules_da_entrega_em_todo_container():
    """Thread `00b92498`: o executor instalou react 19 no workspace, e o
    `node_modules` da entrega venceu o da imagem na resolução do Node — o
    `next` 15.1.3 da imagem carregou react 19 e o build morreu. Build, suíte e
    alvo (visual/pentest) têm de usar só as dependências da imagem."""
    perfil = perfis.obter("nextjs")
    for cmd in (perfil.comando_build(), perfil.comando_container("/out")):
        copia, _, resto = cmd.partition("rm -rf /app/projeto/node_modules")
        assert "cp -r /src /app/projeto" in copia, cmd
        assert "/app/node_modules/.bin/" in resto, cmd
    assert perfil.preparo_alvo.startswith("rm -rf node_modules && ")


def test_nextjs_versoes_do_prompt_batem_com_as_imagens():
    """O executor escreve para as versões que o prompt cita; se elas saírem de
    sincronia com as imagens, a entrega volta a mirar uma API que a jaula não
    tem."""
    raiz = Path(__file__).resolve().parents[1]
    citadas = dict(item.strip().split(" ") for item in perfil_nextjs._VERSOES.split(","))
    for dockerfile in ("Dockerfile.sandbox-nextjs", "Dockerfile.target-nextjs"):
        texto = (raiz / dockerfile).read_text(encoding="utf-8")
        for lib in ("next", "react", "react-dom"):
            assert f"{lib}@{citadas[lib]}" in texto, (dockerfile, lib)
    sandbox = (raiz / "Dockerfile.sandbox-nextjs").read_text(encoding="utf-8")
    assert f"typescript@{citadas['typescript']}" in sandbox


def test_nextjs_proibe_npm_install_no_prompt_do_executor():
    instrucoes = perfis.obter("nextjs").instrucoes_executor
    assert "NÃO rode `npm install`" in instrucoes
    assert perfil_nextjs._VERSOES in instrucoes


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


# ---------- config do vitest: duas lições pagas numa execução real ----------

def test_config_do_vitest_liga_o_jsx_automatico(tmp_path):
    """Sem `jsx: "automatic"`, o esbuild compila JSX para `React.createElement`
    e todo componente que não importa React quebra com "React is not defined".
    Medido: 11 testes de componente vermelhos numa entrega cujos 27 testes de
    lógica pura passavam — falha de ambiente disfarçada de falha de código."""
    perfis.obter("nextjs").preparar_workspace(str(tmp_path))
    config = (tmp_path / perfil_nextjs.ARQUIVO_CONFIG).read_text(encoding="utf-8")
    assert '"automatic"' in config


def test_config_do_vitest_exclui_arquivos_de_config_da_cobertura(tmp_path):
    """RESILIENCIA 20 na outra stack: arquivo nunca importado fica em 0% e
    sequestra o piso por módulo. Medido: `next.config.mjs` apareceu como pior
    módulo a 0,0%, e o QA seria mandado testar um arquivo de configuração."""
    perfis.obter("nextjs").preparar_workspace(str(tmp_path))
    config = (tmp_path / perfil_nextjs.ARQUIVO_CONFIG).read_text(encoding="utf-8")
    for padrao in ("**/*.config.*", ".squad/**", "**/node_modules/**"):
        assert padrao in config, padrao


def test_config_fica_fora_da_entrega(tmp_path):
    """Em `.squad/`, como o coveragerc do perfil Python: a squad configura o
    runner sem acrescentar arquivo à entrega que vai ao deploy."""
    assert perfil_nextjs.ARQUIVO_CONFIG.startswith(".squad/")
    assert ".squad" in perfis.obter("nextjs").ignorar_no_workspace


# ---------- passo de build: existe onde há compilação ----------

def test_stack_compilada_tem_passo_de_build():
    """Python não compila, e é a única. Java entrou na lista quando Spring virou
    obrigatório: `mvn test` compila sozinho, mas uma falha de compilação e um
    contexto que não sobe chegariam ao dev no mesmo bloco de saída, com o erro
    real enterrado numa stack trace do Spring."""
    com_build = sorted(n for n in perfis.nomes() if perfis.obter(n).comando_build)
    assert com_build == ["java", "nextjs"]
    assert not perfis.obter("python").comando_build


def test_build_do_nextjs_invoca_o_next_por_caminho_absoluto():
    """Mesmo motivo do vitest: `npx` consultaria o registry num container sem
    rede e viraria espera até o timeout."""
    cmd = perfis.obter("nextjs").comando_build()
    assert "/app/node_modules/.bin/next build" in cmd
    assert "npx" not in cmd


def test_falha_de_build_e_um_desfecho_distinto():
    """Entrega que não compila não é entrega com teste vermelho: nenhum teste
    rodou, e mandar o dev "corrigir com base na saída dos testes" o aponta para
    um lugar onde não há nada. Medido numa entrega real que passou por 38 testes
    verdes, revisão aprovada e deploy — e não compilava."""
    from src.squad.portas.testes import ResultadoTestes

    normal = ResultadoTestes(testes_ok=False, saida="x", cobertura=Cobertura())
    assert normal.falha_de_build is False
    build = ResultadoTestes(
        testes_ok=False, saida="Failed to compile", cobertura=Cobertura(),
        falha_de_build=True,
    )
    assert build.falha_de_build is True and build.testes_ok is False


def test_config_do_vitest_resolve_o_alias_do_nextjs(tmp_path):
    """`@/` é convenção do Next.js: o tsconfig o declara em `paths` e o
    `next build` o resolve, mas o vitest não lê `paths`. Sem o mapeamento a
    suíte quebra com "Failed to load url @/data/..." — e o laço fica num beco,
    porque o QA escreveu os testes com o alias e o executor não pode editá-los."""
    perfis.obter("nextjs").preparar_workspace(str(tmp_path))
    config = (tmp_path / perfil_nextjs.ARQUIVO_CONFIG).read_text(encoding="utf-8")
    assert '"@"' in config and "import.meta.url" in config


def test_relatorio_de_cobertura_nao_fica_na_raiz_do_mount():
    """O vitest limpa o `reportsDirectory` antes de escrever, e a raiz é o ponto
    de montagem: `rmdir` nela falha com EACCES e derruba a execução. Nem a flag
    nem o `clean` no config impedem isso no caminho de falha — num subdiretório
    a limpeza é legítima."""
    perfil = perfis.obter("nextjs")
    assert "/" in perfil.relatorio_cobertura, perfil.relatorio_cobertura
    assert perfil.relatorio_cobertura.startswith(perfil_nextjs.SUBDIR_COBERTURA + "/")
    cmd = perfil.comando_container("/out")
    assert f"/out/{perfil_nextjs.SUBDIR_COBERTURA}" in cmd
