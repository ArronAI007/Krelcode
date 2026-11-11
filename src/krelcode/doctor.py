"""krelcode doctor：环境自检（离线、快速、不发起网络请求）。

检查项：
- Python 版本（>=3.11，tomllib/zoneinfo 依赖）
- provider API key（OPENAI/ANTHROPIC/GEMINI，脱敏显示；一个都没有 → warn）
- 默认 provider 可解析（resolve_settings 不报 ConfigError）
- 配置文件（显式 --config 必须存在且可解析；缺省探测报告命中的文件）
- git 可用性 + 工作区是否仓库 + 当前分支
- 工作区可写性（.krelcode 状态目录可创建）
- MCP 服务器：stdio → 命令存在性（which/路径）；url → 仅提示（不联网）

状态：ok(✓) / warn(⚠ 不阻断) / fail(✗ 影响使用)。exit code：有 fail → 1。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DEFAULT_PATHS
from .llm.registry import API_KEY_ENV, resolve_settings

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


def check_provider_keys() -> CheckResult:
    found, missing = [], []
    for provider, env_name in API_KEY_ENV.items():
        if not env_name:
            continue
        key = os.environ.get(env_name)
        if key:
            found.append(f"{provider}({env_name}={mask_key(key)})")
        else:
            missing.append(env_name)
    if found:
        return CheckResult("Provider API key", STATUS_OK, "、".join(found))
    return CheckResult("Provider API key", STATUS_WARN,
                       f"未设置任何 key（{', '.join(missing)}）；只能用 --mock")


def check_default_provider(config: Dict[str, Any]) -> CheckResult:
    try:
        settings = resolve_settings(config=config)
    except Exception as e:
        return CheckResult("默认 provider", STATUS_FAIL, f"{type(e).__name__}: {e}")
    detail = f"{settings.provider}"
    if settings.model:
        detail += f" / {settings.model}"
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


def check_git(workspace: Path) -> CheckResult:
    if shutil.which("git") is None:
        return CheckResult("git", STATUS_WARN, "git 不可用（git 快照与 --auto-commit 将失效）")
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        return CheckResult("git", STATUS_WARN, f"git 执行失败: {e}")
    if proc.returncode != 0:
        return CheckResult("git", STATUS_WARN, f"{workspace} 不是 git 仓库（上下文注入无 git 信息）")
    branch_proc = subprocess.run(
        ["git", "-C", str(workspace), "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True, timeout=5)
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

def run_all_checks(workspace: Path, config: Dict[str, Any],
                   config_path: Optional[Path] = None) -> List[CheckResult]:
    results = [
        check_python(),
        check_provider_keys(),
        check_default_provider(config),
        check_config(config_path),
        check_git(workspace),
        check_workspace_writable(workspace),
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
