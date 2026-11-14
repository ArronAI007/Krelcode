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
        "--python", sys.executable,
        "--home", "/tmp/krelcode-test", "--bin-dir", "/tmp/krelcode-bin",
        "--no-modify-path",
    )
    assert local.returncode == 0, local.stderr
    assert f"source={ROOT}" in local.stdout
    assert "backend=" in local.stdout
    assert f"local_python={sys.executable}" in local.stdout

    mirrored = run_installer(
        "--dry-run", "--source", str(ROOT), "--python", sys.executable,
        "--python-mirror", "https://mirror.example/python",
        "--home", "/tmp/krelcode-test", "--bin-dir", "/tmp/krelcode-bin",
        "--no-modify-path",
    )
    assert mirrored.returncode == 0, mirrored.stderr
    assert f"local_python={sys.executable}" in mirrored.stdout
    assert "python_mirror=https://mirror.example/python" in mirrored.stdout

    tagged = run_installer(
        "--dry-run", "--tag", f"v{VERSION}",
        "--home", "/tmp/krelcode-test", "--bin-dir", "/tmp/krelcode-bin",
        "--no-modify-path",
    )
    assert tagged.returncode == 0, tagged.stderr
    assert f"source=git+https://github.com/ArronAI007/Krelcode.git@v{VERSION}" in tagged.stdout


def test_uv_failure_falls_back_to_offline_global_install(tmp_path):
    home = tmp_path / "home dir"
    install_root = tmp_path / "app home"
    bin_dir = tmp_path / "bin dir"
    home.mkdir()
    config = home / ".krelcode" / "config.toml"
    config.parent.mkdir()
    config.write_text("default_provider = \"mock\"\n", encoding="utf-8")

    fake_bin = tmp_path / "fake bin"
    fake_bin.mkdir()
    uv_log = tmp_path / "uv args.log"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        "{\n"
        "  printf 'UV_PYTHON_DOWNLOADS=%s\\n' \"${UV_PYTHON_DOWNLOADS:-}\"\n"
        "  printf 'ARG=%s\\n' \"$@\"\n"
        "} > \"$KRELCODE_UV_LOG\"\n"
        "exit 42\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    env = dict(os.environ)
    # 模拟真实用户环境，避免源码测试的 PYTHONPATH 覆盖私有环境安装包。
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.update({
        "HOME": str(home),
        "ZDOTDIR": str(home),
        "SHELL": "/bin/zsh",
        "KRELCODE_PYTHON": sys.executable,
        "KRELCODE_UV_LOG": str(uv_log),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    })
    common = (
        "--source", str(ROOT), "--home", str(install_root),
        "--bin-dir", str(bin_dir),
    )
    installed = run_installer(*common, env=env)
    assert installed.returncode == 0, installed.stderr + installed.stdout
    assert "无需构建依赖" in installed.stdout
    assert "uv 安装失败，回退到 Krelcode 私有 venv" in installed.stdout
    uv_args = uv_log.read_text(encoding="utf-8")
    assert "UV_PYTHON_DOWNLOADS=never" in uv_args
    assert "ARG=--no-build-isolation" in uv_args
    assert "ARG=--python" in uv_args
    assert f"ARG={sys.executable}" in uv_args

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
