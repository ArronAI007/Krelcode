"""P2-2 enterprise policy, audit and OIDC/SSO tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from krelcode.agent.usage import UsageTracker
from krelcode.app import (
    build_model_switcher,
    build_parser,
    build_policy,
    build_subagent_llm_resolver,
    resolve_permission_mode,
)
from krelcode.config import load_config_layers, load_managed_config
from krelcode.core.events import EventBus, RunStarted
from krelcode.core.messages import ToolCall
from krelcode.enterprise.audit import (
    AuditConfig,
    AuditError,
    ComplianceAuditor,
    verify_audit_file,
)
from krelcode.enterprise.auth import (
    AuthError,
    OIDCClient,
    OIDCDiscovery,
    SSOConfig,
    TokenSet,
    TokenStore,
    _b64url_encode,
)
from krelcode.enterprise.policy import (
    EnterprisePolicy,
    PolicyViolation,
    load_enterprise_policy,
)
from krelcode.safety.policy import SandboxPolicy
from krelcode.tools.base import PermissionMode, RuntimeSnapshot, ToolContext


def _policy(**overrides):
    raw = {
        "enabled": True,
        "organization": "example",
        "policy_version": "2026.11.1",
        "max_permission_mode": "read-only",
        "allow_auto_approve": False,
        "allow_no_sandbox": False,
    }
    raw.update(overrides)
    return EnterprisePolicy.from_mapping(raw)


# --- managed policy --------------------------------------------------------

def test_explicit_config_cannot_hide_managed_enterprise_policy(tmp_path, monkeypatch):
    managed = tmp_path / "managed.toml"
    managed.write_text(
        '[enterprise]\nenabled=true\norganization="managed-org"\n'
        'max_permission_mode="read-only"\n',
        encoding="utf-8")
    user = tmp_path / "user.toml"
    user.write_text(
        '[enterprise]\nenabled=false\norganization="user-org"\n',
        encoding="utf-8")
    monkeypatch.setenv("KRELCODE_MANAGED_CONFIG", str(managed))
    bundle = load_config_layers(tmp_path, explicit_path=user)
    assert bundle.layers[0].scope == "explicit"
    managed_data = load_managed_config(tmp_path)
    policy = load_enterprise_policy(managed_data)
    assert policy.enabled
    assert policy.organization == "managed-org"


def test_bundle_policy_uses_managed_scope_only(tmp_path):
    managed = tmp_path / "managed.toml"
    managed.write_text(
        '[enterprise]\nenabled=true\norganization="managed"\n', encoding="utf-8")
    user = tmp_path / "user.toml"
    user.write_text(
        '[enterprise]\nenabled=false\norganization="user"\n', encoding="utf-8")
    from krelcode.config import ConfigBundle, ConfigLayer

    bundle = ConfigBundle(
        {"enterprise": {"enabled": False, "organization": "user"}},
        (
            ConfigLayer("user", user, True, {"enterprise": {"enabled": False}}),
            ConfigLayer("managed", managed, True, {
                "enterprise": {"enabled": True, "organization": "managed"}}),
        ),
    )
    policy = load_enterprise_policy(bundle.data, config_bundle=bundle)
    assert policy.enabled and policy.organization == "managed"


def test_policy_ceiling_and_unsafe_cli_options_fail_closed():
    policy = _policy()
    args = build_parser().parse_args(["--mock", "--no-sandbox"])
    with pytest.raises(PolicyViolation):
        build_policy(args, Path.cwd(), EventBus(), enterprise_policy=policy)
    auto_args = build_parser().parse_args(["--mock", "--auto-approve"])
    with pytest.raises(PolicyViolation):
        policy.validate_cli(auto_args)
    normal = build_parser().parse_args(["--mock"])
    assert resolve_permission_mode(normal, {}, policy) is PermissionMode.READ_ONLY


def test_model_hot_switch_cannot_escape_managed_provider_allowlist(tmp_path):
    policy = _policy(allowed_providers=["openai"])
    args = build_parser().parse_args(["--mock", "--workspace", str(tmp_path)])
    original = SimpleNamespace(name="mock", model="mock-model")
    loop = SimpleNamespace(
        llm=original,
        tool_context=SimpleNamespace(workspace_root=tmp_path),
    )
    switch = build_model_switcher(
        args, {}, loop, UsageTracker(model="mock-model"),
        enterprise_policy=policy,
    )

    result = switch("anthropic/claude-sonnet-4-5")

    assert "企业策略不允许 Provider anthropic" in result
    assert loop.llm is original


def test_subagent_definition_model_cannot_escape_provider_allowlist(tmp_path):
    policy = _policy(allowed_providers=["openai"])
    args = build_parser().parse_args(["--mock", "--workspace", str(tmp_path)])
    parent = SimpleNamespace(name="openai", model="parent-model")
    resolver = build_subagent_llm_resolver(
        args,
        {"agent_models": {"reviewer": "anthropic/claude-sonnet-4-5"}},
        parent,
        enterprise_policy=policy,
    )

    with pytest.raises(PolicyViolation, match="企业策略不允许 Provider anthropic"):
        resolver("reviewer")


def test_managed_deny_cannot_be_overridden_by_auto_approve(tmp_path):
    policy = _policy(disabled_tools=["run_command"])
    ctx = ToolContext(
        workspace_root=tmp_path,
        runtime=RuntimeSnapshot(permission_mode=PermissionMode.AUTO_APPROVE),
    )
    approval = SandboxPolicy(approver=None, auto_approve=True, managed_policy=policy)

    async def check():
        return await approval.check(
            ToolCall("c", "run_command", {"command": "echo hi"}), ctx)

    decision = asyncio.run(check())
    assert not decision.allowed
    assert "企业策略" in decision.reason


def test_managed_path_and_command_denials():
    policy = _policy(denied_paths=["secrets/**"], denied_commands=["curl *"])
    assert policy.deny_tool_reason(
        ToolCall("r", "read_file", {"path": "secrets/key.txt"}), Path.cwd())
    assert policy.deny_tool_reason(
        ToolCall("c", "run_command", {"command": "curl https://x"}), Path.cwd())


# --- audit -----------------------------------------------------------------

def test_audit_hash_chain_detects_tampering_and_redacts_secrets(tmp_path):
    path = tmp_path / "audit.jsonl"
    auditor = ComplianceAuditor(
        path, organization="example", principal="user-1",
        policy_version="v1", required=True)
    auditor.initialize()
    first = auditor.record("tool_call", {
        "tool": "run_command",
        "arguments": {"command": "echo hi", "password": "super-secret"},
    })
    second = auditor.record("tool_result", {"content": "ok"})
    assert first and second
    result = verify_audit_file(path)
    assert result.valid and result.records == 2
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[0]["seq"] == 1
    assert records[1]["prev_hash"] == records[0]["hash"]
    assert "super-secret" not in path.read_text()
    assert records[0]["org"] == "example"
    assert records[0]["principal"] == "user-1"

    records[0]["data"]["arguments"]["command"] = "tampered"
    path.write_text("\n".join(json.dumps(item) for item in records) + "\n")
    invalid = verify_audit_file(path)
    assert not invalid.valid
    assert "hash" in invalid.first_error or "prev_hash" in invalid.first_error


def test_audit_hmac_requires_key_and_detects_tampering(tmp_path):
    path = tmp_path / "audit.jsonl"
    auditor = ComplianceAuditor(
        path, organization="o", enabled=True, required=True,
        config=AuditConfig(enabled=True, required=True, path=path, mode="hmac"),
        hmac_key=b"test-key",
    )
    auditor.initialize()
    auditor.record("one", {})
    assert not verify_audit_file(path).valid
    assert verify_audit_file(path, hmac_key=b"test-key").valid


def test_audit_resumes_sequence_and_can_export(tmp_path):
    path = tmp_path / "audit.jsonl"
    first = ComplianceAuditor(path, organization="o", enabled=True, required=True)
    first.initialize()
    first.record("one", {})
    second = ComplianceAuditor(path, organization="o", enabled=True, required=True)
    second.initialize()
    second.record("two", {})
    result = verify_audit_file(path)
    assert result.valid and result.records == 2 and result.last_sequence == 2
    exported = second.export(tmp_path / "export.jsonl")
    assert exported.read_bytes() == path.read_bytes()


def test_audit_events_include_identity_and_never_store_user_secret(tmp_path):
    path = tmp_path / "audit.jsonl"
    auditor = ComplianceAuditor(
        path, organization="example", principal="subject-1",
        session_id="session-1", enabled=True, required=True)
    # Attach to a fresh bus to exercise the asynchronous event path.
    bus = EventBus()
    auditor.attach(bus)

    async def emit():
        await bus.emit(RunStarted(
            "use sk-abcdefgh1234567890 in this command",
            turn_id="turn-1", session_id="session-1"))

    asyncio.run(emit())
    text = path.read_text(encoding="utf-8")
    assert "sk-abcdefgh1234567890" not in text
    assert "subject-1" in text and "turn-1" in text


# --- SSO / OIDC ------------------------------------------------------------

_ISSUER = "https://id.example.com"
_CLIENT_ID = "krelcode"


class _Transport:
    def __init__(self, *, id_token: str = "") -> None:
        self.id_token = id_token
        self.discovery_url = f"{_ISSUER}/.well-known/openid-configuration"

    async def get_json(self, url, *, headers=None):
        if url == self.discovery_url:
            return {
                "issuer": _ISSUER,
                "authorization_endpoint": f"{_ISSUER}/authorize",
                "token_endpoint": f"{_ISSUER}/token",
                "jwks_uri": f"{_ISSUER}/jwks",
            }
        if url == f"{_ISSUER}/jwks":
            return {"keys": [_JWK]}
        raise AssertionError(url)

    async def post_form(self, url, data, *, headers=None):
        assert url == f"{_ISSUER}/token"
        assert data["code_verifier"]
        return {
            "access_token": "access-very-secret",
            "id_token": self.id_token,
            "token_type": "Bearer",
            "expires_in": 3600,
        }


_N_HEX = (
    "b64f5d68d3220fd789c0ed70013e07d89ca1b28aacfdc5cb369533cc87552bbd"
    "50364a04e4ec516b74cf510c38134fc93df2ba0501efdcafdb148cfeca31ae83"
    "bc59d19890a9bc0a4cd99de891cbed7eef7fd5e75a779844df938fba25150e21"
    "e2e33d092464509f906ef9d2b92ad49a4e7fa aedaf3f0534ccbdbd391d3c204f".replace(" ", "")
)
_N = int(_N_HEX, 16)
_JWK = {
    "kty": "RSA",
    "kid": "test-key",
    "n": _b64url_encode(_N.to_bytes((_N.bit_length() + 7) // 8, "big")),
    "e": "AQAB",
}
# Private exponent for the fixed test RSA key above.
_D = int(
    "6e3c9732e757d554734b614734b9196d7568a0d911b9717c6b48d4c57b9f988f"
    "248d6f3c7e6016eb6efa5cfb927cbae5e8bb7897c6900f5596975a9a4610bc80"
    "8d18e0e09f5c3685ffe94b0920d0da82f8c1eb0c33686fcc18653dcc49e7a7ef"
    "93f1870df524deaf669c5367a76ca971f9923db2ec279f0ae005bfa6c6a87dc1", 16)


def _signed_id_token(*, nonce: str, include_exp: bool = True) -> str:
    header = {"alg": "RS256", "kid": "test-key", "typ": "JWT"}
    claims = {
        "iss": _ISSUER,
        "aud": _CLIENT_ID,
        "sub": "user-42",
        "name": "Test User",
        "email": "person@example.com",
        "nonce": nonce,
    }
    if include_exp:
        claims["exp"] = int(time.time()) + 3600
    head = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    body = _b64url_encode(json.dumps(claims, separators=(",", ":")).encode())
    message = f"{head}.{body}".encode()
    digest = hashlib.sha256(message).digest()
    tail = bytes.fromhex("3031300d060960864801650304020105000420") + digest
    key_size = (_N.bit_length() + 7) // 8
    encoded = b"\x00\x01" + b"\xff" * (key_size - len(tail) - 3) + b"\x00" + tail
    signature = pow(int.from_bytes(encoded, "big"), _D, _N).to_bytes(key_size, "big")
    return f"{head}.{body}.{_b64url_encode(signature)}"


def test_oidc_pkce_exchange_and_private_token_status(tmp_path):
    config = SSOConfig(
        enabled=True, issuer=_ISSUER, client_id=_CLIENT_ID,
        token_dir=tmp_path,
    )
    nonce = "nonce-abc"
    id_token = _signed_id_token(nonce=nonce)
    client = OIDCClient(config, transport=_Transport(id_token=id_token))
    verifier, challenge = client.new_pkce()
    assert len(verifier) >= 43 and challenge

    async def exchange():
        discovery = await client.discovery()
        url = client.build_authorization_url(
            discovery, redirect_uri="http://127.0.0.1:9999/callback",
            state="state", nonce=nonce, code_challenge=challenge)
        assert "code_challenge_method=S256" in url
        return await client.exchange_code(
            code="code",
            code_verifier=verifier,
            nonce=nonce,
            redirect_uri="http://127.0.0.1:9999/callback",
            expected_state="state",
            returned_state="state",
        )

    tokens = asyncio.run(exchange())
    assert tokens.subject == "user-42"
    assert client.status().authenticated
    assert "access-very-secret" not in json.dumps(client.status().as_record())
    assert "access-very-secret" in client.redaction_secrets()
    assert (tmp_path / f"{hashlib.sha256(_ISSUER.encode()).hexdigest()[:32]}.json").exists()


def test_oidc_rejects_state_mismatch():
    config = SSOConfig(enabled=True, issuer=_ISSUER, client_id=_CLIENT_ID)
    client = OIDCClient(config, transport=_Transport(id_token="placeholder"))
    verifier, _ = client.new_pkce()

    async def exchange():
        await client.exchange_code(
            code="code", code_verifier=verifier, nonce="n",
            redirect_uri="http://127.0.0.1:9999/callback",
            expected_state="a", returned_state="b")

    with pytest.raises(AuthError, match="state"):
        asyncio.run(exchange())


def test_oidc_rejects_id_token_without_exp():
    config = SSOConfig(enabled=True, issuer=_ISSUER, client_id=_CLIENT_ID)
    client = OIDCClient(config, transport=_Transport())

    with pytest.raises(AuthError, match="缺少 exp"):
        asyncio.run(client.verify_id_token(
            _signed_id_token(nonce="nonce", include_exp=False),
            nonce="nonce",
        ))


def test_sso_required_without_token_fails():
    config = SSOConfig(
        enabled=True, required=True, issuer=_ISSUER, client_id=_CLIENT_ID,
        token_dir=Path("/tmp/nonexistent-krelcode-sso-test"),
    )
    client = OIDCClient(config, transport=_Transport())
    with pytest.raises(AuthError):
        asyncio.run(client.require_authenticated())


def test_enterprise_status_and_audit_cli_do_not_expose_tokens(tmp_path, monkeypatch, capsys):
    from krelcode.entrypoints.main import main

    managed = tmp_path / "managed.toml"
    managed.write_text(
        '[enterprise]\nenabled=true\norganization="cli-org"\n'
        'max_permission_mode="default"\n'
        '[enterprise.audit]\nenabled=true\nrequired=true\n'
        f'path="{tmp_path / "audit.jsonl"}"\n',
        encoding="utf-8")
    monkeypatch.setenv("KRELCODE_MANAGED_CONFIG", str(managed))
    assert main(["enterprise", "status", "--workspace", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "cli-org" in out and "audit.jsonl" in out

    audit = ComplianceAuditor(
        tmp_path / "audit.jsonl", organization="cli-org", enabled=True,
        required=True)
    audit.initialize()
    audit.record("event", {"token": "never-print-this"})
    assert main([
        "audit", "verify", str(tmp_path / "audit.jsonl"),
        "--workspace", str(tmp_path),
    ]) == 0
    text = capsys.readouterr().out
    assert "valid: yes" in text
    assert "never-print-this" not in text
