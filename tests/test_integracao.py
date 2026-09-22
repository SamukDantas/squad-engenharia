"""O contrato foi cumprido? O único juiz que olha para mais de uma entrega.

Todos os outros julgam um serviço isolado, e por isso nenhum vê a classe de
defeito que o paralelismo introduz: `room-service` devolve `capacity`,
`event-service` espera `totalSeats`, os dois passam nos próprios testes, e o
sistema não funciona. Testes verdes, revisão aprovada, e nada conversa.
"""
import pytest

from src.squad.adaptadores import contratos as adaptador
from src.squad.adaptadores import perfis
from src.squad.dominio import integracao

SERVICOS = ["event-service", "room-service"]

# Espelha o que a crew de contrato produz de verdade — inclusive a linha "Quem
# responde", que o prompt manda escrever. Ela não é enfeite: é o que diz de QUEM
# cobrar cada símbolo, e sem ela a conferência cai num regime mais fraco (ver
# `test_sem_quem_responde_a_conferencia_e_mais_fraca`).
CONTRATO = """
# Consulta de sala

- Quem chama: event-service
- Quem responde: room-service
- Método/Rota: GET /rooms/{roomId}/availability

Resposta 200: `roomId`, `totalSeats`, `availableAt`.
Erro 404 quando a sala não existe.
"""

CONTRATO_SEM_PROVEDOR = CONTRATO.replace("- Quem responde: room-service\n", "")


def simbolos(texto=CONTRATO, servicos=None):
    return integracao.simbolos_do_contrato(texto, servicos or SERVICOS)


# ---------- extração ----------

def test_extrai_a_rota_e_os_campos_da_secao():
    achados = {(s.termo, s.tipo) for s in simbolos()}
    assert ("/rooms/{roomId}/availability", "rota") in achados
    assert ("totalSeats", "campo") in achados
    assert ("availableAt", "campo") in achados


def test_atribui_o_simbolo_aos_dois_servicos_da_secao():
    totalSeats = next(s for s in simbolos() if s.termo == "totalSeats")
    assert totalSeats.servicos == ("event-service", "room-service")


def test_termo_generico_nao_vira_requisito():
    """`id` e `int` aparecem em qualquer código: cobrá-los daria um verde à toa
    em toda execução, o que é pior que não medir."""
    termos = {s.termo for s in simbolos("`id` e `int` e `name` entre event-service e room-service")}
    assert termos == set()


def test_secao_que_nao_nomeia_servico_conhecido_e_ignorada():
    """Sem saber de quem cobrar, o achado não teria a quem mandar corrigir."""
    assert simbolos("# Notas\n\nGET /health devolve `statusCode`.") == []


def test_sem_cabecalho_o_documento_inteiro_e_uma_secao():
    texto = "event-service chama room-service em GET /rooms e recebe `totalSeats`."
    termos = {s.termo for s in simbolos(texto)}
    assert {"/rooms", "totalSeats"} <= termos


# ---------- conferência ----------

def test_contrato_cumprido_nao_tem_achado():
    codigo = {
        "room-service": '@RequestMapping("/rooms") @GetMapping("/{roomId}/availability") '
                        'Long roomId; int totalSeats; String availableAt;',
        "event-service": 'get("/rooms/" + id + "/availability"); roomModel.totalSeats();',
    }
    assert integracao.conferir(simbolos(), codigo) == []


def test_o_desencontro_de_nome_e_pego():
    """O caso do livro, e a razão de o nó existir: o provedor chamou de
    `capacity` o que o contrato chamou de `totalSeats`."""
    codigo = {
        "room-service": '@RequestMapping("/rooms") @GetMapping("/{roomId}/availability") '
                        'int capacity; String availableAt; Long roomId;',
        "event-service": 'get("/rooms/" + id + "/availability"); totalSeats;',
    }
    achados = integracao.conferir(simbolos(), codigo)
    assert [(a.termo, a.servico) for a in achados] == [("totalSeats", "room-service")]
    assert "room-service responde" in achados[0].detalhe


