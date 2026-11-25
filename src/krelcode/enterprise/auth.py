"""Standards-based OIDC/SSO with Authorization Code + PKCE.

The implementation intentionally uses only the Python standard library.  It
supports the normal browser flow (discovery, local loopback callback, state,
nonce and PKCE) and a small injectable transport so tests never need network
access.  Token material is kept only in a private token store and is never
included in agent history, audit events, logs or prompt context.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import os
import queue
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Tuple


class AuthError(RuntimeError):
    """OIDC discovery, callback, token or identity validation failure."""


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    raw = str(value or "")
    padding = "=" * ((4 - len(raw) % 4) % 4)
    try:
        return base64.urlsafe_b64decode(raw + padding)
    except (ValueError, TypeError) as exc:
        raise AuthError("JWT base64url 编码无效") from exc


def _json_b64(value: str) -> Mapping[str, Any]:
    try:
        parsed = json.loads(_b64url_decode(value).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthError("JWT JSON 字段无效") from exc
    if not isinstance(parsed, Mapping):
        raise AuthError("JWT JSON 字段必须是 object")
    return parsed


def _sha256_b64url(value: str) -> str:
    return _b64url_encode(hashlib.sha256(value.encode("ascii")).digest())


def _issuer_key(issuer: str) -> str:
    return hashlib.sha256(str(issuer).encode("utf-8")).hexdigest()[:32]


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    raise AuthError(f"SSO 布尔值无效: {value!r}")


@dataclass(frozen=True)
class SSOConfig:
    enabled: bool = False
    required: bool = False
    issuer: str = ""
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    client_secret_env: str = ""
    scopes: Tuple[str, ...] = ("openid", "profile", "email")
    redirect_uri: str = ""
    token_dir: Path = field(default_factory=lambda: Path.home() / ".krelcode" / "auth")
    timeout: float = 30.0
    callback_timeout: float = 300.0
    allow_http_issuer: bool = False

    @classmethod
    def from_mapping(
        cls,
        value: Optional[Mapping[str, Any]],
        *,
        required: bool = False,
        home: Optional[Path] = None,
    ) -> "SSOConfig":
        raw = value or {}
        if not isinstance(raw, Mapping):
            raise AuthError("enterprise.sso 必须是 TOML table")
        enabled = _as_bool(raw.get("enabled"), bool(raw.get("issuer")))
        scopes_raw = raw.get("scopes", ("openid", "profile", "email"))
        scopes = ((scopes_raw,) if isinstance(scopes_raw, str) else tuple(
            str(item).strip() for item in scopes_raw or () if str(item).strip()))
        token_dir_raw = str(raw.get("token_dir", "") or "").strip()
        token_dir = (Path(token_dir_raw).expanduser() if token_dir_raw else
                     (Path(home or Path.home()).expanduser() / ".krelcode" / "auth"))
        try:
            timeout = max(1.0, float(raw.get("timeout", 30.0)))
            callback_timeout = max(5.0, float(raw.get("callback_timeout", 300.0)))
        except (TypeError, ValueError) as exc:
            raise AuthError("SSO timeout/callback_timeout 必须是数字") from exc
        return cls(
            enabled=enabled,
            required=bool(required or _as_bool(raw.get("required"), False)),
            issuer=str(raw.get("issuer", "") or "").strip().rstrip("/"),
            client_id=str(raw.get("client_id", "") or "").strip(),
            client_secret=str(raw.get("client_secret", "") or ""),
            client_secret_env=str(raw.get("client_secret_env", "") or "").strip(),
            scopes=tuple(dict.fromkeys(scopes or ("openid",))),
            redirect_uri=str(raw.get("redirect_uri", "") or "").strip(),
            token_dir=token_dir,
            timeout=timeout,
            callback_timeout=callback_timeout,
            allow_http_issuer=_as_bool(raw.get("allow_http_issuer"), False),
        )

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.issuer or not self.client_id:
            raise AuthError("SSO 必须配置 issuer 和 client_id")
        parsed = urllib.parse.urlparse(self.issuer)
        if parsed.scheme not in ({"https", "http"} if self.allow_http_issuer else {"https"}):
            raise AuthError("SSO issuer 必须是 HTTPS（测试环境显式设置 allow_http_issuer）")
        if self.redirect_uri:
            redirect = urllib.parse.urlparse(self.redirect_uri)
            if redirect.scheme not in {"http", "https"} or not redirect.netloc:
                raise AuthError("SSO redirect_uri 必须是合法的 http(s) URL")
            if redirect.scheme == "http" and redirect.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise AuthError("OIDC callback 只允许 localhost HTTP")

    @property
    def token_path(self) -> Path:
        return self.token_dir / f"{_issuer_key(self.issuer)}.json"


@dataclass
class TokenSet:
    access_token: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)
    id_token: str = field(default="", repr=False)
    token_type: str = "Bearer"
    expires_at: float = 0.0
    scope: str = ""
    issuer: str = ""
    subject: str = ""
    display_name: str = ""
    email_hash: str = ""
    auth_method: str = "oidc"

    @property
    def expired(self) -> bool:
        return bool(self.expires_at and self.expires_at <= time.time())

    @property
    def valid(self) -> bool:
        return bool(self.access_token or self.id_token) and not self.expired

    def redaction_secrets(self) -> Tuple[str, ...]:
        return tuple(item for item in (
            self.access_token, self.refresh_token, self.id_token,
        ) if item)

    def as_record(self) -> Dict[str, Any]:
        """Non-secret metadata suitable for status/audit."""
        return {
            "authenticated": self.valid,
            "issuer": self.issuer,
            "subject": self.subject,
            "display_name": self.display_name,
            "email_hash": self.email_hash,
            "expires_at": self.expires_at,
            "auth_method": self.auth_method,
            "expired": self.expired,
        }

    def to_store(self) -> Dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "scope": self.scope,
            "issuer": self.issuer,
            "subject": self.subject,
            "display_name": self.display_name,
            "email_hash": self.email_hash,
            "auth_method": self.auth_method,
        }

    @classmethod
    def from_store(cls, value: Mapping[str, Any]) -> "TokenSet":
        return cls(
            access_token=str(value.get("access_token", "") or ""),
            refresh_token=str(value.get("refresh_token", "") or ""),
            id_token=str(value.get("id_token", "") or ""),
            token_type=str(value.get("token_type", "Bearer") or "Bearer"),
            expires_at=float(value.get("expires_at", 0) or 0),
            scope=str(value.get("scope", "") or ""),
            issuer=str(value.get("issuer", "") or ""),
            subject=str(value.get("subject", "") or ""),
            display_name=str(value.get("display_name", "") or ""),
            email_hash=str(value.get("email_hash", "") or ""),
            auth_method=str(value.get("auth_method", "oidc") or "oidc"),
        )


@dataclass(frozen=True)
class Principal:
    subject: str
    issuer: str = ""
    display_name: str = ""
    email_hash: str = ""
    expires_at: float = 0.0
    auth_method: str = "oidc"

    def as_record(self) -> Dict[str, Any]:
        return {
            "subject": self.subject,
            "issuer": self.issuer,
            "display_name": self.display_name,
            "email_hash": self.email_hash,
            "expires_at": self.expires_at,
            "auth_method": self.auth_method,
        }


@dataclass(frozen=True)
class AuthStatus:
    authenticated: bool
    principal: Optional[Principal] = None
    reason: str = ""
    source: str = "token-store"

    def as_record(self) -> Dict[str, Any]:
        base = {
            "authenticated": self.authenticated,
            "reason": self.reason,
            "source": self.source,
        }
        if self.principal is not None:
            base.update(self.principal.as_record())
        return base


class OIDCTransport(Protocol):
    async def get_json(
        self, url: str, *, headers: Optional[Mapping[str, str]] = None,
    ) -> Mapping[str, Any]:
        ...

    async def post_form(
        self, url: str, data: Mapping[str, str],
        *, headers: Optional[Mapping[str, str]] = None,
    ) -> Mapping[str, Any]:
        ...


class UrllibOIDCTransport:
    """Tiny zero-dependency transport with bounded timeouts."""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = max(1.0, float(timeout))

    async def get_json(
        self, url: str, *, headers: Optional[Mapping[str, str]] = None,
    ) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._get_json, url, dict(headers or {}))

    def _get_json(self, url: str, headers: Mapping[str, str]) -> Mapping[str, Any]:
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except (urllib.error.URLError, OSError) as exc:
            raise AuthError(f"OIDC HTTP GET 失败: {type(exc).__name__}") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthError("OIDC endpoint 返回了非 JSON 响应") from exc
        if not isinstance(value, Mapping):
            raise AuthError("OIDC endpoint 响应必须是 object")
        return value

    async def post_form(
        self, url: str, data: Mapping[str, str],
        *, headers: Optional[Mapping[str, str]] = None,
    ) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._post_form, url, dict(data), dict(headers or {}))

    def _post_form(
        self, url: str, data: Mapping[str, str], headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        body = urllib.parse.urlencode(dict(data)).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, headers={**dict(headers),
                                     "Content-Type": "application/x-www-form-urlencoded"},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except (urllib.error.URLError, OSError) as exc:
            raise AuthError(f"OIDC HTTP POST 失败: {type(exc).__name__}") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthError("OIDC token endpoint 返回了非 JSON 响应") from exc
        if not isinstance(value, Mapping):
            raise AuthError("OIDC token endpoint 响应必须是 object")
        return value


@dataclass(frozen=True)
class OIDCDiscovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str = ""
    end_session_endpoint: str = ""

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any], *, expected_issuer: str,
        allow_http: bool = False,
    ) -> "OIDCDiscovery":
        issuer = str(value.get("issuer", "") or "").rstrip("/")
        if expected_issuer and issuer != expected_issuer.rstrip("/"):
            raise AuthError("OIDC discovery issuer 与配置不一致")
        endpoints = {}
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            endpoint = str(value.get(key, "") or "")
            if not endpoint:
                raise AuthError(f"OIDC discovery 缺少 {key}")
            parsed = urllib.parse.urlparse(endpoint)
            allowed_schemes = {"https", "http"} if allow_http else {"https"}
            if parsed.scheme not in allowed_schemes or not parsed.netloc:
                raise AuthError(f"OIDC discovery {key} 不是合法 URL")
            endpoints[key] = endpoint
        return cls(
            issuer=issuer,
            authorization_endpoint=endpoints["authorization_endpoint"],
            token_endpoint=endpoints["token_endpoint"],
            jwks_uri=endpoints["jwks_uri"],
            userinfo_endpoint=str(value.get("userinfo_endpoint", "") or ""),
            end_session_endpoint=str(value.get("end_session_endpoint", "") or ""),
        )


class TokenStore:
    """Private JSON token store; only metadata is ever returned to callers."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()

    def load(self) -> Optional[TokenSet]:
        if self.path.is_symlink():
            raise AuthError("拒绝读取 symlink SSO token store")
        if not self.path.is_file():
            return None
        try:
            os.chmod(self.path, 0o600)
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AuthError(f"SSO token store 读取失败: {type(exc).__name__}") from exc
        if not isinstance(raw, Mapping):
            raise AuthError("SSO token store 内容必须是 object")
        try:
            return TokenSet.from_store(raw)
        except (TypeError, ValueError) as exc:
            raise AuthError("SSO token store 字段无效") from exc

    def save(self, tokens: TokenSet) -> None:
        if self.path.is_symlink():
            raise AuthError("拒绝写入 symlink SSO token store")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(
                json.dumps(tokens.to_store(), ensure_ascii=False),
                encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise AuthError(f"SSO token store 写入失败: {type(exc).__name__}") from exc

    def delete(self) -> bool:
        try:
            self.path.unlink(missing_ok=True)
            return True
        except OSError as exc:
            raise AuthError(f"SSO token store 删除失败: {type(exc).__name__}") from exc


class _CallbackServer:
    """One-shot localhost callback receiver for the authorization code."""

    def __init__(self, timeout: float = 300.0) -> None:
        self.timeout = timeout
        self.results: "queue.Queue[Dict[str, str]]" = queue.Queue(maxsize=1)
        self.server: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.error: Optional[BaseException] = None
        self.callback_path = "/callback"

    def start(self, redirect_uri: str = "") -> str:
        outer = self
        bind_port = 0
        if redirect_uri:
            parsed_redirect = urllib.parse.urlparse(redirect_uri)
            self.callback_path = parsed_redirect.path or "/callback"
            if parsed_redirect.port:
                bind_port = parsed_redirect.port

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != outer.callback_path:
                    self.send_error(404)
                    return
                values = urllib.parse.parse_qs(parsed.query)
                result = {key: values.get(key, [""])[0] for key in values}
                try:
                    outer.results.put_nowait(result)
                except queue.Full:
                    pass
                body = (
                    "<html><body><h2>Krelcode SSO received.</h2>"
                    "<p>You can close this window.</p></body></html>"
                )
                payload = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, fmt: str, *args: Any) -> None:
                return

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", bind_port), Handler)
        except OSError as exc:
            raise AuthError("无法启动 OIDC 本地回调服务器") from exc
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True,
            name="krelcode-oidc-callback")
        self.thread.start()
        host, port = self.server.server_address[:2]
        if isinstance(host, bytes): host = host.decode("ascii", "replace")
        return f"http://{host}:{port}/callback"

    def receive(self) -> Mapping[str, str]:
        try:
            return self.results.get(timeout=self.timeout)
        except queue.Empty as exc:
            raise AuthError("OIDC 登录超时，未收到回调") from exc

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread is not None:
            self.thread.join(timeout=2)
            self.thread = None


