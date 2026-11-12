"""Step 41 测试：子代理 git worktree 隔离（真 git 仓库，tmp 内全离线）。"""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from krelcode.app import default_tools
from krelcode.core.events import EventBus
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.base import ProviderCapabilities, StreamFinished
from krelcode.subagent.manager import SubagentManager
from krelcode.subagent.tools import SpawnAgentTool, WaitAgentTool
from krelcode.tools.base import ToolContext, ToolResult

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git 不可用")


def git(root, *args):
    r = subprocess.run(["git", "-C", str(root), *args],
                       capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "README.md").write_text("# base\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "init")
    return tmp_path


class WriteFromTaskProvider:
    """无状态：从 user 消息解析 'write <文件名> <内容>'，先 write_file 再报完成。"""
    name = "write-from-task"
    model = "m"
    capabilities = ProviderCapabilities()

    async def stream(self, messages, tools=None):
        users = [m for m in messages if m.role == "user" and m.content]
        task = users[-1].content if users else ""
        n_assistant = sum(1 for m in messages if m.role == "assistant")
        if n_assistant == 0 and task.startswith("write "):
            _, name, content = task.split(" ", 2)
            msg = Message.assistant(tool_calls=[ToolCall(
                "w1", "write_file", {"path": name, "content": content})])
        else:
            msg = Message.assistant(f"已写入: {task}")
        yield StreamFinished(message=msg, finish_reason="stop", usage={})


def make_manager(repo, provider=None):
    return SubagentManager(
        llm=provider or WriteFromTaskProvider(),
        tool_factory=default_tools,
        tool_context=ToolContext(workspace_root=repo),
        bus=EventBus(),
        max_steps=8,
    )


# -- 基本隔离语义 ---------------------------------------------------------------------------

def test_isolated_spawn_commits_to_branch_and_keeps_main_clean(repo):
    async def go():
        mgr = make_manager(repo)
        handle = await mgr.spawn("write feature.txt 来自子代理", isolated=True)
        assert handle.branch == "krelcode/agent-a1"
        assert handle.worktree == repo / ".krelcode" / "worktrees" / "a1"
        assert handle.worktree.is_dir()

        handle = await mgr.wait("a1", timeout=15)
        assert handle.status == "completed"

        # worktree 里有文件；主工作区没有
        assert (handle.worktree / "feature.txt").exists()
        assert not (repo / "feature.txt").exists()

        # 变更已自动提交到隔离分支（提交信息含子代理 id 与任务）
        rc, log = git(repo, "log", handle.branch, "--oneline", "-1")
        assert rc == 0 and "krelcode(a1)" in log and "feature.txt" in log
        rc, blob = git(repo, "show", f"{handle.branch}:feature.txt")
        assert rc == 0 and blob == "来自子代理"

        # 主工作区 status 干净（.krelcode/ 已本地 exclude）
        rc, status = git(repo, "status", "--porcelain")
        assert status == ""

        # shutdown 回收 worktree，分支保留
        await mgr.shutdown()
        assert not handle.worktree.exists()
        rc, branches = git(repo, "branch", "--list", "krelcode/*")
        assert "krelcode/agent-a1" in branches
        rc, wt_list = git(repo, "worktree", "list")
        assert str(handle.worktree) not in wt_list

    asyncio.run(go())


def test_parallel_isolated_same_filename_no_conflict(repo):
    """两个隔离子代理并行写同名文件：各自分支互不干扰。"""
    async def go():
        mgr = make_manager(repo)
        h1 = await mgr.spawn("write data.txt 版本A", isolated=True)
        h2 = await mgr.spawn("write data.txt 版本B", isolated=True)
        handles = await mgr.wait_many(["a1", "a2"], timeout=20)
        assert all(h.status == "completed" for h in handles)
        assert h1.worktree != h2.worktree and h1.worktree.is_dir() and h2.worktree.is_dir()

        _, blob_a = git(repo, "show", f"{h1.branch}:data.txt")
        _, blob_b = git(repo, "show", f"{h2.branch}:data.txt")
        assert blob_a == "版本A" and blob_b == "版本B"
        assert not (repo / "data.txt").exists()      # 主工作区不受影响
        await mgr.shutdown()

    asyncio.run(go())


def test_isolated_requires_git_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    mgr = make_manager(plain)

    async def go():
        with pytest.raises(ValueError, match="git 仓库"):
            await mgr.spawn("x", isolated=True)

    asyncio.run(go())


def test_spawn_tool_isolated_end_to_end(repo):
    async def go():
        mgr = make_manager(repo)
        spawn = SpawnAgentTool(mgr)
        r = await spawn.execute(
            {"task": "write tool.txt 经工具隔离", "isolated": True},
            ToolContext(workspace_root=repo))
        assert not r.is_error
        assert r.data["branch"] == "krelcode/agent-a1"
        assert "隔离分支" in r.content

        wait = WaitAgentTool(mgr)
        r2 = await wait.execute({"agent_id": "a1", "timeout": 15},
                                ToolContext(workspace_root=repo))
        assert "[a1] completed" in r2.content
        assert "krelcode/agent-a1" in r2.content and "merge/cherry-pick" in r2.content
        _, blob = git(repo, "show", "krelcode/agent-a1:tool.txt")
        assert blob == "经工具隔离"
        await mgr.shutdown()

    asyncio.run(go())


def test_spawn_tool_isolated_non_repo_graceful(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    mgr = make_manager(plain)

    async def go():
        r = await SpawnAgentTool(mgr).execute(
            {"task": "x", "isolated": True}, ToolContext(workspace_root=plain))
        assert r.is_error and "git 仓库" in r.content

    asyncio.run(go())


def test_shutdown_commits_cancelled_work(repo):
    """被取消的隔离子代理：已有成果也在 shutdown 时存档到分支。"""
    class SlowWriteProvider:
        name = "slow-write"
        model = "m"
        capabilities = ProviderCapabilities()

        def __init__(self):
            self.phase = 0

        async def stream(self, messages, tools=None):
            self.phase += 1
            if self.phase == 1:      # 先写文件
                msg = Message.assistant(tool_calls=[ToolCall(
                    "w1", "write_file", {"path": "partial.txt", "content": "半成品"})])
            else:                    # 然后卡住（等待时被取消）
                await asyncio.sleep(30)
                msg = Message.assistant("不会到达")
            yield StreamFinished(message=msg, finish_reason="stop", usage={})

    async def go():
        mgr = make_manager(repo, provider=SlowWriteProvider())
        handle = await mgr.spawn("慢任务", isolated=True)
        await asyncio.sleep(0.5)     # 确保文件已写入
        assert (handle.worktree / "partial.txt").exists()
        await mgr.shutdown()         # 取消 + 存档 + 回收
        rc, blob = git(repo, "show", f"{handle.branch}:partial.txt")
        assert rc == 0 and blob == "半成品"   # 被取消的工作也进了分支
        assert not handle.worktree.exists()

    asyncio.run(go())