def test_rota_ausente_no_provedor_e_pega():
    codigo = {
        "room-service": 'nada de rota aqui; totalSeats; availableAt; roomId',
        "event-service": 'get("/rooms/" + id + "/availability"); totalSeats;',
    }
    tipos = {a.tipo for a in integracao.conferir(simbolos(), codigo)}
    assert "rota_ausente" in tipos


@pytest.mark.parametrize("escrito", [
    '@GetMapping("/rooms/{roomId}/availability")',                    # tudo junto
    '@RequestMapping("/rooms") ... @GetMapping("/{roomId}/availability")',  # Spring parte em dois
    'app.get("/rooms/:roomId/availability")',                         # Express
    'router.get("/rooms/" + roomId + "/availability")',               # concatenado
])
def test_a_rota_e_reconhecida_em_qualquer_dialeto(escrito):
    """Cobrar o literal reprovaria implementações corretas.

    O caso do meio é o que quebrou numa execução real: o Spring parte a rota
    entre `@RequestMapping` na classe e `@GetMapping` no método, então `/rooms`
    existe no código e `/rooms/` não. Comparar substring com barra reprovou três
    serviços que honravam o contrato; o que se cobra são os segmentos fixos."""
    codigo = {n: escrito + "; totalSeats; availableAt; roomId" for n in SERVICOS}
    assert not [a for a in integracao.conferir(simbolos(), codigo) if a.tipo == "rota_ausente"]


def test_servico_que_nao_foi_construido_e_ignorado():
    """Numa execução com falha parcial, cobrar o contrato de quem nem existe
    produziria achados sobre um serviço que ninguém pode corrigir agora."""
    codigo = {"event-service": 'get("/rooms/" + id + "/availability"); totalSeats;'}
    achados = integracao.conferir(simbolos(), codigo)
    assert all(a.servico == "event-service" for a in achados)


# ---------- feedback ----------

def test_feedback_e_do_servico_que_vai_corrigir():
    codigo = {"room-service": "capacity",
              "event-service": 'get("/rooms/" + id + "/availability"); totalSeats;'}
    achados = integracao.conferir(simbolos(), codigo)
    texto = integracao.formatar_feedback(achados, "room-service")
    assert "totalSeats" in texto and "congelado" in texto
    assert integracao.formatar_feedback(achados, "event-service") == ""


# ---------- o adaptador ----------

def _entrega(tmp_path, nome, arquivos):
    ws = tmp_path / nome
    for rel, texto in arquivos.items():
        destino = ws / rel
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(texto, encoding="utf-8")
    return {"servico": nome, "stack": "java", "workspace": str(ws), "pronto": True}


def test_le_o_codigo_de_producao_e_ignora_a_suite(tmp_path):
    """Uma suíte que cita `totalSeats` num mock provaria que o QA leu o
    contrato, não que a entrega o implementa — e é a entrega que vai ser
    chamada."""
    entrega = _entrega(tmp_path, "room-service", {
        "src/main/java/Room.java": "int capacity;",
        "src/test/java/RoomTest.java": "int totalSeats; // so no teste",
    })
    codigo = adaptador.ler_codigo(entrega["workspace"], perfis.obter("java"))
    assert "capacity" in codigo and "totalSeats" not in codigo


def test_medicao_completa_reprova_o_desencontro(tmp_path):
    entregas = [
        _entrega(tmp_path, "room-service", {"src/main/java/Room.java":
            '@GetMapping("/rooms/{id}") int capacity; String availableAt; Long roomId;'}),
        _entrega(tmp_path, "event-service", {"src/main/java/Ev.java":
            'get("/rooms/" + id + "/availability"); totalSeats;'}),
    ]
    r = adaptador.medir_integracao(CONTRATO, entregas, perfis.obter)
    assert r["integracao_ok"] is False
    assert r["servicos_em_falta"] == ["room-service"]


def test_um_servico_so_nao_tem_contrato_a_conferir(tmp_path):
    entregas = [_entrega(tmp_path, "api", {"src/main/java/A.java": "x"})]
    assert adaptador.medir_integracao("", entregas, perfis.obter)["integracao_ok"] is True


