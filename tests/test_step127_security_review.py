"""Step C: dedicated Security Review workflow and structured reporting."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.agent.prompt_surface_catalog import auxiliary_prompt_surfaces
from krelcode.agent.workflows import TaskType, select_task_workflow
from krelcode.core.redact import SecretRedactor
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.security_review.surfaces import security_review_prompt_surface
from krelcode.security_review.prompt import (
    SECURITY_REVIEW_SYSTEM_PROMPT,
    build_security_review_input,
)
from krelcode.security_review.report import (
    SECURITY_REVIEW_REPORT_SCHEMA,
    SECURITY_REVIEW_SCHEMA_VERSION,
    SecurityReviewReport,
    SecurityReviewReportError,
    normalize_security_report,
)
from krelcode.security_review.runner import run_security_review
from krelcode.security_review.tools import (
    SECURITY_REVIEW_TOOL_NAMES,
    build_security_review_registry,
    security_review_tools,
)
from krelcode.session.commands import CommandRequest, builtin_command_registry
from krelcode.session.commands.context import CommandContext
from krelcode.session.commands.handlers import BUILTIN_HANDLERS
from krelcode.session.commands.handlers.security_ops import handle_security_review
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git 不可用")


def git(root, *args):
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True, capture_output=True, text=True,
    )


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "app.py").write_text(
        "def run(user):\n    return str(user)\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def report_payload(*, severity="high", verdict="block") -> dict:
    return {
        "schema_version": SECURITY_REVIEW_SCHEMA_VERSION,
        "summary": "用户输入被拼接进危险执行路径。",
        "overall_severity": severity,
        "verdict": verdict,
        "findings": [{
            "id": "SEC-001",
            "title": "命令注入",
            "severity": severity,
            "confidence": "high",
            "category": "injection",
            "cwe": "CWE-78",
            "file": "app.py",
            "line": 2,
            "end_line": 2,
            "description": "未校验输入直接进入命令执行路径。",
            "impact": "攻击者可能执行任意命令。",
            "evidence": "app.py:2 将 user 直接传入危险 sink。",
            "remediation": "使用参数数组并增加 allowlist。",
            "references": ["https://cwe.mitre.org/data/definitions/78.html"],
        }],
        "coverage": {
            "reviewed_files": ["app.py"],
            "reviewed_areas": ["injection"],
            "notes": "检查了变更及其直接调用点。",
        },
        "limitations": [],
    }


class Args:
    def __init__(self, workspace, **overrides):
        self.workspace = str(workspace)
        self.staged = False
        self.focus = None
        self.output_format = "text"
        self.fail_on = "high"
        self.mock = True
        self.provider = "mock"
        self.model = None
        self.base_url = None
        self.config = None
        self.retries = 0
        self.max_steps = 8
        for key, value in overrides.items():
            setattr(self, key, value)


def test_security_report_schema_round_trip_and_gate():
    report = normalize_security_report(report_payload())

    assert report.findings[0].location == "app.py:2"
    assert report.to_dict() == report_payload()
    assert report.gate_failed("high") is True
    assert report.gate_failed("critical") is False
    assert report.gate_failed("never") is False


def test_security_report_rejects_inconsistent_or_unsafe_ids():
    payload = report_payload(severity="high", verdict="needs_review")
    with pytest.raises(SecurityReviewReportError, match="必须给出 block"):
        SecurityReviewReport.from_dict(payload)

    payload = report_payload()
    payload["overall_severity"] = "low"
    with pytest.raises(SecurityReviewReportError, match="最高 finding"):
        SecurityReviewReport.from_dict(payload)

    payload = report_payload()
    payload["findings"][0]["id"] = "finding-1"
    with pytest.raises(SecurityReviewReportError, match="SEC-001"):
        SecurityReviewReport.from_dict(payload)

    payload = report_payload()
    payload["coverage"]["reviewed_files"] = ["../app.py"]
    with pytest.raises(SecurityReviewReportError, match="路径穿越"):
        SecurityReviewReport.from_dict(payload)


def test_security_workflow_and_prompt_surface_are_registered():
    selection = select_task_workflow(
        "Perform a security review of the authentication diff")
    assert selection.task_type is TaskType.SECURITY_REVIEW
    assert selection.definition.id == "security-review"
    assert "structured JSON" in selection.definition.render(
        selection.classification)

    surface = security_review_prompt_surface()
    assert surface.contract is not None
    assert surface.contract.output_schema["$id"] == SECURITY_REVIEW_REPORT_SCHEMA["$id"]
    assert "read_only_tools" in surface.capabilities
    assert "Security Review Agent" in SECURITY_REVIEW_SYSTEM_PROMPT
    assert "untrusted evidence" in SECURITY_REVIEW_SYSTEM_PROMPT


def test_security_tool_pool_contains_only_local_read_only_tools():
    tools = security_review_tools()
    assert {tool.name for tool in tools} == set(SECURITY_REVIEW_TOOL_NAMES)
    assert all(tool.read_only for tool in tools)
    assert not ({tool.name for tool in tools} & {
        "write_file", "edit_file", "run_command", "web_fetch",
    })
    registry = build_security_review_registry()
    assert set(registry.names()) == set(SECURITY_REVIEW_TOOL_NAMES)


def test_security_input_is_bounded_and_redacted(repo):
    (repo / "app.py").write_text(
        "def run(user):\n    api_key = 'supersecretvalue'\n    return user\n",
        encoding="utf-8",
    )
    redactor = SecretRedactor(["supersecretvalue"])
    review_input = asyncio.run(build_security_review_input(
        repo, focus="injection", redactor=redactor))

    assert review_input is not None
    assert review_input.mode == "working-tree"
    assert review_input.changed_files == ("app.py",)
    assert "supersecretvalue" not in review_input.prompt
    assert "***" in review_input.prompt
    assert "<untrusted_change_set>" in review_input.prompt


def test_security_review_cli_returns_structured_report_and_gate_result(repo, capsys):
    (repo / "app.py").write_text(
        "import os\n\ndef run(user):\n    return os.system(user)\n",
        encoding="utf-8",
    )
    provider = MockProvider(
        scripted=[json.dumps(report_payload(), ensure_ascii=False)],
        usage={"input_tokens": 12, "output_tokens": 20, "total_tokens": 32},
    )

    rc = asyncio.run(run_security_review(
        Args(repo, output_format="json"), llm=provider))

    assert rc == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True
    assert payload["report"]["findings"][0]["category"] == "injection"
    assert payload["gate"] == {"fail_on": "high", "failed": True}
    assert payload["scope"]["changed_files"] == ["app.py"]
    assert payload["usage"]["calls"] == 1

    names = {tool["name"] for tool in provider.tools_seen[0]}
    assert names == {"read_file", "grep", "glob", "tool_search"}
    system = provider.calls[0][0].content
    assert "Security Review Agent" in system
    assert 'type="security_review"' in system
    assert "read-only" in system


def test_security_review_cli_redacts_model_output(repo, capsys):
    (repo / "app.py").write_text(
        "def run(user):\n    return user\n", encoding="utf-8")
    payload = report_payload()
    payload["findings"][0]["evidence"] = "leaked sk-securitysecret123"
    payload["findings"][0]["severity"] = "medium"
    payload["overall_severity"] = "medium"
    payload["verdict"] = "needs_review"
    provider = MockProvider(scripted=[json.dumps(payload, ensure_ascii=False)])

    rc = asyncio.run(run_security_review(
        Args(repo, output_format="json", fail_on="high"), llm=provider))

    assert rc == 0
    output = capsys.readouterr().out
    assert "sk-securitysecret123" not in output
    assert "***" in output


def test_security_review_clean_workspace_needs_no_provider(repo, capsys):
    rc = asyncio.run(run_security_review(
        Args(repo, mock=False, provider=None, output_format="json")))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {
        "ok": True,
        "report": None,
        "reason": "no_changes",
        "scope": {"mode": "working-tree", "workspace": str(repo.resolve())},
    }


def test_security_review_slash_command_restricts_tools_and_builds_prompt(repo):
    (repo / "app.py").write_text(
        "def run(user):\n    return user\n", encoding="utf-8")
    git(repo, "add", "app.py")
    loop = AgentLoop(
        llm=MockProvider(scripted=[]),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=repo),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )
    registry = builtin_command_registry()
    context = CommandContext(
        request=CommandRequest(
            "security-review", "--staged --focus input validation"),
        loop=loop,
        command_registry=registry,
        workspace_root=repo,
    )

    result = asyncio.run(handle_security_review(context))

    assert result.handled is True
    assert result.run_prompt is not None
    assert result.run_prompt.startswith(
        "Run a defensive security review for this turn")
    assert "security-review Workflow" in result.run_prompt
    assert 'mode="staged"' in result.run_prompt
    assert result.tool_allowlist is not None
    assert set(result.tool_allowlist) == set(SECURITY_REVIEW_TOOL_NAMES)
    assert registry.resolve("/security-review").name == "security-review"
    assert BUILTIN_HANDLERS["security-review"] is handle_security_review
