"""P2-1: advanced terminal diff review and pre-write approval."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from krelcode.agent.loop import AgentLoop
from krelcode.app import build_parser, build_policy
from krelcode.core.events import EventBus
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import SandboxPolicy
from krelcode.session.input import InputEnvelope, InputKind, InputProcessor
from krelcode.session.state import SessionViewState
from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.write_file import WriteFileTool
from krelcode.tools.registry import ToolRegistry
from krelcode.ui.approval import ApprovalBroker
from krelcode.ui.diff_review import (
    DiffPager,
    build_tool_call_review,
    collect_git_review,
    make_unified_diff,
    parse_unified_diff,
)
from krelcode.ui.repl import run_repl
from krelcode.ui.tui.panels import build_panels


def test_parse_unified_diff_preserves_files_hunks_and_line_numbers():
    text = "\n".join((
        "diff --git a/src/a.py b/src/a.py",
        "--- a/src/a.py",
        "+++ b/src/a.py",
        "@@ -1,2 +1,2 @@",
        "-old = 1",
        "+new = 2",
        " unchanged",
        "diff --git a/new.txt b/new.txt",
        "new file mode 100644",
        "--- /dev/null",
        "+++ b/new.txt",
        "@@ -0,0 +1 @@",
        "+created",
    ))
    review = parse_unified_diff(text, title="test")

    assert review.file_count == 2
    assert review.additions == 2 and review.deletions == 1
    assert review.files[0].path == "src/a.py"
    assert review.files[0].status == "modified"
    assert review.files[1].status == "added"
    assert review.files[1].hunks[0].lines[0].new_line == 1


def test_diff_pager_navigates_files_and_fits_requested_height():
    review = parse_unified_diff(
        make_unified_diff("a\n", "b\n", "one.py") + "\n" +
        make_unified_diff("c\n", "d\n", "two.py"))
    pager = DiffPager(review, can_decide=True)

    rendered = pager.render(width=64, height=14, color=False)
    assert len(rendered) == 14
    assert "one.py" in "\n".join(rendered)
    assert any("y 允许" in line for line in rendered)
    assert pager.handle_key("]") == ""
    assert pager.current_file is not None
    assert pager.current_file.path == "two.py"
    assert pager.handle_key("y") == "approve"
    assert pager.handle_key("q") == "close"
    assert pager.active is False


def test_tool_call_review_previews_edit_without_writing_and_redacts(tmp_path):
    target = tmp_path / "app.py"
    original = "value = 1\nAPI_KEY=sk-abcdefghijk\n"
    target.write_text(original, encoding="utf-8")
    call = ToolCall("edit-1", "edit_file", {
        "path": "app.py",
        "old_string": "value = 1",
        "new_string": "value = 2",
    })

    review = build_tool_call_review(call, tmp_path)

    assert target.read_text(encoding="utf-8") == original
    assert review.has_changes and review.redacted
    rendered = "\n".join(
        f"{'+' if line.kind == 'add' else '-' if line.kind == 'remove' else ' '}{line.text}"
        for file in review.files
        for hunk in file.hunks
        for line in hunk.lines
    )
    assert "+value = 2" in rendered
    assert "sk-abcdefghijk" not in rendered
    assert "API_KEY=***" in rendered


def test_tool_call_review_hides_blocked_secret_and_workspace_escape(tmp_path):
    call = ToolCall("write-1", "write_file", {
        "path": "credentials.json",
        "content": '{"token": "secret"}\n',
    })
    blocked = build_tool_call_review(call, tmp_path)
    assert blocked.files == ()
    assert "敏感文件" in blocked.notice

    escape = ToolCall("write-2", "write_file", {
        "path": "../outside.py",
        "content": "x\n",
    })
    outside = build_tool_call_review(escape, tmp_path)
    assert outside.files == ()
    assert "越出工作区" in outside.notice


def test_collect_git_review_includes_tracked_and_untracked_text(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.py"
    tracked.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    tracked.write_text("new\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text("created\n", encoding="utf-8")

    review = collect_git_review(tmp_path)

    paths = {item.path for item in review.files}
    assert {"tracked.py", "new.txt"} <= paths
    assert review.additions >= 2 and review.deletions >= 1


def test_diff_review_policy_is_enabled_only_for_interactive_default_mode(tmp_path):
    async def approver(_call, _reason):
        return "no"

    def review_enabled(argv, *, with_approver=True, config=None):
        args = build_parser().parse_args(argv)
        policy = build_policy(
            args,
            tmp_path,
            EventBus(),
            approver=approver if with_approver else None,
            config=config,
        )
        return getattr(policy, "review_writes", False)

    assert review_enabled([])
    assert not review_enabled([], with_approver=False)
    assert not review_enabled(["--no-diff-review"])
    assert not review_enabled(["--auto-approve"])
    assert not review_enabled(["-m", "headless"])
    assert not review_enabled([], config={"ui": {"diff_review": False}})


def test_collect_git_review_staged_excludes_worktree_and_untracked(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    target = tmp_path / "tracked.py"
    target.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    target.write_text("staged\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path, check=True)
    target.write_text("worktree\n", encoding="utf-8")
    (tmp_path / "untracked.py").write_text("created\n", encoding="utf-8")

    review = collect_git_review(tmp_path, staged=True)
    rendered = "\n".join(
        line.text
        for file in review.files
        for hunk in file.hunks
        for line in hunk.lines
    )

    assert [file.path for file in review.files] == ["tracked.py"]
    assert "staged" in rendered
    assert "worktree" not in rendered
    assert "untracked.py" not in rendered


def test_review_writes_policy_asks_before_file_mutation(tmp_path):
    calls = []

    async def approver(call, reason):
        calls.append((call, reason))
        return "yes"

    policy = SandboxPolicy(
        approver=approver,
        workspace_root=tmp_path,
        review_writes=True,
    )
    ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)

    decision = asyncio.run(policy.check(
        ToolCall("write-1", "write_file", {"path": "a.py", "content": "x"}),
        ctx,
    ))

    assert decision.allowed
    assert len(calls) == 1
    assert "diff review" in calls[0][1]


def test_diff_command_is_ui_command_and_review_panel_is_visible(tmp_path):
    processor = InputProcessor(tmp_path)
    action = asyncio.run(processor.process(InputEnvelope("/diff staged")))

    assert action.kind is InputKind.UI_COMMAND
    assert action.command == "/diff"

    review = parse_unified_diff(make_unified_diff("a\n", "b\n", "src/a.py"))
    panels = build_panels(
        SessionViewState(),
        permission_mode="default",
        diff_review=review,
    )
    by_id = {panel.id: panel for panel in panels}
    assert "review" in by_id
    assert "src/a.py" in by_id["review"].items[0].label


def test_repl_write_approval_shows_prewrite_diff_and_applies_after_yes(tmp_path, capsys):
    broker = ApprovalBroker()
    target = tmp_path / "created.py"
    provider = MockProvider(scripted=[
        Message.assistant(tool_calls=[ToolCall("write-1", "write_file", {
            "path": "created.py",
            "content": "value = 42\n",
        })]),
        "写入完成",
    ])
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry([WriteFileTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=SandboxPolicy(
            approver=broker.request,
            workspace_root=tmp_path,
            review_writes=True,
        ),
        system_prompt="SYS",
    )

    class Input:
        def __init__(self):
            self.step = 0

        async def __call__(self, _prompt):
            self.step += 1
            if self.step == 1:
                return "创建文件"
            if self.step == 2:
                for _ in range(150):
                    if broker.pending is not None:
                        return "y"
                    await asyncio.sleep(0.01)
                raise AssertionError("write approval did not reach the REPL")
            if self.step == 3:
                for _ in range(150):
                    if target.exists():
                        return "/exit"
                    await asyncio.sleep(0.01)
                raise AssertionError("approved write did not execute")
            raise EOFError

    try:
        rc = asyncio.run(run_repl(
            loop,
            input_fn=Input(),
            persist_history=False,
            approval_broker=broker,
        ))
    finally:
        broker.close()

    output = capsys.readouterr().out
    assert rc == 0
    assert target.read_text(encoding="utf-8") == "value = 42\n"
    assert "差异审阅" in output
    assert "created.py" in output
    assert "+value = 42" in output
