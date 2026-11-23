"""P1-2: plugin signatures, dependency resolution and trusted registries."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from krelcode.app import build_loop, build_parser
from krelcode.plugins import (
    DependencyNode,
    PluginDependency,
    PluginIntegrityError,
    PluginManager,
    PluginRegistryError,
    PluginSignatureError,
    PluginTrustPolicy,
    PluginTrustError,
    PluginTrustStore,
    create_signature_document,
    package_integrity,
    parse_constraint,
    resolve_dependencies,
    sign,
    verify,
)
from krelcode.plugins.registry import create_registry_signature
from krelcode.plugins.registry import PluginRegistry, RegistryEntry
from krelcode.plugins.signing import (
    public_key_from_seed,
    encode_public_key,
)
from krelcode.session.controller import SessionController


SEED = bytes.fromhex(
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
SEED2 = bytes.fromhex(
    "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")


def write_plugin(
    root: Path,
    name: str,
    version: str,
    *,
    dependencies: dict[str, str] | None = None,
    optional: dict[str, str] | None = None,
    conflicts: list[str] | None = None,
) -> Path:
    plugin = root / name
    (plugin / "commands").mkdir(parents=True)
    extra = ""
    if dependencies:
        extra += "\n[dependencies]\n" + "".join(
            f'{key} = "{value}"\n' for key, value in dependencies.items())
    if optional:
        extra += "\n[optional_dependencies]\n" + "".join(
            f'{key} = "{value}"\n' for key, value in optional.items())
    if conflicts:
        extra += "\nconflicts = [" + ", ".join(
            json.dumps(value) for value in conflicts) + "]\n"
    (plugin / "plugin.toml").write_text(
        f'''manifest_version = 2
name = "{name}"
version = "{version}"
description = "{name} plugin"

[commands]
paths = ["commands"]
{extra}''',
        encoding="utf-8",
    )
    (plugin / "commands" / "main.md").write_text(
        f"---\nname: {name}-main\ndescription: Run {name}\n---\nDo {name}.\n",
        encoding="utf-8",
    )
    return plugin


def sign_plugin(
    plugin: Path,
    *,
    registry: str = "team",
    key_id: str = "release-1",
    seed: bytes = SEED,
) -> None:
    manifest_text = (plugin / "plugin.toml").read_text(encoding="utf-8")
    name = next(
        line.split("=", 1)[1].strip().strip('"')
        for line in manifest_text.splitlines() if line.startswith("name ="))
    version = next(
        line.split("=", 1)[1].strip().strip('"')
        for line in manifest_text.splitlines() if line.startswith("version ="))
    document = create_signature_document(
        plugin=name,
        version=version,
        key_id=key_id,
        registry=registry,
        integrity=package_integrity(plugin),
        seed=seed,
    )
    (plugin / "plugin.sig.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def trust_store(tmp_path: Path) -> PluginTrustStore:
    store = PluginTrustStore(tmp_path / "home" / ".krelcode" / "plugin-trust.json")
    registry_root = tmp_path / "registry"
    registry_root.mkdir(parents=True, exist_ok=True)
    store.add_registry("team", registry_root)
    store.add_key(
        "team",
        "release-1",
        encode_public_key(public_key_from_seed(SEED)),
        label="Release One",
    )
    store.add_key(
        "team",
        "release-2",
        encode_public_key(public_key_from_seed(SEED2)),
        label="Release Two",
    )
    return store


def test_semver_constraints_and_dependency_topology():
    for version, constraint in (
        ("1.2.3", "^1.0"),
        ("1.9.9", "~1.9"),
        ("2.0.0", ">=2,<3"),
        ("0.4.1", "0.4.x"),
        ("3.1.0", "2 || 3"),
    ):
        assert parse_constraint(constraint).matches(version)
    assert not parse_constraint("^1.0").matches("2.0.0")

    nodes = {
        "app": DependencyNode(
            "app", "1.0.0", required=(PluginDependency("core", "^1.0"),)),
        "core": DependencyNode(
            "core", "1.2.0", required=(PluginDependency("util", "~2.1"),)),
        "util": DependencyNode("util", "2.1.3"),
        "unrelated": DependencyNode("unrelated", "1.0.0"),
    }
    resolution = resolve_dependencies(nodes)
    assert resolution.order.index("util") < resolution.order.index("core")
    assert resolution.order.index("core") < resolution.order.index("app")
    assert "unrelated" in resolution.order
    assert resolution.blocked == {}


def test_dependency_missing_disabled_cycle_conflict_and_optional(tmp_path):
    missing = resolve_dependencies({
        "app": DependencyNode("app", "1.0.0", required=(PluginDependency("core"),)),
    })
    assert "app" in missing.blocked
    assert any(item.code == "missing" for item in missing.diagnostics)

    disabled = resolve_dependencies({
        "app": DependencyNode("app", "1.0.0", required=(PluginDependency("core"),)),
        "core": DependencyNode("core", "1.0.0"),
    }, disabled={"core"})
    assert set(disabled.blocked) == {"app"}

    cycle = resolve_dependencies({
        "a": DependencyNode("a", "1.0.0", required=(PluginDependency("b"),)),
        "b": DependencyNode("b", "1.0.0", required=(PluginDependency("a"),)),
    })
    assert set(cycle.blocked) == {"a", "b"}
    assert any(item.code == "cycle" for item in cycle.diagnostics)

    conflict = resolve_dependencies({
        "a": DependencyNode(
            "a", "1.0.0", conflicts=(PluginDependency("b", "<2"),)),
        "b": DependencyNode("b", "1.5.0"),
    })
    assert set(conflict.blocked) == {"a", "b"}

    optional = resolve_dependencies({
        "app": DependencyNode(
            "app", "1.0.0", optional=(PluginDependency("docs", "^1"),)),
    })
    assert optional.blocked == {}
    assert optional.order == ("app",)


def test_ed25519_rfc8032_vector_and_tamper_rejection():
    message = b""
    expected_public = bytes.fromhex(
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    expected_signature = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
    assert public_key_from_seed(SEED) == expected_public
    assert sign(message, SEED) == expected_signature
    assert verify(message, expected_signature, expected_public)
    assert not verify(b"tampered", expected_signature, expected_public)
    broken = bytearray(expected_signature)
    broken[0] ^= 1
    assert not verify(message, bytes(broken), expected_public)
    identity_key = (1).to_bytes(32, "little")
    assert not verify(message, expected_signature, identity_key)


def test_signed_plugin_load_tamper_and_revocation(tmp_path):
    store = trust_store(tmp_path)
    with pytest.raises(PluginTrustError, match="已存在"):
        store.add_key(
            "team",
            "release-1",
            encode_public_key(public_key_from_seed(SEED2)),
        )
    root = tmp_path / "plugins"
    plugin = write_plugin(root, "signed", "1.0.0")
    (plugin / "__pycache__").mkdir()
    (plugin / "__pycache__" / "evil.pyc").write_bytes(b"attacker cache")
    sign_plugin(plugin)
    manager = PluginManager(
        tmp_path / "workspace",
        home=tmp_path / "home",
        extra_paths=[root],
        trust_store=store,
        trust_policy=PluginTrustPolicy(mode="prefer"),
    )

    records = asyncio.run(manager.load())
    assert records[0].status == "loaded"
    assert records[0].trust_status == "valid"
    assert records[0].registry == "team"
    assert records[0].key_id == "release-1"
    assert not (plugin / "__pycache__").exists()

    (plugin / "commands" / "main.md").write_text("tampered", encoding="utf-8")
    records = asyncio.run(manager.reload())
    assert records[0].status == "untrusted"
    assert records[0].trust_status == "invalid"
    assert "Hash" in records[0].blocked_reason

    # Restore a valid package, then revoke its key and verify fail-closed reload.
    (plugin / "commands" / "main.md").write_text(
        "---\nname: signed-main\ndescription: Run signed\n---\nDo signed.\n",
        encoding="utf-8",
    )
    sign_plugin(plugin)
    assert asyncio.run(manager.reload())[0].status == "loaded"
    store.set_revoked("team", "release-1")
    records = asyncio.run(manager.reload())
    assert records[0].status == "untrusted"
    assert records[0].trust_status == "revoked"


def test_unsigned_strict_policy_and_bounded_dependency_failure(tmp_path):
    root = tmp_path / "plugins"
    write_plugin(root, "core", "1.0.0")
    write_plugin(root, "app", "1.0.0", dependencies={"core": "^1"})
    write_plugin(root, "orphan", "1.0.0")
    store = trust_store(tmp_path)
    strict = PluginManager(
        tmp_path / "workspace",
        home=tmp_path / "home",
        extra_paths=[root],
        trust_store=store,
        trust_policy=PluginTrustPolicy(mode="strict"),
    )
    records = asyncio.run(strict.load())
    assert {record.name: record.status for record in records} == {
        "app": "untrusted",
        "core": "untrusted",
        "orphan": "untrusted",
    }

    preferred = PluginManager(
        tmp_path / "workspace",
        home=tmp_path / "home",
        extra_paths=[root],
        trust_store=store,
        trust_policy=PluginTrustPolicy(mode="prefer"),
    )
    asyncio.run(preferred.load())
    asyncio.run(preferred.set_enabled("core", False))
    records = asyncio.run(preferred.load())
    statuses = {record.name: record.status for record in records}
    assert statuses["core"] == "disabled"
    assert statuses["app"] == "blocked"
    assert statuses["orphan"] == "loaded"
    with pytest.raises(ValueError, match="仍被.*依赖"):
        asyncio.run(preferred.remove("core"))


def test_integrity_rejects_symlink_and_tampering(tmp_path):
    root = tmp_path / "plugin"
    plugin = write_plugin(tmp_path, "demo", "1.0.0")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (plugin / "link").symlink_to(outside)
    with pytest.raises(PluginIntegrityError, match="符号链接"):
        package_integrity(plugin)
    (plugin / "link").unlink()
    before = package_integrity(plugin)
    (plugin / "commands" / "plugin.sig.json").write_text("nested", encoding="utf-8")
    nested = package_integrity(plugin)
    assert "commands/plugin.sig.json" in {item.path for item in nested.files}
    (plugin / "commands" / "plugin.sig.json").unlink()
    (plugin / "commands" / "main.md").write_text("changed", encoding="utf-8")
    after = package_integrity(plugin)
    assert before.files_sha256 != after.files_sha256
    assert before.manifest_sha256 == after.manifest_sha256

    registry = PluginRegistry(
        "team", tmp_path, (RegistryEntry("demo", "1.0.0", "../outside"),), "0" * 64)
    with pytest.raises(PluginRegistryError, match="越界"):
        registry.package_path(registry.entries[0])


def test_install_rejects_downgrade_and_signer_switch(tmp_path):
    store = trust_store(tmp_path)
    manager = PluginManager(
        tmp_path / "workspace", home=tmp_path / "home", trust_store=store)
    v1_root = tmp_path / "v1"
    v2_root = tmp_path / "v2"
    v1 = write_plugin(v1_root, "demo", "1.0.0")
    v2 = write_plugin(v2_root, "demo", "2.0.0")
    sign_plugin(v1, key_id="release-1", seed=SEED)
    sign_plugin(v2, key_id="release-2", seed=SEED2)

    asyncio.run(manager.install(v1, scope="user"))
    with pytest.raises(PluginSignatureError, match="签名者"):
        asyncio.run(manager.update(v2, scope="user"))

    switched_root = tmp_path / "v2-same-key"
    switched = write_plugin(switched_root, "demo", "2.0.0")
    sign_plugin(switched, key_id="release-1", seed=SEED)
    asyncio.run(manager.update(switched, scope="user"))
    with pytest.raises(ValueError, match="降级"):
        asyncio.run(manager.update(v1, scope="user"))

    unsigned_root = tmp_path / "unsigned"
    unsigned = write_plugin(unsigned_root, "unsigned", "1.0.0")
    hardened = PluginManager(
        tmp_path / "workspace",
        home=tmp_path / "hardened-home",
        trust_store=trust_store(tmp_path / "hardened"),
        trust_policy=PluginTrustPolicy(
            mode="prefer", require_signature_for_install=True),
    )
    with pytest.raises(PluginSignatureError, match="要求有效签名"):
        asyncio.run(hardened.install(unsigned, scope="user"))


def test_trusted_registry_installs_signed_dependency_bundle(tmp_path):
    store = trust_store(tmp_path)
    registry_root = tmp_path / "registry"
    packages = registry_root / "packages"
    util = write_plugin(packages, "util", "2.1.0")
    app = write_plugin(packages, "app", "1.0.0", dependencies={"util": "^2.1"})
    sign_plugin(util)
    sign_plugin(app)
    index = {
        "schema": 1,
        "name": "team",
        "plugins": [
            {"name": "app", "version": "1.0.0", "path": "packages/app"},
            {"name": "util", "version": "2.1.0", "path": "packages/util"},
        ],
    }
    index_bytes = (json.dumps(index, sort_keys=True, indent=2) + "\n").encode("utf-8")
    (registry_root / "registry.json").write_bytes(index_bytes)
    (registry_root / "registry.sig.json").write_text(
        json.dumps(create_registry_signature(
            registry="team",
            index_bytes=index_bytes,
            key_id="release-1",
            seed=SEED,
        ), indent=2) + "\n",
        encoding="utf-8",
    )

    manager = PluginManager(
        tmp_path / "workspace", home=tmp_path / "home", trust_store=store)
    results = asyncio.run(manager.install_from_registry(
        "team", "app", constraint="^1", scope="user"))
    assert [item.name for item in results] == ["util", "app"]
    assert {item.action for item in results} == {"install"}
    records = asyncio.run(manager.load())
    assert [(record.name, record.status, record.trust_status) for record in records] == [
        ("app", "loaded", "valid"),
        ("util", "loaded", "valid"),
    ]
    assert manager.tools == ()
    assert (tmp_path / "home" / ".krelcode" / "plugins" / "app" /
            ".krelcode-registry.json").is_file()

    (registry_root / "registry.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PluginRegistryError, match="签名|Hash"):
        from krelcode.plugins.registry import load_registry
        load_registry("team", trust_store=store, require_signature=True)


def test_registry_solver_intersects_transitive_version_constraints(tmp_path):
    store = trust_store(tmp_path)
    registry_root = tmp_path / "registry"
    packages = registry_root / "packages"
    util_v2 = write_plugin(packages / "v2", "util", "2.0.0")
    util_v1 = write_plugin(packages / "v1", "util", "1.8.0")
    bridge = write_plugin(
        packages, "bridge", "1.0.0", dependencies={"util": "*"})
    app = write_plugin(
        packages,
        "app",
        "1.0.0",
        dependencies={"bridge": "^1", "util": "<2"},
    )
    for package in (util_v2, util_v1, bridge, app):
        sign_plugin(package)
    index = {
        "schema": 1,
        "name": "team",
        "plugins": [
            {"name": "app", "version": "1.0.0", "path": "packages/app"},
            {"name": "bridge", "version": "1.0.0", "path": "packages/bridge"},
            {"name": "util", "version": "1.8.0", "path": "packages/v1/util"},
            {"name": "util", "version": "2.0.0", "path": "packages/v2/util"},
        ],
    }
    index_bytes = (json.dumps(index, sort_keys=True, indent=2) + "\n").encode("utf-8")
    (registry_root / "registry.json").write_bytes(index_bytes)
    (registry_root / "registry.sig.json").write_text(
        json.dumps(create_registry_signature(
            registry="team",
            index_bytes=index_bytes,
            key_id="release-1",
            seed=SEED,
        ), indent=2) + "\n",
        encoding="utf-8",
    )
    manager = PluginManager(
        tmp_path / "workspace", home=tmp_path / "home", trust_store=store)
    asyncio.run(manager.install_from_registry(
        "team", "app", constraint="*", scope="user"))
    records = asyncio.run(manager.load())
    versions = {record.name: record.version for record in records}
    assert versions == {"app": "1.0.0", "bridge": "1.0.0", "util": "1.8.0"}


def test_plugin_commands_verify_deps_and_trust_list(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "plugins"
    write_plugin(root, "demo", "1.0.0")
    args = build_parser().parse_args([
        "--mock", "--workspace", str(tmp_path), "--no-context",
        "--plugins-dir", str(root),
    ])
    loop, _ = asyncio.run(build_loop(args, {}))
    controller = SessionController(loop)

    verify_result = asyncio.run(controller.execute_command("/plugins verify demo"))
    verify_text = "\n".join(verify_result.output)
    assert "signature=unsigned" in verify_text
    assert "message: unsigned" in verify_text

    deps_result = asyncio.run(controller.execute_command("/plugins deps demo"))
    deps_text = "\n".join(deps_result.output)
    assert "requires: （无）" in deps_text
    assert "dependents: （无）" in deps_text

    trust_result = asyncio.run(controller.execute_command("/plugins trust list"))
    assert "plugin-trust" in "\n".join(trust_result.output)


def test_malformed_trust_store_disables_plugin_plane_without_crashing(tmp_path):
    trust_path = tmp_path / "bad-trust.json"
    trust_path.write_text("{broken", encoding="utf-8")
    args = build_parser().parse_args([
        "--mock", "--workspace", str(tmp_path), "--no-context",
    ])
    loop, _ = asyncio.run(build_loop(args, {
        "plugins": {"trust_store": str(trust_path)},
    }))
    assert loop.plugin_manager.enabled is False


def test_build_loop_applies_strict_plugin_trust_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "plugins"
    write_plugin(root, "demo", "1.0.0")
    args = build_parser().parse_args([
        "--mock", "--workspace", str(tmp_path), "--no-context",
        "--plugins-dir", str(root),
    ])
    loop, _ = asyncio.run(build_loop(args, {
        "plugins": {"trust": {"mode": "strict"}},
    }))
    assert loop.plugin_manager.records()[0].status == "untrusted"
