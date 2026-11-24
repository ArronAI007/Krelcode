"""krelcode doctor：环境自检（离线、快速、不发起网络请求）。

检查项：
- Python 版本（>=3.11，tomllib/zoneinfo 依赖）
- provider API key（OPENAI/ANTHROPIC/GEMINI，脱敏显示；一个都没有 → warn）
- 默认 provider 可解析（resolve_settings 不报 ConfigError）
- 配置文件（显式 --config 必须存在且可解析；缺省探测报告命中的文件）
- git 可用性 + 工作区是否仓库 + 当前分支
- 工作区可写性（.krelcode 状态目录可创建）
- 运行时 Sandbox 后端（OS 隔离可用性；static 降级明确警告）
- MCP 服务器：stdio → 命令存在性（which/路径）；url → 仅提示（不联网）
- 企业策略：managed 层解析、权限 ceiling、企业 SSO 与独立审计链状态

状态：ok(✓) / warn(⚠ 不阻断) / fail(✗ 影响使用)。exit code：有 fail → 1。
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .agent.pricing import match_context_window
from .config import DEFAULT_PATHS, ConfigBundle
from .llm.registry import API_KEY_ENV, PROVIDERS, resolve_settings
from .safety.sandbox import ExecutionSandbox, SandboxError
from .core.process import process_isolation_kwargs

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
SYMBOLS = {STATUS_OK: "✓", STATUS_WARN: "⚠", STATUS_FAIL: "✗"}


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str


def mask_key(key: str) -> str:
    if len(key) > 12:
        return key[:6] + "…" + key[-4:]
    return "…"


# -- 各检查项（纯函数，全部可单测） -----------------------------------------------------------

def check_python() -> CheckResult:
    v = sys.version_info
    version_str = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= (3, 11):
        return CheckResult("Python 版本", STATUS_OK, version_str)
    return CheckResult("Python 版本", STATUS_FAIL,
                       f"{version_str}（需要 >= 3.11，tomllib/zoneinfo 依赖）")


def _key_env_for(provider: str, config: Dict[str, Any]) -> str:
    """返回 provider 预期读取的 key 环境变量名（优先 config 的 api_key_env）。"""
    section = (config.get("providers") or {}).get(provider) or {}
    return str(section.get("api_key_env") or API_KEY_ENV.get(provider, ""))


def check_provider_keys(config: Optional[Dict[str, Any]] = None) -> CheckResult:
    config = config or {}
    # 默认 provider 经 resolve_settings 解析：尊重 config 的 api_key / api_key_env
    # （例如 [providers.openai] api_key_env = "DEEPSEEK_API_KEY"）。
    try:
        settings = resolve_settings(config=config)
    except Exception:
        settings = None

    if settings is not None and settings.api_key:
        return CheckResult("Provider API key", STATUS_OK,
                           f"{settings.provider}(key={mask_key(settings.api_key)})")

    found, missing = [], []
    default_provider = settings.provider if settings is not None else "openai"
    # 默认 provider 的预期 env（含自定义 api_key_env，避免误报「未设置」）
    env_name = _key_env_for(default_provider, config)
    if env_name:
        key = os.environ.get(env_name)
        if key:
            found.append(f"{default_provider}({env_name}={mask_key(key)})")
        else:
            missing.append(env_name)
    # 其余标准 provider env（任一有 key 也算可用信号）
    for provider, env in API_KEY_ENV.items():
        if not env or provider == default_provider:
            continue
        key = os.environ.get(env)
        if key:
            found.append(f"{provider}({env}={mask_key(key)})")
        else:
            missing.append(env)

    if found:
        return CheckResult("Provider API key", STATUS_OK, "、".join(found))
    missing_uniq = list(dict.fromkeys(m for m in missing if m))
    return CheckResult("Provider API key", STATUS_WARN,
                       f"未设置任何 key（{', '.join(missing_uniq)}）；只能用 --mock")


def check_default_provider(config: Dict[str, Any]) -> CheckResult:
    try:
        settings = resolve_settings(config=config)
    except Exception as e:
        return CheckResult("默认 provider", STATUS_FAIL, f"{type(e).__name__}: {e}")
    provider_cls = PROVIDERS.get(settings.provider)
    model = settings.model or getattr(provider_cls, "DEFAULT_MODEL", None)
    detail = f"{settings.provider}"
    if model:
        detail += f" / {model}"
        window = match_context_window(model)
        if window:
            detail += f"（上下文 {window // 1000}k，压缩预算 {int(window * 0.75) // 1000}k）"
    if settings.api_key:
        detail += f"（key {mask_key(settings.api_key)}）"
    return CheckResult("默认 provider", STATUS_OK, detail)


def check_config(explicit_path: Optional[Path]) -> CheckResult:
    if explicit_path is not None:
        if not explicit_path.is_file():
            return CheckResult("配置文件", STATUS_FAIL, f"指定的文件不存在: {explicit_path}")
        try:
            with open(explicit_path, "rb") as f:
                tomllib.load(f)
            return CheckResult("配置文件", STATUS_OK, f"{explicit_path}（解析成功）")
        except (tomllib.TOMLDecodeError, OSError) as e:
            return CheckResult("配置文件", STATUS_FAIL, f"{explicit_path} 解析失败: {e}")
    for candidate in DEFAULT_PATHS:
        if candidate.is_file():
            try:
                with open(candidate, "rb") as f:
                    tomllib.load(f)
                return CheckResult("配置文件", STATUS_OK, f"使用 {candidate}")
            except (tomllib.TOMLDecodeError, OSError) as e:
                return CheckResult("配置文件", STATUS_FAIL, f"{candidate} 解析失败: {e}")
    return CheckResult("配置文件", STATUS_WARN,
                       "未找到 krelcode.toml / ~/.krelcode/config.toml（用默认值；krelcode init 可生成模板）")


def check_config_layers(bundle: ConfigBundle) -> CheckResult:
    """Validate the effective user/project/local/managed layer set."""
    explicit_missing = any(
        layer.scope == "explicit" and not layer.exists
        for layer in bundle.layers
    )
    if explicit_missing:
        path = next(
            layer.path for layer in bundle.layers
            if layer.scope == "explicit" and not layer.exists)
        return CheckResult("配置文件", STATUS_FAIL, f"指定的文件不存在: {path}")
    if bundle.errors:
        detail = "；".join(
            f"{layer.scope} {layer.path}: {layer.error}"
            for layer in bundle.errors)
        return CheckResult("配置文件", STATUS_FAIL, detail)
    sources = bundle.sources
    if not sources:
        return CheckResult(
            "配置文件", STATUS_WARN,
            "未找到 user/project/local/managed 配置（使用默认值）")
    detail = " → ".join(
        f"{layer.scope}:{layer.path}"
        for layer in bundle.layers if layer.exists)
    return CheckResult("配置文件", STATUS_OK, f"解析成功（{detail}）")


def check_enterprise_policy(
    config: Dict[str, Any],
    config_bundle: Optional[ConfigBundle] = None,
) -> CheckResult:
    """Validate managed policy without trusting project/user overrides."""
    from .enterprise.policy import load_enterprise_policy

    try:
        policy = load_enterprise_policy(config, config_bundle=config_bundle)
    except Exception as exc:
        return CheckResult("企业策略", STATUS_FAIL, f"{type(exc).__name__}: {exc}")
    if not policy.enabled:
        return CheckResult("企业策略", STATUS_OK, "未启用（向后兼容）")
    detail = (
        f"organization={policy.organization or '-'} "
        f"policy={policy.policy_version or '-'} "
        f"max_mode={policy.max_permission_mode.value} "
        f"auto_approve={'on' if policy.allow_auto_approve else 'off'} "
        f"no_sandbox={'on' if policy.allow_no_sandbox else 'off'} "
        f"telemetry={'on' if policy.allow_telemetry else 'off'}"
    )
    return CheckResult("企业策略", STATUS_OK, detail)


def _private_mode_warning(path: Path) -> str:
    try:
        if not path.exists():
            return ""
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        return f"{path.name} 权限读取失败: {type(exc).__name__}"
    if mode & 0o077:
        return f"{path.name} 权限过宽({oct(mode)})"
    return ""


def check_telemetry(
    config: Dict[str, Any],
    config_bundle: Optional[ConfigBundle] = None,
) -> CheckResult:
    """Check consent, queue bounds and managed policy without network access."""
    from .enterprise.policy import load_enterprise_policy
    from .telemetry import (
        TelemetryConfig,
        TelemetryConfigError,
        TelemetryPreferenceStore,
        TelemetryRuntime,
        TelemetryStoreError,
    )

    section = config.get("telemetry") or {}
    if not isinstance(section, Mapping):
        return CheckResult(
            "匿名遥测", STATUS_FAIL, "[telemetry] 必须是 TOML table")
    try:
        policy = load_enterprise_policy(config, config_bundle=config_bundle)
        telemetry_config = TelemetryConfig.from_mapping(section)
        # from_mapping deliberately fails closed. Doctor performs a second,
        # explicit consent read so malformed/symlinked consent is observable.
        TelemetryPreferenceStore(telemetry_config.preferences_path).load()
        runtime = TelemetryRuntime(
            telemetry_config,
            provider="cli",
            model="doctor",
            run_mode="doctor",
            policy_allowed=policy.allow_telemetry,
        )
        status = runtime.status()
    except (TelemetryConfigError, TelemetryStoreError, OSError, ValueError) as exc:
        return CheckResult("匿名遥测", STATUS_FAIL, str(exc))
    if not policy.allow_telemetry:
        return CheckResult(
            "匿名遥测", STATUS_OK,
            "managed 策略禁止（off；content_collection=never）")
    if status["last_error"]:
        return CheckResult("匿名遥测", STATUS_FAIL, status["last_error"])
    if not status["effective_enabled"]:
        reason = (
            "sample_rate=0" if status["sample_rate"] <= 0
            else "默认关闭，需显式 krelcode telemetry enable"
        )
        return CheckResult(
            "匿名遥测", STATUS_OK,
            f"off（{reason}；content_collection=never）")
    if status["mode"] == "remote" and not status["endpoint_configured"]:
        return CheckResult("匿名遥测", STATUS_FAIL, "remote 模式缺少 HTTPS endpoint")
    queue = status["queue"]
    if not queue["valid"] or queue["error"]:
        return CheckResult(
            "匿名遥测", STATUS_FAIL,
            f"queue 不可用: {queue['error'] or 'invalid'}")
    permission_warnings = [
        warning
        for warning in (
            _private_mode_warning(Path(queue["path"])),
            _private_mode_warning(Path(status["queue"]["path"]).parent),
        )
        if warning
    ]
    if permission_warnings:
        return CheckResult(
            "匿名遥测", STATUS_WARN, "；".join(dict.fromkeys(permission_warnings)))
    return CheckResult(
        "匿名遥测", STATUS_OK,
        f"mode={status['mode']} · queue={queue['events']} events/"
        f"{queue['bytes']} bytes · retention={status['retention_days']}d · "
        "content_collection=never")


def check_audit(
    config: Dict[str, Any],
    workspace: Path,
    config_bundle: Optional[ConfigBundle] = None,
) -> CheckResult:
    from .enterprise.audit import AuditConfig, AuditError, verify_audit_file
    from .enterprise.policy import load_enterprise_policy

    try:
        policy = load_enterprise_policy(config, config_bundle=config_bundle)
        audit = AuditConfig.from_mapping(policy.audit, workspace=workspace)
    except Exception as exc:
        return CheckResult("企业审计", STATUS_FAIL, f"{type(exc).__name__}: {exc}")
    if not audit.enabled:
        return CheckResult("企业审计", STATUS_OK, "未启用")
    if audit.mode == "hmac":
        if not audit.hmac_key_env or not os.environ.get(audit.hmac_key_env):
            return CheckResult(
                "企业审计", STATUS_FAIL,
                f"HMAC 模式缺少环境变量 {audit.hmac_key_env or '(未配置)'}")
    parent = audit.path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        return CheckResult("企业审计", STATUS_FAIL, f"目录不可创建: {parent}: {exc}")
    if not os.access(parent, os.W_OK):
        return CheckResult("企业审计", STATUS_FAIL, f"目录不可写: {parent}")
    if audit.path.is_file():
        key = None
        if audit.mode == "hmac" and audit.hmac_key_env:
            key = os.environ.get(audit.hmac_key_env, "").encode("utf-8") or None
        result = verify_audit_file(audit.path, hmac_key=key)
        if not result.valid:
            return CheckResult(
                "企业审计", STATUS_FAIL,
                f"{audit.path}: {result.first_error}")
        return CheckResult(
            "企业审计", STATUS_OK,
            f"{audit.path}（{audit.mode}，{result.records} records，链完整）")
    state = "required" if audit.required else "optional"
    return CheckResult(
        "企业审计", STATUS_OK if audit.required else STATUS_WARN,
        f"{audit.path}（{audit.mode}，{state}，尚未创建）")


def check_sso(
    config: Dict[str, Any],
    config_bundle: Optional[ConfigBundle] = None,
) -> CheckResult:
    from .enterprise.auth import AuthError, OIDCClient, load_sso_config
    from .enterprise.policy import load_enterprise_policy

    try:
        policy = load_enterprise_policy(config, config_bundle=config_bundle)
        sso = load_sso_config(policy)
        if not sso.enabled:
            if policy.require_sso:
                return CheckResult("企业 SSO", STATUS_FAIL, "策略要求 SSO 但未配置")
            return CheckResult("企业 SSO", STATUS_OK, "未启用")
        status = OIDCClient(sso).status()
    except Exception as exc:
        return CheckResult("企业 SSO", STATUS_FAIL, f"{type(exc).__name__}: {exc}")
    if status.authenticated:
        subject = status.principal.subject if status.principal else "-"
        return CheckResult(
            "企业 SSO", STATUS_OK,
            f"已认证 subject={subject} source={status.source}")
    if policy.require_sso:
        return CheckResult("企业 SSO", STATUS_FAIL, status.reason or "未认证")
    return CheckResult("企业 SSO", STATUS_WARN, status.reason or "未认证")


def check_git(workspace: Path) -> CheckResult:
    if shutil.which("git") is None:
        return CheckResult("git", STATUS_WARN, "git 不可用（git 快照与 --auto-commit 将失效）")
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5,
            **process_isolation_kwargs())
    except (OSError, subprocess.TimeoutExpired) as e:
        return CheckResult("git", STATUS_WARN, f"git 执行失败: {e}")
    if proc.returncode != 0:
        return CheckResult("git", STATUS_WARN, f"{workspace} 不是 git 仓库（上下文注入无 git 信息）")
    branch_proc = subprocess.run(
        ["git", "-C", str(workspace), "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True, timeout=5,
        **process_isolation_kwargs())
    branch = branch_proc.stdout.strip() or "(无提交)"
    return CheckResult("git", STATUS_OK, f"仓库就绪，当前分支: {branch}")


def check_workspace_writable(workspace: Path) -> CheckResult:
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        state_dir = workspace / ".krelcode"
        state_dir.mkdir(parents=True, exist_ok=True)
        probe = state_dir / ".doctor-probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return CheckResult("工作区可写", STATUS_OK, str(workspace))
    except OSError as e:
        return CheckResult("工作区可写", STATUS_FAIL, f"{workspace}: {e}")


def check_sandbox(config: Optional[Dict[str, Any]] = None) -> CheckResult:
    config = config or {}
    section = config.get("sandbox") or {}
    if not isinstance(section, dict):
        return CheckResult("运行时 Sandbox", STATUS_FAIL, "[sandbox] 必须是 TOML table")
    backend = str(section.get("backend", "auto"))
    network = bool(section.get("network", False))
    try:
        sandbox = ExecutionSandbox(
            Path.cwd(), backend=backend, network=network)
    except SandboxError as e:
        return CheckResult("运行时 Sandbox", STATUS_FAIL, str(e))
    capabilities = sandbox.capabilities
    state = "network=on" if sandbox.network else "network=off"
    platform = (
        f"platform={capabilities.platform} "
        f"shell={capabilities.shell_dialect} backend={sandbox.backend} {state}"
    )
    if sandbox.backend == "static":
        return CheckResult(
            "运行时 Sandbox", STATUS_WARN,
            f"{platform}（仅静态护栏；无 OS 文件/网络隔离；进程树统一终止）")
    isolation = (
        "OS 文件/网络隔离"
        if capabilities.filesystem_isolation and capabilities.network_isolation
        else "OS 文件隔离；网络已显式允许"
    )
    return CheckResult(
        "运行时 Sandbox", STATUS_OK,
        f"{platform}（{isolation}；进程树统一终止）")


def check_mcp_servers(config: Dict[str, Any]) -> List[CheckResult]:
    results: List[CheckResult] = []
    servers: Dict[str, Any] = config.get("mcp_servers") or {}
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            results.append(CheckResult(f"MCP {name}", STATUS_FAIL, "配置不是表（table）"))
            continue
        command = spec.get("command")
        url = spec.get("url")
        if command:
            cmd_str = str(command)
            exists = (Path(cmd_str).is_file() if os.sep in cmd_str
                      else shutil.which(cmd_str) is not None)
            if exists:
                results.append(CheckResult(f"MCP {name}", STATUS_OK, f"命令存在: {cmd_str}"))
            else:
                results.append(CheckResult(f"MCP {name}", STATUS_FAIL, f"命令不存在: {cmd_str}"))
        elif url:
            results.append(CheckResult(f"MCP {name}", STATUS_WARN,
                                       f"{url}（doctor 不联网，启动时实际连接）"))
        else:
            results.append(CheckResult(f"MCP {name}", STATUS_FAIL, "缺少 command 或 url"))
    return results


# -- 汇总 -------------------------------------------------------------------------------------------

def run_all_checks(
    workspace: Path,
    config: Dict[str, Any],
    config_path: Optional[Path] = None,
    *,
    config_bundle: Optional[ConfigBundle] = None,
) -> List[CheckResult]:
    results = [
        check_python(),
        check_provider_keys(config),
        check_default_provider(config),
        (check_config_layers(config_bundle)
         if config_bundle is not None else check_config(config_path)),
        check_git(workspace),
        check_workspace_writable(workspace),
        check_sandbox(config),
        check_enterprise_policy(config, config_bundle),
        check_telemetry(config, config_bundle),
        check_audit(config, workspace, config_bundle),
        check_sso(config, config_bundle),
    ]
    results.extend(check_mcp_servers(config))
    return results


def format_report(results: List[CheckResult]) -> str:
    lines = ["Krelcode 环境诊断", "=" * 40]
    for r in results:
        lines.append(f" {SYMBOLS[r.status]} {r.name}: {r.detail}")
    counts = {s: sum(1 for r in results if r.status == s) for s in (STATUS_OK, STATUS_WARN, STATUS_FAIL)}
    lines.append("=" * 40)
    lines.append(f"共 {len(results)} 项：{counts[STATUS_OK]} 通过 / "
                 f"{counts[STATUS_WARN]} 警告 / {counts[STATUS_FAIL]} 失败")
    return "\n".join(lines)


def exit_code(results: List[CheckResult]) -> int:
    return 1 if any(r.status == STATUS_FAIL for r in results) else 0
