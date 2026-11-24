"""Small zero-dependency HTTP client with explicit SSRF and size guards.

The client deliberately does not share cookies or user-provided authorization
headers. It resolves a URL, rejects non-public addresses, pins the connection to
one of the validated addresses, and validates every redirect independently.
Only ``http``/``https`` on the configured ports is accepted.
"""

from __future__ import annotations

import asyncio
import gzip
import http.client
import ipaddress
import re
import socket
import ssl
import zlib
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping, Optional, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit

DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_ALLOWED_PORTS = (80, 443)
_ALLOW_HEADERS = {
    "accept", "accept-language", "user-agent", "x-subscription-token",
    "x-api-key",
}
_REDIRECT_SAFE_HEADERS = {"accept", "accept-language", "user-agent"}
_BLOCKED_HEADERS = {
    "host", "content-length", "transfer-encoding", "connection", "cookie",
    "authorization", "proxy-authorization",
}
_REDIRECTS = {301, 302, 303, 307, 308}
_CONTENT_TYPE_TEXT = re.compile(r"^(?:text/|application/(?:xhtml|json|xml|javascript)|[^;]+\+(?:json|xml))", re.I)


class WebError(RuntimeError):
    """Base error for safe web operations."""


class UnsafeUrlError(WebError):
    """The URL violates the network policy."""


class WebResponseError(WebError):
    """The remote response is invalid or exceeds a configured limit."""


class WebStatusError(WebError):
    """The remote response has a non-success status."""

    def __init__(self, status: int, reason: str, origin: str = "") -> None:
        self.status = status
        self.reason = reason
        super().__init__(f"HTTP {status} {reason}{f' at {origin}' if origin else ''}")


@dataclass(frozen=True)
class ValidatedUrl:
    url: str
    scheme: str
    hostname: str
    port: int
    addresses: tuple[str, ...]
    target: str


@dataclass(frozen=True)
class RawHttpResponse:
    status: int
    reason: str
    headers: Mapping[str, str]
    body: bytes
    url: str


@dataclass(frozen=True)
class HttpResponse:
    status: int
    reason: str
    headers: Mapping[str, str]
    body: bytes
    url: str

    @property
    def content_type(self) -> str:
        return str(self.headers.get("content-type", "")).split(";", 1)[0].strip()

    @property
    def text(self) -> str:
        charset = _charset(self.headers.get("content-type", ""))
        data = self.body
        if not charset:
            match = re.search(br"<meta[^>]+charset\s*=\s*[\"']?([A-Za-z0-9._-]+)",
                              data[:4096], re.I)
            if match:
                charset = match.group(1).decode("ascii", errors="ignore")
        return data.decode(charset or "utf-8", errors="replace")


