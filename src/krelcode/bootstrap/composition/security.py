"""Permission policy and execution sandbox composition."""

from __future__ import annotations

from ._deps import *  # noqa: F401,F403


def build_policy(
    args: argparse.Namespace,
    workspace_root: Path,
    bus: EventBus,
    *,
    approver=None,
    config: Optional[Dict[str, Any]] = None,
    enterprise_policy: Optional[EnterprisePolicy] = None,
):
    """Build approval policy while preserving the managed security ceiling."""
    if enterprise_policy is None:
        # A direct embedder must not be able to convert project/user TOML
        # into enterprise authority.  The managed layer is the only source.
        managed = load_enterprise_policy(
            load_managed_config(workspace=workspace_root))
    else:
        managed = enterprise_policy
    managed.validate_cli(args)
    if args.no_sandbox:
        base = AllowAllPolicy()
        return EnterpriseGuardPolicy(base, managed) if managed.enabled else base
    ui_section = (config or {}).get("ui") or {}
    configured_review = (
        bool(ui_section.get("diff_review", True))
        if isinstance(ui_section, dict) else True
    )
    review_writes = bool(
        configured_review
        and not getattr(args, "no_diff_review", False)
        and not getattr(args, "auto_approve", False)
        and approver is not None
        and resolve_run_mode(args) is RunMode.REPL
    )
    base = SandboxPolicy(
        approver=approver,
        rules_path=workspace_root / ".krelcode" / "allow_rules.json",
        bus=bus,
        auto_approve=bool(
            getattr(args, "auto_approve", False)
            and (not managed.enabled or managed.allow_auto_approve)),
        workspace_root=workspace_root,
        review_writes=review_writes,
        managed_policy=managed if managed.enabled else None,
    )
    return EnterpriseGuardPolicy(base, managed) if managed.enabled else base


def resolve_permission_mode(
    args: argparse.Namespace,
    config: Dict[str, Any],
    enterprise_policy: Optional[EnterprisePolicy] = None,
) -> PermissionMode:
    """Resolve CLI/config mode and clamp it to the managed ceiling."""
    explicit = getattr(args, "permission_mode", None)
    if explicit:
        selected = PermissionMode(explicit)
    else:
        section = config.get("permissions") or {}
        configured = section.get("mode") if isinstance(section, dict) else None
        if configured:
            selected = PermissionMode(str(configured))
        elif getattr(args, "auto_approve", False):
            selected = PermissionMode.AUTO_APPROVE
        else:
            selected = PermissionMode.DEFAULT
    managed = enterprise_policy or load_enterprise_policy(config)
    return effective_permission_mode(selected, managed)


def resolve_run_mode(args: argparse.Namespace) -> RunMode:
    if getattr(args, "input_format", "text") == "stream-json":
        return RunMode.JSON
    if getattr(args, "message", None):
        if getattr(args, "output_format", "text") in {"json", "stream-json"}:
            return RunMode.JSON
        return RunMode.PRINT
    return RunMode.REPL


def _configured_features(config: Dict[str, Any]) -> frozenset[str]:
    raw = config.get("features") or {}
    if isinstance(raw, dict):
        enabled = raw.get("enabled", raw)
        if isinstance(enabled, dict):
            return frozenset(str(k) for k, v in enabled.items() if bool(v))
        raw = enabled
    if isinstance(raw, str):
        raw = [raw]
    return frozenset(str(item) for item in raw or ())


def _disabled_tools(config: Dict[str, Any]) -> frozenset[str]:
    section = config.get("tools") or {}
    raw = section.get("disabled", ()) if isinstance(section, dict) else ()
    if isinstance(raw, str):
        raw = [raw]
    return frozenset(str(item) for item in raw or ())


def build_execution_sandbox(
    args: argparse.Namespace, config: Dict[str, Any], workspace_root: Path,
) -> Optional[ExecutionSandbox]:
    """显式 CLI > TOML [sandbox] > auto；--no-sandbox 同时关闭运行时隔离。"""
    if getattr(args, "no_sandbox", False):
        return None
    section = config.get("sandbox") or {}
    if not isinstance(section, dict):
        section = {}
    backend = getattr(args, "sandbox_backend", None) or section.get("backend", "auto")
    network_arg = getattr(args, "sandbox_network", None)
    network = bool(section.get("network", False)) if network_arg is None else bool(network_arg)
    return ExecutionSandbox(
        workspace_root, backend=str(backend), network=network)

