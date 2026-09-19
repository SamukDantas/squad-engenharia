"""Token Keycloak do gateway corporativo, lido e renovado sem o OpenCode no meio.

O gateway de IA que o cliente cedeu não autentica pela `apiKey` do config: quem
autentica é um bearer token do Keycloak, que o plugin `keycloak-token.ts` do
OpenCode obtém por login interativo (Authorization Code + PKCE via Google) e
grava em `~/.config/opencode/.opencode/keycloak-token.json`.

A squad roda sem terminal nem navegador, então não faz o primeiro login: ela
reaproveita o token que o OpenCode já salvou e o renova pelo `refresh_token`.
Sem token nem refresh possível, falha na hora com a instrução de login — em vez
de o plugin, dentro do executor, abrir um navegador que ninguém vê e esperar
5 minutos até desistir.

O refresh usa **o mesmo lock** do plugin (um diretório criado com `mkdir`), e
relê o arquivo depois de pegá-lo. O executor OpenCode e os agentes Python
renovam o mesmo token; se o Keycloak rotacionar o refresh token, dois refreshes
simultâneos invalidariam um ao outro.
"""
import json
import os
import time
from pathlib import Path

import httpx

ISSUER_PADRAO = "https://auth.example.com/realms/example"
CLIENT_ID_PADRAO = "opencode"

# Os mesmos números do plugin: renova faltando 5 min, e o lock é considerado
# abandonado depois de 10s sem dono — o refresh em si leva milissegundos.
LIMIAR_REFRESH_MS = 5 * 60 * 1000
LOCK_ABANDONADO_S = 10.0
LOCK_ESPERA_MAX_S = 30.0
LOCK_POLL_S = 0.05

INSTRUCAO_LOGIN = (
    "Faça o login uma vez no OpenCode com o config do gateway corporativo, num terminal "
    "interativo — o navegador abre para o login Google:\n"
    "    PowerShell: $env:OPENCODE_CONFIG = \"$HOME\\.config\\opencode\\keys\\gateway.json\"; opencode\n"
    "    bash:       OPENCODE_CONFIG=~/.config/opencode/keys/gateway.json opencode\n"
    "Depois disso a squad renova o token sozinha. Para seguir no Zen enquanto "
    "isso, use LLM_PROVEDOR=zen no .env."
)

# Cache por processo: o interceptor pede o token a cada request, e reler o
# disco a cada chamada seria desperdício enquanto o token ainda vale.
_cache: dict | None = None


def _dir_opencode() -> Path:
    return Path.home() / ".config" / "opencode" / ".opencode"


def arquivo_token() -> Path:
    bruto = (os.getenv("GATEWAY_TOKEN_FILE") or "").strip()
    return Path(bruto).expanduser() if bruto else _dir_opencode() / "keycloak-token.json"


def _dir_lock() -> Path:
    return arquivo_token().parent / ".keycloak-refresh.lock"


def _url_token() -> str:
    issuer = (os.getenv("KEYCLOAK_ISSUER") or ISSUER_PADRAO).strip().rstrip("/")
    return f"{issuer}/protocol/openid-connect/token"


def _agora_ms() -> int:
    return int(time.time() * 1000)


def _fresco(token: dict | None) -> bool:
    return bool(
        token
        and token.get("access_token")
        and token.get("expires_at", 0) > _agora_ms() + LIMIAR_REFRESH_MS
    )


