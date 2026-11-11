"""Step 38 测试：krelcode review（git 变更评审子命令）。"""

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from krelcode.llm.providers.mock import MockProvider
from krelcode.review import (
    MAX_DIFF_CHARS, build_review_input, readonly_tools, run_review,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git 不可用")


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "init")
    return tmp_path


class Args:
    """run_review 所需的最小 args 替身。"""
    def __init__(self, workspace, staged=False, focus=None, output_format="text"):
        self.workspace = str(workspace)
        self.staged = staged
        self.focus = focus
        self.output_format = output_format
        self.mock = True
        self.provider = "mock"
        self.model = None
        self.base_url = None
        self.config = None
        self.retries = 0
        self.max_steps = 8


# -- diff 收集与提示组装 ------------------------------------------------------------------------------

def test_build_review_input_contains_diff(repo):
    (repo / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    prompt = asyncio.run(build_review_input(repo, staged=False))
    assert prompt is not None
    assert "-    return 1" in prompt and "+    return 2" in prompt
    assert "未提交变更" in prompt and "```diff" in prompt


def test_build_review_input_staged_only(repo):
    (repo / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    git(repo, "add", "app.py")
    # 暂存后再改一次：--staged 只应看到暂存版本
    (repo / "app.py").write_text("def f():\n    return 3\n", encoding="utf-8")
    prompt = asyncio.run(build_review_input(repo, staged=True))
    assert "+    return 2" in prompt and "return 3" not in prompt
    assert "已暂存变更" in prompt


def test_build_review_input_no_changes(repo):
    assert asyncio.run(build_review_input(repo, staged=False)) is None


def test_build_review_input_untracked_and_focus(repo):
    (repo / "new_mod.py").write_text("x = 1\n", encoding="utf-8")
    prompt = asyncio.run(build_review_input(repo, staged=False, focus="安全"))
    assert "评审重点：安全" in prompt
    assert "未跟踪文件" in prompt and "new_mod.py" in prompt


def test_build_review_input_truncates_huge_diff(repo, monkeypatch):
    import krelcode.review as review_mod
    # 先提交大文件，再整文件修改，产生大 diff（未跟踪文件不进 git diff）
    original = "\n".join(f"line_{i} = {i}" for i in range(200))
    (repo / "big.py").write_text(original, encoding="utf-8")
    git(repo, "add", "big.py")
    git(repo, "commit", "-qm", "big")
    (repo / "big.py").write_text(
        "\n".join(f"line_{i} = {i + 1}" for i in range(200)), encoding="utf-8")

    monkeypatch.setattr(review_mod, "MAX_DIFF_CHARS", 500)
    prompt = asyncio.run(build_review_input(repo, staged=False))
    assert "中间截断" in prompt


# -- run_review 端到端 --------------------------------------------------------------------------------

def test_run_review_feeds_diff_to_llm(repo, capsys):
    (repo / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    provider = MockProvider(scripted=["发现一个问题：app.py:2 语义变更需确认。\n\n结论: 建议修改后合入"])

    rc = asyncio.run(run_review(Args(repo), llm=provider))
    assert rc == 0
    out = capsys.readouterr().out
    assert "结论: 建议修改后合入" in out

    # 评审 prompt 真的带上了 diff 与评审系统提示语义
    sent = provider.calls[0]
    assert sent[0].role == "system" and "code reviewer" in sent[0].content
    user_prompt = sent[-1].content
    assert "+    return 2" in user_prompt and "```diff" in user_prompt


def test_run_review_readonly_toolset_available(repo):
    (repo / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    provider = MockProvider(scripted=["纯文本评审，无工具调用。"])
    asyncio.run(run_review(Args(repo), llm=provider))
    # 模型收到了只读工具 schema（可查上下文），且没有写/执行工具
    tools = provider.tools_seen[0]
    names = {t["name"] for t in tools}
    assert names == {"read_file", "grep", "glob", "find_refs", "repo_map"}


def test_run_review_no_changes_message(repo, capsys):
    rc = asyncio.run(run_review(Args(repo), llm=MockProvider()))
    assert rc == 0
    assert "没有可评审的 git 变更" in capsys.readouterr().out


def test_run_review_json_mode(repo, capsys):
    (repo / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    provider = MockProvider(scripted=["LGTM\n\n结论: 可合入"],
                            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    rc = asyncio.run(run_review(Args(repo, output_format="json"), llm=provider))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True
    assert "可合入" in payload["review"]
    assert payload["usage"]["calls"] == 1
    assert payload["cost_usd"] is None          # mock-model 不在价目表


def test_run_review_non_repo_friendly(tmp_path, capsys):
    rc = asyncio.run(run_review(Args(tmp_path), llm=MockProvider()))
    assert rc == 0
    assert "没有可评审的 git 变更" in capsys.readouterr().out


def test_readonly_tools_have_no_write_or_exec():
    names = {t.name for t in readonly_tools()}
    assert names == {"read_file", "grep", "glob", "find_refs", "repo_map"}
    assert not (names & {"write_file", "edit_file", "run_command"})
