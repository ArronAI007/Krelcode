"""Step 28 测试：doctor 环境诊断与 init 脚手架（含 CLI 子命令集成）。"""

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import krelcode.doctor as doctor
from krelcode.doctor import (
    STATUS_FAIL, STATUS_OK, STATUS_WARN, check_config, check_default_provider,
    check_git, check_mcp_servers, check_provider_keys, check_python,
    check_workspace_writable, exit_code, format_report, mask_key,
    run_all_checks,
)
from krelcode.scaffold import CONFIG_TEMPLATE, scaffold_workspace


# -- doctor 单项检查 ------------------------------------------------------------------------------

def test_check_python_ok():
    r = check_python()
    assert r.status == STATUS_OK


def test_provider_keys_masked_and_warn(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-abcdefgh1234567890")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    r = check_provider_keys()
    assert r.status == STATUS_OK
    assert "sk-abc" in r.detail and "7890" in r.detail     # 脱敏：首尾片段
    assert "abcdefgh1234567890" not in r.detail            # 全 key 不泄露

    monkeypatch.delenv("OPENAI_API_KEY")
    r2 = check_provider_keys()
    assert r2.status == STATUS_WARN and "--mock" in r2.detail


def test_mask_key_short():
    assert mask_key("abc") == "…"


def test_provider_keys_respects_config_api_key_env(monkeypatch):
    config = {"providers": {"openai": {"api_key_env": "DEEPSEEK_API_KEY"}}}
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-1234567890")
    for env in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    r = check_provider_keys(config)
    assert r.status == STATUS_OK
    assert "openai" in r.detail
    assert "1234567890" not in r.detail          # 全 key 不泄露
    assert "sk-dee" in r.detail                  # 脱敏首段可见


def test_provider_keys_warns_with_custom_env_name(monkeypatch):
    config = {"providers": {"openai": {"api_key_env": "DEEPSEEK_API_KEY"}}}
    for env in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    r = check_provider_keys(config)
    assert r.status == STATUS_WARN
    assert "DEEPSEEK_API_KEY" in r.detail
    assert "--mock" in r.detail


def test_provider_keys_respects_config_plaintext(monkeypatch):
    config = {"providers": {"openai": {"api_key": "sk-plain-1234567890"}}}
    for env in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    r = check_provider_keys(config)
    assert r.status == STATUS_OK
    assert "1234567890" not in r.detail


def test_default_provider_resolution(monkeypatch):
    monkeypatch.delenv("KRELCODE_PROVIDER", raising=False)
    assert check_default_provider({}).status == STATUS_OK
    bad = check_default_provider({"default_provider": "nope"})
    assert bad.status == STATUS_FAIL and "nope" in bad.detail


def test_check_config_explicit_and_discovery(tmp_path, monkeypatch):
    missing = tmp_path / "missing.toml"
    assert check_config(missing).status == STATUS_FAIL

    good = tmp_path / "good.toml"
    good.write_text('default_provider = "mock"\n', encoding="utf-8")
    assert check_config(good).status == STATUS_OK

    broken = tmp_path / "broken.toml"
    broken.write_text("[ unbalanced\n", encoding="utf-8")
    assert check_config(broken).status == STATUS_FAIL

    # 缺省探测：注入不存在的候选路径 → warn
    monkeypatch.setattr(doctor, "DEFAULT_PATHS", (tmp_path / "no1.toml", tmp_path / "no2.toml"))
    assert check_config(None).status == STATUS_WARN


@pytest.mark.skipif(shutil.which("git") is None, reason="git 不可用")
def test_check_git_repo_and_non_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    r = check_git(plain)
    assert r.status == STATUS_WARN and "不是 git 仓库" in r.detail

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    r2 = check_git(repo)
    assert r2.status == STATUS_OK and "当前分支" in r2.detail


def test_check_workspace_writable(tmp_path):
    assert check_workspace_writable(tmp_path / "ws").status == STATUS_OK
    blocker = tmp_path / "file.txt"
    blocker.write_text("x", encoding="utf-8")
    bad = check_workspace_writable(blocker / "sub")   # 文件下建目录 → OSError
    assert bad.status == STATUS_FAIL


def test_check_mcp_servers(tmp_path):
    config = {"mcp_servers": {
        "good": {"command": sys.executable},
        "missing": {"command": "/nonexistent/mcp-binary"},
        "remote": {"url": "http://localhost:9999/mcp"},
        "empty": {},
        "bad": "not-a-table",
    }}
    results = {r.name: r for r in check_mcp_servers(config)}
    assert results["MCP good"].status == STATUS_OK
    assert results["MCP missing"].status == STATUS_FAIL
    assert results["MCP remote"].status == STATUS_WARN and "不联网" in results["MCP remote"].detail
    assert results["MCP empty"].status == STATUS_FAIL
    assert results["MCP bad"].status == STATUS_FAIL


# -- doctor 汇总与 CLI ------------------------------------------------------------------------------

def test_report_and_exit_code(tmp_path, monkeypatch):
    monkeypatch.delenv("KRELCODE_PROVIDER", raising=False)
    config = {"mcp_servers": {"broken": {"command": "/nonexistent/x"}}}
    results = run_all_checks(tmp_path, config, config_path=None)
    report = format_report(results)
    assert "Krelcode 环境诊断" in report and "✗" in report
    assert exit_code(results) == 1                      # 有 fail → 1

    clean = run_all_checks(tmp_path, {}, config_path=None)
    ok_results = [r for r in clean if r.status != STATUS_FAIL]
    assert exit_code(ok_results) == 0


def test_cli_doctor_and_init(tmp_path, capsys, monkeypatch):
    from krelcode.app import main

    # init：创建两个模板
    assert main(["init", "--workspace", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "已创建" in out
    assert (tmp_path / "krelcode.toml").is_file() and (tmp_path / "AGENTS.md").is_file
    # 生成的配置模板必须可被 tomllib 解析（自洽性）
    tomllib.loads((tmp_path / "krelcode.toml").read_text(encoding="utf-8"))

    # 再 init：跳过不覆盖
    (tmp_path / "AGENTS.md").write_text("自定义", encoding="utf-8")
    main(["init", "--workspace", str(tmp_path)])
    assert "跳过" in capsys.readouterr().out
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "自定义"

    # --force：覆盖回模板
    main(["init", "--workspace", str(tmp_path), "--force"])
    assert "已覆盖" in capsys.readouterr().out
    assert "项目规则" in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")

    # doctor：显式 config 不存在 → fail → exit 1
    rc = main(["doctor", "--workspace", str(tmp_path),
               "--config", str(tmp_path / "ghost.toml")])
    report = capsys.readouterr().out
    assert rc == 1 and "Krelcode 环境诊断" in report and "✗" in report

    # doctor：用刚生成的配置 → 配置项通过（key 缺失只是 warn，不影响 exit 0 判定）
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-1234567890ab")
    rc2 = main(["doctor", "--workspace", str(tmp_path),
                "--config", str(tmp_path / "krelcode.toml")])
    report2 = capsys.readouterr().out
    assert rc2 == 0 and "解析成功" in report2