def _ler() -> dict | None:
    try:
        dados = json.loads(arquivo_token().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return dados if isinstance(dados, dict) else None


def _gravar(token: dict) -> None:
    """Escrita atômica, como a do plugin: quem lê no meio vê o arquivo antigo
    inteiro ou o novo inteiro, nunca JSON truncado."""
    destino = arquivo_token()
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_name(f"{destino.name}.{os.getpid()}.{_agora_ms()}.tmp")
    tmp.write_text(json.dumps(token, indent=2), encoding="utf-8")
    os.replace(tmp, destino)


class _Lock:
    """O lock entre processos do plugin: existir o diretório é estar trancado."""

    def __enter__(self):
        lock = _dir_lock()
        lock.parent.mkdir(parents=True, exist_ok=True)
        inicio = time.monotonic()
        while True:
            try:
                lock.mkdir()
                return self
            except FileExistsError:
                try:
                    idade = time.time() - lock.stat().st_mtime
                except OSError:
                    continue  # sumiu entre o mkdir e o stat: tenta de novo
                if idade > LOCK_ABANDONADO_S or time.monotonic() - inicio > LOCK_ESPERA_MAX_S:
                    # Dono morto no meio do refresh: o plugin quebra o lock
                    # pelo mesmo critério, então os dois lados concordam.
                    try:
                        lock.rmdir()
                    except OSError:
                        pass
                    continue
                time.sleep(LOCK_POLL_S)

    def __exit__(self, *_):
        try:
            _dir_lock().rmdir()
        except OSError:
            pass


def _renovar(token: dict) -> dict:
    try:
        r = httpx.post(
            _url_token(),
            data={
                "grant_type": "refresh_token",
                "client_id": os.getenv("KEYCLOAK_CLIENT_ID", CLIENT_ID_PADRAO),
                "refresh_token": token["refresh_token"],
            },
            timeout=30,
        )
    except httpx.HTTPError as e:
        raise RuntimeError(f"Refresh do token Keycloak falhou: {e}\n{INSTRUCAO_LOGIN}") from e
    if r.status_code != 200:
        # O corpo do Keycloak diz o motivo (ex.: invalid_grant, sessão expirada)
        # e não carrega segredo nenhum.
        raise RuntimeError(
            f"Keycloak recusou o refresh do token ({r.status_code}): "
            f"{r.text[:300]}\n{INSTRUCAO_LOGIN}"
        )
    dados = r.json()
    return {
        "access_token": dados["access_token"],
        # Keycloak pode não rotacionar: sem refresh novo, o antigo continua valendo.
        "refresh_token": dados.get("refresh_token") or token["refresh_token"],
        "expires_at": _agora_ms() + int(dados.get("expires_in") or 3600) * 1000,
        # O plugin guarda o verifier do PKCE no mesmo arquivo; não é nosso para apagar.
        **({"code_verifier": token["code_verifier"]} if token.get("code_verifier") else {}),
    }


def token_valido() -> str:
    """Access token com pelo menos 5 min de vida, renovando se preciso."""
    global _cache
    if _fresco(_cache):
        return _cache["access_token"]

    token = _ler()
    if _fresco(token):
        _cache = token
        return token["access_token"]

    if not token or not token.get("refresh_token"):
        if token and token.get("access_token") and token.get("expires_at", 0) > _agora_ms():
            return token["access_token"]  # sem como renovar, mas ainda vale
        raise RuntimeError(
            f"Sem token Keycloak utilizável em {arquivo_token()}.\n{INSTRUCAO_LOGIN}"
        )

    with _Lock():
        # Outro processo — o plugin no executor, ou um ramo paralelo — pode ter
        # renovado enquanto esperávamos o lock. Renovar de novo queimaria o
        # refresh token que ele acabou de gravar.
        token = _ler() or token
        if _fresco(token):
            _cache = token
            return token["access_token"]
        try:
            novo = _renovar(token)
        except RuntimeError:
            # Mesma regra do plugin: refresh recusado com o token ainda vivo
            # usa o que resta dele, e a próxima chamada tenta de novo.
            if token.get("access_token") and token.get("expires_at", 0) > _agora_ms():
                return token["access_token"]
            raise
        _gravar(novo)
    _cache = novo
    return novo["access_token"]


def _limpar_cache() -> None:
    """Para os testes: cada caso começa sem memória do anterior."""
    global _cache
    _cache = None
