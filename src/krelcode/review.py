"""krelcode review：对当前 git 变更做代码评审（headless 单次任务）。

用法：
    krelcode review                 # 评审工作区未提交变更（git diff HEAD）
    krelcode review --staged        # 只评审已暂存变更（git diff --cached）
    krelcode review --focus "安全"   # 附加评审重点
    krelcode review --output-format json

设计：
- 只注册**只读工具**（read_file/grep/glob/find_refs/repo_map），模型可按需
  拉取上下文，但绝无写/执行能力——评审模式天然安全，无需审批
- diff 超预算做中间截断（truncate_middle），未跟踪文件列名提示
- 非 git 仓库 / 无变更 → 友好提示退出（非错误）
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .agent.loop import AgentLoop
from .agent.usage import UsageTracker
from .core.events import EventBus, PermissionDenied, ToolCallCompleted
from .core.textutil import truncate_middle
from .llm.base import LLMProvider
from .llm.providers.mock import MockProvider
from .llm.registry import create_provider, resolve_settings
from .llm.retry import RetryPolicy, RetryingProvider
from .safety.policy import SandboxPolicy
from .tools.base import ToolContext
from .tools.builtin.find_refs import FindRefsTool
from .tools.builtin.glob import GlobTool
from .tools.builtin.grep import GrepTool
from .tools.builtin.read_file import ReadFileTool
from .tools.builtin.repo_map import RepoMapTool
from .tools.registry import ToolRegistry

MAX_DIFF_CHARS = 100_000

REVIEW_SYSTEM_PROMPT = """\
You are a meticulous code reviewer for a git repository.

Review the provided diff and report, in the user's language:
1. Bugs and correctness issues (highest priority, cite file:line)
2. Security risks (injection, secrets, path traversal, unsafe deserialization)
3. Obvious performance problems
4. Maintainability concerns worth fixing now (naming, duplication, dead code)

Rules:
- Be specific: reference file paths and line numbers from the diff.
- Use read-only tools (read_file/grep/glob/find_refs/repo_map) to inspect
  surrounding context when the diff alone is ambiguous.
- Do NOT invent issues to seem thorough; if the change looks good, say so.
- End with a verdict line: 结论: 可合入 / 建议修改后合入 / 需要修改.
"""


def readonly_tools() -> List[Any]:
    """评审模式工具集：只读，绝无写/执行能力。"""
    return [ReadFileTool(), GrepTool(), GlobTool(), FindRefsTool(), RepoMapTool()]


async def _git_diff(root: Path, staged: bool) -> Tuple[str, str]:
    """返回 (diff 文本, 未跟踪文件列表文本)；非仓库时 diff 为空。"""
    from .agent.context import _git   # 复用失败安全的 git runner
    args = ["diff", "--cached"] if staged else ["diff", "HEAD"]
    diff = await _git(root, *args) or ""
    status = await _git(root, "status", "--porcelain") or ""
    untracked = [ln[3:] for ln in status.splitlines() if ln.startswith("??")]
    return diff, "\n".join(untracked)


async def build_review_input(root: Path, staged: bool, focus: str = "") -> Optional[str]:
    """组装评审输入；无变更返回 None。"""
    diff, untracked = await _git_diff(root, staged)
    if not diff.strip() and not untracked:
        return None
    parts: List[str] = []
    scope = "已暂存变更（git diff --cached）" if staged else "未提交变更（git diff HEAD）"
    parts.append(f"请评审以下 {scope}：")
    if focus:
        parts.append(f"评审重点：{focus}")
    if diff.strip():
        parts.append("```diff\n" + truncate_middle(diff, MAX_DIFF_CHARS) + "\n```")
    else:
        parts.append("（diff 为空）")
    if untracked:
        parts.append(
            "未跟踪文件（不在 diff 内，如需评审请用 read_file 阅读）:\n"
            + truncate_middle(untracked, 4_000))
    return "\n\n".join(parts)


def build_review_llm(args: argparse.Namespace, config: Dict[str, Any]) -> LLMProvider:
    if getattr(args, "mock", False) or getattr(args, "provider", None) == "mock":
        return MockProvider(fallback="[mock] 评审完成：未发现问题。\n\n结论: 可合入")
    settings = resolve_settings(
        provider=getattr(args, "provider", None),
        model=getattr(args, "model", None),
        base_url=getattr(args, "base_url", None),
        config=config,
    )
    provider = create_provider(settings)
    retries = getattr(args, "retries", 2)
    if retries > 0:
        provider = RetryingProvider(provider, RetryPolicy(max_retries=retries))
    return provider


class _ReviewSummary:
    def __init__(self) -> None:
        self.tools_used: List[str] = []
        self.denied = 0

    def on_event(self, event) -> None:
        if isinstance(event, ToolCallCompleted):
            self.tools_used.append(event.tool_call.name)
        elif isinstance(event, PermissionDenied):
            self.denied += 1


async def run_review(args: argparse.Namespace, llm: Optional[LLMProvider] = None) -> int:
    from .config import load_config

    workspace = Path(args.workspace).resolve()
    config = load_config(Path(args.config) if getattr(args, "config", None) else None)
    prompt = await build_review_input(workspace, args.staged, getattr(args, "focus", "") or "")
    json_mode = getattr(args, "output_format", "text") == "json"

    if prompt is None:
        message = "没有可评审的 git 变更（工作区干净）。"
        if json_mode:
            print(json.dumps({"ok": True, "review": None, "reason": "no_changes"},
                             ensure_ascii=False))
        else:
            print(message)
        return 0

    provider = llm or build_review_llm(args, config)
    bus = EventBus()
    summary = _ReviewSummary()
    bus.subscribe(summary.on_event)
    usage = UsageTracker(model=getattr(provider, "model", None))
    usage.attach(bus)
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry(readonly_tools()),
        tool_context=ToolContext(workspace_root=workspace),
        # 评审模式只有只读工具；SandboxPolicy 兜底拦截任何未知工具
        policy=SandboxPolicy(approver=None, bus=bus),
        bus=bus,
        system_prompt=REVIEW_SYSTEM_PROMPT,
        max_steps=getattr(args, "max_steps", 12),
    )

    try:
        reply = await loop.run(prompt)
    except Exception as e:
        if json_mode:
            print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                             ensure_ascii=False))
        else:
            print(f"错误：{type(e).__name__}: {e}")
        return 1

    if json_mode:
        print(json.dumps({
            "ok": True,
            "review": reply,
            "usage": {
                "calls": usage.session.calls, "input": usage.session.input,
                "output": usage.session.output, "total": usage.session.total,
            },
            "cost_usd": usage.cost_usd,
            "tools_used": summary.tools_used,
            "denied": summary.denied,
        }, ensure_ascii=False))
    else:
        print(reply)
        if usage.session.calls:
            print(f"\n[usage] {usage.session.text()}{usage.cost_note()}")
    return 0
