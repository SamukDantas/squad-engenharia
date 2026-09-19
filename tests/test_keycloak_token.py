"""Token Keycloak do gateway corporativo e a escolha de provedor da squad.

Sem rede: o arquivo de token é um tmp_path e o POST de refresh é substituído.
"""
import json
import time

import httpx
import pytest

from src.squad import llm
from src.squad.adaptadores import keycloak_token as kc
from src.squad.adaptadores import opencode_cli

HORA_MS = 3600 * 1000


def _agora_ms() -> int:
    return int(time.time() * 1000)


@pytest.fixture
def arquivo(tmp_path, monkeypatch):
    caminho = tmp_path / "keycloak-token.json"
    monkeypatch.setenv("GATEWAY_TOKEN_FILE", str(caminho))
    kc._limpar_cache()
    yield caminho
    kc._limpar_cache()


def _gravar(caminho, **token):
    caminho.write_text(json.dumps(token), encoding="utf-8")


class _Refresh:
    """Substitui o POST ao Keycloak e conta quantas vezes foi chamado."""

    def __init__(self, status=200, corpo=None):
        self.status = status
        self.corpo = corpo or {"access_token": "novo", "refresh_token": "r2", "expires_in": 600}
        self.chamadas = []

    def __call__(self, url, data, timeout):
        self.chamadas.append((url, data))
        return httpx.Response(self.status, json=self.corpo, request=httpx.Request("POST", url))


# ---------- token ----------

def test_token_ainda_valido_nao_renova(arquivo, monkeypatch):
    _gravar(arquivo, access_token="atual", refresh_token="r1", expires_at=_agora_ms() + HORA_MS)
    refresh = _Refresh()
    monkeypatch.setattr(kc.httpx, "post", refresh)
    assert kc.token_valido() == "atual"
    assert refresh.chamadas == []


def test_token_perto_de_vencer_renova_e_preserva_o_verifier(arquivo, monkeypatch):
    """O plugin guarda o verifier do PKCE no mesmo arquivo; apagá-lo quebraria
    o próximo login dele."""
    _gravar(arquivo, access_token="velho", refresh_token="r1",
            expires_at=_agora_ms() + 60_000, code_verifier="v")
    refresh = _Refresh()
    monkeypatch.setattr(kc.httpx, "post", refresh)

    assert kc.token_valido() == "novo"
    url, dados = refresh.chamadas[0]
    assert url.endswith("/realms/example/protocol/openid-connect/token")
    assert dados == {"grant_type": "refresh_token", "client_id": "opencode", "refresh_token": "r1"}
    gravado = json.loads(arquivo.read_text(encoding="utf-8"))
    assert gravado["access_token"] == "novo"
    assert gravado["refresh_token"] == "r2"
    assert gravado["code_verifier"] == "v"
    assert not kc._dir_lock().exists(), "lock tem que ser liberado"


def test_refresh_sem_rotacao_mantem_o_refresh_token(arquivo, monkeypatch):
    _gravar(arquivo, access_token="velho", refresh_token="r1", expires_at=0)
    monkeypatch.setattr(kc.httpx, "post", _Refresh(corpo={"access_token": "novo", "expires_in": 600}))
    kc.token_valido()
    assert json.loads(arquivo.read_text(encoding="utf-8"))["refresh_token"] == "r1"


def test_outro_processo_renovou_durante_a_espera_do_lock(arquivo, monkeypatch):
    """Renovar de novo queimaria o refresh token que o plugin acabou de gravar."""
    _gravar(arquivo, access_token="velho", refresh_token="r1", expires_at=0)
    refresh = _Refresh()
    monkeypatch.setattr(kc.httpx, "post", refresh)

    entrar = kc._Lock.__enter__

    def entrar_depois_do_plugin(self):
        _gravar(arquivo, access_token="do-plugin", refresh_token="r9",
                expires_at=_agora_ms() + HORA_MS)
        return entrar(self)

    monkeypatch.setattr(kc._Lock, "__enter__", entrar_depois_do_plugin)
    assert kc.token_valido() == "do-plugin"
    assert refresh.chamadas == []


def test_lock_abandonado_e_quebrado(arquivo, monkeypatch):
    """Dono morto no meio do refresh não pode travar a squad para sempre."""
    import os
    _gravar(arquivo, access_token="velho", refresh_token="r1", expires_at=0)
    kc._dir_lock().mkdir()
    antigo = time.time() - 60
    os.utime(kc._dir_lock(), (antigo, antigo))
    monkeypatch.setattr(kc.httpx, "post", _Refresh())
    assert kc.token_valido() == "novo"


def test_sem_arquivo_falha_com_instrucao_de_login(arquivo):
    with pytest.raises(RuntimeError, match="login"):
        kc.token_valido()


def test_refresh_recusado_com_token_vencido_falha(arquivo, monkeypatch):
    _gravar(arquivo, access_token="velho", refresh_token="r1", expires_at=0)
    monkeypatch.setattr(kc.httpx, "post", _Refresh(400, {"error": "invalid_grant"}))
    with pytest.raises(RuntimeError, match="invalid_grant"):
        kc.token_valido()
    assert not kc._dir_lock().exists()


