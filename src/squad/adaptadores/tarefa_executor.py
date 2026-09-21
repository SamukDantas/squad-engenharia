"""O que todo executor de desenvolvimento precisa, qualquer que seja o CLI.

Nasceu quando o Codex entrou como segundo executor: a tarefa que o executor
recebe é da squad, não do CLI, e duplicá-la faria as duas cópias divergirem em
silêncio — o executor novo entregaria sob regras diferentes das do antigo, e a
comparação entre eles mediria a diferença dos prompts, não dos executores.

Fica aqui o que é comum: o texto da tarefa, onde ele é gravado e como a saída
do CLI é ecoada. Cada adaptador cuida do que é dele — flags, jaula e a forma
de falhar do seu binário.
"""
import sys
from pathlib import Path

# Diretório de trabalho da squad dentro do workspace: instruções para o
# executor, fora da entrega (ignorado nas varreduras e no deploy).
DIR_SQUAD = ".squad"
ARQUIVO_TAREFA = "tarefa.md"

LIMITE_SAIDA = 4_000    # chars da saída ecoados no log

# Uma linha só, apontando para o arquivo: no Windows o binário costuma ser um
# shim `.CMD`/`.ps1`, e argumento multilinha é truncado na primeira quebra de
# linha (a spec sumia, e specs longas ainda esbarrariam no limite de tamanho
# de argumento).
PROMPT = (
    f"Leia o arquivo {DIR_SQUAD}/{ARQUIVO_TAREFA} e execute exatamente a "
    "tarefa descrita nele, respeitando todas as regras obrigatórias."
)


def relatavel(texto: str) -> str:
    """Saída do executor legível em qualquer stdout.

    Os CLIs imprimem setas, ícones e box-drawing. Quando o stdout é
    redirecionado (arquivo, pipe, CI), o Python no Windows usa a codepage
    local — cp1252, que não tem esses caracteres — e o `print` levanta
    UnicodeEncodeError. Medido na thread `20051ccc`: o nó rodou os 244s,
    escreveu a entrega inteira e morreu ao **relatar** o que tinha feito,
    derrubando o grafo com a entrega já em disco.

    Vale tanto para o eco no log quanto para as mensagens de erro, que a
    `main` também imprime — relatar a falha não pode ser uma segunda falha.
    """
    codificacao = sys.stdout.encoding or "utf-8"
    return texto.encode(codificacao, "replace").decode(codificacao, "replace")


def instrucoes(spec: str, feedback_qa: str, perfil) -> str:
    correcao = ""
    if feedback_qa and feedback_qa.strip():
        correcao = (
            "\n\n## Rodada de correção\n"
            "Antes de qualquer outra coisa, leia os arquivos existentes e "
            "corrija os pontos abaixo:\n\n"
            f"{feedback_qa}\n"
        )
    return (
        "# Tarefa de implementação\n\n"
        "Implemente a especificação abaixo neste diretório, escrevendo "
        "arquivos reais.\n\n"
        "## Regras obrigatórias\n"
        # O bloco específico da stack — linguagem, libs disponíveis e onde a
        # suíte mora — vem do perfil. O resto vale para qualquer entrega.
        f"{perfil.instrucoes_executor}\n"
        "- Escreva ou atualize o `README.md` com a estrutura e as instruções "
        "de execução.\n"
        "- NÃO deixe arquivos de rastro na entrega (saída de comandos, logs, "
        "relatórios de teste). Se precisar salvar algo transitório, use o "
        "diretório `.squad/`.\n"
        "- SÓ SE a entrega for um servidor ou aplicação HTTP (API, página web), "
        "escreva `.squad/run.json` declarando como subi-la, para o pipeline de "
        "segurança atacá-la. Formato exato:\n"
        f"  `{perfil.exemplo_run_json}`\n"
        "  Use o comando real desta entrega (módulo e porta corretos). "
        "`health_path` é uma rota que responde com a app no ar.\n"
        "  Se a especificação pede só um módulo, biblioteca ou CLI, NÃO crie "
        "servidor HTTP nem `run.json`: servidor que ninguém pediu fica sem "
        "teste e derruba a cobertura da entrega.\n"
        "- Mantenha o escopo estritamente na especificação.\n\n"
        "## Especificação\n\n"
        f"{spec}\n"
        f"{correcao}"
    )


def gravar_tarefa(workspace: str, spec: str, feedback_qa: str, perfil) -> Path:
    """Escreve `.squad/tarefa.md` e devolve o caminho."""
    dir_squad = Path(workspace) / DIR_SQUAD
    dir_squad.mkdir(parents=True, exist_ok=True)
    arquivo = dir_squad / ARQUIVO_TAREFA
    arquivo.write_text(instrucoes(spec, feedback_qa, perfil), encoding="utf-8")
    return arquivo
