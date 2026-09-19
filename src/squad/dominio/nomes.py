"""Nome curto do repositório de uma entrega.

O slug do pedido inteiro dava nomes como `criar-um-modulo-python-de-conversao-de`:
o verbo do pedido, artigos, e um corte no meio da ideia. Quem abre a lista de
repositórios quer saber O QUE o projeto é — `conversor-temperatura`.

Um LLM dá esse nome melhor que qualquer regra, mas resposta de LLM é texto livre
(RESILIENCIA.md, item 7): aqui ela só é aceita se já tiver a forma de um nome de
repositório. O resto cai numa regra determinística, que é pior mas nunca falha.
"""
import re
import unicodedata

MAX_PALAVRAS = 4
MAX_CHARS = 40
PALAVRAS_FALLBACK = 3

_NOME_VALIDO = re.compile(rf"^[a-z0-9]+(?:-[a-z0-9]+){{0,{MAX_PALAVRAS - 1}}}$")

# O que não diz o que o projeto é: o verbo do pedido e as palavras de ligação.
_VAZIAS = {
    "criar", "crie", "cria", "criacao", "desenvolver", "desenvolva",
    "implementar", "implemente", "fazer", "faca", "construir", "construa",
    "gerar", "gere", "montar", "monte", "escrever", "escreva", "preciso",
    "quero", "queremos", "um", "uma", "uns", "umas", "o", "a", "os", "as",
    "de", "do", "da", "dos", "das", "para", "pra", "com", "em", "no", "na",
    "nos", "nas", "e", "que", "simples", "novo", "nova",
}


def _ascii_kebab(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto)
    texto = texto.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", texto).strip("-")


def nome_da_resposta(resposta: object) -> str | None:
    """Nome curto se a resposta do LLM já for um; None para qualquer outra coisa.

    Tolera o enfeite comum (aspas, crase, negrito, "Nome:", ponto final) e
    normaliza acento e espaço. Não tolera frase: se sobrar mais de
    `MAX_PALAVRAS` palavras, o modelo explicou em vez de nomear, e um corte
    arbitrário daria o mesmo problema que se queria resolver.
    """
    linhas = [l for l in str(resposta or "").strip().splitlines() if l.strip()]
    if not linhas:
        return None
    linha = re.sub(r"^\s*(nome|name)\s*:\s*", "", linhas[0], flags=re.I)
    nome = _ascii_kebab(linha.strip(" \t`'\"*._"))
    if len(nome) > MAX_CHARS or not _NOME_VALIDO.match(nome):
        return None
    return nome


def nome_do_pedido(pedido: str) -> str | None:
    """Fallback sem LLM: as primeiras palavras que dizem algo, da primeira frase.

    "Criar um modulo Python de conversao de temperaturas: ..." vira
    `modulo-python-conversao`. None quando não sobra nada (só verbo e artigo,
    ou outro alfabeto) — quem chama decide o último recurso.
    """
    frase = re.split(r"[.:;!?\n]", pedido or "", maxsplit=1)[0]
    palavras = [p for p in _ascii_kebab(frase).split("-") if p and p not in _VAZIAS]
    nome = "-".join(palavras[:PALAVRAS_FALLBACK])
    return nome[:MAX_CHARS].strip("-") or None
