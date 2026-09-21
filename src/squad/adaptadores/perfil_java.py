"""Perfil da stack Java: Spring Boot sobre Maven offline, JUnit 5, JaCoCo.

É a stack mais hostil ao `--network none`: o Maven baixa não só as dependências
como os próprios plugins do build, e sem rede ele falha antes de compilar. A
imagem resolve isso com um `~/.m2` pré-populado, e o `-o` (offline) transforma
qualquer coisa faltando num erro imediato — em vez de uma espera longa até o
timeout de rede.

A consequência é que o `pom.xml` da entrega não é livre: ele precisa declarar
exatamente as versões que estão no repositório local da imagem. O executor
recebe o pom no prompt, e um desvio vira erro de build — que é a mesma régua de
sempre, exit code manda.

Spring é obrigatório aqui. Isso não é preferência de framework: é o que permite
à entrega ter camada de adapters, entrypoint HTTP e persistência — e, por
tabela, é o que faz os nós de pentest e de verificação visual deixarem de ser
inócuos em Java, porque agora existe um servidor para subir e atacar.
"""
import re
from pathlib import Path

from ..portas.perfil import Cobertura, PerfilStack

RELATORIO = "jacoco.xml"

_LIBS = (
    "a biblioteca padrão do JDK 21 e o ecossistema Spring Boot 3.4 já presente "
    "na imagem: spring-boot-starter-web, -data-jpa, -validation, -actuator e "
    "-test, com H2 e PostgreSQL como drivers (versões fixadas pelo pom)"
)

_GITIGNORE = "target/\n.gradle/\n.squad/\n*.class\n*.log\n"

# O mesmo arquivo que a imagem usa para popular o `~/.m2`. Ler daqui, em vez de
# repetir o XML numa string, mantém uma fonte de verdade só: se o pom do prompt
# divergir do pom que semeou a imagem, o build offline falha por dependência
# ausente e o erro não aponta para a causa.
_ARQUIVO_POM = Path(__file__).resolve().parents[3] / "docker" / "pom-referencia.xml"
try:
    _POM_REFERENCIA = _ARQUIVO_POM.read_text(encoding="utf-8")
except OSError:  # instalação sem a árvore de docker/ ao lado
    _POM_REFERENCIA = (
        "(pom-referencia.xml não encontrado — use JUnit 5.11.3, "
        "jacoco-maven-plugin 0.8.12 e maven.compiler.release 21)\n"
    )


# A regra de banco por ambiente, dita ao executor e medida depois por
# `adaptadores/config_spring.py`. Escrita uma vez e reaproveitada nos dois
# lugares: instrução e medição que divergem produzem uma entrega que obedece ao
# prompt e reprova na verificação, que é o pior dos mundos.
AMBIENTES = ("dev", "hml", "prod")

_INSTRUCOES_AMBIENTES = (
    "- A entrega configura TRÊS ambientes, um arquivo por perfil em "
    "`src/main/resources/`: `application-dev.yml`, `application-hml.yml` e "
    "`application-prod.yml`, mais o `application.yml` comum.\n"
    "- `dev` e os testes usam H2 em memória (`jdbc:h2:mem:...`), que é o único "
    "banco que roda dentro da jaula sem rede. `hml` e `prod` usam PostgreSQL "
    "(`jdbc:postgresql://...`) — o driver já está no classpath.\n"
    "- Em `prod`, `spring.jpa.hibernate.ddl-auto` só pode ser `validate` ou "
    "`none`. `create-drop` apaga o banco no boot, e `update` altera schema de "
    "produção sem revisão.\n"
    "- NENHUMA credencial literal nos YAML: use ${DB_URL}, ${DB_USER}, "
    "${DB_PASSWORD}. Senha escrita no arquivo vai para o repositório junto "
    "com o código.\n"
    "- Os três ambientes apontam para URLs de banco diferentes. Três perfis "
    "com a mesma URL não são três ambientes."
)


def _e_teste(caminho: str) -> bool:
    """Convenção do Maven: `src/test/java/...`. `src/main/java/` é entrega."""
    return caminho.startswith("src/test/")


def _preparar(workspace: str) -> None:
    """Nada a escrever: a configuração do build é o `pom.xml` da própria
    entrega, e escrevê-lo é trabalho do executor."""


