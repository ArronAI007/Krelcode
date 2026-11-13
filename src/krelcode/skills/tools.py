"""暴露给 LLM 的 Skill 工具：发现、加载、资源读取和脚本执行。"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..safety.environment import scrub_environment
from ..safety.sandbox import SandboxError
from ..tools.base import ToolContext, ToolResult
from ..tools.builtin.run_command import MAX_OUTPUT_CHARS
from ..core.textutil import truncate_middle
from .manager import SkillManager
from .models import SkillError

DEFAULT_SCRIPT_TIMEOUT = 60.0
MAX_SCRIPT_TIMEOUT = 300.0
MAX_ARGS = 64


class ListSkillsTool:
    name = "list_skills"
    description = (
        "列出当前可用的 Agent Skills（名称、描述、来源、参数提示）。"
        "支持 query 关键词过滤。选择与任务匹配的 skill 后，必须调用 load_skill "
        "读取完整说明。"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "按名称/描述过滤（可选）"},
        },
    }

    def __init__(self, manager: SkillManager) -> None:
        self.manager = manager

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        skills = self.manager.list(
            query=str(args.get("query") or ""), model_invocable_only=True)
        if not skills:
            return ToolResult(content="（没有匹配的可用 skill）", data={"count": 0})
        lines = []
        for skill in skills:
            hint = f" · args: {skill.argument_hint}" if skill.argument_hint else ""
            lines.append(
                f"- {skill.name}: {skill.description} "
                f"[source={skill.source}{hint}]")
        return ToolResult(
            content="\n".join(lines),
            data={"count": len(skills), "names": [s.name for s in skills]},
        )


class LoadSkillTool:
    name = "load_skill"
    description = (
        "读取指定 Skill 的完整 SKILL.md 指令，并返回 skill 目录、可用资源和允许工具提示。"
        "遇到匹配任务时先调用本工具，再按其中说明执行。arguments 会替换 "
        "$ARGUMENTS 占位符。"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "skill 名称"},
            "arguments": {"type": "string", "description": "传给 skill 的参数（可选）"},
        },
        "required": ["name"],
    }

    def __init__(self, manager: SkillManager) -> None:
        self.manager = manager

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(args.get("name") or "").strip()
        try:
            content = self.manager.load(
                name, str(args.get("arguments") or ""), for_model=True)
        except (SkillError, OSError, ValueError) as e:
            return ToolResult(content=str(e), is_error=True)
        return ToolResult(content=content, data={"skill": name})


class ListSkillResourcesTool:
    name = "list_skill_resources"
    description = (
        "列出 Skill 目录内的参考文档、模板、资产和脚本。path 可限定子目录；"
        "返回路径可直接传给 read_skill_resource 或 run_skill_script。"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "skill 名称"},
            "path": {"type": "string", "description": "skill 内相对子目录（可选）"},
            "max_depth": {"type": "integer", "minimum": 1, "maximum": 8,
                          "description": "最大目录深度（默认 3）"},
        },
        "required": ["name"],
    }

    def __init__(self, manager: SkillManager) -> None:
        self.manager = manager

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(args.get("name") or "").strip()
        try:
            resources = self.manager.list_resources(
                name,
                str(args.get("path") or ""),
                int(args.get("max_depth") or 3),
            )
        except (SkillError, OSError, ValueError, TypeError) as e:
            return ToolResult(content=str(e), is_error=True)
        if not resources:
            return ToolResult(content="（该 skill 没有额外资源）", data={"count": 0})
        lines = [f"{r.kind:4s} {r.size:>8d}  {r.path}" for r in resources]
        return ToolResult(
            content="\n".join(lines),
            data={"count": len(resources), "paths": [r.path for r in resources]},
        )


class ReadSkillResourceTool:
    name = "read_skill_resource"
    description = (
        "读取 Skill 目录内的 UTF-8 文本资源，返回带行号内容。"
        "可用 offset/limit 分段读取；敏感凭据文件会被拦截或脱敏。"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "skill 名称"},
            "path": {"type": "string", "description": "skill 内相对文件路径"},
            "offset": {"type": "integer", "minimum": 1, "description": "起始行（默认 1）"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 2000,
                      "description": "最多读取行数（默认 2000）"},
        },
        "required": ["name", "path"],
    }

    def __init__(self, manager: SkillManager) -> None:
        self.manager = manager

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(args.get("name") or "").strip()
        path = str(args.get("path") or "").strip()
        try:
            content = self.manager.read_resource(
                name, path,
                offset=int(args.get("offset") or 1),
                limit=int(args.get("limit") or 2000),
            )
        except (SkillError, OSError, ValueError, TypeError) as e:
            return ToolResult(content=str(e), is_error=True)
        return ToolResult(content=content, data={"skill": name, "path": path})


class RunSkillScriptTool:
    name = "run_skill_script"
    description = (
        "执行 Skill 目录内的 bundled 脚本，使用参数数组而非 shell 字符串。"
        "支持 .py/.sh/.bash/.zsh/.js/.mjs/.rb/.pl 与可执行文件；"
        "输出经过截断，超时会终止整个进程组。执行脚本需要审批。"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "skill 名称"},
            "script": {"type": "string", "description": "skill 内相对脚本路径"},
            "args": {
                "type": "array", "items": {"type": "string"},
                "description": "传给脚本的参数数组（不经过 shell）",
            },
            "timeout": {"type": "number", "minimum": 1,
                        "description": f"超时秒数（默认 {int(DEFAULT_SCRIPT_TIMEOUT)}，上限 {int(MAX_SCRIPT_TIMEOUT)}）"},
        },
        "required": ["name", "script"],
    }

    def __init__(self, manager: SkillManager) -> None:
        self.manager = manager

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(args.get("name") or "").strip()
        script = str(args.get("script") or "").strip()
        raw_args = args.get("args") or []
        if not isinstance(raw_args, list) or len(raw_args) > MAX_ARGS:
            return ToolResult(content=f"args 必须是最多 {MAX_ARGS} 项的数组", is_error=True)
        script_args = [str(a) for a in raw_args]
        if any(len(a) > 2000 for a in script_args):
            return ToolResult(content="单个脚本参数不能超过 2000 字符", is_error=True)
        try:
            skill, path = self.manager.resolve_script(name, script)
            command = _script_command(path) + [str(path)] + script_args
        except (SkillError, OSError, ValueError) as e:
            return ToolResult(content=str(e), is_error=True)

        timeout = _clamp_timeout(args.get("timeout"))
        sandbox = getattr(ctx, "sandbox", None)
        extra_roots = [skill.directory]
        if sandbox is not None:
            violation = sandbox.audit_exec(
                command, skill.directory, allowed_read_roots=extra_roots)
            if violation:
                return ToolResult(
                    content=f"Sandbox 拒绝 Skill 脚本: {violation}", is_error=True,
                    data={"sandbox_denied": True},
                )
            try:
                prepared = sandbox.prepare_exec(
                    command, skill.directory, extra_read_roots=extra_roots)
            except SandboxError as e:
                return ToolResult(
                    content=f"Sandbox 准备失败: {e}", is_error=True,
                    data={"sandbox_denied": True},
                )
            exec_argv = prepared.argv
            env = prepared.env
        else:
            exec_argv = command
            env = scrub_environment(os.environ)
        try:
            proc = await asyncio.create_subprocess_exec(
                *exec_argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(skill.directory),
                start_new_session=True,
                env=env,
            )
        except OSError as e:
            return ToolResult(content=f"无法启动 skill 脚本: {e}", is_error=True)
        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            _terminate_process_tree(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            return ToolResult(
                content=f"skill 脚本超时（>{timeout:g}s），已终止: {script}",
                is_error=True,
                data={"timed_out": True, "exit_code": None},
            )
        output = truncate_middle(
            raw.decode("utf-8", "replace"), MAX_OUTPUT_CHARS)
        code = proc.returncode if proc.returncode is not None else -1
        return ToolResult(
            content=f"exit_code: {code}\n{output if output.strip() else '（无输出）'}",
            is_error=code != 0,
            data={
                "exit_code": code, "timed_out": False, "script": script,
                "sandbox_backend": getattr(sandbox, "backend", "none"),
            },
        )


def build_skill_tools(manager: SkillManager) -> List[Any]:
    return [
        ListSkillsTool(manager),
        LoadSkillTool(manager),
        ListSkillResourcesTool(manager),
        ReadSkillResourceTool(manager),
        RunSkillScriptTool(manager),
    ]


def _script_command(path: Path) -> List[str]:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return [sys.executable]
    if suffix in {".sh", ".bash"}:
        shell = shutil.which("bash") or shutil.which("sh")
        if not shell:
            raise ValueError("找不到 shell 解释器")
        return [shell]
    if suffix == ".zsh":
        zsh = shutil.which("zsh")
        if not zsh:
            raise ValueError("找不到 zsh 解释器")
        return [zsh]
    if suffix in {".js", ".mjs"}:
        node = shutil.which("node")
        if not node:
            raise ValueError("找不到 Node.js 解释器")
        return [node]
    if suffix == ".rb":
        ruby = shutil.which("ruby")
        if not ruby:
            raise ValueError("找不到 Ruby 解释器")
        return [ruby]
    if suffix == ".pl":
        perl = shutil.which("perl")
        if not perl:
            raise ValueError("找不到 Perl 解释器")
        return [perl]
    if os.access(path, os.X_OK):
        return []
    raise ValueError(f"无法判断脚本解释器（或文件不可执行）: {path.name}")


def _clamp_timeout(value: Any) -> float:
    try:
        t = float(value)
    except (TypeError, ValueError):
        return DEFAULT_SCRIPT_TIMEOUT
    return max(1.0, min(t, MAX_SCRIPT_TIMEOUT))


def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
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