class OIDCClient:
    """OIDC Authorization Code + PKCE client and local identity gate."""

    def __init__(
        self,
        config: SSOConfig,
        *,
        transport: Optional[OIDCTransport] = None,
        environ: Optional[Mapping[str, str]] = None,
        store: Optional[TokenStore] = None,
    ) -> None:
        config.validate()
        self.config = config
        self.transport = transport or UrllibOIDCTransport(config.timeout)
        self.environ = os.environ if environ is None else environ
        self.store = store or TokenStore(config.token_path)
        self._discovery: Optional[OIDCDiscovery] = None
        self._jwks: Optional[Mapping[str, Any]] = None

    async def discovery(self, *, refresh: bool = False) -> OIDCDiscovery:
        if self._discovery is not None and not refresh:
            return self._discovery
        url = f"{self.config.issuer}/.well-known/openid-configuration"
        raw = await self.transport.get_json(url)
        self._discovery = OIDCDiscovery.from_mapping(
            raw, expected_issuer=self.config.issuer,
            allow_http=self.config.allow_http_issuer)
        return self._discovery

    def new_pkce(self) -> tuple[str, str]:
        verifier = _b64url_encode(secrets.token_bytes(48))
        challenge = _sha256_b64url(verifier)
        return verifier, challenge

    def build_authorization_url(
        self,
        discovery: OIDCDiscovery,
        *,
        redirect_uri: str,
        state: str,
        nonce: str,
        code_challenge: str,
    ) -> str:
        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.config.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        separator = "&" if "?" in discovery.authorization_endpoint else "?"
        return discovery.authorization_endpoint + separator + urllib.parse.urlencode(params)

    async def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        nonce: str,
        redirect_uri: str,
        expected_state: str = "",
        returned_state: str = "",
    ) -> TokenSet:
        if expected_state and not secrets.compare_digest(
                str(expected_state), str(returned_state)):
            raise AuthError("OIDC state 校验失败")
        discovery = await self.discovery()
        data = {
            "grant_type": "authorization_code",
            "code": str(code),
            "redirect_uri": redirect_uri,
            "client_id": self.config.client_id,
            "code_verifier": code_verifier,
        }
        client_secret = self.config.client_secret
        if self.config.client_secret_env:
            client_secret = str(self.environ.get(
                self.config.client_secret_env, "") or client_secret)
        if client_secret:
            data["client_secret"] = client_secret
        response = await self.transport.post_form(
            discovery.token_endpoint, data)
        access_token = str(response.get("access_token", "") or "")
        id_token = str(response.get("id_token", "") or "")
        if not access_token or not id_token:
            raise AuthError("OIDC token endpoint 未返回 access_token/id_token")
        claims = await self.verify_id_token(id_token, nonce=nonce)
        expires_in = response.get("expires_in")
        try:
            expires_at = time.time() + float(expires_in) if expires_in is not None else 0.0
        except (TypeError, ValueError):
            expires_at = 0.0
        subject = str(claims.get("sub", "") or "")
        if not subject:
            raise AuthError("OIDC ID token 缺少 sub")
        email = str(claims.get("email", "") or "")
        email_hash = hashlib.sha256(email.encode("utf-8")).hexdigest() if email else ""
        tokens = TokenSet(
            access_token=access_token,
            refresh_token=str(response.get("refresh_token", "") or ""),
            id_token=id_token,
            token_type=str(response.get("token_type", "Bearer") or "Bearer"),
            expires_at=expires_at,
            scope=str(response.get("scope", " ".join(self.config.scopes)) or ""),
            issuer=self.config.issuer,
            subject=subject,
            display_name=str(
                claims.get("name") or claims.get("preferred_username") or ""),
            email_hash=email_hash,
            auth_method="oidc",
        )
        self.store.save(tokens)
        return tokens

    async def verify_id_token(
        self, token: str, *, nonce: str = "",
    ) -> Mapping[str, Any]:
        parts = str(token).split(".")
        if len(parts) != 3:
            raise AuthError("ID token 不是 JWT")
        header = _json_b64(parts[0])
        claims = _json_b64(parts[1])
        if header.get("alg") != "RS256":
            raise AuthError("只接受 RS256 ID token")
        key = await self._select_jwk(str(header.get("kid", "") or ""))
        signature = _b64url_decode(parts[2])
        signed = f"{parts[0]}.{parts[1]}".encode("ascii")
        if not _verify_rs256(signature, signed, key):
            raise AuthError("ID token 签名无效")
        issuer = str(claims.get("iss", "") or "").rstrip("/")
        if issuer != self.config.issuer.rstrip("/"):
            raise AuthError("ID token issuer 不匹配")
        audience = claims.get("aud")
        if isinstance(audience, str):
            audience_ok = audience == self.config.client_id
        elif isinstance(audience, list):
            audience_ok = self.config.client_id in audience
        else:
            audience_ok = False
        if not audience_ok:
            raise AuthError("ID token audience 不匹配")
        try:
            expiry = float(claims["exp"])
        except (TypeError, ValueError):
            raise AuthError("ID token exp 无效")
        except KeyError:
            raise AuthError("ID token 缺少 exp")
        if expiry <= time.time():
            raise AuthError("ID token 已过期")
        not_before = claims.get("nbf")
        if not_before is not None:
            try:
                if float(not_before) > time.time() + 60:
                    raise AuthError("ID token 尚未生效")
            except (TypeError, ValueError):
                raise AuthError("ID token nbf 无效")
        if nonce:
            if not secrets.compare_digest(str(claims.get("nonce", "")), str(nonce)):
                raise AuthError("ID token nonce 校验失败")
        return claims

    async def _select_jwk(self, kid: str) -> Dict[str, str]:
        if self._jwks is None:
            discovery = await self.discovery()
            self._jwks = await self.transport.get_json(discovery.jwks_uri)
        keys = self._jwks.get("keys") if isinstance(self._jwks, Mapping) else None
        if not isinstance(keys, list):
            raise AuthError("JWKS 缺少 keys")
        candidates = [
            item for item in keys
            if isinstance(item, Mapping)
            and item.get("kty") == "RSA"
            and (not kid or str(item.get("kid", "")) == kid)
        ]
        if len(candidates) != 1:
            raise AuthError("找不到唯一的 RSA JWKS key")
        item = candidates[0]
        if not item.get("n") or not item.get("e"):
            raise AuthError("JWKS RSA key 缺少 n/e")
        return {"n": str(item["n"]), "e": str(item["e"])}

    def status(self) -> AuthStatus:
        env_token = str(
            self.environ.get("KRELCODE_SSO_ACCESS_TOKEN", "") or "").strip()
        if env_token:
            return AuthStatus(
                authenticated=True,
                principal=Principal(
                    subject="environment",
                    issuer=self.config.issuer,
                    auth_method="environment",
                ),
                source="environment",
            )
        try:
            tokens = self.store.load()
        except AuthError as exc:
            return AuthStatus(False, reason=str(exc))
        if tokens is None:
            return AuthStatus(False, reason="未登录 SSO")
        if tokens.issuer and tokens.issuer.rstrip("/") != self.config.issuer.rstrip("/"):
            return AuthStatus(False, reason="SSO token issuer 不匹配")
        principal = Principal(
            subject=tokens.subject or "unknown",
            issuer=tokens.issuer or self.config.issuer,
            display_name=tokens.display_name,
            email_hash=tokens.email_hash,
            expires_at=tokens.expires_at,
            auth_method=tokens.auth_method,
        )
        if not tokens.valid:
            return AuthStatus(False, principal=principal, reason="SSO token 已过期或无效")
        return AuthStatus(True, principal=principal)

    async def require_authenticated(self) -> Principal:
        status = self.status()
        if not status.authenticated or status.principal is None:
            raise AuthError(f"SSO 预检失败: {status.reason or '未认证'}")
        return status.principal

    async def login(
        self,
        *,
        open_browser: bool = True,
        callback_timeout: Optional[float] = None,
        authorization_url_callback: Optional[Callable[[str], object]] = None,
    ) -> TokenSet:
        """Run the browser callback flow and persist a private token set."""
        discovery = await self.discovery()
        callback = _CallbackServer(
            timeout=callback_timeout or self.config.callback_timeout)
        try:
            if self.config.redirect_uri:
                redirect_uri = self.config.redirect_uri
                callback.start(redirect_uri)
            else:
                redirect_uri = callback.start()
            state = _b64url_encode(secrets.token_bytes(24))
            nonce = _b64url_encode(secrets.token_bytes(24))
            verifier, challenge = self.new_pkce()
            url = self.build_authorization_url(
                discovery,
                redirect_uri=redirect_uri,
                state=state,
                nonce=nonce,
                code_challenge=challenge,
            )
            if authorization_url_callback is not None:
                authorization_url_callback(url)
            if open_browser:
                try:
                    webbrowser.open(url, new=2)
                except Exception:
                    pass
            result = await asyncio.to_thread(callback.receive)
            if result.get("error"):
                raise AuthError(
                    f"OIDC 授权失败: {result.get('error')}")
            code = str(result.get("code", "") or "")
            if not code:
                raise AuthError("OIDC 回调缺少 code")
            return await self.exchange_code(
                code=code,
                code_verifier=verifier,
                nonce=nonce,
                redirect_uri=redirect_uri,
                expected_state=state,
                returned_state=str(result.get("state", "") or ""),
            )
        finally:
            callback.close()

    def logout(self) -> bool:
        return self.store.delete()

    def redaction_secrets(self) -> Tuple[str, ...]:
        secrets_list: list[str] = []
        env_token = str(
            self.environ.get("KRELCODE_SSO_ACCESS_TOKEN", "") or "").strip()
        if env_token:
            secrets_list.append(env_token)
        try:
            tokens = self.store.load()
        except AuthError:
            return tuple(secrets_list)
        if tokens is not None:
            secrets_list.extend(tokens.redaction_secrets())
        return tuple(dict.fromkeys(secrets_list))

    def authorization_url_for_tests(
        self, discovery: OIDCDiscovery, *,
        redirect_uri: str = "http://127.0.0.1:8765/callback",
    ) -> str:
        verifier, challenge = self.new_pkce()
        _ = verifier
        return self.build_authorization_url(
            discovery,
            redirect_uri=redirect_uri,
            state="state",
            nonce="nonce",
            code_challenge=challenge,
        )