def _comando_build() -> str:
    """Compilação em passo separado, antes da suíte.

    Antes não havia: `mvn test` já compila, e um passo próprio só pagava outro
    start de container e de JVM. Com Spring a conta virou. Uma entrega que não
    compila e uma cujo contexto não sobe são consertos diferentes — a primeira é
    sintaxe ou import, a segunda é bean faltando ou datasource mal configurado —
    e no `mvn test` as duas chegam ao dev como o mesmo bloco de saída, com o
    erro real enterrado numa stack trace do Spring de centenas de linhas.

    Separar devolve a distinção: falha aqui é `falha_de_build`, e o dev recebe
    "a entrega NÃO COMPILA" com a saída do compilador, e nada mais.
    """
    return "cp -r /src /app/projeto && cd /app/projeto && mvn -o -B -q compile"


def _comando_container(dir_saida: str) -> str:
    # `-o` offline: dependência faltando falha na hora, em vez de esperar o
    # timeout de rede num container que nem tem rota para fora.
    # `-B` batch: sem cores nem barra de progresso no log.
    return (
        f"cp -r /src /app/projeto && cd /app/projeto && "
        f"mvn -o -B test && "
        f"cp target/site/jacoco/{RELATORIO} {dir_saida}/{RELATORIO}"
    )


def _comando_verificacao() -> str:
    """`mvn compile`: só a compilação, sem rodar a suíte — a verificação do
    deploy responde "compila?", não "passa?"."""
    return "cp -r /src /app/projeto && cd /app/projeto && mvn -o -B -q compile"


def _comando_host(dir_saida: str) -> list[str]:
    # Inalcançável: `permite_host=False`.
    return ["mvn", "-o", "-B", "test"]


# O XML do JaCoCo traz <counter type="LINE" missed="N" covered="M"/> em três
# níveis: por método, por classe e no fechamento de cada <package>/<report>. O
# que interessa é o contador do relatório inteiro e o de cada <sourcefile>.
_SOURCEFILE = re.compile(
    r'<sourcefile name="([^"]+)"(.*?)</sourcefile>', re.DOTALL
)
# A ordem dos atributos não é garantida pelo XML, então o contador é casado
# inteiro e os números são extraídos por nome — casar `missed="N" covered="M"`
# na sequência daria zero achados num relatório escrito na ordem inversa.
_COUNTER_LINHA = re.compile(r'<counter\b[^>]*type="LINE"[^>]*/>')
_ATRIBUTO = re.compile(r'\b(missed|covered)="(\d+)"')


def _contadores_de_linha(texto: str) -> list[tuple[int, int]]:
    achados = []
    for tag in _COUNTER_LINHA.findall(texto):
        valores = dict(_ATRIBUTO.findall(tag))
        if "missed" in valores and "covered" in valores:
            achados.append((int(valores["missed"]), int(valores["covered"])))
    return achados


def _pct(missed: int, covered: int) -> float | None:
    total = missed + covered
    if total == 0:
        return None
    return round(covered * 100 / total, 1)


def _ler_cobertura(texto: str) -> Cobertura:
    """Cobertura de linha do relatório XML do JaCoCo.

    Lido por regex e não por parser XML de propósito: o relatório pode vir
    truncado se o build morrer no meio, e um parser estrito levantaria em vez de
    devolver o que deu para medir. O veredito de teste já veio do exit code; a
    cobertura aqui é o segundo piso, e zero reprova sozinho.
    """
    if not texto or "<report" not in texto:
        return Cobertura()

    # O último contador de LINE do documento é o do relatório inteiro: o JaCoCo
    # fecha `<report>` com os agregados depois de todos os pacotes.
    todos = _contadores_de_linha(texto)
    if not todos:
        return Cobertura()
    total = _pct(*todos[-1])
    if total is None:
        return Cobertura()

    pior, pior_arquivo = None, ""
    for nome, corpo in _SOURCEFILE.findall(texto):
        contadores = _contadores_de_linha(corpo)
        if not contadores:
            continue
        pct = _pct(*contadores[-1])
        # Arquivo sem linha executável não diz nada sobre a suíte.
        if pct is None:
            continue
        if pior is None or pct < pior:
            pior, pior_arquivo = pct, nome

    return Cobertura(
        total=total,
        pior=total if pior is None else pior,
        pior_arquivo=pior_arquivo,
    )


