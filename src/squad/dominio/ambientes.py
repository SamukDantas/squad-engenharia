"""Configuração de banco por ambiente: o que reprova uma entrega Spring.

Quarta camada de veredito por medição, ao lado dos testes, do pentest e da
renderização — e a mais barata das quatro, porque é leitura de arquivo, sem
container e sem LLM.

Existe porque há uma classe de defeito que nenhuma das outras alcança: os testes
rodam no perfil `test`, com H2 em memória, e ficam verdes independentemente do
que os perfis de homologação e produção digam. Uma entrega pode passar em tudo e
trazer `ddl-auto: create-drop` em produção — que apaga o banco no boot — ou a
senha do banco escrita no YAML, que vai para o repositório junto com o código.
Nenhum desses aparece numa suíte verde.

Funções puras de um mapeamento de configuração para achados. A leitura do disco
e o parsing de YAML ficam em `adaptadores/config_spring.py`: aqui não se sabe o
que é um arquivo.
"""
from dataclasses import dataclass
from typing import Mapping

# Os três ambientes que a entrega precisa declarar. `test` não entra: ele é do
# ciclo da squad (H2 na jaula sem rede), não da arquitetura da entrega.
AMBIENTES_EXIGIDOS = ("dev", "hml", "prod")

# Ambientes onde um banco de brinquedo é defeito. `dev` fica de fora de
# propósito: H2 ali é escolha legítima, e é o que roda dentro da jaula.
AMBIENTES_DE_VERDADE = ("hml", "prod")

# `validate` confere o schema contra as entidades e falha se divergir; `none`
# não toca em nada. Qualquer outro valor deixa o Hibernate alterar o schema no
# boot — `create-drop` apaga o banco, `update` migra sem revisão.
DDL_SEGURO = frozenset({"validate", "none"})

CHAVE_URL = "spring.datasource.url"
CHAVE_DDL = "spring.jpa.hibernate.ddl-auto"
CHAVES_SEGREDO = ("spring.datasource.password", "spring.datasource.username")

_MARCA_H2 = "jdbc:h2"


@dataclass(frozen=True)
class Achado:
    """Um problema medido, com o ambiente onde ele está."""
    tipo: str
    ambiente: str
    detalhe: str


def _efetiva(comum: Mapping[str, str], perfil: Mapping[str, str]) -> dict[str, str]:
    """Configuração que o Spring de fato enxerga: o perfil sobrepõe o comum.

    Sem isso a medição acusaria falta de URL em toda entrega que declara
    `spring.datasource.url: ${DB_URL}` no `application.yml` e só especializa o
    resto por perfil — que é um jeito correto de escrever, não um defeito.
    """
    return {**dict(comum), **dict(perfil)}


def _e_referencia(valor: str) -> bool:
    """`${VAR}` ou `${VAR:padrao}` — valor que vem do ambiente, não do arquivo."""
    return "${" in valor


def conferir(
    comum: Mapping[str, str],
    por_ambiente: Mapping[str, Mapping[str, str]],
) -> list[Achado]:
    """Mede a configuração dos três ambientes e devolve os achados.

    Lista vazia é aprovação. A ordem é estável — ausência primeiro, porque um
    ambiente que não existe explica sozinho os achados que viriam depois dele.
    """
    achados: list[Achado] = []

    for nome in AMBIENTES_EXIGIDOS:
        if nome not in por_ambiente:
            achados.append(Achado(
                "ambiente_ausente", nome,
                f"não existe configuração para o perfil '{nome}'",
            ))

    efetivas = {
        nome: _efetiva(comum, perfil)
        for nome, perfil in por_ambiente.items()
    }

    for nome in AMBIENTES_DE_VERDADE:
        config = efetivas.get(nome)
        if config is None:
            continue
        url = str(config.get(CHAVE_URL, ""))
        if _MARCA_H2 in url.lower():
            achados.append(Achado(
                "banco_de_brinquedo", nome,
                f"usa H2 ({url}) — H2 é o banco da jaula de teste, não de "
                f"{'produção' if nome == 'prod' else 'homologação'}",
            ))

    config_prod = efetivas.get("prod")
    if config_prod is not None:
        # Ausente não é achado: sem `ddl-auto` declarado e com banco não
        # embarcado, o padrão do Spring Boot já é não mexer no schema.
        ddl = str(config_prod.get(CHAVE_DDL, "")).strip().lower()
        if ddl and ddl not in DDL_SEGURO:
            achados.append(Achado(
                "ddl_perigoso", "prod",
                f"ddl-auto='{ddl}' deixa o Hibernate alterar o schema no boot; "
                f"use {' ou '.join(sorted(DDL_SEGURO))}",
            ))

    for nome in sorted(efetivas):
        # Credencial ao lado de banco embarcado não é segredo: `sa` é o usuário
        # padrão do H2 em memória, o banco morre com o processo e não há o que
        # vazar. Medido numa execução real — a regra sem esta exceção reprovou
        # um `username: sa` em dev e custou ao ramo uma rodada de correção
        # inteira para trocar um valor que não protege nada.
        if _MARCA_H2 in str(efetivas[nome].get(CHAVE_URL, "")).lower():
            continue
        for chave in CHAVES_SEGREDO:
            valor = str(efetivas[nome].get(chave, "")).strip()
            if valor and not _e_referencia(valor):
                achados.append(Achado(
                    "credencial_literal", nome,
                    f"{chave} tem valor literal no arquivo; use ${{VARIAVEL}} "
                    "para que a credencial venha do ambiente",
                ))

    urls = {
        nome: str(efetivas[nome].get(CHAVE_URL, "")).strip()
        for nome in AMBIENTES_EXIGIDOS
        if nome in efetivas
    }
    distintas = {u for u in urls.values() if u}
    if len(urls) > 1 and len(distintas) == 1:
        achados.append(Achado(
            "ambientes_indistintos", ", ".join(sorted(urls)),
            f"todos apontam para a mesma URL ({distintas.pop()}) — três perfis "
            "com o mesmo banco não são três ambientes",
        ))

    return achados


def formatar_feedback(achados: list[Achado]) -> str:
    """Brief de correção determinístico, no formato do pentest e do visual: o
    achado é a medição, não uma paráfrase dela."""
    if not achados:
        return ""
    linhas = [
        "A configuração de ambientes reprovou a entrega. Os problemas abaixo "
        "foram lidos dos arquivos de configuração. Corrija-os em "
        "`src/main/resources/`:",
        "",
    ]
    for i, a in enumerate(achados, 1):
        linhas.append(f"{i}. [{a.tipo}] ({a.ambiente}) {a.detalhe}.")
    return "\n".join(linhas)
