"""Leitura da configuração Spring da entrega, para o domínio julgar.

Faz só o trabalho sujo: acha os arquivos em `src/main/resources/`, entende as
três formas em que o Spring aceita a mesma coisa (YAML aninhado, `.properties`
com chave pontuada, e YAML multi-documento com `on-profile`) e devolve tudo
achatado em chaves pontuadas. A decisão do que é defeito mora em
`dominio/ambientes.py`.

Ler as três formas não é zelo excessivo: o executor é um LLM, e reprovar uma
configuração correta por ela ter sido escrita em `.properties` seria a squad
cobrando um estilo que ninguém pediu.
"""
from pathlib import Path

import yaml

from ..dominio import ambientes

DIR_RECURSOS = "src/main/resources"

# `application.yml`, `application-prod.yaml`, `application-hml.properties`...
_PREFIXO = "application"
_EXTENSOES_YAML = (".yml", ".yaml")
_EXTENSAO_PROPS = ".properties"

# Chave com que um documento YAML declara a qual perfil pertence — o jeito de
# escrever os três ambientes num arquivo só.
_CHAVE_PERFIL = "spring.config.activate.on-profile"


def _achatar(dados, prefixo: str = "") -> dict[str, str]:
    """`{spring: {datasource: {url: x}}}` vira `{"spring.datasource.url": "x"}`.

    Listas viram string: nenhuma das chaves que a medição olha é lista, e
    achatar índices só criaria chaves que ninguém consulta.
    """
    if not isinstance(dados, dict):
        return {prefixo: "" if dados is None else str(dados)} if prefixo else {}
    plano: dict[str, str] = {}
    for chave, valor in dados.items():
        caminho = f"{prefixo}.{chave}" if prefixo else str(chave)
        if isinstance(valor, dict):
            plano.update(_achatar(valor, caminho))
        else:
            plano[caminho] = "" if valor is None else str(valor)
    return plano


def _ler_properties(texto: str) -> dict[str, str]:
    plano: dict[str, str] = {}
    for linha in texto.splitlines():
        linha = linha.strip()
        if not linha or linha.startswith(("#", "!")) or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        plano[chave.strip()] = valor.strip()
    return plano


def _documentos_yaml(texto: str) -> list[dict[str, str]]:
    """Um YAML do Spring pode trazer vários documentos separados por `---`.

    Devolve cada um já achatado; quem chama decide de que perfil é cada um.
    Arquivo ilegível não derruba a medição: uma entrega com YAML quebrado já vai
    reprovar no boot, e levantar aqui trocaria um achado legível por um
    traceback.
    """
    try:
        return [_achatar(d) for d in yaml.safe_load_all(texto) if d is not None]
    except yaml.YAMLError:
        return []


def _perfil_do_nome(nome: str) -> str:
    """`application-prod` -> `prod`; `application` -> `` (comum)."""
    if nome == _PREFIXO:
        return ""
    return nome[len(_PREFIXO) + 1:] if nome.startswith(_PREFIXO + "-") else ""


def carregar(workspace: str) -> tuple[dict[str, str], dict[str, dict[str, str]], list[str]]:
    """Lê a configuração da entrega.

    Devolve `(comum, por_ambiente, arquivos_lidos)`.
    """
    raiz = Path(workspace) / DIR_RECURSOS
    comum: dict[str, str] = {}
    por_ambiente: dict[str, dict[str, str]] = {}
    lidos: list[str] = []

    if not raiz.is_dir():
        return comum, por_ambiente, lidos

    for arquivo in sorted(raiz.glob(f"{_PREFIXO}*")):
        if not arquivo.is_file():
            continue
        sufixo = arquivo.suffix.lower()
        if sufixo not in _EXTENSOES_YAML + (_EXTENSAO_PROPS,):
            continue
        try:
            texto = arquivo.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lidos.append(arquivo.name)
        perfil_do_arquivo = _perfil_do_nome(arquivo.stem)

        if sufixo == _EXTENSAO_PROPS:
            documentos = [_ler_properties(texto)]
        else:
            documentos = _documentos_yaml(texto)

        for doc in documentos:
            # O documento pode se declarar de outro perfil que não o do nome do
            # arquivo — e quando declara, é ele que manda.
            perfil = str(doc.pop(_CHAVE_PERFIL, "") or perfil_do_arquivo).strip()
            destino = por_ambiente.setdefault(perfil, {}) if perfil else comum
            destino.update(doc)

    return comum, por_ambiente, lidos


def medir_ambientes(workspace: str) -> dict:
    """Veredito objetivo sobre a configuração de ambientes da entrega.

    `ambientes_ok = nenhum achado`. Mesma forma de retorno de `executar_visual`
    e `executar_pentest`, para o nó do grafo tratar os três do mesmo jeito.
    """
    comum, por_ambiente, lidos = carregar(workspace)
    achados = ambientes.conferir(comum, por_ambiente)
    aprovado = not achados

    if aprovado:
        print(
            f">>> Ambientes: {len(lidos)} arquivo(s) de configuração, "
            f"{len(por_ambiente)} perfil(is) — verde."
        )
    else:
        print(f">>> Ambientes: {len(achados)} achado(s) — vermelho.")

    return {
        "ambientes_ok": aprovado,
        "achados_ambientes": [
            {"tipo": a.tipo, "ambiente": a.ambiente, "detalhe": a.detalhe}
            for a in achados
        ],
        "feedback_ambientes": ambientes.formatar_feedback(achados),
        "arquivos_config": lidos,
    }
