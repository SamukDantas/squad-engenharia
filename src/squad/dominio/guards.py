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
import re
from typing import Callable, Mapping

from . import vereditos
from .rotas import MAX_REVISOES_SUITE, e_teste_padrao

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


# ---------- correção inerte: quando o vermelho é do teste ----------

# O teto (`MAX_REVISOES_SUITE`, em rotas) é uma revisão: mais que isso vira
# dois agentes discordando em laço, e quem desempata é o humano.

# A classificação do vermelho (asserção x ambiente) mora em `vereditos`, que
# não importa nada do domínio: `rotas` também precisa dela para decidir se a
# suíte sobrevive a uma rodada de correção, e `rotas` não pode importar daqui.
falha_de_assercao = vereditos.falha_de_assercao


def destino_da_correcao_inerte(
    origem: str, saida_testes: str, falha_de_build: bool, revisoes_suite: int
) -> str:
    """Para onde vai uma rodada de correção que não mudou nada.

    O guard acima parte da premissa de que o executor ignorou o feedback. Nem
    sempre: medido na thread `33c19d49` (calculadora de juros, Next.js), 61 de
    62 testes passavam e o vermelho era um teste do QA que exigia 2 casas
    decimais com tolerância de 1e-6 num montante da ordem de 10^10 — onde o
    próprio ponto flutuante já erra mais que isso. O código estava certo, o
    executor é proibido de tocar na suíte, a rodada não mudou um byte, e o guard
    derrubou a execução como se o executor tivesse falhado.

    - `revisar_suite`: vermelho de asserção, e o QA ainda não revisou a suíte
      nesta situação. Ele recebe a falha e decide se o teste está errado.
    - `aprovacao_humana`: a revisão já aconteceu e o impasse continua. Dois
      agentes discordando não se resolvem com mais uma rodada.
    - `erro`: qualquer outro caso — o comportamento de antes.
    """
    if origem != "testes" or falha_de_build or not falha_de_assercao(saida_testes):
        return "erro"
    if revisoes_suite < MAX_REVISOES_SUITE:
        return "revisar_suite"
    return "aprovacao_humana"


def brief_revisao_suite(saida_testes: str) -> str:
    """O que o QA recebe quando a suíte é contestada."""
    return (
        "A suíte ficou VERMELHA e o desenvolvedor, depois de analisar a falha, "
        "NÃO alterou o código: a implementação foi mantida como estava. Isso "
        "sugere que o erro pode estar no TESTE, e não no código. Revise os "
        "testes que falharam abaixo contra a especificação:\n"
        "- se a expectativa ou a tolerância do teste estiver errada (por "
        "exemplo, precisão de ponto flutuante exigida além do que a spec pede, "
        "ou valor esperado diferente do que a spec define), corrija o teste;\n"
        "- se o teste estiver certo e o código errado, mantenha o teste "
        "exatamente como está.\n"
        "Não remova nem enfraqueça teste que cobre um critério de aceite da "
        "spec só para ficar verde.\n\n"
        f"Saída real dos testes:\n{saida_testes}"
    )


def motivo_impasse_suite(saida_testes: str) -> str:
    """Diagnóstico que acompanha o gate quando o impasse persiste."""
    return (
        "Impasse entre desenvolvimento e QA: a suíte continua vermelha por uma "
        "asserção, o desenvolvedor não alterou o código em duas rodadas e o QA "
        "manteve o teste depois de revisá-lo. Confira quem está certo antes de "
        "autorizar — o deploy só exige que a entrega compile.\n\n"
        f"Saída dos testes:\n{saida_testes[:1500]}"
    )


# ---------- teste escrito pelo executor ----------

def testes_do_executor(
    antes: set[str], depois: list[str], e_teste, spec: str = ""
) -> list[str]:
    """Arquivos de teste que surgiram durante a rodada de desenvolvimento.

    Quem escreve a suíte é o QA, e o executor recebe a proibição por escrito.
    Na thread `055e0f17` o Codex criou um `test_temperatura.py` na raiz, que
    entrou na cobertura como código sem teste e custou uma passada extra do QA.
    Prompt é pedido; esta função é o que o grafo usa para garantir a separação
    entre quem implementa e quem valida, com qualquer executor.

    Só arquivo NOVO conta: os testes do QA de rodadas anteriores já estavam
    lá, e apagá-los destruiria a suíte que o laço está tentando satisfazer.

    E só o que a spec NÃO pediu. Naquela mesma thread o arquivo não era
    desobediência: a spec listava `test_temperatura.py` como parte da entrega,
    e o QA escreveu um teste que o importava. Removê-lo quebrou a suíte (medido
    ao limpar o repositório publicado) — e dentro do laço seria pior: a regra
    apagaria o arquivo a cada rodada, a suíte ficaria vermelha, e nenhuma
    correção do executor sobreviveria. Arquivo citado pelo nome na spec é
    entrega, não rastro.
    """
    return sorted(
        a for a in depois
        if e_teste(a) and a not in antes and not _citado_na_spec(a, spec)
    )