PERFIL = PerfilStack(
    nome="java",
    imagem_sandbox="squad-sandbox-java:latest",
    dockerfile_sandbox="Dockerfile.sandbox-java",
    runner="maven",
    comando_build=_comando_build,
    comando_container=_comando_container,
    comando_verificacao=_comando_verificacao,
    comando_host=_comando_host,
    relatorio_cobertura=RELATORIO,
    ler_cobertura=_ler_cobertura,
    preparar_workspace=_preparar,
    permite_host=False,
    # JVM + Maven + contexto Spring: o mais lento e o mais pesado dos três,
    # mesmo com o repositório já populado. Subir o contexto custa segundos por
    # classe de teste, e a folga de memória é o que separa um teste lento de um
    # OOM que o pipeline leria como suíte vermelha.
    timeout_testes=600,
    memoria="2560m",
    e_teste=_e_teste,
    ignorar_no_workspace=frozenset({"target", ".gradle", ".mvn", ".squad", ".git"}),
    extensoes_descartaveis=frozenset({".class", ".jar"}),
    gitignore_entrega=_GITIGNORE,
    exige_ambientes=True,
    imagem_alvo="squad-target-java:latest",
    # O alvo sobe o jar, então precisa do `package`: `compile` deixava
    # `target/classes` e nenhum executável, e o `run.json` não tinha o que rodar.
    preparo_alvo="mvn -o -B -q package -DskipTests && ",
    dockerfile_alvo="Dockerfile.target-java",
    libs_permitidas=_LIBS,
    ambiente_testes=(
        "JUnit 5 com Maven offline e as dependências do pom: regras de domínio "
        "com JUnit puro, e controller, JSON e JPA com o contexto do Spring."
    ),
    instrucoes_qa=(
        "Escreva testes JUnit 5 em `src/test/java/`, espelhando o pacote da "
        "classe testada. Eles serão executados por `mvn test`.\n"
        "- Teste as regras de negócio com JUnit puro, SEM subir contexto: "
        "entidades e casos de uso não dependem de Spring, e um `@SpringBootTest` "
        "para verificar uma regra de domínio custa segundos de startup por classe "
        "sem cobrir nada a mais.\n"
        "- Use `@SpringBootTest` (ou `@WebMvcTest`) só onde o que está sob teste é "
        "a integração: controller, serialização JSON, repositório JPA.\n"
        "- O banco dos testes é H2 em memória, no perfil `test`. A suíte roda numa "
        "jaula SEM REDE: teste que tenta alcançar host externo, banco externo ou "
        "baixar algo falha por timeout, não por asserção.\n"
        "- Dublês de teste são feitos à mão ou com o Mockito que já vem em "
        "`spring-boot-starter-test`. Não acrescente dependência nenhuma."
    ),
    instrucoes_executor=(
        f"- Java 21 com Spring Boot 3.4, usando {_LIBS}. Não acrescente "
        "dependência nenhuma ao pom: o repositório Maven da imagem é offline, e o "
        "que não estiver lá falha o build na hora.\n"
        "- Use EXATAMENTE este `pom.xml`, sem alterar versões:\n"
        f"```xml\n{_POM_REFERENCIA}```\n"
        "- Código de produção em `src/main/java/`. NÃO escreva nada em "
        "`src/test/java/` — a suíte é de outro agente da equipe de qualidade.\n"
        "- Arquitetura em camadas, com a dependência apontando sempre para dentro: "
        "`entity` (regras de negócio, sem anotacao de framework) → `usecase` "
        "(interactors e as interfaces de gateway que eles declaram) → `adapter` "
        "(controllers REST, presenters, repositórios JPA que implementam aqueles "
        "gateways) → a classe `@SpringBootApplication`. Entidade de domínio nao e "
        "entidade JPA: se precisar persistir, escreva uma classe `@Entity` "
        "separada no adapter e converta.\n"
        f"{_INSTRUCOES_AMBIENTES}"
    ),
    exemplo_run_json=(
        '{"cmd": ["java", "-jar", "target/app.jar"], '
        '"port": 8080, "health_path": "/actuator/health"}'
    ),
)
