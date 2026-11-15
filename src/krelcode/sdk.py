"""零依赖 Python SDK：通过 stream-json 子进程复用完整 Krelcode CLI。

SDK 不复制 Provider、工具、MCP、Hooks 或 Sandbox 的组装逻辑。它在独立
Python 子进程中启动 ``python -m krelcode``，使用 UTF-8 JSONL 双向通信：

* stdin：``user`` / ``control`` 消息；
* stdout：`krelcode.stream-json` v1 事件流；
* stderr：仅保留有界诊断尾部，异常文本会二次脱敏。

API key 只随用户显式提供的环境继承给子进程，不进入命令参数、消息或异常对象。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Optional, Sequence

PROTOCOL_NAME = "krelcode.stream-json"
PROTOCOL_VERSION = "1"
_EOF = object()
_RESERVED_ARGS = {
    "-m", "--message", "--input-format", "--output-format", "--version",
}


class SDKError(RuntimeError):
    """SDK 基础异常。"""


class SDKProtocolError(SDKError):
    """子进程输出不是合法 stream-json 协议。"""


class SDKProcessError(SDKError):
    """子进程提前退出、启动失败或无法通信。"""


class SDKResultError(SDKError):
    """查询正常结束，但终态 ``result.is_error`` 为真。"""

    def __init__(self, message: str, result: "KrelcodeMessage") -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True)
class ImageInput:
    """内联图片输入；协议只传 base64，source_path 仅作可追踪元数据。"""

    media_type: str
    data_b64: str
    source_path: Optional[str] = None

    def to_block(self) -> dict[str, Any]:
        source: dict[str, Any] = {
            "type": "base64",
            "media_type": self.media_type,
            "data": self.data_b64,
        }
        if self.source_path:
            source["path"] = self.source_path
        return {"type": "image", "source": source}


@dataclass(frozen=True)
class KrelcodeOptions:
    """子进程启动选项。

    ``env`` 是附加/覆盖到父进程环境上的映射。不要把 API key 放进
    ``extra_args``；推荐沿用 Krelcode 的 ``api_key_env`` 配置机制。
    """

    workspace: Path | str = field(default_factory=Path.cwd)
    provider: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    config: Optional[Path | str] = None
    mock: bool = False
    permission_mode: Optional[str] = None
    auto_approve: bool = False
    no_sandbox: bool = False
    no_context: bool = False
    no_skills: bool = False
    skills_dirs: tuple[Path | str, ...] = ()
    no_mcp: bool = False
    no_session: bool = False
    no_checkpoint: bool = False
    auto_commit: bool = False
    no_compress: bool = False
    max_steps: Optional[int] = None
    repeat_failure_limit: Optional[int] = None
    token_budget: Optional[int] = None
    retries: Optional[int] = None
    context_tokens: Optional[int] = None
    sandbox_backend: Optional[str] = None
    sandbox_network: bool = False
    resume: Optional[str] = None
    cwd: Optional[Path | str] = None
    executable: Optional[str] = None
    extra_args: tuple[str, ...] = ()
    env: Optional[Mapping[str, str]] = field(default=None, repr=False)
    startup_timeout: float = 30.0
    close_timeout: float = 5.0
    max_line_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", Path(self.workspace).expanduser().resolve())
        if self.cwd is not None:
            object.__setattr__(self, "cwd", Path(self.cwd).expanduser().resolve())
        if self.executable is None:
            object.__setattr__(self, "executable", sys.executable)
        if self.startup_timeout <= 0 or self.close_timeout <= 0:
            raise ValueError("startup_timeout 与 close_timeout 必须大于 0")
        if self.max_line_bytes < 64 * 1024:
            raise ValueError("max_line_bytes 不能小于 64 KiB")
        for arg in self.extra_args:
            name = arg.split("=", 1)[0]
            if name in _RESERVED_ARGS:
                raise ValueError(f"extra_args 不能覆盖 SDK 保留参数: {name}")

    @property
    def process_cwd(self) -> Path:
        return Path(self.cwd) if self.cwd is not None else Path(self.workspace)

    def command(self) -> list[str]:
        cmd = [
            str(self.executable or sys.executable),
            "-m",
            "krelcode",
            "--workspace",
            str(self.workspace),
        ]
        if self.mock:
            cmd.append("--mock")
        elif self.provider:
            cmd.extend(["--provider", self.provider])
        if self.model:
            cmd.extend(["--model", self.model])
        if self.base_url:
            cmd.extend(["--base-url", self.base_url])
        if self.config:
            cmd.extend(["--config", str(self.config)])
        if self.permission_mode:
            cmd.extend(["--permission-mode", self.permission_mode])
        if self.auto_approve:
            cmd.append("--auto-approve")
        if self.no_sandbox:
            cmd.append("--no-sandbox")
        if self.sandbox_backend:
            cmd.extend(["--sandbox-backend", self.sandbox_backend])
        if self.sandbox_network:
            cmd.append("--sandbox-network")
        if self.no_context:
            cmd.append("--no-context")
        if self.no_skills:
            cmd.append("--no-skills")
        for path in self.skills_dirs:
            cmd.extend(["--skills-dir", str(path)])
        if self.no_mcp:
            cmd.append("--no-mcp")
        if self.no_session:
            cmd.append("--no-session")
        if self.no_checkpoint:
            cmd.append("--no-checkpoint")
        if self.auto_commit:
            cmd.append("--auto-commit")
        if self.no_compress:
            cmd.append("--no-compress")
        if self.max_steps is not None:
            cmd.extend(["--max-steps", str(self.max_steps)])
        if self.repeat_failure_limit is not None:
            cmd.extend([
                "--repeat-failure-limit", str(self.repeat_failure_limit),
            ])
        if self.token_budget is not None:
            cmd.extend(["--token-budget", str(self.token_budget)])
        if self.retries is not None:
            cmd.extend(["--retries", str(self.retries)])
        if self.context_tokens is not None:
            cmd.extend(["--context-tokens", str(self.context_tokens)])
        if self.resume:
            cmd.extend(["--resume", self.resume])
        cmd.extend(self.extra_args)
        # SDK 固定协议方向，并放在最后，避免 extra_args 中的重复项改变模式。
        cmd.extend(["--input-format", "stream-json", "--output-format", "stream-json"])
        return cmd

    def environment(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.env:
            env.update({str(key): str(value) for key, value in self.env.items()})
        return env


@dataclass(frozen=True)
class KrelcodeMessage:
    """一条已解析的 stream-json 消息。"""

    data: Mapping[str, Any]

    @property
    def type(self) -> str:
        return str(self.data.get("type", ""))

    @property
    def subtype(self) -> str:
        return str(self.data.get("subtype", ""))

    @property
    def request_id(self) -> Optional[str]:
        value = self.data.get("request_id")
        return str(value) if value is not None else None

    @property
    def session_id(self) -> Optional[str]:
        value = self.data.get("session_id")
        return str(value) if value is not None else None

    @property
    def turn_id(self) -> Optional[str]:
        value = self.data.get("turn_id")
        return str(value) if value is not None else None

    @property
    def is_terminal(self) -> bool:
        return self.type == "result"

    @property
    def is_error(self) -> bool:
        return bool(self.data.get("is_error", False))

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


class KrelcodeClient:
    """持久 Krelcode stream-json 客户端。

    典型用法::

        async with KrelcodeClient(workspace=".", mock=True) as client:
            async for message in client.query("解释 README"):
                print(message.type, message.data)

    同一客户端可以有多个查询生成器并发等待；Krelcode 会话运行器会按输入到达
    顺序串行执行 turn，SDK 使用 ``request_id`` 路由各自事件。
    """

    def __init__(
        self,
        workspace: Path | str = ".",
        *,
        options: Optional[KrelcodeOptions] = None,
        **kwargs: Any,
    ) -> None:
        if options is not None:
            if Path(workspace) != Path(".") or kwargs:
                raise TypeError("传入 options 时不能再覆盖 workspace 或其他选项")
            self.options = options
        else:
            self.options = KrelcodeOptions(workspace=workspace, **kwargs)

        self._process: Optional[asyncio.subprocess.Process] = None
        self._stdout_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._request_queues: dict[str, asyncio.Queue[Any]] = {}
        self._control_queue: asyncio.Queue[Any] = asyncio.Queue()
        self._stderr_lines: deque[str] = deque(maxlen=200)
        self._fatal_error: Optional[BaseException] = None
        self._session_id: Optional[str] = None
        self._init_message: Optional[KrelcodeMessage] = None
        self._ids = count(1)
        self._write_lock = asyncio.Lock()
        self._closed = False
        self._started = False
        self._sensitive_values = _sensitive_environment_values(
            self.options.environment())

    @classmethod
    def from_options(cls, options: KrelcodeOptions) -> "KrelcodeClient":
        return cls(options=options)

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def init_message(self) -> Optional[KrelcodeMessage]:
        return self._init_message

    @property
    def process_id(self) -> Optional[int]:
        return self._process.pid if self._process is not None else None

    async def __aenter__(self) -> "KrelcodeClient":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def start(self) -> "KrelcodeClient":
        """启动子进程并等待 ``system/init``；可重复调用。"""
        if self._closed:
            raise SDKError("KrelcodeClient 已关闭")
        if self._process is not None and self._process.returncode is None:
            return self
        if self._started:
            raise SDKProcessError("Krelcode 子进程已经退出，客户端不能重新启动")

        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.options.command(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.options.process_cwd),
                env=self.options.environment(),
                limit=self.options.max_line_bytes,
            )
        except (OSError, ValueError) as exc:
            raise SDKProcessError(
                f"无法启动 Krelcode 子进程: {type(exc).__name__}: {exc}"
            ) from exc

        self._started = True
        self._stdout_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

        try:
            first = await asyncio.wait_for(
                self._control_queue.get(), timeout=self.options.startup_timeout)
        except asyncio.TimeoutError as exc:
            await self.close()
            raise SDKProcessError(
                f"等待 Krelcode 初始化超时（{self.options.startup_timeout:g}s）"
            ) from exc

        if first is _EOF:
            error = self._fatal_error
            detail = self._process_error("Krelcode 初始化前子进程已退出")
            await self.close()
            if error is not None:
                raise SDKProtocolError(str(error)) from error
            raise detail
        if not isinstance(first, KrelcodeMessage):
            await self.close()
            raise SDKProtocolError("初始化阶段收到未知消息")
        if first.type != "system" or first.subtype != "init":
            await self.close()
            raise SDKProtocolError(
                f"首条消息必须是 system/init，实际为 {first.type}/{first.subtype}"
            )
        if first.get("protocol") != PROTOCOL_NAME:
            await self.close()
            raise SDKProtocolError(f"不支持的协议: {first.get('protocol')!r}")
        if str(first.get("protocol_version")) != PROTOCOL_VERSION:
            await self.close()
            raise SDKProtocolError(
                f"不支持的协议版本: {first.get('protocol_version')!r}"
            )
        self._init_message = first
        self._session_id = first.session_id
        return self

    async def query(
        self,
        prompt: str = "",
        *,
        request_id: Optional[str] = None,
        images: Sequence[ImageInput] = (),
    ) -> AsyncIterator[KrelcodeMessage]:
        """发送一条 user 消息并流式产出该请求的事件，直到终态 ``result``。"""
        await self.start()
        if not isinstance(prompt, str):
            raise TypeError("prompt 必须是字符串")
        if not prompt.strip() and not images:
            raise ValueError("prompt 与 images 不能同时为空")

        rid = request_id or f"sdk-{next(self._ids)}"
        if not rid.strip():
            raise ValueError("request_id 不能为空")
        if rid in self._request_queues:
            raise ValueError(f"request_id 正在使用: {rid}")

        content: str | list[dict[str, Any]]
        if images:
            content = [{"type": "text", "text": prompt}]
            for image in images:
                if not isinstance(image, ImageInput):
                    raise TypeError("images 元素必须是 ImageInput")
                content.append(image.to_block())
        else:
            content = prompt

        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._request_queues[rid] = queue
        try:
            await self._write_json({
                "type": "user",
                "content": content,
                "request_id": rid,
            })
            while True:
                item = await self._next_message(queue)
                if not isinstance(item, KrelcodeMessage):
                    raise SDKProtocolError("查询流收到未知内部消息")
                yield item
                if item.is_terminal:
                    return
        finally:
            self._request_queues.pop(rid, None)

    async def run(self, prompt: str, **query_kwargs: Any) -> str:
        """便利方法：消费完整查询并返回最终文本；错误终态抛出异常。"""
        result: Optional[KrelcodeMessage] = None
        async for message in self.query(prompt, **query_kwargs):
            if message.is_terminal:
                result = message
        if result is None:
            raise SDKProcessError("查询未产生 result 终态")
        text = str(result.get("result", ""))
        if result.is_error:
            raise SDKResultError(text or "Krelcode 查询失败", result)
        return text

    async def interrupt(self) -> bool:
        """中止当前正在执行的 turn；没有活动进程时返回 False。"""
        if not self._is_running():
            return False
        await self._write_json({"type": "control", "subtype": "interrupt"})
        return True

    async def close(self) -> None:
        """优雅结束会话，必要时 terminate/kill 子进程；可重复调用。"""
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is None:
            return

        if process.returncode is None and process.stdin is not None:
            try:
                await self._write_json({"type": "control", "subtype": "end"})
            except (SDKError, BrokenPipeError, ConnectionResetError):
                pass
            try:
                process.stdin.close()
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError, AttributeError):
                pass

        if process.returncode is None:
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=self.options.close_timeout)
            except asyncio.TimeoutError:
                await self._terminate(process)

        await self._finish_background_tasks()
        self._process = None

    def stderr_tail(self, limit: int = 8000) -> str:
        """返回已脱敏且有界的 stderr 诊断尾部。"""
        text = "".join(self._stderr_lines)
        return _scrub_secrets(
            text[-max(1, limit):], self._sensitive_values)

    def _is_running(self) -> bool:
        return (
            self._process is not None
            and self._process.returncode is None
            and self._process.stdin is not None
        )

    async def _write_json(self, payload: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            raise self._process_error("Krelcode 子进程不可写")
        data = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        async with self._write_lock:
            try:
                process.stdin.write(data)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise SDKProcessError(self._process_error("写入子进程失败")) from exc

    async def _next_message(self, queue: asyncio.Queue[Any]) -> KrelcodeMessage:
        item = await queue.get()
        if item is _EOF:
            if self._fatal_error is not None:
                if isinstance(self._fatal_error, SDKError):
                    raise self._fatal_error
                raise SDKProtocolError(str(self._fatal_error)) from self._fatal_error
            raise self._process_error("Krelcode 子进程在查询结束前关闭了 stdout")
        if not isinstance(item, KrelcodeMessage):
            raise SDKProtocolError("收到未知内部消息")
        return item

    async def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                    message = _message_from_mapping(value)
                except (json.JSONDecodeError, SDKProtocolError) as exc:
                    self._fatal_error = SDKProtocolError(
                        f"无效 stream-json 输出: {exc}")
                    break

                rid = message.request_id
                target = self._request_queues.get(rid) if rid else None
                if target is None:
                    target = self._control_queue
                await target.put(message)
                if message.type == "system" and message.subtype == "session_end":
                    break
        except asyncio.CancelledError:
            raise
        except (ValueError, asyncio.LimitOverrunError) as exc:
            self._fatal_error = SDKProtocolError(
                f"读取 stream-json 输出失败: {type(exc).__name__}: {exc}")
        except Exception as exc:
            self._fatal_error = SDKProtocolError(
                f"读取 stream-json 输出失败: {type(exc).__name__}: {exc}")
        finally:
            for queue in list(self._request_queues.values()):
                await queue.put(_EOF)
            await self._control_queue.put(_EOF)

    async def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                raw = await process.stderr.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace")
                if len(line) > 4000:
                    line = line[-4000:]
                self._stderr_lines.append(line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._stderr_lines.append(
                f"[sdk] stderr reader failed: {type(exc).__name__}\n")

    async def _finish_background_tasks(self) -> None:
        tasks = [task for task in (self._stdout_task, self._stderr_task) if task]
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=max(1.0, self.options.close_timeout),
                )
            except asyncio.TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except (ProcessLookupError, OSError):
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except (ProcessLookupError, OSError):
                pass
            await process.wait()

    def _process_error(self, prefix: str) -> SDKProcessError:
        code = self._process.returncode if self._process is not None else None
        tail = self.stderr_tail()
        detail = f"{prefix}（exit={code}）"
        if tail.strip():
            detail += f"\nstderr:\n{tail.rstrip()}"
        return SDKProcessError(detail)


def _message_from_mapping(value: Any) -> KrelcodeMessage:
    if not isinstance(value, dict):
        raise SDKProtocolError("stream-json 每行必须是 JSON 对象")
    if not isinstance(value.get("type"), str):
        raise SDKProtocolError("stream-json 消息缺少字符串 type")
    return KrelcodeMessage(value)


_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{5,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|secret)\s*[=:]\s*)"
        r"[^\s\"']{6,}"
    ),
)


def _sensitive_environment_values(env: Mapping[str, str]) -> tuple[str, ...]:
    values: list[str] = []
    sensitive_suffixes = (
        "API_KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD",
        "CREDENTIAL", "CREDENTIALS",
    )
    for key, value in env.items():
        upper = str(key).upper()
        if upper.endswith(sensitive_suffixes) and len(str(value)) >= 6:
            value_text = str(value)
            if value_text not in values:
                values.append(value_text)
    return tuple(sorted(values, key=len, reverse=True))


def _scrub_secrets(
    text: str, known_secrets: Sequence[str] = (),
) -> str:
    result = text
    for secret in known_secrets:
        if secret:
            result = result.replace(secret, "***")
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            result = pattern.sub(lambda m: m.group(1) + "***", result)
        else:
            result = pattern.sub("***", result)
    return result
