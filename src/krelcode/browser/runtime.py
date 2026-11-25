"""Browser backend abstraction, bounded sessions, and safe page snapshots."""

from __future__ import annotations

import asyncio
import html
import importlib.util
import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from ..core.redact import redact_obvious_secrets
from ..web.http import HttpResponse, SafeHttpClient

MAX_BROWSER_TEXT_CHARS = 100_000
MAX_BROWSER_ELEMENTS = 200
MAX_ELEMENT_LABEL_CHARS = 300
SESSION_ID_RE = re.compile(r"^b[1-9][0-9]{0,8}$")
ELEMENT_ID_RE = re.compile(r"^e[1-9][0-9]{0,3}$")
CAP_SNAPSHOT = "snapshot"
CAP_CLICK = "click"
CAP_FILL = "fill"
CAP_SCREENSHOT = "screenshot"
ALL_CAPABILITIES = frozenset({
    CAP_SNAPSHOT, CAP_CLICK, CAP_FILL, CAP_SCREENSHOT,
})


class BrowserBackendError(RuntimeError):
    """The selected browser backend cannot satisfy an operation."""


class BrowserSessionError(RuntimeError):
    """A browser session is missing, expired, or outside its limit."""


@dataclass(frozen=True)
class BrowserOptions:
    enabled: bool = True
    backend: str = "static"
    max_bytes: int = 2 * 1024 * 1024
    timeout: float = 20.0
    max_text_chars: int = 30_000
    max_sessions: int = 4
    session_ttl: float = 600.0
    screenshot_max_bytes: int = 5 * 1024 * 1024
    allowed_ports: tuple[int, ...] = (80, 443)
    allow_private: bool = False

    @classmethod
    def from_config(cls, section: Any = None) -> "BrowserOptions":
        value = section if isinstance(section, Mapping) else {}
        backend = str(value.get("backend") or "static").strip().lower()
        if backend not in {"auto", "static", "playwright"}:
            backend = "static"
        return cls(
            enabled=bool(value.get("enabled", True)),
            backend=backend,
            max_bytes=_positive_int(value.get("max_bytes"), 2 * 1024 * 1024),
            timeout=_positive_float(value.get("timeout"), 20.0),
            max_text_chars=max(
                500, min(
                    _positive_int(value.get("max_text_chars"), 30_000),
                    MAX_BROWSER_TEXT_CHARS)),
            max_sessions=max(1, min(
                _positive_int(value.get("max_sessions"), 4), 16)),
            session_ttl=max(
                30.0, _positive_float(value.get("session_ttl"), 600.0)),
            screenshot_max_bytes=max(
                64 * 1024,
                _positive_int(value.get("screenshot_max_bytes"), 5 * 1024 * 1024)),
            allowed_ports=_ports(value.get("allowed_ports")),
            allow_private=bool(value.get("allow_private", False)),
        )

    def http_client(self) -> SafeHttpClient:
        return SafeHttpClient(
            max_bytes=self.max_bytes,
            timeout=self.timeout,
            allowed_ports=self.allowed_ports,
            allow_private=self.allow_private,
        )


@dataclass(frozen=True)
class BrowserElement:
    id: str
    tag: str
    role: str = ""
    label: str = ""
    input_type: str = ""
    name: str = ""
    placeholder: str = ""
    target: str = ""

    def render(self) -> str:
        parts = [f"- [{self.id}] {self.tag}"]
        if self.role:
            parts.append(f"role={json.dumps(self.role)}")
        if self.input_type:
            parts.append(f"type={json.dumps(self.input_type)}")
        if self.name:
            parts.append(f"name={json.dumps(self.name)}")
        if self.label:
            parts.append(json.dumps(self.label))
        if self.placeholder:
            parts.append(f"placeholder={json.dumps(self.placeholder)}")
        if self.target:
            parts.append(f"-> {_safe_url(self.target)}")
        return redact_obvious_secrets(" ".join(parts))


