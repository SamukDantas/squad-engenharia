"""Guards de entrega: o nó de desenvolvimento produziu trabalho de verdade?

Os dois guards existem porque o executor é externo e sinaliza sucesso pelo exit
code — que classifica o **processo**, não o **trabalho**. Cada um cobre uma
metade do problema, e nenhuma cobre a outra:

- `conferir_entrega` pega a rodada que não escreveu nada;
- `conferir_correcao` pega a rodada que escreveu exatamente o que já existia.

Ambos devolvem a mensagem de falha, ou `None` quando está tudo bem. Quem chama
levanta — assim a decisão é testável sem `pytest.raises` e sem disco: o dado de
entrada é um manifesto, não um caminho.
"""
from typing import Callable, Mapping

from .rotas import e_teste_padrao

# Manifesto da entrega: caminho relativo -> o arquivo tem conteúdo?
# "Ter conteúdo" é o critério, não "existir": um `__init__.py` de 0 bytes é
# estrutura, não trabalho.
Manifesto = Mapping[str, bool]

# Impressão da entrega: caminho relativo -> hash do conteúdo.
Impressao = Mapping[str, str]


def conferir_entrega(
    manifesto: Manifesto,
    workspace: str,
    e_teste: Callable[[str], bool] = e_teste_padrao,
) -> str | None:
    """Guard de entrega vazia: o nó de desenvolvimento não pode concluir sem ter
    produzido nada.

    Medido duas vezes em execução real: o OpenCode CLI abortou (permissão negada
    numa rodada, saldo insuficiente na outra), saiu com 0, e a squad seguiu
    pagando 855s de QA, dois guards de critérios e um pytest em cima de um
    workspace vazio, ainda entrando em outra rodada de desenvolvimento.

    Falha alto em vez de rotear de volta: não há o que corrigir sem código, e a
    causa é sempre de configuração (modelo sem tool calling, credencial,
    permissão) — repetir a mesma rodada só repete a mesma falha (item 26).

    Arquivo de teste não conta como entrega: é do QA, e o executor é proibido de
    tocá-lo. Arquivo **vazio** também não: a primeira versão deste guard contava
    qualquer caminho fora da suíte, e o mesmo executor que não escreveu nada numa
    rodada deixou um `app/__init__.py` de 0 bytes na seguinte — estrutura sem
    conteúdo, que teria passado batido.
    """
    if any(tem_conteudo and not e_teste(a) for a, tem_conteudo in manifesto.items()):
        return None

    if not manifesto:
        achado = "nada"
    elif all(e_teste(a) for a in manifesto):
        achado = f"apenas {len(manifesto)} arquivo(s) de teste"
    else:
        achado = f"{len(manifesto)} arquivo(s), todos vazios ou só de teste"
    return (
        f"O executor de desenvolvimento concluiu sem escrever a entrega: {achado} "
        f"em {workspace}. Exit code 0 não prova trabalho feito — confira se o "
        "modelo do executor suporta tool calling, se as credenciais têm saldo e "
        "se a saída acima registra permissão negada. Nada a corrigir sem código: "
        "o grafo para aqui em vez de pagar QA e pytest em cima do vazio."
    )


def conferir_correcao(antes: Impressao, depois: Impressao, origem: str) -> str | None:
    """Guard de rodada de correção: corrigir sem mudar nada não é corrigir.

    O guard de entrega vazia cobre a rodada **inicial** — se não há arquivo, o
    executor não trabalhou. Numa rodada de correção ele não cobre nada: o
    workspace já está cheio da rodada anterior, e um executor que ignorou o
    feedback por completo passa como nó concluído.

    Medido na thread `ac0c7d4e` (agendador de tarefas): o revisor reprovou por
    uma condição de corrida entre cancelamento e execução, a rodada de correção
    rodou 109,5s, **não escreveu um único arquivo**, o pytest seguinte devolveu
    cobertura idêntica ao dígito e a segunda revisão repetiu o apontamento
    palavra por palavra — porque o código era o mesmo. O orçamento de revisões
    acabou ali, sem que nenhuma tentativa de conserto tivesse existido.

    Compara conteúdo e não mtime: executor que reescreve o arquivo idêntico não
    corrigiu nada.
    """
    if antes != depois:
        return None
    return (
        f"A rodada de correção (origem: {origem}) terminou sem alterar a entrega: "
        f"{len(depois)} arquivo(s), todos byte a byte idênticos aos de antes. "
        "O executor recebeu o feedback e não o acionou — repetir a rodada tende "
        "a repetir o resultado, e o veredito seguinte vai reproduzir o mesmo "
        "apontamento sobre o mesmo código. Confira se o feedback chegou "
        "acionável ao executor e se o modelo suporta tool calling. O checkpoint "
        "preserva o progresso: `main.py --thread <id>` retoma."
    )
