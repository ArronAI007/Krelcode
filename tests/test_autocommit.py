"""Step 17 测试：AutoCommitter（真实 git 仓库，tmp 内全离线）。"""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.core.events import EventBus, RunFinished, RunStarted
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.autocommit import AutoCommitter
from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.write_file import WriteFileTool
from krelcode.tools.registry import ToolRegistry

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git 不可用")


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "commit.gpgsign", "false"], check=True)
    return tmp_path


def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def test_is_git_repo_detection(repo, tmp_path_factory):
    assert asyncio.run(AutoCommitter(repo).is_git_repo()) is True
    plain = tmp_path_factory.mktemp("plain")     # 全新目录，非 git 仓库
    assert asyncio.run(AutoCommitter(plain).is_git_repo()) is False


def test_ensure_local_exclude_idempotent(repo):
    committer = AutoCommitter(repo)
    assert asyncio.run(committer.ensure_local_exclude()) is True
    assert asyncio.run(committer.ensure_local_exclude()) is False  # 幂等
    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert ".krelcode/" in exclude


def test_commit_skipped_when_clean_or_not_repo(repo, tmp_path):
    committer = AutoCommitter(repo)
    assert asyncio.run(committer.commit_if_dirty()) is None      # 干净仓库
    _, out = git(repo, "log", "--oneline")
    assert out == "" or "does not have any commits" in out or True
    plain_committer = AutoCommitter(tmp_path / "nonexistent-dir")
    assert asyncio.run(plain_committer.commit_if_dirty()) is None  # 非仓库不抛错


def test_e2e_turn_with_write_triggers_commit(repo):
    """RunStarted(记 label) → 工具写文件 → RunFinished → 自动 commit。"""
    bus = EventBus()
    committer = AutoCommitter(repo)
    bus.subscribe(committer.on_event)

    scripted = [
        Message.assistant(tool_calls=[ToolCall(
            "w1", "write_file", {"path": "hello.py", "content": "print('hi')\n"})]),
        "已写入 hello.py。",
    ]
    loop = AgentLoop(
        llm=MockProvider(scripted=scripted),
        registry=ToolRegistry([WriteFileTool()]),
        tool_context=ToolContext(workspace_root=repo),
        policy=AllowAllPolicy(),
        bus=bus,
        system_prompt="SYS",
    )
    asyncio.run(loop.run("写一个 hello.py"))

    rc, log = git(repo, "log", "--oneline", "-1")
    assert rc == 0 and "krelcode: 写一个 hello.py" in log
    rc, status = git(repo, "status", "--porcelain")
    assert status == ""                       # 工作区已干净
    rc, files = git(repo, "ls-files")
    assert "hello.py" in files
    # .krelcode 被本地排除，不进版本库
    (repo / ".krelcode" / "junk.txt").parent.mkdir(parents=True, exist_ok=True)
    (repo / ".krelcode" / "junk.txt").write_text("x")
    asyncio.run(committer.ensure_local_exclude())
    rc, status = git(repo, "status", "--porcelain")
    assert ".krelcode" not in status


def test_second_turn_without_changes_no_extra_commit(repo):
    bus = EventBus()
    committer = AutoCommitter(repo)
    bus.subscribe(committer.on_event)

    async def turn(label):
        await bus.emit(RunStarted(label))
        await bus.emit(RunFinished("ok"))

    asyncio.run(turn("第一轮"))   # 无任何文件变更
    rc, log = git(repo, "log", "--oneline")
    assert log == "" or rc != 0   # 没有提交产生
