"""Skill, plugin, custom-command and hook composition."""

from __future__ import annotations

from ._deps import *  # noqa: F401,F403


def configured_skill_paths(
    args: argparse.Namespace, config: Dict[str, Any],
) -> list[Any]:
    """Return explicit Skill roots ordered below plugin/CLI overrides."""
    skill_config = config.get("skills") or {}
    configured = skill_config.get("paths") or []
    if isinstance(configured, str):
        configured = [configured]
    cli_paths = getattr(args, "skills_dir", None) or []
    return list(configured) + list(cli_paths)


def build_skill_manager(
    args: argparse.Namespace,
    config: Dict[str, Any],
    *,
    plugin_paths: Optional[List[Any]] = None,
) -> SkillManager:
    """Build SkillManager with plugin roots before explicit CLI/project roots."""
    skill_config = config.get("skills") or {}
    enabled = (not getattr(args, "no_skills", False)
               and bool(skill_config.get("enabled", True)))
    explicit_paths = configured_skill_paths(args, config)
    return SkillManager(
        Path(args.workspace).resolve(),
        builtin_paths=(BUILTIN_SKILL_ROOT,),
        extra_paths=list(plugin_paths or ()) + explicit_paths,
        enabled=enabled,
    )


def build_plugin_manager(
    args: argparse.Namespace,
    config: Dict[str, Any],
    workspace_root: Path,
    *,
    enterprise_policy: Optional[EnterprisePolicy] = None,
) -> PluginManager:
    """按配置/CLI 构建目录插件管理器；插件代码仅从显式目录加载。"""
    section = config.get("plugins") or {}
    if not isinstance(section, dict):
        section = {}
    configured = section.get("paths") or []
    if isinstance(configured, str):
        configured = [configured]
    cli_paths = getattr(args, "plugins_dir", None) or []
    enabled = (
        not getattr(args, "no_plugins", False)
        and bool(section.get("enabled", True))
        and not (enterprise_policy and enterprise_policy.disable_plugins)
    )
    trust_section = section.get("trust")
    if trust_section is not None and not isinstance(trust_section, dict):
        trust_section = None
    trust_policy = PluginTrustPolicy.from_config(
        section, trust_section=trust_section)
    trust_store_path = section.get("trust_store")
    if trust_store_path:
        configured_store = Path(str(trust_store_path)).expanduser()
        if not configured_store.is_absolute():
            configured_store = workspace_root / configured_store
    else:
        configured_store = None
    return PluginManager(
        workspace_root,
        extra_paths=list(configured) + list(cli_paths),
        enabled=enabled,
        trust_policy=trust_policy,
        trust_store_path=configured_store,
    )


def build_custom_command_manager(
    config: Dict[str, Any], workspace_root: Path,
) -> CustomCommandManager:
    section = config.get("commands") or {}
    if not isinstance(section, dict):
        section = {}
    configured = section.get("paths") or []
    if isinstance(configured, str):
        configured = [configured]
    return CustomCommandManager(workspace_root, extra_paths=configured)


def compose_hook_specs(
    config: Dict[str, Any],
    plugin_hooks: tuple[Dict[str, Any], ...],
) -> List[HookSpec]:
    """Merge layered config hooks with plugin-provided hook declarations."""
    raw_hooks = config.get("hooks") or []
    if not isinstance(raw_hooks, list):
        raw_hooks = []
    return parse_hooks({
        "hooks": [*raw_hooks, *plugin_hooks],
    })