@dataclass(frozen=True)
class BrowserPage:
    session_id: str
    url: str
    title: str
    status: int
    text: str
    elements: tuple[BrowserElement, ...] = ()
    backend: str = "static"
    content_type: str = ""

    def render(self, max_chars: int) -> str:
        limit = max(500, min(int(max_chars), MAX_BROWSER_TEXT_CHARS))
        safe_text = redact_obvious_secrets(self.text)
        if len(safe_text) > limit:
            safe_text = safe_text[:limit].rstrip() + "\n…[浏览器页面内容已截断]"
        safe_url = redact_obvious_secrets(_safe_url(self.url))
        safe_title = redact_obvious_secrets(self.title)
        lines = [
            (
                f'<browser_page trust="untrusted" backend={json.dumps(self.backend)} '
                f'session_id={json.dumps(self.session_id)} status="{self.status}">'
            ),
            f"URL: {safe_url}",
            f"Title: {safe_title}",
        ]
        if self.elements:
            lines.append("Elements:")
            lines.extend(element.render() for element in self.elements)
        else:
            lines.append("Elements: (none discovered)")
        lines.extend(("Page text:", safe_text, "</browser_page>"))
        return "\n".join(lines)


class BrowserBackend(Protocol):
    name: str
    capabilities: frozenset[str]

    async def open(
        self, session_id: str, url: str, *, max_chars: int,
    ) -> BrowserPage:
        ...

    async def snapshot(self, session_id: str, *, max_chars: int) -> BrowserPage:
        ...

    async def click(self, session_id: str, element_id: str) -> BrowserPage:
        ...

    async def fill(self, session_id: str, element_id: str, value: str) -> BrowserPage:
        ...

    async def screenshot(
        self, session_id: str, path: Path, *, full_page: bool,
        max_bytes: int,
    ) -> dict[str, Any]:
        ...

    async def close(self, session_id: str) -> bool:
        ...


class _StaticSession:
    def __init__(self, page: BrowserPage, created_at: float) -> None:
        self.page = page
        self.created_at = created_at