def test_refresh_recusado_com_token_ainda_vivo_usa_o_que_resta(arquivo, monkeypatch):
    """Mesma regra do plugin: 2 min de vida ainda servem a esta chamada."""
    _gravar(arquivo, access_token="quase", refresh_token="r1", expires_at=_agora_ms() + 120_000)
    monkeypatch.setattr(kc.httpx, "post", _Refresh(400, {"error": "invalid_grant"}))
    assert kc.token_valido() == "quase"


# ---------- escolha do provedor ----------

def test_provedor_padrao_e_gateway(monkeypatch):
    monkeypatch.delenv("LLM_PROVEDOR", raising=False)
    assert llm.provedor() == "gateway"


def test_provedor_invalido_falha_alto(monkeypatch):
    monkeypatch.setenv("LLM_PROVEDOR", "openai")
    with pytest.raises(ValueError, match="LLM_PROVEDOR"):
        llm.provedor()


def test_gateway_usa_gateway_e_interceptor(monkeypatch):
    monkeypatch.setenv("LLM_PROVEDOR", "gateway")
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.delenv("GATEWAY_BASE_URL", raising=False)
    modelo = llm.squad_llm()
    assert modelo.model == "gateway"
    assert modelo.base_url == llm.GATEWAY_BASE_URL
    assert isinstance(modelo.interceptor, llm._KeycloakInterceptor)


def test_interceptor_troca_o_authorization_a_cada_request(monkeypatch):
    tokens = iter(["t1", "t2"])
    monkeypatch.setattr(llm, "token_valido", lambda: next(tokens))
    interceptor = llm._KeycloakInterceptor()
    primeiro = interceptor.on_outbound(
        httpx.Request("POST", "https://x/chat", headers={"Authorization": "Bearer keycloak"}))
    segundo = interceptor.on_outbound(httpx.Request("POST", "https://x/chat"))
    assert primeiro.headers["Authorization"] == "Bearer t1"
    assert segundo.headers["Authorization"] == "Bearer t2"


def test_zen_continua_como_antes(monkeypatch):
    monkeypatch.setenv("LLM_PROVEDOR", "zen")
    monkeypatch.setenv("OPENCODE_API_KEY", "chave")
    monkeypatch.setenv("MODEL", "openai/deepseek-v4-pro")
    monkeypatch.delenv("OPENCODE_BASE_URL", raising=False)
    modelo = llm.squad_llm()
    assert modelo.model == "deepseek-v4-pro"
    assert modelo.base_url == llm.ZEN_BASE_URL
    assert modelo.interceptor is None


# ---------- executor OpenCode ----------

@pytest.fixture
def sem_config_global(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("OPENCODE_MCP_PERMITIDOS", raising=False)
    monkeypatch.delenv("OPENCODE_RUN_MODEL", raising=False)


def test_escopo_gateway_traz_provider_e_plugin(sem_config_global, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVEDOR", "gateway")
    monkeypatch.setenv("GATEWAY_OPENCODE_PLUGIN", "/cfg/keycloak-token.ts")
    config = json.loads(opencode_cli._config_escopo(str(tmp_path)))
    assert config["plugin"] == ["/cfg/keycloak-token.ts"]
    assert config["provider"]["gateway"]["options"]["baseURL"] == llm.GATEWAY_BASE_URL
    assert "gateway" in config["provider"]["gateway"]["models"]
    # O escopo que já existia continua fechado.
    assert config["mcp"]["github"] == {"enabled": False}
    assert config["agent"]["build"]["permission"]["external_directory"]["*"] == "deny"
    assert opencode_cli._modelo_executor() == "gateway/gateway"


def test_escopo_zen_nao_traz_provider(sem_config_global, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVEDOR", "zen")
    config = json.loads(opencode_cli._config_escopo(str(tmp_path)))
    assert "provider" not in config and "plugin" not in config
    assert opencode_cli._modelo_executor() == ""


def test_modelo_explicito_vence_o_padrao_do_provedor(monkeypatch):
    monkeypatch.setenv("LLM_PROVEDOR", "gateway")
    monkeypatch.setenv("OPENCODE_RUN_MODEL", "opencode-go/deepseek-v4-pro")
    assert opencode_cli._modelo_executor() == "opencode-go/deepseek-v4-pro"


def test_config_global_jsonc_e_aceito(tmp_path, monkeypatch):
    """Vírgula sobrando e comentário: o OpenCode aceita, e a squad derrubava
    toda execução por isso."""
    pasta = tmp_path / "opencode"
    pasta.mkdir()
    (pasta / "opencode.json").write_text(
        '{\n  // comentário\n  "plugin": ["a",],\n  "mcp": {"x": {"url": "http://h//p"},},\n}',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert opencode_cli._mcp_declarados() == ["x"]
