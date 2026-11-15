"""Step 19 测试：Hook 系统（解析/触发映射/变量 quote/超时/失败安全/e2e）。"""

import asyncio
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.core.events import (
    EventBus, HookTriggered, RunFinished, ToolCallCompleted,
)
from krelcode.core.messages import Message, ToolCall
from krelcode.hooks import HookRunner, HookSpec, parse_hooks
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.input import InputProcessor
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.builtin.write_file import WriteFileTool
from krelcode.tools.registry import ToolRegistry
from krelcode.ui.repl import run_repl


# -- 配置解析 ------------------------------------------------------------------------------

def test_parse_hooks_valid_and_invalid():
    config = {"hooks": [
        {"on": "file_edited", "command": "fmt {path}"},
        {"on": "run_finished", "command": "notify", "timeout": 5},
        {"on": "bogus_event", "command": "x"},          # 未知触发器 → 跳过
        {"on": "tool_failed"},                          # 缺 command → 跳过
        {"on": "tool_completed", "command": "  "},       # 空 command → 跳过
        "not-a-dict",
    ]}
    specs = parse_hooks(config)
    assert [(s.on, s.command, s.timeout) for s in specs] == [
        ("file_edited", "fmt {path}", 60.0),
        ("run_finished", "notify", 5.0),
    ]
    assert parse_hooks({}) == []
    assert parse_hooks({"hooks": "not-a-list"}) == []


# -- 触发与执行 --------------------------------------------------------------------------------

def make_runner(tmp_path, specs, logs=None):
    # 注意：不能写 `logs or []`——空列表为 falsy 会绑到新列表上
    sink = logs if logs is not None else []
    return HookRunner(tmp_path, specs, log=sink.append)


def emit_completed(bus, name, args, content="ok", is_error=False):
    return bus.emit(ToolCallCompleted(ToolCall("c1", name, args), content, is_error))


def test_file_edited_hook_runs_with_path_var(tmp_path):
    logs = []
    runner = make_runner(tmp_path, [
        HookSpec(on="file_edited", command="echo {path} > hook.log"),
    ], logs)
    bus = EventBus()
    runner.attach(bus)
    asyncio.run(emit_completed(bus, "write_file", {"path": "a.txt"}))
    assert (tmp_path / "hook.log").read_text().strip() == "a.txt"
    assert runner.executions[0]["rc"] == 0
    assert any("[hook:file_edited]" in l for l in logs)


def test_path_with_spaces_and_injection_is_quoted(tmp_path):
    runner = make_runner(tmp_path, [
        HookSpec(on="file_edited", command="echo {path} > hook.log"),
    ])
    bus = EventBus()
    runner.attach(bus)
    evil = "a b.txt; touch PWNED"     # LLM 参数里的恶意内容必须被 quote
    asyncio.run(emit_completed(bus, "edit_file", {"path": evil}))
    assert (tmp_path / "hook.log").read_text().strip() == evil
    assert not (tmp_path / "PWNED").exists()


def test_tool_failed_only_on_error(tmp_path):
    runner = make_runner(tmp_path, [
        HookSpec(on="tool_failed", command="echo {tool} >> failed.log"),
        HookSpec(on="tool_completed", command="echo {tool}:{is_error} >> all.log"),
    ])
    bus = EventBus()
    runner.attach(bus)

    async def go():
        await emit_completed(bus, "run_command", {"command": "x"}, content="ok", is_error=False)
        await emit_completed(bus, "run_command", {"command": "x"}, content="boom", is_error=True)

    asyncio.run(go())
    assert not (tmp_path / "failed.log").exists() or \
        (tmp_path / "failed.log").read_text() == "run_command\n"   # 只有失败那次
    assert (tmp_path / "all.log").read_text().splitlines() == \
        ["run_command:false", "run_command:true"]


def test_write_tool_error_does_not_trigger_file_edited(tmp_path):
    runner = make_runner(tmp_path, [
        HookSpec(on="file_edited", command="echo x >> edited.log"),
    ])
    bus = EventBus()
    runner.attach(bus)
    asyncio.run(emit_completed(bus, "write_file", {"path": "a.txt"},
                               content="越出工作区", is_error=True))
    assert not (tmp_path / "edited.log").exists()


def test_run_finished_reply_var(tmp_path):
    runner = make_runner(tmp_path, [
        HookSpec(on="run_finished", command="echo {reply} > done.log"),
    ])
    bus = EventBus()
    runner.attach(bus)
    asyncio.run(bus.emit(RunFinished("任务完成了")))
    assert (tmp_path / "done.log").read_text().strip() == "任务完成了"


