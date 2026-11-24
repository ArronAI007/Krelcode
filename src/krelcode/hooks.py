"""用户 Hook 系统：配置驱动、生命周期事件与结构化 I/O。

安全模型：
- hooks 只能由用户在 TOML 配置里定义（LLM 无法注册钩子）；
  ``krelcode.toml`` 与 ``.krelcode/`` 已列入写保护路径，代理无法注入。
- Hook 命令由用户显式信任，不进入工具审批；但继承的是净化后的环境，
  默认不把宿主的 API key/token/proxy 等秘密传给 Hook 子进程。
- 模板变量值来自 LLM 工具参数或用户输入，替换前按平台 shell 规则引用。
- Hook 失败、超时、输出过大都不会中断主循环；只有明确的 block 决策或
  Unix 退出码 2 才会阻断对应的工具调用。

兼容性：旧版 ``{path}``/``{prompt}`` 等模板变量、纯文本 stdout 注入和
``user_input`` 触发器保持可用。新 Hook 同时从 stdin 收到 JSON payload，
可将结构化决策写到 stdout。
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .core.events import (
    AgentEvent,
    EventBus,
    RunFinished,
    SubagentFeedUpdated,
    SubagentFinished,
    SubagentSpawned,
    SubagentStateChanged,
    ToolCallCompleted,
)
from .core.hooks import HookResult
from .core.messages import ToolCall
from .core.process import (
    current_process_platform,
    quote_shell_value,
    stop_process_tree,
)
from .agent.prompt_surface_contract import (
    PromptSurfaceExecutor,
    contract_for_hook,
    observe_surface_result,
)
from .safety.environment import scrub_environment

SUPPORTED_TRIGGERS = (
    "session_start",
    "user_input",
    "user_prompt_submit",
    "pre_tool_use",
    "post_tool_use",
    "file_edited",
    "tool_completed",
    "tool_failed",
    "pre_compact",
    "post_compact",
    "subagent_started",
    "subagent_feed",
    "subagent_state",
    "subagent_finished",
    "run_finished",
    "session_end",
)
CONTEXT_TRIGGERS = {
    "session_start", "user_input", "user_prompt_submit", "pre_tool_use",
    "post_tool_use", "pre_compact", "post_compact",
}
TOOL_TRIGGERS = {
    "pre_tool_use", "post_tool_use", "file_edited",
    "tool_completed", "tool_failed",
}
FILE_EDIT_TOOLS = {"write_file", "edit_file"}
OUTPUT_PREVIEW = 400
DEFAULT_TIMEOUT = 60.0
MAX_HOOK_STDOUT = 64_000
MAX_HOOK_STDERR = 8_000
MAX_INPUT_HOOK_PROMPT = 12_000
MAX_INPUT_HOOK_CONTEXT = 4_000
MAX_HOOK_SYSTEM_MESSAGE = 4_000
_TEMPLATE_VARIABLE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ENV_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
HOOK_KINDS = frozenset({"command", "http", "prompt"})


@dataclass(frozen=True)
class HookSpec:
    on: str
    command: str = ""
    timeout: float = DEFAULT_TIMEOUT
    tools: tuple[str, ...] = ()
    source: str = "config"
    plugin_root: str = ""
    kind: str = "command"
    url: str = ""
    prompt: str = ""
    headers: tuple[tuple[str, str], ...] = ()

    @property
    def target(self) -> str:
        if self.kind == "http":
            return self.url
        if self.kind == "prompt":
            return "prompt"
        return self.command

    def matches_tool(self, tool_name: str) -> bool:
        if not self.tools:
            return True
        return any(fnmatch.fnmatchcase(tool_name, pattern) for pattern in self.tools)


def parse_hooks(config: Dict[str, Any]) -> List[HookSpec]:
    """从 TOML config 的 ``[[hooks]]`` 数组解析；非法项跳过。"""
    raw = config.get("hooks")
    if not isinstance(raw, list):
        return []
    specs: List[HookSpec] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("enabled", True) is False:
            continue
        on = str(item.get("on", "")).strip()
        if on not in SUPPORTED_TRIGGERS:
            continue
        kind = str(item.get("type") or item.get("kind") or "command").strip().lower()
        if kind not in HOOK_KINDS:
            continue
        command = item.get("command")
        prompt = item.get("prompt")
        http_config = item.get("http")
        if not isinstance(http_config, dict):
            http_config = {}
        url = item.get("url", http_config.get("url"))
        headers_raw = item.get("headers", http_config.get("headers", {}))
        if isinstance(headers_raw, dict):
            headers = tuple(
                (str(key), str(value)) for key, value in headers_raw.items())
        elif isinstance(headers_raw, list):
            headers = tuple(
                (str(pair[0]), str(pair[1]))
                for pair in headers_raw
                if isinstance(pair, (list, tuple)) and len(pair) == 2
            )
        else:
            headers = ()
        if kind == "command" and (
            not isinstance(command, str) or not command.strip()
        ):
            continue
        if kind == "http" and (
            not isinstance(url, str)
            or not url.strip()
            or not _valid_http_url_template(url)
        ):
            continue
        if kind == "prompt" and (
            not isinstance(prompt, str) or not prompt.strip()
        ):
            continue
        raw_tools = item.get("tools", item.get("tool", ()))
        if isinstance(raw_tools, str):
            tools = (raw_tools,)
        elif isinstance(raw_tools, (list, tuple)):
            tools = tuple(str(value).strip() for value in raw_tools if str(value).strip())
        else:
            tools = ()
        try:
            timeout = max(
                0.1, min(float(item.get("timeout", DEFAULT_TIMEOUT)), 600.0))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        specs.append(HookSpec(
            on=on,
            command=str(command or ""),
            timeout=timeout,
            tools=tools,
            source=str(item.get("_source") or "config"),
            plugin_root=str(item.get("_plugin_root") or ""),
            kind=kind,
            url=str(url or ""),
            prompt=str(prompt or ""),
            headers=headers,
        ))
    return specs


class HookRunner:
    """配置 Hook 的执行器，同时实现 AgentLoop 的 HookPort。"""

    def __init__(
        self,
        workspace_root: Path,
        specs: List[HookSpec],
        log=print,
        *,
        executions: Optional[List[Dict[str, Any]]] = None,
        redactor=None,
        prompt_runner: Optional[
            Callable[[str, Dict[str, Any]], Awaitable[str]]
        ] = None,
        prompt_surface_registry=None,
    ) -> None:
        self.root = Path(workspace_root)
        self.specs = list(specs)
        self._log = log
        self._redactor = redactor
        self._prompt_runner = prompt_runner
        self._prompt_surface_registry = prompt_surface_registry
        self.executions: List[Dict[str, Any]] = (
            executions if executions is not None else []
        )

    def replace_specs(self, specs: List[HookSpec]) -> None:
        """Atomically replace hook declarations after extension reload."""
        self.specs = list(specs)

    def rebind(self, workspace_root: Path) -> "HookRunner":
        """为子代理 worktree 创建同配置、共享审计记录的执行器。"""
        return HookRunner(
            workspace_root,
            self.specs,
            log=self._log,
            executions=self.executions,
            redactor=self._redactor,
            prompt_runner=self._prompt_runner,
            prompt_surface_registry=self._prompt_surface_registry,
        )

    def set_prompt_runner(
        self,
        runner: Optional[Callable[[str, Dict[str, Any]], Awaitable[str]]],
    ) -> None:
        self._prompt_runner = runner

    def attach(self, bus: EventBus) -> None:
        bus.subscribe(self.on_event)

    async def on_event(self, event: AgentEvent) -> None:
        for trigger, variables in self._map_event(event):
            await self._run_matching(trigger, variables)

    def _map_event(self, event: AgentEvent) -> List[tuple[str, Dict[str, str]]]:
        """观察型事件 → [(trigger, 模板变量)]。"""
        base = {"workspace": str(self.root)}
        if isinstance(event, ToolCallCompleted):
            name = event.tool_call.name
            base.update({
                "tool": name,
                "is_error": "true" if event.is_error else "false",
            })
            out = [("tool_completed", dict(base))]
            if event.is_error:
                failed = dict(base)
                failed["error"] = event.content[:OUTPUT_PREVIEW]
                out.append(("tool_failed", failed))
            elif name in FILE_EDIT_TOOLS:
                edited = dict(base)
                edited["path"] = str(event.tool_call.arguments.get("path", ""))
                out.append(("file_edited", edited))
            return out
        if isinstance(event, RunFinished):
            done = dict(base)
            done["reply"] = event.final_text[:OUTPUT_PREVIEW]
            return [("run_finished", done)]
        if isinstance(event, SubagentSpawned):
            return [("subagent_started", {
                **base,
                "agent_id": event.agent_id,
                "task": event.task[:OUTPUT_PREVIEW],
                "definition": event.definition,
            })]
        if isinstance(event, SubagentFeedUpdated):
            return [("subagent_feed", {
                **base,
                "agent_id": event.agent_id,
                "sequence": event.sequence,
                "kind": event.kind,
                "text": event.text[:OUTPUT_PREVIEW],
            })]
        if isinstance(event, SubagentStateChanged):
            return [("subagent_state", {
                **base,
                "agent_id": event.agent_id,
                "status": event.status,
                "reason": event.reason[:OUTPUT_PREVIEW],
            })]
        if isinstance(event, SubagentFinished):
            return [("subagent_finished", {
                **base,
                "agent_id": event.agent_id,
                "status": event.status,
                "preview": event.preview[:OUTPUT_PREVIEW],
            })]
        return []

    # -- HookPort 执行前/后 -------------------------------------------------------------

    async def before_tool(self, tool_call: ToolCall) -> HookResult:
        variables = self._tool_variables(tool_call)
        return await self._run_matching(
            "pre_tool_use", variables, tool_call=tool_call)

    async def after_tool(
        self, tool_call: ToolCall, content: str, is_error: bool,
    ) -> HookResult:
        variables = self._tool_variables(tool_call)
        variables.update({
            "is_error": "true" if is_error else "false",
            "result": content[:OUTPUT_PREVIEW],
            "error": content[:OUTPUT_PREVIEW] if is_error else "",
        })
        return await self._run_matching(
            "post_tool_use", variables, tool_call=tool_call)

    def _tool_variables(self, tool_call: ToolCall) -> Dict[str, str]:
        return {
            "workspace": str(self.root),
            "tool": tool_call.name,
            "path": str(tool_call.arguments.get("path", "")),
            "arguments": json.dumps(
                tool_call.arguments, ensure_ascii=False, sort_keys=True),
        }

    # -- 通用上下文 Hook ----------------------------------------------------------------

    async def collect_input_context(self, prompt: str) -> tuple[str, ...]:
        """运行前台 ``user_input`` Hook，返回成功 stdout 上下文。

        ``user_prompt_submit`` 属于查询引擎生命周期，在 AgentLoop 中统一执行，
        因此 headless、REPL 与子代理不会出现行为分叉。
        """
        variables = {
            "workspace": str(self.root),
            "prompt": prompt[:MAX_INPUT_HOOK_PROMPT],
        }
        result = await self._run_matching(
            "user_input", variables, plaintext_context=True)
        return tuple(item[:MAX_INPUT_HOOK_CONTEXT] for item in result.context)

    async def collect_context(
        self, trigger: str, variables: Optional[Dict[str, str]] = None,
    ) -> tuple[str, ...]:
        """运行任意上下文型触发器；失败返回空元组。"""
        result = await self.run_lifecycle(trigger, dict(variables or {}))
        return result.context

    async def run_lifecycle(
        self, trigger: str, variables: Optional[Dict[str, str]] = None,
    ) -> HookResult:
        """运行生命周期 Hook；普通 stdout 在上下文型事件中可注入。"""
        resolved = dict(variables or {})
        resolved.setdefault("workspace", str(self.root))
        return await self._run_matching(
            trigger,
            resolved,
            plaintext_context=trigger in CONTEXT_TRIGGERS,
        )

    # -- 执行与协议 ---------------------------------------------------------------------

    def render(self, command: str, variables: Dict[str, str]) -> str:
        """单次 {var} 占位替换；值按当前平台 shell 规则引用。"""
        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in variables:
                return match.group(0)
            return quote_shell_value(str(variables[key]))

        return _TEMPLATE_VARIABLE.sub(replace, command)

    async def run_hook(
        self, spec: HookSpec, trigger: str, variables: Dict[str, str],
        *,
        tool_call: Optional[ToolCall] = None,
        plaintext_context: bool = False,
    ) -> HookResult:
        payload = self._payload(trigger, variables, tool_call=tool_call)
        started = time.monotonic()
        record: Dict[str, Any] = {
            "trigger": trigger,
            "kind": spec.kind,
            "target": self._diagnostic_target(spec, variables),
            "tools": list(spec.tools),
            "source": spec.source,
        }
        if spec.kind == "command":
            record["command"] = self._redact(self.render(spec.command, variables))
        stdout = ""
        stderr = ""
        rc: Optional[int] = None
        try:
            if spec.kind == "command":
                rc, stdout, stderr = await self._execute_command_hook(
                    spec, trigger, variables, payload, tool_call)
            elif spec.kind == "http":
                rc, stdout, stderr = await self._execute_http_hook(
                    spec, payload)
            elif spec.kind == "prompt":
                rc, stdout, stderr = await self._execute_prompt_hook(
                    spec, variables, payload)
            else:
                raise ValueError(f"未知 Hook 类型: {spec.kind}")
        except Exception as exc:
            stderr = f"Hook 执行失败: {type(exc).__name__}: {exc}"

        result = self._interpret(
            trigger, rc, stdout, stderr,
            plaintext_context=plaintext_context,
        )
        output = self._redact("\n".join(
            part for part in (stdout.strip(), stderr.strip()) if part))
        record.update({
            "rc": rc,
            "output": output[:OUTPUT_PREVIEW],
            "stdout": self._redact(stdout[:MAX_HOOK_STDERR]),
            "stderr": self._redact(stderr[:MAX_HOOK_STDERR]),
            "ms": int((time.monotonic() - started) * 1000),
            "matched": result.matched,
            "blocked": result.blocked,
            "reason": result.reason,
            "context_count": len(result.context),
        })
        self.executions.append(record)
        decision = " blocked" if result.blocked else ""
        self._log(
            f"  [hook:{trigger}] rc={rc}{decision} ({record['ms']}ms) "
            f"{output.strip()[:120]}")
        return result

    async def _execute_command_hook(
        self,
        spec: HookSpec,
        trigger: str,
        variables: Dict[str, str],
        payload: Dict[str, Any],
        tool_call: Optional[ToolCall],
    ) -> tuple[Optional[int], str, str]:
        command = self.render(spec.command, variables)
        env = scrub_environment(os.environ)
        env.update({
            "KRELCODE_HOOK_EVENT": trigger,
            "KRELCODE_WORKSPACE": str(self.root),
            "KRELCODE_HOOK_SOURCE": spec.source,
        })
        if spec.plugin_root:
            env["KRELCODE_PLUGIN_ROOT"] = spec.plugin_root
        if tool_call is not None:
            env["KRELCODE_TOOL"] = tool_call.name
        process_platform = current_process_platform()
        proc = await asyncio.create_subprocess_exec(
            *process_platform.shell_argv(command),
            cwd=str(self.root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            **process_platform.subprocess_kwargs(),
        )
        try:
            raw_out, raw_err = await asyncio.wait_for(
                proc.communicate(
                    json.dumps(payload, ensure_ascii=False).encode("utf-8")),
                timeout=spec.timeout,
            )
            return (
                proc.returncode,
                raw_out.decode("utf-8", "replace")[:MAX_HOOK_STDOUT],
                raw_err.decode("utf-8", "replace")[:MAX_HOOK_STDERR],
            )
        except asyncio.TimeoutError:
            await stop_process_tree(proc, force=True, timeout=5)
            return None, "", f"超时（>{spec.timeout:g}s），已终止"
        except asyncio.CancelledError:
            await stop_process_tree(proc, force=True, timeout=5)
            raise
        except BaseException:
            await stop_process_tree(proc, force=True, timeout=5)
            raise

    async def _execute_http_hook(
        self, spec: HookSpec, payload: Dict[str, Any],
    ) -> tuple[Optional[int], str, str]:
        url = _ENV_VARIABLE.sub(
            lambda match: os.environ.get(match.group(1), ""), spec.url)
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("HTTP Hook 只允许 http/https URL")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain",
        }
        for key, raw in spec.headers:
            value = _ENV_VARIABLE.sub(
                lambda match: os.environ.get(match.group(1), ""), raw)
            if value:
                headers[key] = value
                if self._redactor is not None:
                    self._redactor.add(value)
                    _scheme, _, credential = value.partition(" ")
                    self._redactor.add(credential.strip())
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return await asyncio.to_thread(
            _post_hook_http, url, headers, body, spec.timeout)

    async def _execute_prompt_hook(
        self,
        spec: HookSpec,
        variables: Dict[str, str],
        payload: Dict[str, Any],
    ) -> tuple[Optional[int], str, str]:
        if self._prompt_runner is None:
            raise RuntimeError("Prompt Hook 未配置模型执行器")
        probe = self._render_prompt(spec.prompt, variables)
        prompt = (
            f"{probe.rstrip()}\n\n"
            "<hook_input>\n"
            "{hook_input_json}\n"
            "</hook_input>\n"
            "Return only the Hook decision as JSON, or the additional context text."
        )

        async def run_surface(safe_input: object) -> str:
            data = safe_input if isinstance(safe_input, dict) else {}
            rendered_prompt = str(data.get("prompt", "")).replace(
                "{hook_input_json}",
                json.dumps(
                    data.get("payload", {}), ensure_ascii=False, sort_keys=True),
            )
            safe_payload = data.get("payload", {})
            return await self._prompt_runner(
                rendered_prompt,
                safe_payload if isinstance(safe_payload, dict) else {},
            )

        try:
            index = self.specs.index(spec) + 1
        except ValueError:
            index = 1
        surface_id = f"hook:{spec.on}:{index}"
        result = await PromptSurfaceExecutor().execute(
            surface_id,
            contract_for_hook(spec.timeout),
            {"prompt": prompt, "payload": payload},
            run_surface,
            redactor=self._redactor,
            observer=lambda _surface_id, surface_result: observe_surface_result(
                self._prompt_surface_registry, surface_result),
        )
        if not result.ok:
            raise RuntimeError(
                f"Prompt Hook Surface {result.status}: {result.error}")
        return 0, result.text[:MAX_HOOK_STDOUT], ""

    def _render_prompt(self, prompt: str, variables: Dict[str, str]) -> str:
        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            return str(variables.get(key, match.group(0)))

        return _TEMPLATE_VARIABLE.sub(replace, prompt)

    def _redact(self, text: str) -> str:
        return self._redactor.redact(text) if self._redactor is not None else text

    def _diagnostic_target(
        self, spec: HookSpec, variables: Dict[str, str],
    ) -> str:
        if spec.kind == "http":
            url = _ENV_VARIABLE.sub(
                lambda match: os.environ.get(match.group(1), ""), spec.url)
            return self._redact(_safe_http_target(url))
        if spec.kind == "prompt":
            return "prompt"
        return self._redact(self.render(spec.command, variables))

    async def _run_matching(
        self,
        trigger: str,
        variables: Dict[str, str],
        *,
        tool_call: Optional[ToolCall] = None,
        plaintext_context: bool = False,
    ) -> HookResult:
        results: List[HookResult] = []
        tool_name = tool_call.name if tool_call is not None else variables.get("tool")
        for spec in self.specs:
            if spec.on != trigger:
                continue
            if tool_name and trigger in TOOL_TRIGGERS and not spec.matches_tool(tool_name):
                continue
            result = await self.run_hook(
                spec,
                trigger,
                variables,
                tool_call=tool_call,
                plaintext_context=plaintext_context,
            )
            results.append(result)
            if result.blocked:
                break
        return HookResult.merge(results)

    def _payload(
        self, trigger: str, variables: Dict[str, str],
        *, tool_call: Optional[ToolCall] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "event": trigger,
            "workspace": str(self.root),
            "timestamp": int(time.time()),
            "data": dict(variables),
        }
        if tool_call is not None:
            payload["tool"] = {
                "name": tool_call.name,
                "arguments": tool_call.arguments,
                "id": tool_call.id,
            }
        return payload

    def _interpret(
        self,
        trigger: str,
        rc: Optional[int],
        stdout: str,
        stderr: str,
        *,
        plaintext_context: bool,
    ) -> HookResult:
        text = stdout.strip()
        parsed: Optional[Dict[str, Any]] = None
        if text.startswith("{"):
            try:
                candidate = json.loads(text)
                if isinstance(candidate, dict):
                    parsed = candidate
            except json.JSONDecodeError:
                parsed = None

        contexts: List[str] = []
        system_message = ""
        blocked = False
        reason = ""
        if parsed is not None:
            raw_context = parsed.get(
                "additional_context", parsed.get("additionalContext"))
            if isinstance(raw_context, str) and raw_context.strip():
                contexts.append(raw_context.strip()[:MAX_INPUT_HOOK_CONTEXT])
            elif isinstance(raw_context, list):
                contexts.extend(
                    str(item).strip()[:MAX_INPUT_HOOK_CONTEXT]
                    for item in raw_context if str(item).strip())
            system_message = str(
                parsed.get("system_message", parsed.get("systemMessage", ""))
            ).strip()[:MAX_HOOK_SYSTEM_MESSAGE]
            decision = str(
                parsed.get("decision", parsed.get("permissionDecision", ""))).lower()
            if decision in {"block", "deny", "denied"}:
                blocked = True
                reason = str(
                    parsed.get("reason", parsed.get("message", "Hook blocked"))).strip()
        elif plaintext_context and rc == 0 and text:
            contexts.append(text[:MAX_INPUT_HOOK_CONTEXT])

        # Claude Code 兼容：退出码 2 = 阻断，并优先用 stderr 作为原因。
        if trigger == "pre_tool_use" and rc == 2:
            blocked = True
            reason = (stderr.strip() or text or "PreToolUse Hook 退出码 2")
        if blocked and not reason:
            reason = "Hook 阻止了工具调用"
        return HookResult(
            matched=True,
            blocked=blocked,
            reason=self._redact(reason),
            context=tuple(self._redact(context) for context in contexts),
            system_message=self._redact(system_message),
        )


def _post_hook_http(
    url: str,
    headers: Dict[str, str],
    body: bytes,
    timeout: float,
) -> tuple[Optional[int], str, str]:
    request = urllib.request.Request(
        url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_HOOK_STDOUT + 1)
            stdout = raw[:MAX_HOOK_STDOUT].decode("utf-8", "replace")
            return 0, stdout, ""
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(MAX_HOOK_STDOUT + 1)
        except Exception:
            raw = b""
        stdout = raw[:MAX_HOOK_STDOUT].decode("utf-8", "replace")
        return 1, stdout, f"HTTP Hook 返回 {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, "", f"HTTP Hook 执行失败: {exc}"


def _valid_http_url_template(url: str) -> bool:
    raw = str(url or "").strip()
    match = _URL_SCHEME.match(raw)
    if match is not None and match.group(0).lower() not in {"http:", "https:"}:
        return False
    if _ENV_VARIABLE.match(raw):
        # The complete origin may be supplied at runtime; execution validates
        # the expanded URL again before any request is sent.
        return True
    expanded = _ENV_VARIABLE.sub("placeholder", raw)
    parsed = urllib.parse.urlsplit(expanded)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _safe_http_target(url: str) -> str:
    """Keep scheme/host/path for diagnostics; drop userinfo/query/fragment."""
    parsed = urllib.parse.urlsplit(str(url or ""))
    if not parsed.scheme or not parsed.hostname:
        return "<invalid-http-url>"
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port is not None else host
    return urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path or "/", "", ""))
