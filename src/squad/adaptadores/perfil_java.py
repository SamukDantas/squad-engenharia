"""Perfil da stack Java: Maven offline, JUnit 5, JaCoCo.

É a stack mais hostil ao `--network none`: o Maven baixa não só as dependências
como os próprios plugins do build, e sem rede ele falha antes de compilar. A
imagem resolve isso com um `~/.m2` pré-populado, e o `-o` (offline) transforma
qualquer coisa faltando num erro imediato — em vez de uma espera longa até o
timeout de rede.

A consequência é que o `pom.xml` da entrega não é livre: ele precisa declarar
exatamente as versões que estão no repositório local da imagem. O executor
recebe o pom no prompt, e um desvio vira erro de build — que é a mesma régua de
sempre, exit code manda.
"""
import re
from pathlib import Path

from ..portas.perfil import Cobertura, PerfilStack

RELATORIO = "jacoco.xml"

_LIBS = "a biblioteca padrão do JDK, JUnit 5 e JaCoCo (versões fixadas no pom)"

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


def _e_teste(caminho: str) -> bool:
    """Convenção do Maven: `src/test/java/...`. `src/main/java/` é entrega."""
    return caminho.startswith("src/test/")


def _preparar(workspace: str) -> None:
    """Nada a escrever: a configuração do build é o `pom.xml` da própria
    entrega, e escrevê-lo é trabalho do executor."""


# Sem `comando_build`: `mvn test` já compila antes de testar, e um passo
# separado só pagaria outro start de container e de JVM. A contrapartida é que
# um erro de compilação chega ao dev rotulado como falha de teste — mas a saída
# do Maven diz "COMPILATION ERROR" na primeira linha, então a instrução não se
# perde.
def _comando_container(dir_saida: str) -> str:
    # `-o` offline: dependência faltando falha na hora, em vez de esperar o
    # timeout de rede num container que nem tem rota para fora.
    # `-B` batch: sem cores nem barra de progresso no log.
    return (
        f"cp -r /src /app/projeto && cd /app/projeto && "
        f"mvn -o -B test && "
        f"cp target/site/jacoco/{RELATORIO} {dir_saida}/{RELATORIO}"
    )


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
    comando_container=_comando_container,
    comando_host=_comando_host,
    relatorio_cobertura=RELATORIO,
    ler_cobertura=_ler_cobertura,
    preparar_workspace=_preparar,
    permite_host=False,
    # JVM + Maven: o mais lento dos três, mesmo com o repositório já populado.
    timeout_testes=420,
    memoria="1500m",
    e_teste=_e_teste,
    ignorar_no_workspace=frozenset({"target", ".gradle", ".mvn", ".squad", ".git"}),
    extensoes_descartaveis=frozenset({".class", ".jar"}),
    gitignore_entrega=_GITIGNORE,
    imagem_alvo="squad-target-java:latest",
    dockerfile_alvo="Dockerfile.target-java",
    libs_permitidas=_LIBS,
    instrucoes_qa=(
        "Escreva testes JUnit 5 em `src/test/java/`, espelhando o pacote "
        "da classe testada. Eles serão executados por `mvn test`."
    ),
    instrucoes_executor=(
        f"- Java 21 apenas, usando {_LIBS}. Não acrescente dependência nenhuma "
        "ao pom: o repositório Maven da imagem é offline, e o que não estiver "
        "lá falha o build na hora.\n"
        "- Use EXATAMENTE este `pom.xml`, sem alterar versões:\n"
        f"```xml\n{_POM_REFERENCIA}```\n"
        "- Código de produção em `src/main/java/`. NÃO escreva nada em "
        "`src/test/java/` — a suíte é de outro agente da equipe de qualidade."
    ),
    exemplo_run_json=(
        '{"cmd": ["java", "-cp", "target/classes", "com.squad.Main"], '
        '"port": 8080, "health_path": "/health"}'
    ),
)