def test_contrato_vazio_passa(tmp_path):
    entregas = [
        _entrega(tmp_path, "a", {"src/main/java/A.java": "x"}),
        _entrega(tmp_path, "b", {"src/main/java/B.java": "y"}),
    ]
    assert adaptador.medir_integracao("   ", entregas, perfis.obter)["integracao_ok"] is True


# ---------- os dois regimes de conferência ----------

def test_consumidor_nao_precisa_usar_todo_campo_da_resposta():
    """O erro que reprovou três serviços numa execução real: exigir o símbolo de
    TODOS os serviços da seção transforma cada campo da resposta num requisito
    para quem apenas chama. Um consumidor pode ignorar `availableAt` sem violar
    contrato nenhum — quem tem de expô-lo é o provedor."""
    codigo = {
        "room-service": '@GetMapping("/rooms/{roomId}/availability") roomId totalSeats availableAt',
        "event-service": 'get("/rooms/" + roomId + "/availability"); totalSeats;',
    }
    assert integracao.conferir(simbolos(), codigo) == []


def test_sem_quem_responde_a_conferencia_e_mais_fraca():
    """Sem provedor declarado não há como saber de quem cobrar, e a régua passa
    a ser "algum serviço usa o termo". Ainda pega o caso de ninguém ter
    implementado; deixa passar o desencontro entre os dois. É o preço de um
    contrato mal escrito, e está dito aqui para não virar surpresa."""
    sem = simbolos(CONTRATO_SEM_PROVEDOR)
    assert all(s.provedor is None for s in sem)
    codigo = {
        "room-service": "capacity",
        "event-service": 'get("/rooms/"); availability; roomId; totalSeats; availableAt',
    }
    assert integracao.conferir(sem, codigo) == []


def test_sem_provedor_ainda_pega_o_que_ninguem_implementou():
    sem = simbolos(CONTRATO_SEM_PROVEDOR)
    codigo = {"room-service": "nada", "event-service": "nada"}
    assert len(integracao.conferir(sem, codigo)) == len(sem)


def test_o_provedor_e_lido_do_contrato():
    for s in simbolos():
        assert s.provedor == "room-service", s


# ---------- só campo definido é cobrado (execução paralela eaad3461) ----------

CONTRATO_TIPADO = """# Contrato HTTP entre serviços

## Convenções

- JSON com campos em `camelCase`.
- `event-service` gera `eventId` e `sessionId` antes das alocações.

## Alocar sala

**Chama:** `event-service`
**Responde:** `room-service`
**Método e rota:** `POST /rooms/{roomId}/allocations`

- `allocationId: string(UUID)` — igual ao `sessionId`; chave de idempotência.
- `totalSeats: integer`
"""


def test_palavra_da_prosa_nao_vira_campo():
    """`camelCase` e `sessionId` só aparecem explicando algo; o event-service
    ficou retido por eles, com o contrato cumprido."""
    campos = {s.termo for s in integracao.simbolos_do_contrato(
        CONTRATO_TIPADO, ["event-service", "room-service"]) if s.tipo == "campo"}
    assert campos == {"allocationId", "totalSeats"}


def test_campo_definido_continua_cobrado():
    simbolos = integracao.simbolos_do_contrato(
        CONTRATO_TIPADO, ["event-service", "room-service"])
    achados = integracao.conferir(simbolos, {
        "event-service": "allocationId rooms allocations",
        "room-service": "rooms allocations allocationId",  # nenhum tem totalSeats
    })
    assert [a.termo for a in achados] == ["totalSeats"]


def test_contrato_sem_campo_tipado_usa_a_regra_antiga():
    """Contrato no formato antigo, com o campo solto entre crases, continua
    tendo os campos cobrados."""
    antigo = "## Sala\nroom-service responde `totalSeats` a event-service.\n"
    campos = {s.termo for s in integracao.simbolos_do_contrato(
        antigo, ["event-service", "room-service"])}
    assert campos == {"totalSeats"}
