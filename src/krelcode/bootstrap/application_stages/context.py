"""Application build context and shared layered-config composition."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ...agent.loop import AgentLoop
from ...config import load_config_layers, load_managed_config
from ...contracts.service_ports import PrincipalPort
from ...contracts.tool import RunMode
from ...core.errors import ConfigError
from ...enterprise.auth import AuthError, OIDCClient, load_sso_config
from ...enterprise.policy import (
    EnterprisePolicy,
    EnterprisePolicyError,
    PolicyViolation,
    load_enterprise_policy,
)
from ...hooks import HookSpec
from ..composition import (
    compose_hook_specs,
    compose_mcp_config,
    resolve_run_mode,
)


class BuildStageError(RuntimeError):
    """A user-facing bootstrap failure that should terminate with status 2."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class ApplicationBuildContext:
    """Immutable process inputs; hot-reloaded config lives in the composer."""

    args: argparse.Namespace
    note: Callable[[str], None]
    config_path: Optional[Path]
    config: Dict[str, Any]
    enterprise_policy: EnterprisePolicy
    principal: Optional[PrincipalPort]
    sso_client: Optional[OIDCClient]
    run_mode: RunMode

    @property
    def input_format(self) -> str:
        return str(getattr(self.args, "input_format", "text"))


@dataclass
class RuntimeConfigComposer:
    """Compose plugin and layered config without owning either source."""

    loop: AgentLoop
    current_config: Dict[str, Any]

    def update(self, config: Dict[str, Any]) -> None:
        self.current_config = config

    def merged_mcp_config(
        self,
        source: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        active = self.current_config if source is None else source
        return compose_mcp_config(
            active, dict(self.loop.plugin_manager.mcp_servers))

    def merged_hook_specs(
        self,
        source: Optional[Dict[str, Any]] = None,
    ) -> list[HookSpec]:
        active = self.current_config if source is None else source
        return compose_hook_specs(
            active, self.loop.plugin_manager.hook_definitions)


def _validate_stream_json_args(args: argparse.Namespace) -> None:
    input_format = getattr(args, "input_format", "text")
    if input_format == "stream-json":
        if args.message is not None:
            raise BuildStageError(
                "--input-format stream-json 不能与 -m/--message 同时使用")
        if args.output_format != "stream-json":
            raise BuildStageError(
                "--input-format stream-json 需要 --output-format stream-json")
    if (
        args.output_format == "stream-json"
        and args.message is None
        and input_format != "stream-json"
    ):
        raise BuildStageError(
            "--output-format stream-json 需要 -m 或 --input-format stream-json")


def _build_note(args: argparse.Namespace) -> Callable[[str], None]:
    quiet = (
        args.message is not None
        and args.output_format in {"json", "stream-json"}
    ) or getattr(args, "input_format", "text") == "stream-json"
    return lambda msg: print(msg, file=sys.stderr) if quiet else print(msg)


async def build_application_context(
    args: argparse.Namespace,
) -> ApplicationBuildContext:
    """Validate CLI input, load layered policy, and resolve SSO identity."""
    _validate_stream_json_args(args)
    note = _build_note(args)
    config_path = Path(args.config).expanduser() if args.config else None
    try:
        config_bundle = load_config_layers(
            workspace=Path(args.workspace),
            explicit_path=config_path,
        )
    except ConfigError as exc:
        raise BuildStageError(f"配置错误: {exc}") from exc
    if config_path is not None and not config_bundle.sources:
        raise BuildStageError(f"配置错误: 指定的配置文件不存在: {config_path}")

    config = config_bundle.data
    try:
        # ``--config`` is an exclusive user configuration, never an
        # enterprise-policy bypass. Managed policy is therefore loaded
        # independently in that case.
        if config_path is not None:
            managed_config = load_managed_config(
                workspace=Path(args.workspace))
            enterprise_policy = load_enterprise_policy(managed_config)
        else:
            enterprise_policy = load_enterprise_policy(
                config, config_bundle=config_bundle)
        enterprise_policy.validate_cli(args)
        sso_client: Optional[OIDCClient] = None
        principal: Optional[PrincipalPort] = None
        sso_config = load_sso_config(enterprise_policy)
        if sso_config.enabled:
            sso_client = OIDCClient(sso_config)
            status = sso_client.status()
            if enterprise_policy.require_sso:
                principal = await sso_client.require_authenticated()
            elif status.authenticated:
                principal = status.principal
    except (ConfigError, EnterprisePolicyError, PolicyViolation, AuthError) as exc:
        raise BuildStageError(f"企业策略/SSO 错误: {exc}") from exc

    return ApplicationBuildContext(
        args=args,
        note=note,
        config_path=config_path,
        config=config,
        enterprise_policy=enterprise_policy,
        principal=principal,
        sso_client=sso_client,
        run_mode=resolve_run_mode(args),
    )


__all__ = [
    "ApplicationBuildContext",
    "BuildStageError",
    "RuntimeConfigComposer",
    "build_application_context",
]