def _citado_na_spec(caminho: str, spec: str) -> bool:
    """O caminho, ou o nome do arquivo, aparece na spec como palavra inteira.

    Pelo nome e não só pelo caminho: a spec costuma dizer `test_temperatura.py`
    sem pasta. Fronteira de palavra para `test_a.py` não casar dentro de
    `test_abc.py`.
    """
    if not spec:
        return False
    nome = caminho.rsplit("/", 1)[-1]
    return any(
        re.search(rf"(?<![\w./-]){re.escape(alvo)}(?![\w-])", spec)
        for alvo in {caminho, nome}
    )


# ---------- QA que mexe fora da suíte ----------

def fora_da_suite(
    antes: Mapping[str, str], depois: Mapping[str, str], e_teste
) -> tuple[list[str], list[str]]:
    """O que o QA mexeu fora da suíte: (alterados ou apagados, criados).

    `antes` e `depois` mapeiam caminho para o hash do conteúdo. Espelho da
    regra do executor (`testes_do_executor`): quem implementa não escreve
    teste, e quem testa não reescreve o código. Com o QA do CrewAI isso era
    garantido pelas ferramentas; com o QA rodando pelo Codex no workspace,
    ele pode editar qualquer arquivo, e sem esta conferência um QA que
    "corrige" o código para o teste passar apagaria a separação entre quem
    implementa e quem valida.
    """
    alterados = sorted(
        a for a, h in antes.items() if not e_teste(a) and depois.get(a) != h
    )
    criados = sorted(a for a in depois if a not in antes and not e_teste(a))
    return alterados, criados


# ---------- a mesma falha depois da correção ----------

# Linha que nomeia um teste vermelho: `FAIL  arq > descr > caso` (vitest) e
# `FAILED arq::caso - msg` (pytest). O que vem depois de " - " é a mensagem,
# que pode trazer valores que mudam de rodada para rodada.
_LINHA_FALHA = re.compile(r"^\s*FAIL(?:ED)?\s+(.+?)(?:\s+-\s+.*)?\s*$", re.M)


def testes_que_falharam(saida_testes: str) -> frozenset[str]:
    """Os nomes dos testes vermelhos de uma saída do runner."""
    return frozenset(m.group(1).strip() for m in _LINHA_FALHA.finditer(saida_testes or ""))


def falha_repetida(anterior: str, atual: str) -> bool:
    """Os mesmos testes falharam por asserção antes e depois da correção.

    Complementa `destino_da_correcao_inerte`, que só pega a rodada que não
    mudou nada. Medido na thread `9a86ddb0` (calculadora de juros): o QA
    calculou de cabeça 1234,56 x 1,025^3 = 1329,67 (o certo é 1329,49); o
    executor mexeu no código duas vezes tentando satisfazer um número
    impossível, a mesma asserção falhou nas duas e a execução bateu o teto de
    correção sem nunca devolver a suíte ao QA.
    """
    atuais = testes_que_falharam(atual)
    return (
        bool(atuais)
        and atuais == testes_que_falharam(anterior)
        and falha_de_assercao(atual)
    )


def brief_falha_repetida(saida_testes: str) -> str:
    """O que o QA recebe quando a mesma falha sobrevive a uma correção."""
    return (
        "A suíte ficou VERMELHA nos MESMOS testes antes e depois de uma rodada "
        "de correção do desenvolvedor: ele mudou o código e a asserção "
        "continuou falhando igual. O erro provavelmente está no TESTE. Para "
        "cada teste abaixo, confira a expectativa contra a especificação:\n"
        "- valor esperado que a spec não fixa literalmente precisa ser "
        "recalculado com um comando (por exemplo `node -e` ou `python -c`), "
        "nunca de cabeça;\n"
        "- seletor que não encontra o elemento (resultado vazio) precisa "
        "mirar o que a página realmente renderiza;\n"
        "- se o teste estiver certo e o código errado, mantenha o teste "
        "exatamente como está.\n"
        "Não remova nem enfraqueça teste que cobre um critério de aceite da "
        "spec só para ficar verde.\n\n"
        f"Saída real dos testes:\n{saida_testes}"
    )