def test_hook_failure_does_not_raise(tmp_path):
    logs = []
    runner = make_runner(tmp_path, [
        HookSpec(on="run_finished", command="exit 3"),
    ], logs)
    bus = EventBus()
    runner.attach(bus)
    asyncio.run(bus.emit(RunFinished("x")))     # 不应抛异常
    assert runner.executions[0]["rc"] == 3


def test_hook_timeout_terminated(tmp_path):
    logs = []
    runner = make_runner(tmp_path, [
        HookSpec(on="run_finished", command="sleep 30", timeout=0.5),
    ], logs)
    bus = EventBus()
    runner.attach(bus)
    start = time.monotonic()
    asyncio.run(bus.emit(RunFinished("x")))
    assert time.monotonic() - start < 5
    assert "超时" in runner.executions[0]["output"]


# -- e2e：真实 AgentLoop 一轮写文件触发 formatter 钩子 ------------------------------------------

def test_e2e_agent_loop_triggers_hook(tmp_path):
    logs = []
    bus = EventBus()
    runner = HookRunner(tmp_path, [
        HookSpec(on="file_edited", command="echo {path} >> edited.log"),
        HookSpec(on="run_finished", command="echo done >> edited.log"),
    ], log=logs.append)
    runner.attach(bus)

    scripted = [
        Message.assistant(tool_calls=[ToolCall(
            "w1", "write_file", {"path": "gen.txt", "content": "hello"})]),
        "写好了",
    ]
    loop = AgentLoop(
        llm=MockProvider(scripted=scripted),
        registry=ToolRegistry([WriteFileTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        bus=bus,
        system_prompt="SYS",
    )
    asyncio.run(loop.run("生成 gen.txt"))

    assert (tmp_path / "gen.txt").exists()
    assert (tmp_path / "edited.log").read_text().splitlines() == ["gen.txt", "done"]


def test_user_input_hook_collects_context_and_failure_is_nonblocking(tmp_path):
    from krelcode.hooks import HookRunner, HookSpec

    runner = HookRunner(tmp_path, [
        HookSpec("user_input", "printf 'CTX:%s' {prompt}", timeout=2),
        HookSpec("user_input", "exit 7", timeout=2),
    ])
    contexts = asyncio.run(runner.collect_input_context("解释 @a.py"))
    assert contexts == ("CTX:解释 @a.py",)
    assert [r["trigger"] for r in runner.executions] == ["user_input", "user_input"]


def test_user_input_hook_parse_is_supported(tmp_path):
    from krelcode.hooks import parse_hooks

    specs = parse_hooks({"hooks": [{"on": "user_input", "command": "cat"}]})
    assert len(specs) == 1
    assert specs[0].on == "user_input"


def test_repl_input_hook_context_reaches_model_history(tmp_path):
    runner = HookRunner(tmp_path, [
        HookSpec("user_input", "printf '项目规则：先读 AGENTS.md'"),
    ])
    processor = InputProcessor(
        tmp_path,
        hook_context_provider=runner.collect_input_context,
    )
    loop = AgentLoop(
        llm=MockProvider(scripted=["收到"]),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )
    lines = ["解释这个仓库", "/exit"]

    async def scripted_input(_prompt):
        if not lines:
            raise EOFError
        await asyncio.sleep(0.01)
        return lines.pop(0)

    rc = asyncio.run(run_repl(
        loop,
        input_fn=scripted_input,
        input_processor=processor,
    ))

    assert rc == 0
    user_message = next(m for m in loop.history if m.role == "user")
    assert "解释这个仓库" in user_message.content
    assert "<hook_context>\n项目规则：先读 AGENTS.md\n</hook_context>" in user_message.content
    assert runner.executions[0]["trigger"] == "user_input"


# -- Hook v2：结构化输入/输出、匹配器和阻断 -------------------------------------------

def test_parse_hooks_supports_matchers_and_new_lifecycle_triggers():
    specs = parse_hooks({"hooks": [
        {"on": "pre_tool_use", "command": "check", "tools": ["edit_*", "write_file"]},
        {"on": "session_start", "command": "load-context"},
        {"on": "subagent_finished", "command": "notify", "enabled": False},
        {"on": "user_prompt_submit", "command": "context", "tool": "read_file"},
    ]})
    assert [(s.on, s.tools) for s in specs] == [
        ("pre_tool_use", ("edit_*", "write_file")),
        ("session_start", ()),
        ("user_prompt_submit", ("read_file",)),
    ]


def test_hook_receives_json_payload_on_stdin_and_standard_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-leak")
    runner = HookRunner(tmp_path, [
        HookSpec(
            "pre_tool_use",
            "cat > payload.json; printf '%s' \"${DEEPSEEK_API_KEY-unset}\" > env.txt",
        ),
    ])
    result = asyncio.run(runner.before_tool(
        ToolCall("c1", "write_file", {"path": "a.txt", "content": "x"})))
    payload = __import__("json").loads((tmp_path / "payload.json").read_text())
    assert payload["event"] == "pre_tool_use"
    assert payload["workspace"] == str(tmp_path)
    assert payload["tool"]["name"] == "write_file"
    assert payload["tool"]["arguments"]["path"] == "a.txt"
    assert (tmp_path / "env.txt").read_text() == "unset"
    assert result.matched and not result.blocked


def test_structured_hook_output_can_block_before_tool(tmp_path):
    runner = HookRunner(tmp_path, [
        HookSpec(
            "pre_tool_use",
            "printf '%s' '{\"decision\":\"block\",\"reason\":\"禁止写这个文件\"}'",
        ),
    ])
    result = asyncio.run(runner.before_tool(
        ToolCall("c1", "write_file", {"path": "a.txt"})))
    assert result.blocked and "禁止写这个文件" in result.reason
    assert runner.executions[0]["blocked"] is True


def test_pre_tool_exit_code_two_blocks_with_stderr_reason(tmp_path):
    runner = HookRunner(tmp_path, [
        HookSpec("pre_tool_use", "printf '安全检查失败' >&2; exit 2"),
    ])
    result = asyncio.run(runner.before_tool(
        ToolCall("c1", "run_command", {"command": "ls"})))
    assert result.blocked and "安全检查失败" in result.reason


def test_hook_tool_matcher_only_runs_for_selected_tools(tmp_path):
    runner = HookRunner(tmp_path, [
        HookSpec(
            "pre_tool_use", "echo edit >> matched.log", tools=("edit_*",)),
        HookSpec(
            "pre_tool_use", "echo read >> matched.log", tools=("read_file",)),
    ])
    asyncio.run(runner.before_tool(ToolCall("c1", "write_file", {})))
    asyncio.run(runner.before_tool(ToolCall("c2", "edit_file", {})))
    assert (tmp_path / "matched.log").read_text().splitlines() == ["edit"]


def test_structured_user_input_hook_injects_context(tmp_path):
    runner = HookRunner(tmp_path, [
        HookSpec(
            "user_prompt_submit",
            "printf '%s' '{\"additional_context\":\"来自 Hook 的规则\"}'",
        ),
    ])
    result = asyncio.run(runner.run_lifecycle(
        "user_prompt_submit", {"prompt": "解释代码"}))
    assert result.context == ("来自 Hook 的规则",)


def test_user_prompt_submit_runs_for_query_engine_and_stays_runtime_only(tmp_path):
    events = []
    bus = EventBus()
    bus.subscribe(events.append)
    hooks = HookRunner(tmp_path, [
        HookSpec(
            "user_prompt_submit",
            "printf '%s' '{\"additional_context\":\"headless-rule\","
            "\"systemMessage\":\"hook loaded\"}'",
        ),
    ])
    provider = MockProvider(scripted=["ok"])
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        bus=bus,
        system_prompt="SYS",
        hook_port=hooks,
    )

    asyncio.run(loop.run("hello"))

    assert loop.runtime_context == ["headless-rule"]
    assert "<runtime_context>\nheadless-rule\n</runtime_context>" in \
        provider.calls[0][0].content
    assert all("headless-rule" not in (m.content or "") for m in loop.history)
    triggered = [event for event in events if isinstance(event, HookTriggered)]
    assert triggered[-1].trigger == "user_prompt_submit"
    assert triggered[-1].system_message == "hook loaded"


def test_template_values_are_not_recursively_expanded(tmp_path):
    runner = HookRunner(tmp_path, [
        HookSpec("user_input", "printf '%s' {prompt} > prompt.txt"),
    ])
    asyncio.run(runner.collect_input_context("literal {workspace} value"))
    assert (tmp_path / "prompt.txt").read_text() == "literal {workspace} value"


def test_hook_context_logs_and_audit_are_redacted(tmp_path):
    class Redactor:
        def redact(self, text):
            return text.replace("sk-secret-value", "***")

    runner = HookRunner(
        tmp_path,
        [HookSpec(
            "user_prompt_submit",
            "printf '%s' '{\"additional_context\":\"sk-secret-value\","
            "\"systemMessage\":\"key=sk-secret-value\"}'",
        )],
        redactor=Redactor(),
    )
    result = asyncio.run(runner.run_lifecycle("user_prompt_submit", {}))
    assert result.context == ("***",)
    assert result.system_message == "key=***"
    assert "sk-secret-value" not in runner.executions[0]["stdout"]
    assert "sk-secret-value" not in runner.executions[0]["output"]


def test_pretool_hook_blocks_before_policy_and_execution(tmp_path):
    class SpyTool:
        name = "spy"
        description = "spy"
        parameters = {"type": "object", "properties": {}}

        def __init__(self):
            self.calls = 0

        async def execute(self, args, ctx):
            self.calls += 1
            return ToolResult(content="should not run")

    spy = SpyTool()
    hooks = HookRunner(tmp_path, [
        HookSpec(
            "pre_tool_use",
            "printf '%s' '{\"decision\":\"block\",\"reason\":\"hook veto\"}'",
            tools=("spy",),
        ),
    ])
    provider = MockProvider(scripted=[
        Message.assistant(tool_calls=[ToolCall("c1", "spy", {})]),
        "已停止",
    ])
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry([spy]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
        hook_port=hooks,
    )
    asyncio.run(loop.run("调用工具"))
    assert spy.calls == 0
    tool_message = next(m for m in loop.history if m.role == "tool")
    assert "PreToolUse Hook 阻止执行" in tool_message.content
    assert "hook veto" in tool_message.content


def test_posttool_hook_context_is_appended_to_tool_result(tmp_path):
    class EchoTool:
        name = "echo_tool"
        description = "echo"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, args, ctx):
            return ToolResult(content="tool-output")

    hooks = HookRunner(tmp_path, [
        HookSpec(
            "post_tool_use",
            "printf '%s' '{\"additional_context\":\"linter: 0 issues\"}'",
            tools=("echo_tool",),
        ),
    ])
    loop = AgentLoop(
        llm=MockProvider(scripted=[
            Message.assistant(tool_calls=[ToolCall("c1", "echo_tool", {})]),
            "完成",
        ]),
        registry=ToolRegistry([EchoTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
        hook_port=hooks,
    )
    asyncio.run(loop.run("执行"))
    tool_message = next(m for m in loop.history if m.role == "tool")
    assert "tool-output" in tool_message.content
    assert "<hook_context>\nlinter: 0 issues\n</hook_context>" in tool_message.content


def test_session_start_context_and_session_end_hook_run_around_session(tmp_path):
    from krelcode.agent.usage import UsageTracker
    from krelcode.core.redact import SecretRedactor
    from krelcode.session.runner import run_session
    from types import SimpleNamespace

    hooks = HookRunner(tmp_path, [
        HookSpec("session_start", "printf 'startup-context'"),
        HookSpec("session_end", "echo ended >> lifecycle.log"),
    ])
    loop = AgentLoop(
        llm=MockProvider(scripted=["ok"]),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
        hook_port=hooks,
    )
    loop.skill_manager = None
    runtime = __import__(
        "krelcode.agent.runtime", fromlist=["AgentRuntime"]).AgentRuntime(loop)
    args = SimpleNamespace(
        message="hello", output_format="text", resume=None, no_session=True)
    rc = asyncio.run(run_session(
        args,
        runtime,
        supervisor=None,
        checkpoints=None,
        usage=UsageTracker(),
        model_switch=None,
        skills_reload=None,
        redactor=SecretRedactor(),
        hooks=hooks,
    ))
    assert rc == 0
    assert loop.runtime_context == ["startup-context"]
    assert (tmp_path / "lifecycle.log").read_text().strip() == "ended"


def test_pre_and_post_compact_hooks_inject_runtime_context(tmp_path):
    from krelcode.agent.compression import ContextManager

    hooks = HookRunner(tmp_path, [
        HookSpec("pre_compact", "printf 'pre-compact-context'"),
        HookSpec(
            "post_compact",
            "printf '%s' '{\"additional_context\":\"post-compact-context\"}'",
        ),
    ])
    provider = MockProvider(scripted=["summary", "finished"])
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
        context_manager=ContextManager(
            provider, max_tokens=80, keep_recent=2, keep_recent_tools=1),
        hook_port=hooks,
    )
    loop.history.extend(Message.user("old-" + "x" * 1200) for _ in range(6))
    asyncio.run(loop.run("new request"))
    assert "pre-compact-context" in loop.runtime_context
    assert "post-compact-context" in loop.runtime_context
    assert [r["trigger"] for r in hooks.executions] == [
        "pre_compact", "post_compact",
    ]
