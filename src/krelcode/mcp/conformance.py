"""可执行的 MCP conformance matrix。

报告只读取握手结果、本地 capability 和已加载目录计数，不主动执行
tools/call、resources/read、OAuth 或 elicitation。这样 ``/mcp conformance``
可安全用于生产连接，并诚实区分：
- supported：客户端已实现且当前运行时已启用；
- partial：实现存在但当前前端/传输或防护只覆盖一部分；
- unsupported：客户端未实现；
- not_applicable：当前服务器未声明该能力，或协议版本不适用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .security import safe_label


SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
ELICITATION_PROTOCOL_VERSION = "2025-06-18"

STATUS_SUPPORTED = "supported"
STATUS_PARTIAL = "partial"
STATUS_UNSUPPORTED = "unsupported"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_ORDER = (
    STATUS_SUPPORTED,
    STATUS_PARTIAL,
    STATUS_UNSUPPORTED,
    STATUS_NOT_APPLICABLE,
)


@dataclass(frozen=True)
class ConformanceCheck:
    id: str
    category: str
    status: str
    detail: str


@dataclass(frozen=True)
class MCPConformanceReport:
    server: str
    transport: str
    requested_protocol_version: str
    negotiated_protocol_version: str
    server_info: str
    client_capabilities: Tuple[str, ...]
    server_capabilities: Tuple[str, ...]
    loaded: Tuple[Tuple[str, int], ...]
    checks: Tuple[ConformanceCheck, ...]

    def counts(self) -> Dict[str, int]:
        result = {status: 0 for status in STATUS_ORDER}
        for check in self.checks:
            result[check.status] = result.get(check.status, 0) + 1
        return result

    def check(self, check_id: str) -> Optional[ConformanceCheck]:
        return next((item for item in self.checks if item.id == check_id), None)


def build_conformance_report(
    client: Any,
    *,
    loaded_tools: Optional[int] = None,
    loaded_prompts: Optional[int] = None,
    loaded_resources: Optional[int] = None,
    loaded_resource_templates: Optional[int] = None,
    redactor: Optional[Any] = None,
) -> MCPConformanceReport:
    """Build one content-light report from a connected MCPClient."""
    server_caps = client.capabilities or {}
    client_caps = getattr(client, "client_capabilities", {}) or {}
    version = str(getattr(client, "server_protocol_version", "") or "")
    requested = str(
        getattr(client, "requested_protocol_version", "") or "")
    safe_version = safe_label(
        version, max_chars=80, fallback="", redactor=redactor)
    transport = safe_label(
        getattr(getattr(client, "_transport", None), "kind", "?") or "?",
        max_chars=80, fallback="?", redactor=redactor)
    caps = tuple(sorted(
        safe_label(name, max_chars=80, fallback="?", redactor=redactor)
        for name in server_caps))
    local_caps = tuple(sorted(
        safe_label(name, max_chars=80, fallback="?", redactor=redactor)
        for name in client_caps))
    loaded = tuple(
        (key, int(value))
        for key, value in (
            ("tools", loaded_tools or 0),
            ("prompts", loaded_prompts or 0),
            ("resources", loaded_resources or 0),
            ("resource_templates", loaded_resource_templates or 0),
        )
    )
    checks: list[ConformanceCheck] = []

    def add(
        check_id: str,
        category: str,
        status: str,
        detail: str,
    ) -> None:
        checks.append(ConformanceCheck(
            id=check_id, category=category, status=status, detail=detail))

    if not version:
        add("protocol:negotiated", "protocol", STATUS_PARTIAL,
            "initialize 未返回可识别的 protocolVersion")
    elif version in SUPPORTED_PROTOCOL_VERSIONS:
        add("protocol:negotiated", "protocol", STATUS_SUPPORTED,
            f"协商版本 {safe_version}")
    else:
        add("protocol:negotiated", "protocol", STATUS_UNSUPPORTED,
            f"未声明兼容的协议版本 {safe_version}")

    for candidate in SUPPORTED_PROTOCOL_VERSIONS:
        add(
            f"protocol:{candidate}",
            "protocol",
            STATUS_SUPPORTED,
            "客户端支持协商",
        )

    transport_status = (
        STATUS_SUPPORTED if transport in {"stdio", "streamable-http"}
        else STATUS_PARTIAL if transport == "sse"
        else STATUS_UNSUPPORTED
    )
    transport_detail = {
        "stdio": "换行分隔 JSON-RPC 子进程传输",
        "streamable-http": "单端点 POST/SSE、session、resume 与 DELETE",
        "sse": "旧版 HTTP+SSE 兼容传输",
    }.get(transport, "未知传输")
    add("transport:current", "transport", transport_status, transport_detail)

    add("lifecycle:initialize", "lifecycle", STATUS_SUPPORTED,
        "initialize 请求与 capability 协商")
    add("lifecycle:initialized", "lifecycle", STATUS_SUPPORTED,
        "notifications/initialized 已等待写盘后继续")
    add("lifecycle:ping", "lifecycle", STATUS_SUPPORTED,
        "服务器 ping 自动应答")
    add("lifecycle:close", "lifecycle", STATUS_SUPPORTED,
        "幂等关闭并使 pending 请求 fail-closed")

    if transport == "streamable-http":
        add("transport:session", "transport", STATUS_SUPPORTED,
            "Mcp-Session-Id 自动捕获")
        add("transport:resume", "transport", STATUS_SUPPORTED,
            "GET 监听流退避重连与 Last-Event-ID")
        add("transport:delete", "transport", STATUS_SUPPORTED,
            "关闭时尽力 DELETE 终止会话")
    else:
        add("transport:session", "transport", STATUS_NOT_APPLICABLE,
            "仅 Streamable HTTP 使用会话")
        add("transport:resume", "transport", STATUS_NOT_APPLICABLE,
            "仅 Streamable HTTP GET 监听流可恢复")
        add("transport:delete", "transport", STATUS_NOT_APPLICABLE,
            "仅 Streamable HTTP 使用会话 DELETE")

    capability_checks = (
        ("tools", "tools"),
        ("resources", "resources"),
        ("prompts", "prompts"),
    )
    for capability, check_id in capability_checks:
        if capability in server_caps:
            add(f"capability:{check_id}", "server_capability", STATUS_SUPPORTED,
                f"服务器已声明且客户端已实现 {capability}")
        else:
            add(f"capability:{check_id}", "server_capability",
                STATUS_NOT_APPLICABLE, f"服务器未声明 {capability}")
    if "resources" in server_caps:
        add("capability:resource_templates", "server_capability",
            STATUS_SUPPORTED, "resources capability 下支持 templates/list")
    else:
        add("capability:resource_templates", "server_capability",
            STATUS_NOT_APPLICABLE, "服务器未声明 resources")

    add("reverse:sampling", "reverse_request",
        STATUS_SUPPORTED if "sampling" in client_caps else STATUS_NOT_APPLICABLE,
        "sampling/createMessage 桥接共享 Provider"
        if "sampling" in client_caps else "未配置 sampler")
    add("reverse:roots", "reverse_request",
        STATUS_SUPPORTED if "roots" in client_caps else STATUS_NOT_APPLICABLE,
        "roots/list 返回工作区根"
        if "roots" in client_caps else "未配置 roots")
    elicitation_version_ok = _version_at_least(
        version, ELICITATION_PROTOCOL_VERSION)
    if not elicitation_version_ok:
        elicitation_status = STATUS_NOT_APPLICABLE
        elicitation_detail = (
            f"当前协商版本 {safe_version or 'unknown'} 早于 "
            f"{ELICITATION_PROTOCOL_VERSION}")
    elif "elicitation" in client_caps:
        elicitation_status = STATUS_SUPPORTED
        elicitation_detail = (
            "elicitation/create 经前台 broker 应答；Schema 与敏感字段 fail-closed")
    else:
        elicitation_status = STATUS_PARTIAL
        elicitation_detail = (
            "协议实现存在，但当前 headless/daemon 无前台，将自动 decline")
    add("reverse:elicitation", "reverse_request",
        elicitation_status, elicitation_detail)

    add("robustness:pagination", "robustness", STATUS_SUPPORTED,
        "四类列表均跟随 nextCursor，重复 cursor 与页数超限 fail-closed")
    add("robustness:list_changed", "robustness", STATUS_SUPPORTED,
        "tools/resources/prompts list_changed 串行刷新")
    add("robustness:unknown_method", "robustness", STATUS_SUPPORTED,
        "未知服务器请求返回 -32601")
    add("robustness:malformed_result", "robustness", STATUS_PARTIAL,
        "列表、调用与 Prompt 有严格校验；并非所有规范对象都做完整 Schema 验证")
    add("robustness:oversized_payload", "robustness", STATUS_PARTIAL,
        "MCP Schema/Prompt 有边界；JSON-RPC 与 Resource 尚无统一字节上限")

    add("spec:completion", "spec_feature", STATUS_UNSUPPORTED,
        "completion/complete 未实现")
    add("spec:logging", "spec_feature", STATUS_UNSUPPORTED,
        "logging/setLevel 未实现")
    add("spec:progress", "spec_feature", STATUS_UNSUPPORTED,
        "progressToken 与 notifications/progress 未接入")
    add("spec:resources_subscribe", "spec_feature", STATUS_UNSUPPORTED,
        "resources/subscribe 未实现")

    info = safe_label(
        (getattr(client, "server_info", {}) or {}).get("name", ""),
        max_chars=160, fallback="", redactor=redactor)
    return MCPConformanceReport(
        server=safe_label(
            getattr(client, "name", "?") or "?",
            max_chars=160, fallback="?", redactor=redactor),
        transport=transport,
        requested_protocol_version=safe_label(
            requested, max_chars=80, fallback="", redactor=redactor),
        negotiated_protocol_version=safe_version,
        server_info=info,
        client_capabilities=local_caps,
        server_capabilities=caps,
        loaded=loaded,
        checks=tuple(checks),
    )


def format_conformance_report(report: MCPConformanceReport) -> List[str]:
    counts = report.counts()
    loaded = " · ".join(
        f"{name}={value}" for name, value in report.loaded if value)
    lines = [
        f"  [mcp:conformance] {report.server}"
        f"（{report.server_info or 'unknown'}，{report.transport}）",
        f"  protocol: requested={report.requested_protocol_version or '?'}"
        f" · negotiated={report.negotiated_protocol_version or '?'}",
        "  client caps: "
        + (", ".join(report.client_capabilities) or "(none)")
        + " · server caps: "
        + (", ".join(report.server_capabilities) or "(none)"),
        "  loaded: " + (loaded or "(none)"),
        "  status: "
        + " · ".join(
            f"{status}={counts.get(status, 0)}"
            for status in STATUS_ORDER),
    ]
    for check in report.checks:
        lines.append(
            f"  [{check.status}] {check.id} · {check.detail}")
    lines.append(
        "  content-light=true · 未执行 tools/call、resources/read、"
        "OAuth 或 elicitation")
    return lines


def _version_at_least(version: str, minimum: str) -> bool:
    try:
        current = tuple(int(part) for part in version.split("-"))
        floor = tuple(int(part) for part in minimum.split("-"))
    except (TypeError, ValueError):
        return False
    return current >= floor


__all__ = [
    "ConformanceCheck",
    "ELICITATION_PROTOCOL_VERSION",
    "MCPConformanceReport",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "STATUS_NOT_APPLICABLE",
    "STATUS_PARTIAL",
    "STATUS_SUPPORTED",
    "STATUS_UNSUPPORTED",
    "build_conformance_report",
    "format_conformance_report",
]
