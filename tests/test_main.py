"""O ponto de entrada compila.

Nenhum teste importa o `main.py` (ele carrega `.env` e injeta o truststore ao
ser importado), então um erro de sintaxe ali passava pela suíte inteira e só
aparecia ao rodar a squad — foi assim que um `print` com quebra de linha
literal derrubou a retomada de uma thread antes do primeiro nó.
"""
import py_compile
from pathlib import Path


def test_main_compila():
    py_compile.compile(
        str(Path(__file__).resolve().parents[1] / "main.py"), doraise=True
    )
