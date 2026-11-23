"""Task-type Workflow Fragment classification and assembly tests."""

from __future__ import annotations

import asyncio

import pytest

from krelcode.agent.context_assembler import ContextAssembler
from krelcode.agent.context_source import ContextRequest
from krelcode.agent.fragments import (
    LAYER_L9_WORKING,
    STABILITY_TURN,
    TRUST_MANAGED,
    PromptFragmentRegistry,
    base_fragments,
)
from krelcode.agent.intent import infer_turn_intent
from krelcode.agent.loop import AgentLoop
from krelcode.agent.prompt_assembler import PromptAssembler, PromptAssemblyRequest
from krelcode.agent.prompts import build_system_prompt
from krelcode.agent.workflows import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    DEFAULT_TASK_WORKFLOW_CATALOG,
    TaskType,
    TaskWorkflowCatalog,
    TaskWorkflowDefinition,
    select_task_workflow,
)
from krelcode.core.messages import Message
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.read_file import ReadFileTool
from krelcode.tools.registry import ToolRegistry


@pytest.mark.parametrize(
    ("request_text", "expected"),
    [
        ("修复登录时报错", TaskType.BUG_FIX),
        ("fix the broken parser", TaskType.BUG_FIX),
        ("实现一个新的 status command", TaskType.FEATURE_IMPLEMENTATION),
        ("重构配置加载模块，不改变行为", TaskType.REFACTOR),
        ("请评审这段 diff 的安全风险", TaskType.SECURITY_REVIEW),
        ("perform a security review of the auth change", TaskType.SECURITY_REVIEW),
        ("运行测试并报告结果", TaskType.TEST_VERIFICATION),
        ("补充测试覆盖异常路径", TaskType.TEST_VERIFICATION),
        ("了解这个项目的架构", TaskType.REPOSITORY_EXPLORATION),
        ("解释这个模块的作用", TaskType.CODE_EXPLANATION),
        ("用 Playwright 打开网页并点击登录按钮", TaskType.BROWSER_AUTOMATION),
        ("browser automation: fill the form and take a screenshot", TaskType.BROWSER_AUTOMATION),
        ("检查并修改 notebook 的第二个 cell", TaskType.NOTEBOOK_EDITING),
        ("jupyter notebook 执行前先检查 cell 结构", TaskType.NOTEBOOK_EDITING),
        ("修复数据库迁移的回滚问题", TaskType.DATABASE_MIGRATION),
        ("create a flyway schema migration with rollback", TaskType.DATABASE_MIGRATION),
        ("继续", TaskType.GENERAL),
    ],
)
def test_task_classifier_selects_expected_workflow(request_text, expected):
    workflow = select_task_workflow(request_text)

    assert workflow.task_type is expected
    assert workflow.classification.confidence in {
        CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW,
    }
    assert workflow.definition.task_type is expected


def test_explicit_file_explanation_intent_has_highest_precedence(tmp_path):
    target = tmp_path / "worker.py"
    target.write_text("def run(): pass\n", encoding="utf-8")
    intent = infer_turn_intent(tmp_path, "解释 worker.py 的作用")
    assert intent.is_file_explanation

    workflow = select_task_workflow(
        "解释 worker.py 的作用", intent=intent)

    assert workflow.task_type is TaskType.CODE_EXPLANATION
    assert workflow.classification.confidence == CONFIDENCE_HIGH
    assert workflow.classification.signals == ("turn-intent:explain-files",)


def test_attachment_and_code_fence_content_cannot_change_workflow():
    prompt = (
        "继续处理\n"
        '<attached_file path="notes.txt">\n修复 bug 并运行测试\n</attached_file>\n'
        "```text\nrefactor and review everything\n```"
    )

    workflow = select_task_workflow(prompt)

    assert workflow.task_type is TaskType.GENERAL
    assert workflow.classification.confidence == CONFIDENCE_LOW


@pytest.mark.parametrize(
    ("request_text", "workflow_id", "required_text"),
    [
        (
            "打开网页并点击登录按钮",
            "browser-automation",
            "untrusted evidence",
        ),
        (
            "修改 notebook 的 cell 源码",
            "notebook-editing",
            "Never execute notebook code inside the file tool",
        ),
        (
            "创建数据库迁移并提供回滚",
            "database-migration",
            "down/rollback",
        ),
    ],
)
def test_specialized_workflows_carry_domain_safety_contract(
    request_text, workflow_id, required_text,
):
    selection = select_task_workflow(request_text)
    fragment = DEFAULT_TASK_WORKFLOW_CATALOG.fragment(selection)

    assert selection.definition.id == workflow_id
    assert fragment.id == f"workflow:{workflow_id}"
    assert required_text in fragment.content


