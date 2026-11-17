"""OAuth 2.1 helpers for HTTP MCP servers.

The implementation intentionally stays on the standard library so the runtime
keeps its zero-dependency contract. It supports static tokens, refresh tokens,
client-credentials, protected-resource metadata discovery, dynamic client
registration, and an interactive authorization-code + PKCE flow.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from .transport import MCPAuthRequired, MCPError

TOKEN_REFRESH_SKEW_SECONDS = 60
DEFAULT_CALLBACK_TIMEOUT = 300
_HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_BEARER_PARAM_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_-]*)="([^"]*)"')

HTTPResult = Tuple[int, Dict[str, str], bytes]
HTTPRequest = Callable[[str, str, Dict[str, str], Optional[bytes], float], HTTPResult]


@dataclass(frozen=True)
class MCPOAuthConfig:
    enabled: bool = True
    access_token: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)
    token_url: str = ""
    authorization_url: str = ""
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    scopes: Tuple[str, ...] = ()
    resource: str = ""
    resource_metadata_url: str = ""
    authorization_server: str = ""
    registration_url: str = ""
    redirect_uri: str = ""
    redirect_host: str = "127.0.0.1"
    redirect_port: int = 8765
    callback_timeout: float = DEFAULT_CALLBACK_TIMEOUT
    token_endpoint_auth_method: str = "client_secret_basic"
    grant_type: str = ""

    @property
    def configured(self) -> bool:
        return bool(
            self.enabled
            and (
                self.access_token
                or self.refresh_token
                or self.token_url
                or self.authorization_url
                or self.resource_metadata_url
                or self.authorization_server
            )
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "MCPOAuthConfig":
        scopes = raw.get("scopes", raw.get("scope", ()))
        if isinstance(scopes, str):
            scope_values = tuple(item for item in scopes.split() if item)
        elif isinstance(scopes, (list, tuple)):
            scope_values = tuple(str(item).strip() for item in scopes if str(item).strip())
        else:
            scope_values = ()
        try:
            port = int(raw.get("redirect_port", 8765))
        except (TypeError, ValueError):
            port = 8765
        try:
            callback_timeout = float(
                raw.get("callback_timeout", DEFAULT_CALLBACK_TIMEOUT))
        except (TypeError, ValueError):
            callback_timeout = DEFAULT_CALLBACK_TIMEOUT
        return cls(
            enabled=bool(raw.get("enabled", True)),
            access_token=str(raw.get("access_token") or ""),
            refresh_token=str(raw.get("refresh_token") or ""),
            token_url=str(raw.get("token_url") or raw.get("token_endpoint") or ""),
            authorization_url=str(
                raw.get("authorization_url")
                or raw.get("authorization_endpoint") or ""),
            client_id=str(raw.get("client_id") or ""),
            client_secret=str(raw.get("client_secret") or ""),
            scopes=scope_values,
            resource=str(raw.get("resource") or ""),
            resource_metadata_url=str(raw.get("resource_metadata_url") or ""),
            authorization_server=str(raw.get("authorization_server") or ""),
            registration_url=str(raw.get("registration_url") or ""),
            redirect_uri=str(raw.get("redirect_uri") or ""),
            redirect_host=str(raw.get("redirect_host") or "127.0.0.1"),
            redirect_port=max(0, min(port, 65535)),
            callback_timeout=max(1.0, min(callback_timeout, 1800.0)),
            token_endpoint_auth_method=str(
                raw.get("token_endpoint_auth_method")
                or "client_secret_basic"),
            grant_type=str(raw.get("grant_type") or ""),
        )


class MCPOAuthRequired(MCPAuthRequired):
    """No usable OAuth token is available; user interaction may be required."""


@dataclass(frozen=True)
class OAuthMetadata:
    authorization_url: str = ""
    token_url: str = ""
    registration_url: str = ""
    scopes: Tuple[str, ...] = ()
    resource: str = ""


class OAuthTokenManager:
    """Per-server OAuth token cache and refresh/interactive authorization service."""

    def __init__(
        self,
        server_name: str,
        server_url: str,
        config: Mapping[str, Any] | MCPOAuthConfig,
        *,
        token_path: Optional[Path] = None,
        request: Optional[HTTPRequest] = None,
        log: Optional[Callable[[str], None]] = None,
        open_browser: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self.server_name = server_name
        self.server_url = server_url
        self.config = (
            config if isinstance(config, MCPOAuthConfig)
            else MCPOAuthConfig.from_mapping(config)
        )
        self.token_path = (
            Path(token_path).expanduser()
            if token_path is not None
            else Path.home() / ".krelcode" / "mcp-oauth.json"
        )
        self._request = request or _urllib_http_request
        self._log = log or (lambda _message: None)
        self._open_browser = open_browser or webbrowser.open
        self._token_key = _token_key(server_name, server_url)
        self._record = self._load_record()
        self._metadata = OAuthMetadata(
            authorization_url=self.config.authorization_url,
            token_url=self.config.token_url,
            registration_url=self.config.registration_url,
            scopes=self.config.scopes,
            resource=self.config.resource,
        )
        self._challenge_scope = ""
        self._challenge_resource_metadata = ""

    @property
    def has_token(self) -> bool:
        return bool(self._access_token_value(allow_expired=True))

    @property
    def access_token(self) -> str:
        return self._access_token_value(allow_expired=False)

    def _access_token_value(self, *, allow_expired: bool) -> str:
        if self.config.access_token:
            return self.config.access_token
        token = str(self._record.get("access_token") or "")
        if token and (allow_expired or not self._expired()):
            return token
        return ""

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        if not force_refresh:
            token = self._access_token_value(allow_expired=False)
            if token:
                return token
        if self.config.refresh_token or self._record.get("refresh_token"):
            if await self.refresh():
                return self._access_token_value(allow_expired=False)
        if self.config.grant_type == "client_credentials":
            if await self._client_credentials():
                return self._access_token_value(allow_expired=False)
        raise MCPOAuthRequired(
            f"MCP 服务器 {self.server_name!r} 需要 OAuth 授权；"
            f"运行 /mcp auth {self.server_name}",
            status=401,
        )

    async def headers(self, *, force_refresh: bool = False) -> Dict[str, str]:
        token = await self.get_access_token(force_refresh=force_refresh)
        return {"Authorization": f"Bearer {token}"}

    def note_challenge(self, headers: Mapping[str, str]) -> None:
        value = next(
            (str(raw) for key, raw in headers.items()
             if str(key).lower() == "www-authenticate"),
            "",
        )
        if not value:
            return
        params = {key: item for key, item in _BEARER_PARAM_RE.findall(value)}
        self._challenge_scope = params.get("scope", "")
        self._challenge_resource_metadata = params.get("resource_metadata", "")

    async def refresh(self) -> bool:
        refresh_token = str(
            self.config.refresh_token or self._record.get("refresh_token") or "")
        token_url = self._metadata.token_url
        if not refresh_token:
            return False
        if not token_url:
            await self.discover()
            token_url = self._metadata.token_url
        if not token_url:
            return False
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        self._append_client_credentials(data)
        try:
            payload = await self._post_token(token_url, data)
        except MCPError as exc:
            self._log(f"[mcp:oauth] {self.server_name} token 刷新失败: {exc}")
            return False
        self._save_token_payload(payload, fallback_refresh=refresh_token)
        return True

    async def _client_credentials(self) -> bool:
        token_url = self._metadata.token_url
        if not token_url:
            await self.discover()
            token_url = self._metadata.token_url
        if not token_url or not self.config.client_id:
            return False
        data = {"grant_type": "client_credentials"}
        if self.config.resource:
            data["resource"] = self.config.resource
        if self.config.scopes:
            data["scope"] = " ".join(self.config.scopes)
        self._append_client_credentials(data)
        try:
            payload = await self._post_token(token_url, data)
        except MCPError as exc:
            self._log(f"[mcp:oauth] {self.server_name} client_credentials 失败: {exc}")
            return False
        self._save_token_payload(payload)
        return True

    async def discover(self) -> OAuthMetadata:
        if self._metadata.authorization_url and self._metadata.token_url:
            return self._metadata
        auth_server = self.config.authorization_server
        resource = self.config.resource or self.server_url
        scopes = list(self.config.scopes)
        candidates = []
        if self.config.resource_metadata_url:
            candidates.append(self.config.resource_metadata_url)
        if self._challenge_resource_metadata:
            candidates.insert(0, self._challenge_resource_metadata)
        candidates.extend(_protected_resource_metadata_candidates(self.server_url))
        protected: Dict[str, Any] = {}
        for url in _dedupe(candidates):
            if not _safe_http_url(url):
                continue
            status, _headers, body = await asyncio.to_thread(
                self._request, "GET", url, {"Accept": "application/json"},
                None, 15.0)
            if status != 200:
                continue
            protected = _json_object(body)
            if protected:
                break
        if protected:
            resource = str(protected.get("resource") or resource)
            if not scopes:
                scopes = [str(item) for item in protected.get("scopes_supported") or []]
            servers = protected.get("authorization_servers") or []
            if not auth_server and isinstance(servers, list) and servers:
                auth_server = str(servers[0])
        if not auth_server:
            parsed = urllib.parse.urlsplit(self.server_url)
            auth_server = f"{parsed.scheme}://{parsed.netloc}"

        authorization_url = self.config.authorization_url
        token_url = self.config.token_url
        registration_url = self.config.registration_url
        if not token_url or not authorization_url:
            metadata = await self._authorization_server_metadata(auth_server)
            authorization_url = authorization_url or str(
                metadata.get("authorization_endpoint") or "")
            token_url = token_url or str(metadata.get("token_endpoint") or "")
            registration_url = registration_url or str(
                metadata.get("registration_endpoint") or "")
            if not scopes:
                scopes = [str(item) for item in metadata.get("scopes_supported") or []]
        self._metadata = OAuthMetadata(
            authorization_url=authorization_url,
            token_url=token_url,
            registration_url=registration_url,
            scopes=tuple(scopes),
            resource=resource,
        )
        return self._metadata

    async def _authorization_server_metadata(self, issuer: str) -> Dict[str, Any]:
        parsed = urllib.parse.urlsplit(issuer)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path.rstrip("/")
        candidates = [
            f"{origin}/.well-known/oauth-authorization-server{path}",
            f"{issuer.rstrip('/')}/.well-known/openid-configuration",
            f"{origin}/.well-known/openid-configuration{path}",
        ]
        for url in _dedupe(candidates):
            status, _headers, body = await asyncio.to_thread(
                self._request, "GET", url, {"Accept": "application/json"},
                None, 15.0)
            if status == 200:
                value = _json_object(body)
                if value:
                    return value
        return {}

    async def authorize_interactive(self, timeout: Optional[float] = None) -> None:
        metadata = await self.discover()
        if not metadata.authorization_url or not metadata.token_url:
            raise MCPOAuthRequired(
                f"MCP 服务器 {self.server_name!r} 未提供可用的 OAuth 元数据；"
                "请配置 authorization_url 和 token_url")
        if not self.config.client_id:
            await self._register_client()
            metadata = self._metadata
        if not self.config.client_id:
            raise MCPOAuthRequired(
                f"MCP 服务器 {self.server_name!r} 缺少 client_id，且动态注册不可用")
        result = await asyncio.to_thread(
            self._interactive_sync, metadata, timeout or self.config.callback_timeout)
        self._save_token_payload(result)
        self._log(f"[mcp:oauth] {self.server_name} 授权成功")

    def _interactive_sync(
        self, metadata: OAuthMetadata, timeout: float,
    ) -> Dict[str, Any]:
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
        state = secrets.token_urlsafe(32)
        captured: Dict[str, str] = {}
        done = threading.Event()

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib contract
                query = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(self.path).query)
                error = (query.get("error") or [""])[0]
                code = (query.get("code") or [""])[0]
                returned_state = (query.get("state") or [""])[0]
                if returned_state != state:
                    body = b"OAuth state mismatch. You can close this window."
                    self.send_response(400)
                elif error:
                    captured["error"] = error
                    captured["error_description"] = (
                        query.get("error_description") or [""])[0]
                    body = b"OAuth authorization failed. You can close this window."
                    self.send_response(400)
                elif code:
                    captured["code"] = code
                    body = b"Krelcode OAuth authorization completed. You can close this window."
                    self.send_response(200)
                else:
                    body = b"Missing authorization code."
                    self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                done.set()

            def log_message(self, _format: str, *args: Any) -> None:
                return

        server = HTTPServer(
            (self.config.redirect_host, self.config.redirect_port), CallbackHandler)
        server.timeout = 0.5
        port = int(server.server_address[1])
        redirect_uri = self.config.redirect_uri or (
            f"http://{self.config.redirect_host}:{port}/callback")
        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": metadata.resource or self.server_url,
        }
        scopes = metadata.scopes or self.config.scopes
        if scopes:
            params["scope"] = " ".join(scopes)
        authorization_url = metadata.authorization_url + (
            "&" if "?" in metadata.authorization_url else "?") + urllib.parse.urlencode(params)
        self._log(f"[mcp:oauth] 请在浏览器完成授权: {authorization_url}")
        try:
            self._open_browser(authorization_url)
        except Exception:
            pass
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            if not done.wait(timeout):
                raise MCPOAuthRequired(
                    f"MCP 服务器 {self.server_name!r} OAuth 授权超时")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)
        if captured.get("error"):
            detail = captured.get("error_description") or captured["error"]
            raise MCPOAuthRequired(
                f"MCP 服务器 {self.server_name!r} OAuth 授权失败: {detail}")
        code = captured.get("code")
        if not code:
            raise MCPOAuthRequired(
                f"MCP 服务器 {self.server_name!r} 未收到 authorization code")
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "resource": metadata.resource or self.server_url,
        }
        self._append_client_credentials(data)
        return self._post_token_sync(metadata.token_url, data)

    async def _register_client(self) -> None:
        metadata = await self.discover()
        if not metadata.registration_url:
            return
        redirect_uri = self.config.redirect_uri or (
            f"http://{self.config.redirect_host}:"
            f"{self.config.redirect_port or 8765}/callback")
        body = json.dumps({
            "client_name": "Krelcode",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }).encode("utf-8")
        status, _headers, raw = await asyncio.to_thread(
            self._request, "POST", metadata.registration_url,
            {"Accept": "application/json", "Content-Type": "application/json"},
            body, 30.0)
        value = _json_object(raw)
        if status not in (200, 201) or not value.get("client_id"):
            raise MCPOAuthRequired(
                f"MCP 服务器 {self.server_name!r} 动态客户端注册失败（HTTP {status}）")
        self.config = _replace_config(
            self.config,
            client_id=str(value["client_id"]),
            client_secret=str(value.get("client_secret") or ""),
            registration_url=metadata.registration_url,
        )

    async def _post_token(self, token_url: str, data: Dict[str, str]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._post_token_sync, token_url, data)

    def _post_token_sync(
        self, token_url: str, data: Dict[str, str],
    ) -> Dict[str, Any]:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        self._append_client_headers(headers)
        status, _headers, raw = self._request(
            "POST", token_url, headers, body, 30.0)
        value = _json_object(raw)
        if status not in (200, 201) or not value.get("access_token"):
            detail = value.get("error_description") or value.get("error") or ""
            raise MCPError(
                f"OAuth token endpoint 失败 HTTP {status}: {detail or 'invalid response'}")
        return value

    def _append_client_credentials(self, data: Dict[str, str]) -> None:
        if self.config.grant_type:
            data.setdefault("grant_type", self.config.grant_type)
        if self.config.scopes and "scope" not in data:
            data["scope"] = " ".join(self.config.scopes)
        if self.config.resource:
            data.setdefault("resource", self.config.resource)
        method = self.config.token_endpoint_auth_method
        if self.config.client_id and method == "client_secret_post":
            data["client_id"] = self.config.client_id
            if self.config.client_secret:
                data["client_secret"] = self.config.client_secret

    def _append_client_headers(self, headers: Dict[str, str]) -> None:
        if (
            self.config.client_id
            and self.config.client_secret
            and self.config.token_endpoint_auth_method != "client_secret_post"
        ):
            raw = f"{self.config.client_id}:{self.config.client_secret}".encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")

    def _expired(self) -> bool:
        try:
            expires_at = float(self._record.get("expires_at") or 0)
        except (TypeError, ValueError):
            return False
        return bool(expires_at and expires_at <= time.time() + TOKEN_REFRESH_SKEW_SECONDS)

    def _save_token_payload(
        self, payload: Mapping[str, Any], *, fallback_refresh: str = "",
    ) -> None:
        access_token = str(payload.get("access_token") or "")
        if not access_token:
            raise MCPError("OAuth token response 缺少 access_token")
        try:
            expires_in = float(payload.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0.0
        record = {
            "access_token": access_token,
            "refresh_token": str(
                payload.get("refresh_token") or fallback_refresh or ""),
            "token_type": str(payload.get("token_type") or "Bearer"),
            "scope": str(payload.get("scope") or ""),
            "expires_at": time.time() + expires_in if expires_in > 0 else 0.0,
        }
        self._record = record
        data = self._load_store()
        data[self._token_key] = record
        self._write_store(data)

    def _load_record(self) -> Dict[str, Any]:
        value = self._load_store().get(self._token_key)
        return dict(value) if isinstance(value, dict) else {}

    def _load_store(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_store(self, value: Mapping[str, Any]) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.token_path.with_name(self.token_path.name + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, self.token_path)
        try:
            os.chmod(self.token_path, 0o600)
        except OSError:
            pass


def _replace_config(config: MCPOAuthConfig, **changes: Any) -> MCPOAuthConfig:
    values = {
        field_name: getattr(config, field_name)
        for field_name in MCPOAuthConfig.__dataclass_fields__
    }
    values.update(changes)
    return MCPOAuthConfig(**values)


def _token_key(server_name: str, server_url: str) -> str:
    raw = f"{server_name}\0{server_url}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _safe_http_url(url: str) -> bool:
    return bool(_HTTP_URL_RE.match(str(url or ""))) and bool(
        urllib.parse.urlsplit(url).netloc)


def _dedupe(values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _json_object(body: bytes) -> Dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _protected_resource_metadata_candidates(server_url: str) -> list[str]:
    parsed = urllib.parse.urlsplit(server_url)
    if not parsed.scheme or not parsed.netloc:
        return []
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    candidates = [f"{origin}/.well-known/oauth-protected-resource{path}"]
    if path:
        candidates.append(f"{origin}/.well-known/oauth-protected-resource")
    return _dedupe(candidates)


def _urllib_http_request(
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Optional[bytes],
    timeout: float,
) -> HTTPResult:
    request = urllib.request.Request(
        url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (
                int(response.status),
                {key.lower(): value for key, value in response.headers.items()},
                response.read(),
            )
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:
            raw = b""
        return (
            int(exc.code),
            {key.lower(): value for key, value in (exc.headers or {}).items()},
            raw,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MCPError(f"OAuth HTTP 请求失败: {exc}") from exc