class SafeHttpClient:
    """Resolve/validate, connect to a pinned address, and bound every response."""

    def __init__(
        self,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        timeout: float = DEFAULT_TIMEOUT,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        allowed_ports: Sequence[int] = DEFAULT_ALLOWED_PORTS,
        allow_private: bool = False,
        resolver: Optional[Callable[[str, int], Sequence[str]]] = None,
        transport: Optional[
            Callable[
                [ValidatedUrl, Mapping[str, str], int, float],
                RawHttpResponse | Awaitable[RawHttpResponse],
            ]
        ] = None,
    ) -> None:
        if max_bytes < 1 or timeout <= 0 or max_redirects < 0:
            raise ValueError("Web HTTP 限制必须为正数")
        self.max_bytes = int(max_bytes)
        self.timeout = float(timeout)
        self.max_redirects = int(max_redirects)
        self.allowed_ports = frozenset(int(port) for port in allowed_ports)
        self.allow_private = bool(allow_private)
        self._resolver = resolver
        self._transport = transport

    async def validate(self, url: str) -> ValidatedUrl:
        text = str(url or "").strip()
        if not text or len(text) > 8192:
            raise UnsafeUrlError("URL 为空或过长")
        parsed = urlsplit(text)
        if parsed.scheme.lower() not in {"http", "https"}:
            raise UnsafeUrlError("只允许 http/https URL")
        if parsed.username is not None or parsed.password is not None:
            raise UnsafeUrlError("URL 不允许包含用户名或密码")
        if not parsed.hostname:
            raise UnsafeUrlError("URL 缺少 hostname")
        try:
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        except ValueError as exc:
            raise UnsafeUrlError("URL 端口非法") from exc
        if port not in self.allowed_ports:
            raise UnsafeUrlError(f"端口 {port} 不在允许范围内")
        hostname = parsed.hostname.rstrip(".").lower()
        if not hostname or hostname in {"localhost", "localhost.localdomain"}:
            raise UnsafeUrlError("不允许访问 localhost")
        addresses = await self._resolve(hostname, port)
        if not addresses:
            raise UnsafeUrlError(f"无法解析 hostname: {hostname}")
        normalized: list[str] = []
        for address in addresses:
            try:
                parsed_ip = ipaddress.ip_address(str(address).split("%", 1)[0])
            except ValueError:
                raise UnsafeUrlError(f"DNS 返回非法地址: {address}") from None
            if not self.allow_private and not parsed_ip.is_global:
                raise UnsafeUrlError(
                    f"拒绝访问非公网地址: {hostname} -> {parsed_ip}")
            normalized.append(str(parsed_ip))
        netloc = hostname
        if ":" in hostname and not hostname.startswith("["):
            netloc = f"[{hostname}]"
        if parsed.port is not None:
            netloc += f":{port}"
        target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        safe_url = urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/",
                               parsed.query, ""))
        return ValidatedUrl(
            url=safe_url,
            scheme=parsed.scheme.lower(),
            hostname=hostname,
            port=port,
            addresses=tuple(normalized),
            target=target,
        )

    async def _resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        literal = _literal_ip(hostname)
        if literal is not None:
            return (literal,)
        if self._resolver is not None:
            try:
                values = self._resolver(hostname, port)
            except (OSError, ValueError) as exc:
                raise UnsafeUrlError(f"DNS 解析失败: {hostname}") from exc
            return tuple(str(item) for item in values or ())
        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo, hostname, port, 0, socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafeUrlError(f"DNS 解析失败: {hostname}") from exc
        output: list[str] = []
        for info in infos:
            address = info[4][0]
            if address not in output:
                output.append(address)
        return tuple(output)

    async def fetch(
        self,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        max_bytes: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> HttpResponse:
        current = str(url)
        limit = min(self.max_bytes, int(max_bytes or self.max_bytes))
        deadline = float(timeout or self.timeout)
        request_headers = {
            key: value for key, value in (headers or {}).items()
            if str(key).lower() in _ALLOW_HEADERS
        }
        for redirect_count in range(self.max_redirects + 1):
            validated = await self.validate(current)
            raw = await self._invoke_transport(
                validated, request_headers, limit, deadline)
            if len(raw.body) > limit:
                raise WebResponseError("响应体超过大小上限")
            if raw.status in _REDIRECTS:
                if redirect_count >= self.max_redirects:
                    raise WebResponseError("重定向次数超过上限")
                location = _header(raw.headers, "location")
                if not location:
                    raise WebResponseError("重定向响应缺少 Location")
                next_url = urljoin(current, location)
                if not _same_origin(current, next_url):
                    request_headers = {
                        key: value for key, value in request_headers.items()
                        if str(key).lower() in _REDIRECT_SAFE_HEADERS
                    }
                current = next_url
                continue
            if raw.status < 200 or raw.status >= 300:
                raise WebStatusError(
                    raw.status, raw.reason, _safe_origin(current))
            body = _decode_body(raw.body, raw.headers, limit)
            content_type = str(_header(raw.headers, "content-type"))
            if content_type and not _CONTENT_TYPE_TEXT.match(content_type):
                raise WebResponseError(
                    f"不支持的 Content-Type: {content_type.split(';', 1)[0]}")
            return HttpResponse(
                status=raw.status,
                reason=raw.reason,
                headers={str(k).lower(): str(v) for k, v in raw.headers.items()},
                body=body,
                url=current,
            )
        raise WebResponseError("重定向次数超过上限")

    async def _invoke_transport(
        self,
        validated: ValidatedUrl,
        headers: Mapping[str, str],
        max_bytes: int,
        timeout: float,
    ) -> RawHttpResponse:
        if self._transport is not None:
            result = self._transport(validated, headers, max_bytes, timeout)
            if isinstance(result, RawHttpResponse):
                return result
            return await result
        return await asyncio.to_thread(
            self._blocking_transport, validated, headers, max_bytes, timeout)

    def _blocking_transport(
        self,
        validated: ValidatedUrl,
        headers: Mapping[str, str],
        max_bytes: int,
        timeout: float,
    ) -> RawHttpResponse:
        last_error: Optional[BaseException] = None
        for address in validated.addresses:
            try:
                return _request_pinned(
                    validated, address, headers, max_bytes, timeout)
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                last_error = exc
                continue
        raise WebResponseError(
            f"无法连接 {validated.hostname}: "
            f"{type(last_error).__name__ if last_error else 'unknown error'}")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port, timeout=timeout,
                         context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), self.timeout)
        if self._tunnel_host:  # type: ignore[attr-defined]
            self.sock = raw
            self._tunnel()  # type: ignore[attr-defined]
            raw = self.sock
        self.sock = self._context.wrap_socket(  # type: ignore[attr-defined]
            raw, server_hostname=self.host)


