"""Complete runtime release identity, diffing and WebUI lookup."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from krelcode.app import build_loop, build_parser
from krelcode.bootstrap.composition.release_manifest import (
    build_runtime_release_manifest,
)
from krelcode.enterprise.policy import EnterprisePolicy
from krelcode.observability.models import RunManifest, RuntimeReleaseManifest
from krelcode.plugins import PluginManager
from krelcode.skills import SkillManager
from krelcode.webui.store import DashboardStore


class Supervisor:
    def status(self) -> list[dict[str, object]]:
        return [{
            "name": "docs",
            "transport": "stdio",
            "alive": True,
            "tools": 2,
            "prompts": 0,
            "resources": 0,
            "resource_templates": 0,
            "oauth": False,
            "server": "docs-server",
            "instructions": False,
        }]


def release(**changes: object) -> RuntimeReleaseManifest:
    values = {
        "agent_definition": {"name": "krelcode", "kind": "main"},
        "prompt_release": {"release_id": "release-1", "release_hash": "hash-1"},
        "model_profile": {"profile": "default"},
        "tool_catalog": {"hash": "tools-1"},
        "mcp_servers": {"count": 0},
        "plugins": {"count": 0},
        "skills": {"count": 0},
        "policy_version": {"version": "1"},
        "memory_schema": {"schema": "memory-v2"},
        "context_engine": {"engine": "v3"},
        "code_version": {"krelcode": "0.81.0"},
    }
    values.update(changes)
    return RuntimeReleaseManifest(**values)


def test_runtime_release_manifest_round_trip_and_component_diff() -> None:
    left = release()
    right = release(
        prompt_release={"release_id": "release-2", "release_hash": "hash-2"},
        plugins={"count": 1, "records": [{"name": "demo", "version": "2.0.0"}]},
    )

    restored = RuntimeReleaseManifest.from_dict(left.to_dict())
    assert restored.release_hash == left.release_hash
    assert restored.release_id == left.release_id
    assert len(restored.component_hashes()) == 11

    diff = left.diff(right)
    changed = diff["changed_components"]
    assert set(changed) == {"prompt_release", "plugins"}
    assert changed["prompt_release"]["left"]["release_id"] == "release-1"
    assert changed["prompt_release"]["right"]["release_id"] == "release-2"
    assert "model_profile" in diff["unchanged_components"]


def test_run_manifest_records_complete_runtime_release() -> None:
    runtime_release = release()
    manifest = RunManifest(
        run_id="run-1",
        started_at=1.0,
        workspace_root="/repo",
        code_version="0.81.0",
        provider="mock",
        model="mock",
        run_mode="repl",
        permission_mode="default",
        config_hash="config",
        tool_catalog_hash="tools",
        runtime_release=runtime_release,
    )

    raw = manifest.to_dict()
    assert raw["runtime_release_id"] == runtime_release.release_id
    assert raw["runtime_release_hash"] == runtime_release.release_hash
    assert set(raw["runtime_release"]) >= {
        "agent_definition",
        "prompt_release",
        "model_profile",
        "tool_catalog",
        "mcp_servers",
        "plugins",
        "skills",
        "policy_version",
        "memory_schema",
        "context_engine",
        "code_version",
    }
    restored = RunManifest.from_dict(raw)
    assert isinstance(restored.runtime_release, RuntimeReleaseManifest)
    assert restored.runtime_release.release_hash == runtime_release.release_hash


def test_builder_collects_assembled_runtime_components(tmp_path: Path) -> None:
    parser = build_parser()
    args = parser.parse_args([
        "--mock",
        "--workspace",
        str(tmp_path),
        "--no-context",
        "--no-mcp",
        "--no-skills",
        "--permission-mode",
        "default",
    ])

    async def build():
        loop, _ = await build_loop(args, {})
        plugin_manager = PluginManager(tmp_path, home=tmp_path / "home")
        skill_manager = SkillManager(
            tmp_path, home=tmp_path / "home", enabled=False)
        memory_store = object()
        built = build_runtime_release_manifest(
            code_version="0.81.0",
            run_mode="repl",
            loop=loop,
            supervisor=Supervisor(),  # type: ignore[arg-type]
            plugin_manager=plugin_manager,
            skill_manager=skill_manager,
            memory_store=memory_store,  # type: ignore[arg-type]
            enterprise_policy=EnterprisePolicy(),
            tool_names=loop.registry.names(),
        )
        return built

    built = asyncio.run(build())
    assert built.agent_definition["name"] == "krelcode"
    assert built.prompt_release["release_id"]
    assert built.tool_catalog["count"] > 0
    assert built.mcp_servers["servers"][0]["name"] == "docs"
    assert built.plugins["count"] == 0
    assert built.memory_schema["schema"] == "memory-v2"
    assert built.context_engine["engine"] == "v3"
    assert built.context_engine["enabled"] is False
    assert built.code_version["krelcode"] == "0.81.0"


def test_webui_store_exposes_release_and_diff(tmp_path: Path) -> None:
    runs_root = tmp_path / ".krelcode" / "observability" / "runs"
    for run_id, runtime_release in (
        ("run-a", release()),
        ("run-b", release(
            model_profile={"profile": "deepseek"},
            code_version={"krelcode": "0.82.0"},
        )),
    ):
        run_dir = runs_root / run_id
        run_dir.mkdir(parents=True)
        manifest = RunManifest(
            run_id=run_id,
            started_at=1.0,
            workspace_root=str(tmp_path),
            code_version=str(runtime_release.code_version["krelcode"]),
            provider="mock",
            model="mock",
            run_mode="repl",
            permission_mode="default",
            config_hash="config",
            tool_catalog_hash="tools",
            runtime_release=runtime_release,
        )
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest.to_dict()), encoding="utf-8")

    store = DashboardStore(tmp_path)
    loaded = store.runtime_release("run-a")
    assert loaded.release_id == release().release_id
    diff = store.diff_runtime_releases("run-a", "run-b")
    assert set(diff["changed_components"]) == {
        "model_profile", "code_version"}
