"""P1-3 IDE integration contracts: manifests, hosts and shared safety rules."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EDITORS = ROOT / "editors"


def test_vscode_manifest_exposes_minimal_editor_surface():
    manifest = json.loads((EDITORS / "vscode" / "package.json").read_text())
    commands = [item["command"] for item in manifest["contributes"]["commands"]]
    assert commands == [
        "krelcode.ask",
        "krelcode.explainSelection",
        "krelcode.reviewFile",
        "krelcode.stop",
        "krelcode.newSession",
        "krelcode.showStatus",
    ]
    properties = manifest["contributes"]["configuration"]["properties"]
    assert properties["krelcode.autoApprove"]["default"] is False
    assert properties["krelcode.noSandbox"]["default"] is False
    assert properties["krelcode.permissionTimeoutSeconds"]["default"] == 120
    assert manifest["main"] == "./dist/extension.js"


def test_jetbrains_plugin_registers_toolwindow_actions_and_settings():
    xml = ET.parse(
        EDITORS / "jetbrains" / "src" / "main" / "resources" / "META-INF" / "plugin.xml"
    )
    root = xml.getroot()
    assert root.findtext("id") == "com.krelcode.ide"
    assert root.find(".//toolWindow[@id='Krelcode']") is not None
    assert root.find(".//applicationConfigurable[@displayName='Krelcode']") is not None
    assert root.find(".//projectService") is not None
    action_ids = {node.attrib["id"] for node in root.findall(".//actions/action")}
    assert action_ids == {
        "Krelcode.Ask",
        "Krelcode.ExplainSelection",
        "Krelcode.ReviewFile",
        "Krelcode.Stop",
    }
    for node in root.findall(".//actions/action"):
        class_name = node.attrib["class"].split(".")[-1]
        assert (
            EDITORS / "jetbrains" / "src" / "main" / "java" /
            "com" / "krelcode" / "ide" / f"{class_name}.java"
        ).exists()


def test_shared_bridge_has_no_agent_core_duplication():
    source = (EDITORS / "shared" / "src" / "bridge.ts").read_text()
    assert "AgentTransport" in source
    assert "@krelcode/sdk" not in source
    sdk_adapter = (EDITORS / "shared" / "src" / "sdk_transport.ts").read_text()
    assert 'const packageName = "@krelcode/sdk"' in sdk_adapter
    assert "Workspace / Provider / MCP / Sandbox" not in source


def test_editor_context_blocks_sensitive_paths_and_marks_evidence_untrusted():
    context = (EDITORS / "shared" / "src" / "context.ts").read_text()
    assert "\\.env" in context
    assert "credentials*" in context
    assert "requires_validation: true" in context
    assert "trust: untrusted-editor-data" in context
    assert "maxSelectionCharacters" in context

    java = (
        EDITORS / "jetbrains" / "src" / "main" / "java" /
        "com" / "krelcode" / "ide" / "SecretGuard.java"
    ).read_text()
    assert "credentials*" in java
    assert "sk-[A-Za-z0-9]" in java


def test_editor_docs_cover_protocol_install_and_security():
    docs = (EDITORS / "README.md").read_text()
    for required in (
        "permission_response",
        "user_input_response",
        "Krelcode: Explain Selection",
        "gradle buildPlugin",
        "api_key_env",
        "untrusted-editor-data",
    ):
        assert required in docs
