"""用户 Hook 系统：配置文件定义的事件钩子（dsh「一切皆副作用」的轻量版）。

典型用途：file_edited 后自动跑 formatter/linter、run_finished 后发通知。

安全模型：
- hooks 只能由用户在 TOML 配置里定义（LLM 无法注册钩子）；
  krelcode.toml 与 .krelcode/ 已列入写保护路径（Step 19a），
  代理无法通过改配置注入钩子
- 钩子命令以用户身份直接执行（不走 SandboxPolicy 审批）——配置即信任
- 模板变量值来自 LLM 的工具参数（如 path），一律 shlex.quote 后再拼接，
  防命令注入
- 钩子失败/超时只记日志，绝不影响主循环（失败安全）
"""

from __future__ import annotations

import asyncio
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .core.events import (
    AgentEvent, EventBus, RunFinished, ToolCallCompleted,
)

SUPPORTED_TRIGGERS = ("file_edited", "tool_completed", "tool_failed", "run_finished")
FILE_EDIT_TOOLS = {"write_file", "edit_file"}
OUTPUT_PREVIEW = 400
DEFAULT_TIMEOUT = 60.0


@dataclass(frozen=True)
class HookSpec:
    on: str
    command: str
    timeout: float = DEFAULT_TIMEOUT


def parse_hooks(config: Dict[str, Any]) -> List[HookSpec]:
    """从 TOML config 的 [[hooks]] 数组解析；非法项跳过（返回前不抛错）。"""
    raw = config.get("hooks")
    if not isinstance(raw, list):
        return []
    specs: List[HookSpec] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        on = str(item.get("on", ""))
        command = item.get("command")
        if on not in SUPPORTED_TRIGGERS or not isinstance(command, str) or not command.strip():
            continue
        try:
            timeout = max(0.1, min(float(item.get("timeout", DEFAULT_TIMEOUT)), 600.0))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        specs.append(HookSpec(on=on, command=command, timeout=timeout))
    return specs


class HookRunner:
    def __init__(self, workspace_root: Path, specs: List[HookSpec], log=print) -> None:
        self.root = Path(workspace_root)
        self.specs = list(specs)
        self._log = log
        self.executions: List[Dict[str, Any]] = []  # 审计记录（测试/调试用）

    def attach(self, bus: EventBus) -> None:
        bus.subscribe(self.on_event)

    async def on_event(self, event: AgentEvent) -> None:
        for trigger, variables in self._map_event(event):
            for spec in self.specs:
                if spec.on == trigger:
                    await self.run_hook(spec, trigger, variables)

    def _map_event(self, event: AgentEvent) -> List[tuple]:
        """事件 → [(trigger, 模板变量)]，一个事件可触发多个 trigger。"""
        base = {"workspace": str(self.root)}
        if isinstance(event, ToolCallCompleted):
            name = event.tool_call.name
            base.update({"tool": name, "is_error": "true" if event.is_error else "false"})
            out = [("tool_completed", dict(base))]
            if event.is_error:
                vars_failed = dict(base)
                vars_failed["error"] = event.content[:OUTPUT_PREVIEW]
                out.append(("tool_failed", vars_failed))
            elif name in FILE_EDIT_TOOLS:
                vars_edited = dict(base)
                path = event.tool_call.arguments.get("path", "")
                vars_edited["path"] = str(path)
                out.append(("file_edited", vars_edited))
            return out
        if isinstance(event, RunFinished):
            vars_done = dict(base)
            vars_done["reply"] = event.final_text[:OUTPUT_PREVIEW]
            return [("run_finished", vars_done)]
        return []

    # -- 执行 ---------------------------------------------------------------------------

    def render(self, command: str, variables: Dict[str, str]) -> str:
        """{var} 占位替换；值一律 shell-quote（值可能来自 LLM 参数）。"""
        rendered = command
        for key, value in variables.items():
            rendered = rendered.replace("{" + key + "}", shlex.quote(str(value)))
        return rendered

    async def run_hook(self, spec: HookSpec, trigger: str,
                       variables: Dict[str, str]) -> None:
        command = self.render(spec.command, variables)
        started = time.monotonic()
        record: Dict[str, Any] = {"trigger": trigger, "command": command}
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self.root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=spec.timeout)
                record["rc"] = proc.returncode
                record["output"] = out.decode("utf-8", "replace")[:OUTPUT_PREVIEW]
            except asyncio.TimeoutError:
                _terminate(proc)
                record["rc"] = None
                record["output"] = f"超时（>{spec.timeout:g}s），已终止"
        except OSError as e:
            record["rc"] = None
            record["output"] = f"无法启动: {e}"
        record["ms"] = int((time.monotonic() - started) * 1000)
        self.executions.append(record)
        self._log(f"  [hook:{trigger}] rc={record['rc']} ({record['ms']}ms) "
                  f"{record['output'].strip()[:120]}")


def _terminate(proc: asyncio.subprocess.Process) -> None:
    import os
    import signal
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError, AttributeError):
            pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass
