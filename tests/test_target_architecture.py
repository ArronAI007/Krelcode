"""Target layer directories and end-to-end lifecycle identity."""

from __future__ import annotations

from pathlib import Path

from krelcode.agent.orchestration import AgentLoop, ToolIntent
from krelcode.application import (
    AcceptanceContract,
    ApplicationRequest,
    ApplicationRequestFactory,
)
from krelcode.contracts.change import ChangeCandidate, ChangeType
from krelcode.contracts.governance import AuditCorrelation, GovernanceDecision
from krelcode.contracts.lifecycle import RuntimeLifecycleEnvelope
from krelcode.contracts.runtime import (
    ArtifactIdentity,
    RuntimeReleaseManifest,
)
from krelcode.governance.artifact_registry import ArtifactRegistry
from krelcode.governance.control_plane import DefaultControlPlane
from krelcode.runtime.execution import (
    ExecutionRuntimeRouter,
    LocalToolRuntime,
)
from krelcode.tools.runtime import ExecutionRuntimeRouter as LegacyRouter
from krelcode.tuning.evaluation import BenchmarkPorts
from krelcode.tuning.releases import RuntimeReleaseManifest as TuningRelease


ROOT = Path(__file__).parent.parent / "src" / "krelcode"


def test_target_directories_and_canonical_imports_exist() -> None:
    expected = (
        "application/triggers",
        "application/acceptance",
        "agent/prompt",
        "agent/context",
        "agent/memory",
        "agent/planner",
        "agent/orchestration",
        "runtime/execution",
        "runtime/tools",
        "runtime/mcp",
        "runtime/sandbox",
        "runtime/jobs",
        "runtime/daemon",
        "runtime/scheduler",
        "governance/control_plane",
        "governance/policy_adapters",
        "governance/artifact_registry",
        "governance/audit",
        "tuning/tracing",
        "tuning/evaluation",
        "tuning/safety_review",
        "tuning/change_pipeline",
        "tuning/releases",
    )
    for relative in expected:
        assert (ROOT / relative).is_dir()
        assert (ROOT / relative / "__init__.py").is_file()

    assert ApplicationRequest is not None
    assert AcceptanceContract is not None
    assert AgentLoop is not None
    assert ToolIntent is not None
    assert ExecutionRuntimeRouter is LegacyRouter
    assert LocalToolRuntime is not None
    assert DefaultControlPlane is not None
    assert BenchmarkPorts is not None
    assert TuningRelease is RuntimeReleaseManifest


def test_runtime_lifecycle_envelope_links_application_to_next_release() -> None:
    application_request = ApplicationRequestFactory.from_input_source(
        "repl", goal="change behavior")
    release = RuntimeReleaseManifest(
        agent_definition={"name": "krelcode"},
        prompt_release={"release_id": "release-1"},
        model_profile={"profile": "default"},
        tool_catalog={"hash": "tools-1"},
        mcp_servers={"count": 0},
        plugins={"count": 0},
        skills={"count": 0},
        policy_version={"version": "1"},
        memory_schema={"schema": "memory-v2"},
        context_engine={"engine": "v3"},
        code_version={"krelcode": "1.0.0"},
    )
    decision = GovernanceDecision(
        allowed=True,
        audit=AuditCorrelation(correlation_id="trace-1"),
    )
    candidate = ChangeCandidate(
        change_type=ChangeType.PROMPT,
        target="prompt:base",
        before={"release": "release-1"},
        after={"release": "release-2"},
    )
    next_release = RuntimeReleaseManifest(
        **{
            **{
                name: dict(getattr(release, name))
                for name in (
                    "agent_definition", "prompt_release", "model_profile",
                    "tool_catalog", "mcp_servers", "plugins", "skills",
                    "policy_version", "memory_schema", "context_engine",
                    "code_version",
                )
            },
            "prompt_release": {"release_id": "release-2"},
        },
    )

    envelope = RuntimeLifecycleEnvelope(
        application_request=application_request,
        runtime_release=release,
        governance_decision=decision,
        trace_id="trace-1",
        change_candidate=candidate,
        next_runtime_release=next_release,
    )
    assert envelope.complete
    assert envelope.trace_id == "trace-1"


def test_artifact_registry_uses_runtime_contract_identity() -> None:
    registry = ArtifactRegistry()
    artifact = ArtifactIdentity(
        kind="plugin",
        id="demo",
        version="1.0.0",
        digest="sha256:abc",
    )
    registry.register(artifact)

    assert registry.get("demo") == artifact
    assert registry.list() == (artifact,)