def _request_pinned(
    validated: ValidatedUrl,
    address: str,
    headers: Mapping[str, str],
    max_bytes: int,
    timeout: float,
) -> RawHttpResponse:
    connection = (
        _PinnedHTTPSConnection(validated.hostname, validated.port, address, timeout)
        if validated.scheme == "https"
        else _PinnedHTTPConnection(validated.hostname, validated.port, address, timeout)
    )
    request_headers = {
        "User-Agent": "krelcode-web/1.0",
        "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.1",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    request_headers.update({str(k): str(v) for k, v in headers.items()})
    request_headers.pop("Host", None)
    try:
        connection.request("GET", validated.target, headers=request_headers)
        response = connection.getresponse()
        content_length = response.getheader("Content-Length")
        if content_length:
            try:
                declared = int(content_length)
            except ValueError:
                declared = 0
            if declared > max_bytes:
                raise WebResponseError("响应 Content-Length 超过上限")
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise WebResponseError("响应体超过大小上限")
        headers_out = {name.lower(): value for name, value in response.getheaders()}
        return RawHttpResponse(
            status=response.status,
            reason=response.reason,
            headers=headers_out,
            body=body,
            url=validated.url,
        )
    finally:
        connection.close()


def _decode_body(body: bytes, headers: Mapping[str, str], max_bytes: int) -> bytes:
    encoding = str(_header(headers, "content-encoding", "")).lower().strip()
    if not encoding or encoding == "identity":
        return body
    try:
        if encoding == "gzip":
            decoded = gzip.decompress(body)
        elif encoding == "deflate":
            try:
                decoded = zlib.decompress(body)
            except zlib.error:
                decoded = zlib.decompress(body, -zlib.MAX_WBITS)
        else:
            raise WebResponseError(f"不支持的 Content-Encoding: {encoding}")
    except (OSError, zlib.error) as exc:
        raise WebResponseError("响应压缩格式损坏") from exc
    if len(decoded) > max_bytes:
        raise WebResponseError("解压后的响应体超过大小上限")
    return decoded


def _literal_ip(hostname: str) -> Optional[str]:
    try:
        return str(ipaddress.ip_address(hostname))
    except ValueError:
        return None


def _header(headers: Mapping[str, str], name: str, default: str = "") -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return default


def _charset(content_type: str) -> str:
    match = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9._-]+)", content_type or "", re.I)
    return match.group(1) if match else ""


def _safe_origin(url: str) -> str:
    try:
        parsed = urlsplit(url)
        return f"{parsed.scheme}://{parsed.hostname or ''}{f':{parsed.port}' if parsed.port else ''}"
    except ValueError:
        return ""


def _same_origin(left: str, right: str) -> bool:
    """Compare origins with default ports; malformed URLs fail closed."""
    try:
        a = urlsplit(left)
        b = urlsplit(right)
        a_scheme = a.scheme.lower()
        b_scheme = b.scheme.lower()
        a_default = 443 if a_scheme == "https" else 80 if a_scheme == "http" else None
        b_default = 443 if b_scheme == "https" else 80 if b_scheme == "http" else None
        return (
            a_scheme == b_scheme
            and (a.hostname or "").rstrip(".").lower()
            == (b.hostname or "").rstrip(".").lower()
            and (a.port or a_default) == (b.port or b_default)
        )
    except ValueError:
        return False


__all__ = [
    "DEFAULT_ALLOWED_PORTS",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TIMEOUT",
    "HttpResponse",
    "RawHttpResponse",
    "SafeHttpClient",
    "UnsafeUrlError",
    "ValidatedUrl",
    "WebError",
    "WebResponseError",
    "WebStatusError",
]
