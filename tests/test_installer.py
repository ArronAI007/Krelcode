"""全局安装器：静态语法、dry-run 与私有环境端到端安装。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from krelcode import __version__ as VERSION

ROOT = Path(__file__).parent.parent
INSTALLER = ROOT / "install.sh"


pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="install.sh 面向 macOS/Linux",
)


def run_installer(*args: str, env=None, timeout=90):
    return subprocess.run(
        ["bash", str(INSTALLER), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_installer_is_executable_and_has_valid_help():
    assert os.access(INSTALLER, os.X_OK)
    syntax = subprocess.run(
        ["bash", "-n", str(INSTALLER)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr

    result = run_installer("--help")
    assert result.returncode == 0
    assert "用户不需要运行 python -m venv" in result.stdout
    assert "--uninstall" in result.stdout


def test_installer_dry_run_resolves_local_source_and_tag():
    local = run_installer(
        "--dry-run", "--source", str(ROOT),
        "--home", "/tmp/krelcode-test", "--bin-dir", "/tmp/krelcode-bin",
        "--no-modify-path",
    )
    assert local.returncode == 0, local.stderr
    assert f"source={ROOT}" in local.stdout
    assert "backend=" in local.stdout

    tagged = run_installer(
        "--dry-run", "--tag", f"v{VERSION}",
        "--home", "/tmp/krelcode-test", "--bin-dir", "/tmp/krelcode-bin",
        "--no-modify-path",
    )
    assert tagged.returncode == 0, tagged.stderr
    assert f"source=git+https://github.com/ArronAI007/Krelcode.git@v{VERSION}" in tagged.stdout


def test_private_venv_install_is_offline_global_and_uninstall_keeps_config(tmp_path):
    home = tmp_path / "home dir"
    install_root = tmp_path / "app home"
    bin_dir = tmp_path / "bin dir"
    home.mkdir()
    config = home / ".krelcode" / "config.toml"
    config.parent.mkdir()
    config.write_text("default_provider = \"mock\"\n", encoding="utf-8")

    env = dict(os.environ)
    # 模拟真实用户环境，避免源码测试的 PYTHONPATH 覆盖私有环境安装包。
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.update({
        "HOME": str(home),
        "ZDOTDIR": str(home),
        "SHELL": "/bin/zsh",
        "KRELCODE_PYTHON": sys.executable,
    })
    common = (
        "--no-uv", "--source", str(ROOT), "--home", str(install_root),
        "--bin-dir", str(bin_dir),
    )
    installed = run_installer(*common, env=env)
    assert installed.returncode == 0, installed.stderr + installed.stdout
    assert "无需构建依赖" in installed.stdout

    command = bin_dir / "krelcode"
    assert command.is_symlink()
    version = subprocess.run(
        [str(command), "--version"], env=env, capture_output=True, text=True)
    assert version.returncode == 0
    assert f"krelcode {VERSION}" in version.stdout

    imported = subprocess.run(
        [str(install_root / "venv" / "bin" / "python"),
         "-c", "import krelcode; print(krelcode.__file__)"],
        env=env, capture_output=True, text=True)
    assert imported.returncode == 0
    assert str(install_root) in imported.stdout
    assert "krelcode installer" in (home / ".zshrc").read_text(encoding="utf-8")

    uninstalled = run_installer(
        "--uninstall", "--home", str(install_root), "--bin-dir", str(bin_dir),
        env=env,
    )
    assert uninstalled.returncode == 0, uninstalled.stderr + uninstalled.stdout
    assert not install_root.exists() and not command.exists()
    assert config.is_file()
    assert "krelcode installer" not in (home / ".zshrc").read_text(encoding="utf-8")