class StaticBrowserBackend:
    """Read-only HTML/JSON page snapshots using the SSRF-safe HTTP client."""

    name = "static"
    capabilities = frozenset({CAP_SNAPSHOT})

    def __init__(
        self,
        http: SafeHttpClient,
        *,
        max_bytes: int = 2 * 1024 * 1024,
        allowed_ports: Sequence[int] = (80, 443),
        allow_private: bool = False,
    ) -> None:
        self.http = http
        self.max_bytes = int(max_bytes)
        self.allowed_ports = tuple(allowed_ports)
        self.allow_private = bool(allow_private)
        self._sessions: dict[str, _StaticSession] = {}

    async def open(
        self, session_id: str, url: str, *, max_chars: int,
    ) -> BrowserPage:
        response = await self.http.fetch(
            url, max_bytes=self.max_bytes, timeout=self.http.timeout)
        page = _static_page_from_response(
            session_id, response, max_chars=max_chars)
        self._sessions[session_id] = _StaticSession(page, time.monotonic())
        return page

    async def snapshot(self, session_id: str, *, max_chars: int) -> BrowserPage:
        session = self._session(session_id)
        return _replace_snapshot_limit(session.page, max_chars)

    async def click(self, session_id: str, element_id: str) -> BrowserPage:
        raise BrowserBackendError(
            "static 浏览器后端只支持只读快照；交互请显式配置 "
            "[browser].backend = \"playwright\"")

    async def fill(self, session_id: str, element_id: str, value: str) -> BrowserPage:
        raise BrowserBackendError(
            "static 浏览器后端不支持填表；交互请显式配置 "
            "[browser].backend = \"playwright\"")

    async def screenshot(
        self, session_id: str, path: Path, *, full_page: bool,
        max_bytes: int,
    ) -> dict[str, Any]:
        raise BrowserBackendError(
            "static 浏览器后端不支持截图；交互请显式配置 "
            "[browser].backend = \"playwright\"")

    async def close(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

    def _session(self, session_id: str) -> _StaticSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise BrowserSessionError(f"浏览器会话不存在或已关闭: {session_id}")
        return session


class PlaywrightBrowserBackend:
    """Optional Chromium backend with fixed actions and per-request URL checks.

    Playwright is imported lazily. The model can never pass JavaScript, cookies,
    headers, or storage state. Every request and final navigation URL is checked
    through ``SafeHttpClient.validate``; browser DNS pinning cannot be guaranteed,
    so private-network access is also rejected at the URL policy layer.
    """

    name = "playwright"
    capabilities = ALL_CAPABILITIES

    def __init__(self, http: SafeHttpClient, *, max_bytes: int = 2 * 1024 * 1024,
                 timeout: float = 20.0) -> None:
        self.http = http
        self.max_bytes = int(max_bytes)
        self.timeout = float(timeout)
        self._playwright: Any = None
        self._browser: Any = None
        self._manager: Any = None
        self._sessions: dict[str, tuple[Any, Any]] = {}
        self._lock = asyncio.Lock()

    async def open(
        self, session_id: str, url: str, *, max_chars: int,
    ) -> BrowserPage:
        await self._ensure_browser()
        await self.http.validate(url)
        context = await self._browser.new_context(
            ignore_https_errors=False,
            service_workers="block",
            accept_downloads=False,
            java_script_enabled=True,
        )

        async def guard(route: Any, request: Any) -> None:
            try:
                parsed = urlsplit(request.url)
                if parsed.scheme.lower() not in {"http", "https"}:
                    await route.abort()
                    return
                await self.http.validate(request.url)
                await route.continue_()
            except Exception:
                await route.abort()

        await context.route("**/*", guard)
        page = await context.new_page()
        try:
            response = await page.goto(
                url, wait_until="domcontentloaded",
                timeout=max(1.0, self.timeout) * 1000,
            )
            if response is not None and response.status >= 400:
                raise BrowserBackendError(
                    f"页面返回 HTTP {response.status}")
            await self.http.validate(page.url)
            snapshot = await self._snapshot_page(
                session_id, page, context, max_chars=max_chars)
            self._sessions[session_id] = (context, page)
            return snapshot
        except Exception:
            await context.close()
            raise

    async def snapshot(self, session_id: str, *, max_chars: int) -> BrowserPage:
        context, page = self._session(session_id)
        await self.http.validate(page.url)
        return await self._snapshot_page(
            session_id, page, context, max_chars=max_chars)

    async def click(self, session_id: str, element_id: str) -> BrowserPage:
        _validate_element_id(element_id)
        context, page = self._session(session_id)
        locator = page.locator(
            f'[data-krelcode-element-id="{element_id}"]')
        await locator.click(timeout=max(1.0, self.timeout) * 1000)
        try:
            await page.wait_for_load_state(
                "domcontentloaded", timeout=max(1.0, self.timeout) * 1000)
        except Exception:
            pass
        await self.http.validate(page.url)
        return await self._snapshot_page(
            session_id, page, context, max_chars=30_000)

    async def fill(self, session_id: str, element_id: str, value: str) -> BrowserPage:
        _validate_element_id(element_id)
        context, page = self._session(session_id)
        locator = page.locator(
            f'[data-krelcode-element-id="{element_id}"]')
        await locator.fill(value, timeout=max(1.0, self.timeout) * 1000)
        await self.http.validate(page.url)
        return await self._snapshot_page(
            session_id, page, context, max_chars=30_000)

    async def screenshot(
        self, session_id: str, path: Path, *, full_page: bool,
        max_bytes: int,
    ) -> dict[str, Any]:
        _context, page = self._session(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_name = (
            f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp{path.suffix.lower()}")
        temp_path = path.parent / temp_name
        try:
            await page.screenshot(path=str(temp_path), full_page=bool(full_page))
            size = temp_path.stat().st_size
            if size > max_bytes:
                raise BrowserBackendError(
                    f"截图超过大小上限（{size} bytes，上限 {max_bytes}）")
            os.replace(temp_path, path)
            return {"path": str(path), "bytes": size, "full_page": bool(full_page)}
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    async def close(self, session_id: str) -> bool:
        record = self._sessions.pop(session_id, None)
        if record is None:
            return False
        context, _page = record
        await context.close()
        return True

    async def _ensure_browser(self) -> None:
        async with self._lock:
            if self._browser is not None:
                return
            try:
                from playwright.async_api import async_playwright  # type: ignore[import-not-found]
            except ImportError as exc:
                raise BrowserBackendError(
                    "playwright 后端未安装；运行 "
                    "`pip install playwright && playwright install chromium`") from exc
            self._manager = async_playwright()
            self._playwright = await self._manager.__aenter__()
            try:
                self._browser = await self._playwright.chromium.launch(
                    headless=True)
            except Exception as exc:
                await self._manager.__aexit__(type(exc), exc, exc.__traceback__)
                self._manager = None
                self._playwright = None
                raise BrowserBackendError(
                    "Chromium 启动失败；请运行 `playwright install chromium`") from exc

    def _session(self, session_id: str) -> tuple[Any, Any]:
        record = self._sessions.get(session_id)
        if record is None:
            raise BrowserSessionError(f"浏览器会话不存在或已关闭: {session_id}")
        return record

    async def _snapshot_page(self, session_id: str, page: Any, context: Any,
                             *, max_chars: int) -> BrowserPage:
        title = await page.title()
        body_text = await page.evaluate(
            "(limit) => (document.body && document.body.innerText || '').slice(0, limit)",
            MAX_BROWSER_TEXT_CHARS,
        )
        elements = await page.evaluate(_ELEMENT_SNAPSHOT_SCRIPT)
        values: list[BrowserElement] = []
        for item in (elements or [])[:MAX_BROWSER_ELEMENTS]:
            if not isinstance(item, dict):
                continue
            element_id = str(item.get("id") or "")
            if not ELEMENT_ID_RE.fullmatch(element_id):
                continue
            values.append(BrowserElement(
                id=element_id,
                tag=_bounded(item.get("tag"), 32),
                role=_bounded(item.get("role"), 64),
                label=_bounded(item.get("label"), MAX_ELEMENT_LABEL_CHARS),
                input_type=_bounded(item.get("type"), 64),
                name=_bounded(item.get("name"), 128),
                placeholder=_bounded(item.get("placeholder"), 256),
                target=_bounded(item.get("href"), 2_000),
            ))
        return BrowserPage(
            session_id=session_id,
            url=str(page.url),
            title=str(title or ""),
            status=200,
            text=str(body_text or ""),
            elements=tuple(values),
            backend=self.name,
            content_type="text/html",
        )


@dataclass
class BrowserRuntime:
    options: BrowserOptions
    backend: BrowserBackend
    _sessions: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_options(
        cls,
        options: BrowserOptions,
        *,
        http: Optional[SafeHttpClient] = None,
        backend: Optional[BrowserBackend] = None,
    ) -> "BrowserRuntime":
        client = http or options.http_client()
        selected = backend
        if selected is None:
            name = options.backend
            if name == "auto":
                name = (
                    "playwright"
                    if importlib.util.find_spec("playwright") is not None
                    else "static"
                )
            if name == "playwright":
                selected = PlaywrightBrowserBackend(
                    client,
                    max_bytes=options.max_bytes,
                    timeout=options.timeout,
                )
            else:
                selected = StaticBrowserBackend(
                    client,
                    max_bytes=options.max_bytes,
                    allowed_ports=options.allowed_ports,
                    allow_private=options.allow_private,
                )
        return cls(options=options, backend=selected)

    @property
    def backend_name(self) -> str:
        return self.backend.name

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(self.backend.capabilities)

    async def open(self, url: str, *, max_chars: Optional[int] = None) -> BrowserPage:
        await self._prune()
        if len(self._sessions) >= self.options.max_sessions:
            raise BrowserSessionError(
                f"浏览器会话达到上限 {self.options.max_sessions}；请先关闭一个会话")
        session_id = self._next_session_id()
        page = await self.backend.open(
            session_id, url,
            max_chars=_bounded_chars(max_chars, self.options.max_text_chars),
        )
        self._sessions[session_id] = time.monotonic()
        return page

    async def snapshot(
        self, session_id: str, *, max_chars: Optional[int] = None,
    ) -> BrowserPage:
        await self._touch(session_id)
        return await self.backend.snapshot(
            session_id,
            max_chars=_bounded_chars(max_chars, self.options.max_text_chars),
        )

    async def click(self, session_id: str, element_id: str) -> BrowserPage:
        await self._touch(session_id)
        page = await self.backend.click(session_id, element_id)
        return page

    async def fill(
        self, session_id: str, element_id: str, value: str,
    ) -> BrowserPage:
        await self._touch(session_id)
        return await self.backend.fill(session_id, element_id, value)

    async def screenshot(
        self, session_id: str, path: Path, *, full_page: bool = False,
    ) -> dict[str, Any]:
        await self._touch(session_id)
        return await self.backend.screenshot(
            session_id, path, full_page=full_page,
            max_bytes=self.options.screenshot_max_bytes)

    async def close(self, session_id: str) -> bool:
        await self._prune()
        closed = await self.backend.close(session_id)
        self._sessions.pop(session_id, None)
        return closed

    async def close_all(self) -> None:
        for session_id in tuple(self._sessions):
            try:
                await self.backend.close(session_id)
            except Exception:
                pass
        self._sessions.clear()

    async def _touch(self, session_id: str) -> None:
        _validate_session_id(session_id)
        await self._prune()
        if session_id not in self._sessions:
            raise BrowserSessionError(f"浏览器会话不存在或已过期: {session_id}")
        self._sessions[session_id] = time.monotonic()

    async def _prune(self) -> None:
        now = time.monotonic()
        expired = [
            session_id for session_id, touched in self._sessions.items()
            if now - touched > self.options.session_ttl
        ]
        for session_id in expired:
            self._sessions.pop(session_id, None)
            try:
                await self.backend.close(session_id)
            except Exception:
                pass

    def _next_session_id(self) -> str:
        used = {int(value[1:]) for value in self._sessions if SESSION_ID_RE.fullmatch(value)}
        candidate = 1
        while candidate in used:
            candidate += 1
        return f"b{candidate}"


class _PageParser(HTMLParser):
    _BLOCK_TAGS = {
        "p", "div", "section", "article", "main", "aside", "header", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol", "table",
        "tr", "br", "hr", "pre", "blockquote", "form", "nav", "title",
    }
    _SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "canvas"}

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: list[str] = []
        self.elements: list[BrowserElement] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False
        self._active: Optional[dict[str, Any]] = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        if tag in self._BLOCK_TAGS and not self._skip_depth:
            self.parts.append("\n")
        if self._skip_depth or len(self.elements) >= MAX_BROWSER_ELEMENTS:
            return
        if tag in {"a", "button", "input", "textarea", "select"} or (
                values.get("role") in {"button", "link", "textbox"}):
            element_id = f"e{len(self.elements) + 1}"
            target = values.get("href", "")
            if target:
                target = urljoin(self.base_url, target)
            self._active = {
                "id": element_id,
                "tag": tag,
                "role": values.get("role", ""),
                "label": "",
                "type": values.get("type", ""),
                "name": values.get("name", ""),
                "placeholder": values.get("placeholder", ""),
                "target": target,
            }
            self.elements.append(_element_from_dict(self._active))
            if tag in {"input", "textarea", "select"}:
                self._active = None

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        if self._active is not None and tag == self._active["tag"]:
            self._update_active_label()
            self._active = None
        if tag in self._BLOCK_TAGS and not self._skip_depth:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = html.unescape(data)
        if self._in_title:
            self.title += text
        if self._active is not None:
            self._active["label"] += text
        self.parts.append(text)

    def text(self) -> str:
        value = "".join(self.parts)
        value = re.sub(r"[ \t\f\v]+", " ", value)
        value = re.sub(r" *\n *", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    def finish(self) -> None:
        if self._active is not None:
            self._update_active_label()
            self._active = None

    def _update_active_label(self) -> None:
        if self._active is None:
            return
        index = int(self._active["id"][1:]) - 1
        if 0 <= index < len(self.elements):
            self.elements[index] = _element_from_dict(self._active)


def _element_from_dict(value: Mapping[str, Any]) -> BrowserElement:
    label = " ".join(str(value.get("label") or "").split())
    return BrowserElement(
        id=str(value.get("id") or "e1"),
        tag=_bounded(value.get("tag"), 32),
        role=_bounded(value.get("role"), 64),
        label=_bounded(label, MAX_ELEMENT_LABEL_CHARS),
        input_type=_bounded(value.get("type"), 64),
        name=_bounded(value.get("name"), 128),
        placeholder=_bounded(value.get("placeholder"), 256),
        target=_bounded(value.get("target"), 2_000),
    )


def _static_page_from_response(
    session_id: str, response: HttpResponse, *, max_chars: int,
) -> BrowserPage:
    content_type = response.content_type.lower()
    text = response.text
    title = ""
    elements: tuple[BrowserElement, ...] = ()
    if "html" in content_type or content_type in {"application/xhtml+xml", ""}:
        parser = _PageParser(response.url)
        try:
            parser.feed(text)
            parser.finish()
            text = parser.text()
            title = " ".join(parser.title.split())
            elements = tuple(parser.elements)
        except Exception:
            # The safe HTTP layer already bounded the response. A malformed HTML
            # parse falls back to plain text rather than executing or inflating it.
            pass
    elif content_type == "application/json" or content_type.endswith("+json"):
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            pass
    text = redact_obvious_secrets(text)
    if len(text) > MAX_BROWSER_TEXT_CHARS:
        text = text[:MAX_BROWSER_TEXT_CHARS] + "\n…[浏览器页面内容已截断]"
    return BrowserPage(
        session_id=session_id,
        url=response.url,
        title=title,
        status=response.status,
        text=text,
        elements=elements,
        backend="static",
        content_type=response.content_type,
    )


def _replace_snapshot_limit(page: BrowserPage, max_chars: int) -> BrowserPage:
    # Snapshot returns the stored page. Tool rendering applies the requested
    # character limit without mutating the session's bounded source evidence.
    return page


def _bounded(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


_SENSITIVE_QUERY_KEYS = frozenset({
    "access_token", "auth", "authorization", "code", "credential",
    "id_token", "key", "password", "passwd", "refresh_token", "secret",
    "session", "session_id", "token", "api_key", "apikey",
})


def _safe_url(value: str) -> str:
    """Hide common secret-bearing URL components before model-facing output."""
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return "***"
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        port = ""
    netloc = hostname + port
    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        query.append((
            key,
            "***" if key.casefold().replace("-", "_") in _SENSITIVE_QUERY_KEYS else item,
        ))
    safe_query = urlencode(query, doseq=True)
    return urlunsplit((
        parsed.scheme,
        netloc,
        parsed.path,
        safe_query,
        "",  # fragments can carry tokens and add no evidence value
    ))


def _bounded_chars(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(500, min(parsed, MAX_BROWSER_TEXT_CHARS))


def _validate_session_id(session_id: str) -> None:
    if not SESSION_ID_RE.fullmatch(str(session_id or "")):
        raise BrowserSessionError("session_id 非法")


def _validate_element_id(element_id: str) -> None:
    if not ELEMENT_ID_RE.fullmatch(str(element_id or "")):
        raise BrowserSessionError("element_id 非法")


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _ports(value: Any) -> tuple[int, ...]:
    if isinstance(value, str):
        value = [item.strip() for item in value.split(",")]
    if not isinstance(value, (list, tuple)):
        return (80, 443)
    ports: list[int] = []
    for item in value:
        try:
            port = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535 and port not in ports:
            ports.append(port)
    return tuple(ports) or (80, 443)


_ELEMENT_SNAPSHOT_SCRIPT = r"""
() => {
  const selector = "a,button,input,textarea,select,[role=button],[role=link],[role=textbox]";
  const nodes = Array.from(document.querySelectorAll(selector));
  const visible = [];
  for (const el of nodes) {
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" ||
        el.getAttribute("aria-hidden") === "true") continue;
    visible.push(el);
    if (visible.length >= 200) break;
  }
  return visible.map((el, index) => {
    const id = `e${index + 1}`;
    el.setAttribute("data-krelcode-element-id", id);
    const text = (el.innerText || el.getAttribute("aria-label") ||
                  el.getAttribute("title") || "").trim().slice(0, 300);
    return {
      id,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute("role") || "",
      label: text,
      type: el.getAttribute("type") || "",
      name: el.getAttribute("name") || "",
      placeholder: el.getAttribute("placeholder") || "",
      href: el.href || ""
    };
  });
}
"""


__all__ = [
    "ALL_CAPABILITIES",
    "BrowserBackend",
    "BrowserBackendError",
    "BrowserElement",
    "BrowserOptions",
    "BrowserPage",
    "BrowserRuntime",
    "BrowserSessionError",
    "PlaywrightBrowserBackend",
    "StaticBrowserBackend",
]
