"""Step 15 测试：打包元数据、--version、跨平台进程终止、/compact 强制压缩。"""

import asyncio
import os
import signal
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


# -- 打包元数据 -----------------------------------------------------------------------------

def test_pyproject_metadata():
    with open(ROOT / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    project = data["project"]
    assert project["name"] == "krelcode"
    assert project["dependencies"] == []           # 零第三方运行时依赖的承诺
    assert project["requires-python"] == ">=3.11"  # tomllib 需要 3.11+
    # 控制台入口
    assert project["scripts"]["krelcode"] == "krelcode.app:main"
    # 版本与包内 __version__ 一致
    import krelcode
    assert project["version"] == krelcode.__version__


def test_entry_point_callable():
    from krelcode.app import main
    assert callable(main)


def test_version_flag(capsys):
    from krelcode.app import build_parser
    parser = build_parser()
    with pytest.raises(SystemExit) as ei:
        parser.parse_args(["--version"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    import krelcode
    assert f"krelcode {krelcode.__version__}" in out


# -- 跨平台进程终止 ----------------------------------------------------------------------------

class FakeProc:
    def __init__(self, pid=4242):
        self.pid = pid
        self.killed = False

    def kill(self):
        self.killed = True


def test_terminate_posix_uses_killpg(monkeypatch):
    from krelcode.tools.builtin.run_command import _terminate_process_tree
    calls = []
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "getpgid", lambda pid: 999)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    proc = FakeProc()
    _terminate_process_tree(proc)
    assert calls == [(999, signal.SIGKILL)]
    assert not proc.killed          # 组击杀成功则不再单独 kill


def test_terminate_posix_killpg_failure_falls_back(monkeypatch):
    from krelcode.tools.builtin.run_command import _terminate_process_tree
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "getpgid", lambda pid: 999)
    def boom(pgid, sig):
        raise OSError("not permitted")
    monkeypatch.setattr(os, "killpg", boom)
    proc = FakeProc()
    _terminate_process_tree(proc)
    assert proc.killed              # 组击杀失败 → 杀主进程兜底


def test_terminate_windows_fallback(monkeypatch):
    from krelcode.tools.builtin.run_command import _terminate_process_tree
    monkeypatch.setattr(os, "name", "nt")
    # Windows 上 os.killpg/SIGKILL 不存在——代码不得触碰它们
    monkeypatch.delattr(os, "killpg", raising=False)
    monkeypatch.delattr(signal, "SIGKILL", raising=False)
    proc = FakeProc()
    _terminate_process_tree(proc)
    assert proc.killed


# -- /compact 强制压缩 ---------------------------------------------------------------------------

def test_force_compress_under_budget():
    from krelcode.agent.compression import SUMMARY_PREFIX, ContextManager
    from krelcode.core.messages import Message
    from krelcode.llm.providers.mock import MockProvider

    history = [Message.system("SYS")]
    for i in range(10):
        history.append(Message.user(f"问题{i}"))
        history.append(Message.assistant(f"回答{i}"))

    provider = MockProvider(scripted=["手动压缩产生的摘要"])
    cm = ContextManager(provider, max_tokens=10**9, keep_recent=4)  # 预算极高

    # 非 force：低于预算不压缩
    assert asyncio.run(cm.maybe_compress(history)) is False
    # force（/compact 语义）：立即压缩
    assert asyncio.run(cm.maybe_compress(history, force=True)) is True
    assert len(history) == 1 + 1 + 4
    assert history[1].content.startswith(SUMMARY_PREFIX)
    assert "手动压缩产生的摘要" in history[1].content


def test_parser_help_renders():
    """守护：argparse 会对 help 字符串做 %% 插值，
    字面 % 未转义会在渲染 --help 时抛 ValueError（Step 38 抓到的潜在 bug）。"""
    from krelcode.app import build_parser
    text = build_parser().format_help()
    assert "--context-tokens" in text and "75%" in text
    assert "--output-format" in text and "review" in text
