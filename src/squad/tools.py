"""Ferramentas de arquivo confinadas ao workspace da execução.

No lugar das FileWriterTool/FileReadTool genéricas do crewai_tools: aqui todo
caminho é resolvido contra a raiz do workspace e tentativas de escapar dela
(caminho absoluto ou ..) são recusadas — a jaula mínima possível sem Docker.
"""
from pathlib import Path

from crewai.tools import BaseTool
from pydantic import BaseModel, Field


def _resolver(workspace: str, caminho: str) -> Path:
    raiz = Path(workspace).resolve()
    alvo = (raiz / caminho).resolve()
    if alvo != raiz and raiz not in alvo.parents:
        raise ValueError(
            f"caminho '{caminho}' aponta para fora do workspace — use apenas "
            "caminhos relativos à raiz do projeto, ex.: 'app/main.py'."
        )
    return alvo


class _ArgsEscrever(BaseModel):
    caminho: str = Field(description="Caminho do arquivo, relativo à raiz do workspace")
    conteudo: str = Field(description="Conteúdo completo do arquivo")


class EscreverArquivoTool(BaseTool):
    name: str = "escrever_arquivo"
    description: str = (
        "Escreve (cria ou sobrescreve) um arquivo no workspace do projeto. "
        "Use caminhos relativos, ex.: 'app/main.py' ou 'tests/test_api.py'."
    )
    args_schema: type[BaseModel] = _ArgsEscrever
    workspace: str

    def _run(self, caminho: str, conteudo: str) -> str:
        try:
            alvo = _resolver(self.workspace, caminho)
        except ValueError as e:
            return f"RECUSADO: {e}"
        alvo.parent.mkdir(parents=True, exist_ok=True)
        alvo.write_text(conteudo, encoding="utf-8")
        rel = alvo.relative_to(Path(self.workspace).resolve())
        return f"Arquivo escrito: {str(rel).replace(chr(92), '/')}"


class _ArgsLer(BaseModel):
    caminho: str = Field(description="Caminho do arquivo, relativo à raiz do workspace")


class LerArquivoTool(BaseTool):
    name: str = "ler_arquivo"
    description: str = "Lê o conteúdo de um arquivo existente no workspace do projeto."
    args_schema: type[BaseModel] = _ArgsLer
    workspace: str

    def _run(self, caminho: str) -> str:
        try:
            alvo = _resolver(self.workspace, caminho)
        except ValueError as e:
            return f"RECUSADO: {e}"
        if not alvo.is_file():
            return f"Arquivo não encontrado: {caminho}"
        return alvo.read_text(encoding="utf-8", errors="replace")


class _SemArgs(BaseModel):
    pass


class ListarArquivosTool(BaseTool):
    name: str = "listar_arquivos"
    description: str = "Lista todos os arquivos existentes no workspace do projeto."
    args_schema: type[BaseModel] = _SemArgs
    workspace: str

    def _run(self) -> str:
        raiz = Path(self.workspace).resolve()
        arquivos = sorted(
            str(p.relative_to(raiz)).replace("\\", "/")
            for p in raiz.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        )
        return "\n".join(arquivos) or "(workspace vazio)"


def ferramentas_workspace(workspace: str) -> list[BaseTool]:
    return [
        EscreverArquivoTool(workspace=workspace),
        LerArquivoTool(workspace=workspace),
        ListarArquivosTool(workspace=workspace),
    ]
