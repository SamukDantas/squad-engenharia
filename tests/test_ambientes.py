"""Configuração de banco por ambiente: o que reprova uma entrega Spring.

O nó nasceu de um buraco de cobertura, não de gosto por configuração: a suíte
roda no perfil `test`, com H2 em memória, e fica verde independentemente do que
`hml` e `prod` digam. `ddl-auto: create-drop` em produção apaga o banco no boot
e passa por todos os outros juízes do pipeline — testes, revisor, pentest,
renderização — sem que nenhum tenha como vê-lo.
"""
import pytest

from src.squad.adaptadores import config_spring, perfis
from src.squad.dominio import ambientes

PG_PROD = "jdbc:postgresql://db-prod:5432/eventos"
PG_HML = "jdbc:postgresql://db-hml:5432/eventos"
H2 = "jdbc:h2:mem:devdb"


def cfg(url, **extra):
    return {ambientes.CHAVE_URL: url, **extra}


def sadia():
    return {
        "dev": cfg(H2),
        "hml": cfg(PG_HML, **{ambientes.CHAVE_DDL: "validate"}),
        "prod": cfg(PG_PROD, **{ambientes.CHAVE_DDL: "validate"}),
    }


def tipos(achados):
    return sorted(a.tipo for a in achados)


# ---------- o caso verde ----------

def test_configuracao_sadia_nao_tem_achado():
    assert ambientes.conferir({}, sadia()) == []


def test_h2_em_dev_e_legitimo():
    """É justamente o banco que roda dentro da jaula sem rede. Reprovar aqui
    obrigaria a entrega a apontar dev para um Postgres que o sandbox não
    alcança."""
    assert not [a for a in ambientes.conferir({}, sadia()) if a.ambiente == "dev"]


# ---------- ambiente ausente ----------

@pytest.mark.parametrize("faltando", ambientes.AMBIENTES_EXIGIDOS)
def test_ambiente_ausente_e_achado(faltando):
    config = sadia()
    del config[faltando]
    achados = ambientes.conferir({}, config)
    ausentes = [a for a in achados if a.tipo == "ambiente_ausente"]
    assert [a.ambiente for a in ausentes] == [faltando]


def test_entrega_sem_configuracao_nenhuma_reprova_nos_tres():
    achados = ambientes.conferir({}, {})
    assert tipos(achados) == ["ambiente_ausente"] * 3


# ---------- banco de brinquedo onde não devia ----------

@pytest.mark.parametrize("ambiente", ambientes.AMBIENTES_DE_VERDADE)
def test_h2_em_homologacao_ou_producao_reprova(ambiente):
    config = sadia()
    config[ambiente] = cfg(H2)
    achados = [a for a in ambientes.conferir({}, config) if a.tipo == "banco_de_brinquedo"]
    assert [a.ambiente for a in achados] == [ambiente]


def test_h2_e_reconhecido_em_qualquer_caixa():
    config = sadia()
    config["prod"] = cfg("JDBC:H2:MEM:PROD")
    assert "banco_de_brinquedo" in tipos(ambientes.conferir({}, config))


# ---------- ddl-auto ----------

@pytest.mark.parametrize("valor", ["create", "create-drop", "update"])
def test_ddl_que_altera_schema_em_producao_reprova(valor):
    config = sadia()
    config["prod"][ambientes.CHAVE_DDL] = valor
    achados = [a for a in ambientes.conferir({}, config) if a.tipo == "ddl_perigoso"]
    assert len(achados) == 1 and valor in achados[0].detalhe


@pytest.mark.parametrize("valor", sorted(ambientes.DDL_SEGURO))
def test_ddl_seguro_passa(valor):
    config = sadia()
    config["prod"][ambientes.CHAVE_DDL] = valor
    assert "ddl_perigoso" not in tipos(ambientes.conferir({}, config))


def test_ddl_ausente_nao_e_achado():
    """Sem `ddl-auto` declarado e com banco não embarcado, o padrão do Spring
    Boot já é não mexer no schema. Cobrar a declaração seria exigir cerimônia."""
    config = sadia()
    del config["prod"][ambientes.CHAVE_DDL]
    assert "ddl_perigoso" not in tipos(ambientes.conferir({}, config))


