"""Process entrypoint dispatch: fast paths stay light, full CLI is lazy."""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _run_python(source: str, *, cwd: Path, extra_env=None):
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env["PYTHONPATH"] = str(ROOT / "src")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", source],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_version_fast_path_does_not_load_full_cli():
    result = _run_python(
        "import sys\n"
        "from krelcode.entrypoints.cli import main\n"
        "assert main(['--version']) == 0\n"
        "assert 'krelcode.entrypoints.main' not in sys.modules\n"
        "assert 'krelcode.app' not in sys.modules\n"
        "assert 'krelcode.config' not in sys.modules\n",
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert "krelcode" in result.stdout


def test_version_ignores_broken_project_config(tmp_path):
    (tmp_path / "krelcode.toml").write_text("this is not = toml [", encoding="utf-8")
    result = _run_python(
        "from krelcode.entrypoints.cli import main\n"
        "raise SystemExit(main(['--version']))\n",
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("krelcode ")


def test_python_dash_m_uses_light_entrypoint():
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env["PYTHONPATH"] = str(ROOT / "src")

    result = subprocess.run(
        [sys.executable, "-m", "krelcode", "--version"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("krelcode ")


def test_version_token_used_as_option_value_stays_on_full_cli(monkeypatch):
    from krelcode.entrypoints import cli

    received = []
    fake_main = types.ModuleType("krelcode.entrypoints.main")

    def main(argv):
        received.append(argv)
        return 2

    fake_main.main = main
    monkeypatch.setattr(cli, "import_module", lambda _name: fake_main)

    assert cli.main(["-m", "--version"]) == 2
    assert received == [["-m", "--version"]]


def test_non_fast_path_forwards_original_argv(monkeypatch):
    from krelcode.entrypoints import cli

    received = []
    fake_main = types.ModuleType("krelcode.entrypoints.main")

    def main(argv):
        received.append(argv)
        return 17

    fake_main.main = main
    monkeypatch.setattr(
        cli, "import_module",
        lambda name: fake_main if name == "krelcode.entrypoints.main" else None,
    )

    argv = ["doctor", "--workspace", "repo with spaces", "--config", "x.toml"]
    assert cli.main(argv) == 17
    assert received == [argv]


def test_doctor_route_does_not_load_agent_composition_root(tmp_path):
    result = _run_python(
        "import sys\n"
        "from krelcode.entrypoints.main import main\n"
        "main(['doctor', '--workspace', '.'])\n"
        "assert 'krelcode.app' not in sys.modules\n",
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "krelcode.app" not in result.stderr


def test_full_cli_loads_agent_composition_root_lazily(monkeypatch, tmp_path):
    import krelcode.app as app
    from krelcode.entrypoints import main as full_cli

    seen = []

    async def fake_async_main(args):
        seen.append(args)
        return 23

    monkeypatch.setattr(app, "async_main", fake_async_main)
    assert full_cli.main(["--workspace", str(tmp_path), "-m", "hello"]) == 23
    assert seen[0].message == "hello"


def test_full_help_does_not_require_config_or_agent_root(tmp_path):
    result = _run_python(
        "import sys\n"
        "from krelcode.entrypoints.main import main\n"
        "try:\n"
        "    main(['--help'])\n"
        "except SystemExit as exc:\n"
        "    assert exc.code == 0\n"
        "assert 'krelcode.app' not in sys.modules\n",
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "usage: krelcode" in result.stdout
    assert "krelcode.app" not in result.stderr


def test_entrypoint_module_is_importable_without_agent_dependencies():
    result = _run_python(
        "import sys\n"
        "import krelcode.entrypoints.cli\n"
        "assert 'krelcode.app' not in sys.modules\n"
        "assert 'krelcode.agent' not in sys.modules\n",
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