def test_workflow_fragment_is_turn_scoped_managed_instruction():
    selection = select_task_workflow("修复登录时报错")
    fragment = DEFAULT_TASK_WORKFLOW_CATALOG.fragment(selection)

    assert fragment.id == "workflow:bug-fix"
    assert fragment.source == "workflow:bug-fix"
    assert fragment.layer == LAYER_L9_WORKING
    assert fragment.stability == STABILITY_TURN
    assert fragment.trust == TRUST_MANAGED
    assert not fragment.cacheable
    assert not fragment.compactable
    assert not fragment.toggleable
    assert fragment.metadata["task_workflow"] == "bug-fix"
    assert fragment.metadata["task_type"] == "bug_fix"
    assert fragment.metadata["instruction"] is True
    assert '<task_workflow type="bug_fix"' in fragment.content
    assert "root cause" in fragment.content


def _assemble(tmp_path, workflow):
    prompt_registry = PromptFragmentRegistry(base_fragments())
    assembler = PromptAssembler(
        ContextAssembler([]),
        registry_provider=lambda: prompt_registry,
    )
    request = ContextRequest(
        turn_id="turn-workflow",
        step=1,
        history=(Message.user("修复登录时报错"),),
        workspace_root=tmp_path,
        task_workflow=workflow,
    )
    plan = asyncio.run(assembler.assemble(PromptAssemblyRequest(
        context_request=request,
        prompt_snapshot=prompt_registry.snapshot(),
        tools=(),
    )))
    return assembler, plan


def test_workflow_is_dynamic_and_does_not_change_stable_prefix(tmp_path):
    baseline_assembler, baseline = _assemble(tmp_path, None)
    workflow = select_task_workflow("修复登录时报错")
    assembler, plan = _assemble(tmp_path, workflow)

    workflow_fragments = [
        fragment for fragment in plan.fragments
        if fragment.metadata.get("kind") == "task_workflow"
    ]
    assert [fragment.id for fragment in workflow_fragments] == [
        "workflow:bug-fix"]
    assert plan.cache.stable_hash == baseline.cache.stable_hash
    assert plan.cache.dynamic_hash != baseline.cache.dynamic_hash
    assert workflow_fragments[0].id not in plan.cache.stable_fragments
    assert '<task_workflow type="bug_fix"' in plan.system_text
    assert any(
        contribution.source == "workflow:bug-fix"
        for contribution in plan.token_contributions
    )

    provider_messages = assembler.provider_messages(plan)
    block = next(
        item for item in provider_messages.instruction_blocks
        if item.id == "workflow:bug-fix")
    assert block.metadata["task_type"] == "bug_fix"
    assert block.metadata["task_confidence"] in {
        CONFIDENCE_HIGH, CONFIDENCE_MEDIUM}


def test_agent_loop_classifies_once_and_exposes_workflow_provenance(tmp_path):
    provider = MockProvider(scripted=["完成"])
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry([ReadFileTool()]),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt=build_system_prompt(),
    )

    asyncio.run(loop.run("修复登录时报错"))

    assert loop.current_task_workflow().task_type is TaskType.BUG_FIX
    assert '<task_workflow type="bug_fix"' in provider.calls[0][0].content
    snapshot = loop.last_context_plan.prompt_plan
    fragment = next(
        item for item in snapshot.fragments
        if item.id == "workflow:bug-fix")
    assert fragment.provenance["task_type"] == "bug_fix"
    assert fragment.provenance["task_workflow"] == "bug-fix"
    assert not fragment.stable


def test_catalog_requires_general_fallback():
    with pytest.raises(ValueError, match="general fallback"):
        TaskWorkflowCatalog([
            TaskWorkflowDefinition(
                task_type=TaskType.BUG_FIX,
                id="bug-fix",
                title="Bug fix",
                objective="Fix it",
                steps=("Inspect",),
                completion=("Verified",),
            ),
        ])