def test_ddl_perigoso_em_dev_nao_reprova():
    """`create-drop` em dev é o comportamento certo: o banco é efêmero."""
    config = sadia()
    config["dev"][ambientes.CHAVE_DDL] = "create-drop"
    assert "ddl_perigoso" not in tipos(ambientes.conferir({}, config))


# ---------- credencial ----------

@pytest.mark.parametrize("chave", ambientes.CHAVES_SEGREDO)
def test_credencial_literal_reprova(chave):
    config = sadia()
    config["prod"][chave] = "segredo123"
    achados = [a for a in ambientes.conferir({}, config) if a.tipo == "credencial_literal"]
    assert len(achados) == 1 and achados[0].ambiente == "prod"


@pytest.mark.parametrize("valor", ["${DB_PASSWORD}", "${DB_PASSWORD:}", "${DB_PASSWORD:troque}"])
def test_referencia_ao_ambiente_passa(valor):
    config = sadia()
    config["prod"]["spring.datasource.password"] = valor
    assert "credencial_literal" not in tipos(ambientes.conferir({}, config))


def test_credencial_vazia_nao_e_achado():
    """Chave declarada e vazia é o mesmo que não declarada — reprovar viraria
    ruído sobre um arquivo que não guarda segredo nenhum."""
    config = sadia()
    config["prod"]["spring.datasource.password"] = "   "
    assert "credencial_literal" not in tipos(ambientes.conferir({}, config))


# ---------- três perfis, um banco só ----------

def test_tres_perfis_com_a_mesma_url_nao_sao_tres_ambientes():
    mesma = {n: cfg(PG_PROD) for n in ambientes.AMBIENTES_EXIGIDOS}
    assert "ambientes_indistintos" in tipos(ambientes.conferir({}, mesma))


def test_urls_distintas_passam():
    assert "ambientes_indistintos" not in tipos(ambientes.conferir({}, sadia()))


# ---------- herança do arquivo comum ----------

def test_perfil_herda_o_que_o_application_yml_define():
    """`spring.datasource.url: ${DB_URL}` no comum, com os perfis especializando
    só o resto, é um jeito correto de escrever. Sem a herança, a medição
    acusaria falta de URL em toda entrega assim."""
    comum = {ambientes.CHAVE_URL: "${DB_URL}", ambientes.CHAVE_DDL: "validate"}
    por_ambiente = {n: {} for n in ambientes.AMBIENTES_EXIGIDOS}
    achados = ambientes.conferir(comum, por_ambiente)
    assert "ambiente_ausente" not in tipos(achados)
    assert "ddl_perigoso" not in tipos(achados)


def test_o_perfil_sobrepoe_o_comum():
    comum = {ambientes.CHAVE_DDL: "create-drop"}
    config = sadia()
    assert "ddl_perigoso" not in tipos(ambientes.conferir(comum, config))


# ---------- o brief de correção ----------

def test_feedback_vazio_quando_aprovado():
    assert ambientes.formatar_feedback([]) == ""


def test_feedback_nomeia_o_ambiente_e_o_tipo_de_cada_achado():
    """Mesmo contrato do pentest e do visual: o achado é a medição, não uma
    paráfrase dela — o dev precisa saber qual arquivo abrir."""
    config = sadia()
    config["prod"] = cfg(H2, **{ambientes.CHAVE_DDL: "create-drop"})
    texto = ambientes.formatar_feedback(ambientes.conferir({}, config))
    assert "banco_de_brinquedo" in texto and "ddl_perigoso" in texto
    assert "prod" in texto and "src/main/resources/" in texto


# ---------- leitura do disco (adaptador) ----------

def escrever(tmp_path, nome, texto):
    destino = tmp_path / config_spring.DIR_RECURSOS
    destino.mkdir(parents=True, exist_ok=True)
    (destino / nome).write_text(texto, encoding="utf-8")
    return tmp_path


