"""Leitura das entregas para conferir o contrato entre elas.

Faz só o trabalho sujo: varre o workspace de cada serviço, junta o código-fonte
(sem testes, sem artefato de build) e entrega ao domínio. A decisão do que é
desencontro mora em `dominio/integracao.py`.

Os testes ficam de fora de propósito. Uma suíte que cita `totalSeats` num mock
provaria que o QA leu o contrato, não que a entrega o implementa — e é a
entrega que os outros serviços vão chamar.
"""
from pathlib import Path

from ..dominio import integracao

# Teto de leitura por serviço. O que se procura são nomes de campo e caminhos de
# rota, que aparecem nas primeiras dezenas de KB de qualquer entrega desta
# escala; ler sem limite só abriria a porta para uma entrega gigante consumir a
# memória do processo que orquestra as outras.
LIMITE_POR_SERVICO = 400_000

# Extensões que carregam contrato: código e configuração declarativa.
_FONTES = {".java", ".py", ".ts", ".tsx", ".js", ".jsx", ".yml", ".yaml", ".json", ".properties"}


def ler_codigo(workspace: str, perfil) -> str:
    """O código de produção de um serviço, concatenado."""
    raiz = Path(workspace)
    if not raiz.is_dir():
        return ""
    pedacos: list[str] = []
    total = 0
    for caminho in sorted(raiz.rglob("*")):
        if not caminho.is_file() or caminho.suffix.lower() not in _FONTES:
            continue
        rel = str(caminho.relative_to(raiz)).replace("\\", "/")
        if perfil.ignorar_no_workspace.intersection(caminho.relative_to(raiz).parts):
            continue
        if perfil.e_teste(rel):
            continue
        try:
            texto = caminho.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        pedacos.append(texto)
        total += len(texto)
        if total >= LIMITE_POR_SERVICO:
            break
    return "\n".join(pedacos)


def medir_integracao(contratos: str, entregas: list[dict], obter_perfil) -> dict:
    """Veredito objetivo sobre o contrato entre os serviços entregues.

    Mesma forma de retorno dos outros nós de medição. `integracao_ok` é falso
    quando algum serviço não usa um símbolo que o contrato lhe atribuiu.
    """
    servicos = [e["servico"] for e in entregas]
    if not contratos.strip() or len(servicos) < 2:
        return {
            "integracao_ok": True, "achados_integracao": [],
            "simbolos": 0, "servicos_em_falta": [],
        }

    simbolos = integracao.simbolos_do_contrato(contratos, servicos)
    codigo = {
        e["servico"]: ler_codigo(e["workspace"], obter_perfil(e["stack"]))
        for e in entregas
    }
    achados = integracao.conferir(simbolos, codigo)

    if not simbolos:
        print(">>> Integração: o contrato não nomeou símbolo verificável.")
    elif achados:
        print(f">>> Integração: {len(achados)} desencontro(s) em {len(simbolos)} símbolo(s).")
        for a in achados[:10]:
            print(f"    - {a.detalhe}")
    else:
        print(f">>> Integração: {len(simbolos)} símbolo(s) do contrato conferem — verde.")

    return {
        "integracao_ok": not achados,
        "achados_integracao": [
            {"tipo": a.tipo, "termo": a.termo, "servico": a.servico, "detalhe": a.detalhe}
            for a in achados
        ],
        "simbolos": len(simbolos),
        "servicos_em_falta": sorted({a.servico for a in achados}),
    }