# Backward-friendly aliases used by composition roots and tests.
AuthConfig = SSOConfig
AuthManager = OIDCClient


# ---------------------------------------------------------------------------
# RS256 verification (JWK n/e -> PKCS#1 v1.5 SHA-256)
# ---------------------------------------------------------------------------

_SHA256_DIGEST_INFO = bytes.fromhex(
    "3031300d060960864801650304020105000420"
)


def _verify_rs256(signature: bytes, message: bytes, jwk: Mapping[str, str]) -> bool:
    try:
        modulus_bytes = _b64url_decode(jwk["n"])
        exponent_bytes = _b64url_decode(jwk["e"])
        modulus = int.from_bytes(modulus_bytes, "big")
        exponent = int.from_bytes(exponent_bytes, "big")
    except (KeyError, ValueError, TypeError):
        return False
    if modulus <= 0 or exponent <= 0:
        return False
    key_size = (modulus.bit_length() + 7) // 8
    if key_size <= 0 or len(signature) != key_size:
        return False
    decoded = pow(int.from_bytes(signature, "big"), exponent, modulus)
    encoded = decoded.to_bytes(key_size, "big")
    digest = hashlib.sha256(message).digest()
    expected_tail = _SHA256_DIGEST_INFO + digest
    padding_len = key_size - len(expected_tail) - 3
    if padding_len < 8:
        return False
    expected = b"\x00\x01" + (b"\xff" * padding_len) + b"\x00" + expected_tail
    return secrets.compare_digest(encoded, expected)


def load_sso_config(
    enterprise_policy: Any,
    *,
    home: Optional[Path] = None,
) -> SSOConfig:
    raw = getattr(enterprise_policy, "raw_sso", {}) or {}
    required = bool(getattr(enterprise_policy, "require_sso", False))
    return SSOConfig.from_mapping(raw, required=required, home=home)