def test_le_yaml_aninhado_como_chave_pontuada(tmp_path):
    ws = escrever(tmp_path, "application-prod.yml", (
        "spring:\n"
        "  datasource:\n"
        f"    url: {PG_PROD}\n"
    ))
    _, por_ambiente, _ = config_spring.carregar(str(ws))
    assert por_ambiente["prod"][ambientes.CHAVE_URL] == PG_PROD


def test_le_properties_tambem(tmp_path):
    """O executor é um LLM: reprovar uma configuração correta por ela ter sido
    escrita em `.properties` seria cobrar um estilo que ninguém pediu."""
    ws = escrever(tmp_path, "application-prod.properties",
                  f"# comentario\n{ambientes.CHAVE_URL}={PG_PROD}\n")
    _, por_ambiente, _ = config_spring.carregar(str(ws))
    assert por_ambiente["prod"][ambientes.CHAVE_URL] == PG_PROD


def test_le_yaml_multi_documento_com_on_profile(tmp_path):
    """Os três ambientes num arquivo só, separados por `---`, é forma corrente
    no Spring Boot 3."""
    ws = escrever(tmp_path, "application.yml", (
        "spring:\n  application:\n    name: eventos\n"
        "---\n"
        "spring:\n  config:\n    activate:\n      on-profile: prod\n"
        f"  datasource:\n    url: {PG_PROD}\n"
    ))
    comum, por_ambiente, _ = config_spring.carregar(str(ws))
    assert comum["spring.application.name"] == "eventos"
    assert por_ambiente["prod"][ambientes.CHAVE_URL] == PG_PROD


def test_yaml_quebrado_nao_derruba_a_medicao(tmp_path):
    """Uma entrega com YAML inválido já reprova no boot; levantar aqui trocaria
    um achado legível por um traceback."""
    ws = escrever(tmp_path, "application-prod.yml", "spring:\n  - isto: [nao\n")
    resultado = config_spring.medir_ambientes(str(ws))
    assert resultado["ambientes_ok"] is False


def test_workspace_sem_resources_reprova_pelos_tres_ausentes(tmp_path):
    resultado = config_spring.medir_ambientes(str(tmp_path))
    assert resultado["ambientes_ok"] is False
    assert len(resultado["achados_ambientes"]) == 3


# ---------- só a stack que pede ----------

def test_so_java_exige_configuracao_por_ambiente():
    """A exigência nasce do Spring, que lê configuração por perfil. Cobrar o
    mesmo de uma entrega Python seria a squad inventando um requisito que a
    spec não pediu."""
    exigem = sorted(n for n in perfis.nomes() if perfis.obter(n).exige_ambientes)
    assert exigem == ["java"]


def test_a_instrucao_ao_executor_cita_os_mesmos_ambientes_que_a_medicao():
    """Instrução e medição que divergem produzem uma entrega que obedece ao
    prompt e reprova na verificação — o pior dos mundos."""
    instrucoes = perfis.obter("java").instrucoes_executor
    for nome in ambientes.AMBIENTES_EXIGIDOS:
        assert f"application-{nome}.yml" in instrucoes
    assert ambientes.CHAVE_DDL.rsplit(".", 1)[-1] in instrucoes


def test_credencial_ao_lado_de_banco_embarcado_nao_e_segredo():
    """`sa` é o usuário padrão do H2 em memória: o banco morre com o processo e
    não há o que vazar. Medido numa execução real — sem esta exceção, a regra
    reprovou um `username: sa` em dev e custou ao ramo uma rodada inteira de
    correção para trocar um valor que não protege nada."""
    config = sadia()
    config["dev"]["spring.datasource.username"] = "sa"
    config["dev"]["spring.datasource.password"] = ""
    assert "credencial_literal" not in tipos(ambientes.conferir({}, config))


def test_credencial_literal_em_banco_de_verdade_continua_reprovando():
    """A exceção é do banco embarcado, não do ambiente: senha literal ao lado de
    um Postgres é vazamento em qualquer perfil."""
    config = sadia()
    config["hml"]["spring.datasource.password"] = "segredo123"
    achados = [a for a in ambientes.conferir({}, config) if a.tipo == "credencial_literal"]
    assert [a.ambiente for a in achados] == ["hml"]
