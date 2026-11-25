"""Validated, frozen-at-run-start ablation knobs.

Experiment profiles are intentionally not a generic mutation API.  Only named
safe knobs can be changed, and security ceilings (sandbox, approval, network,
credentials) are absent from the registry.  The Web UI writes the profile file;
the next Agent process validates and freezes it before composition.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, MutableMapping, Sequence

from .journal import atomic_write_json
from .models import stable_hash, utc_timestamp


class ExperimentError(ValueError):
    """Invalid profile or unsafe/unknown experiment knob."""


@dataclass(frozen=True)
class ExperimentKnob:
    id: str
    label: str
    group: str
    kind: str
    description: str
    default: object
    options: tuple[str, ...] = ()
    risk: str = "low"
    restart_required: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "group": self.group,
            "kind": self.kind,
            "description": self.description,
            "default": self.default,
            "options": list(self.options),
            "risk": self.risk,
            "restart_required": self.restart_required,
        }


KNOBS: tuple[ExperimentKnob, ...] = (
    ExperimentKnob(
        "context.enabled",
        "Context 装配",
        "Context",
        "boolean",
        "启用分层规则、工作上下文和 Context v3 装配。",
        True,
    ),
    ExperimentKnob(
        "context.compression",
        "自动 Compact",
        "Context",
        "boolean",
        "启用上下文微压缩与结构化摘要。",
        True,
    ),
    ExperimentKnob(
        "context.directory_rules",
        "目录级规则",
        "Context",
        "boolean",
        "允许按目录加载 AGENTS / KRELCODE 规则。",
        True,
    ),
    ExperimentKnob(
        "memory.mode",
        "Auto Memory",
        "Memory",
        "select",
        "控制稳定项目知识的候选、建议和自动写入行为。",
        "suggest",
        options=("off", "suggest", "auto"),
    ),
    ExperimentKnob(
        "prompt.authoring",
        "Prompt Authoring",
        "Prompt",
        "boolean",
        "启用可选 Prompt 编写契约与 Prompt Lab 工具。",
        False,
        risk="medium",
    ),
    ExperimentKnob(
        "mcp.enabled",
        "MCP",
        "Tools",
        "boolean",
        "启动并暴露配置的 MCP 工具与 Prompt。",
        True,
    ),
    ExperimentKnob(
        "tools.disabled",
        "禁用工具",
        "Tools",
        "multi_select",
        "从动态工具池中移除指定工具。",
        (),
        options=(
            "run_command", "write_file", "edit_file", "grep", "glob",
            "repo_map", "web_search", "web_fetch", "browser",
        ),
    ),
    ExperimentKnob(
        "features.enabled",
        "Feature Flags",
        "Runtime",
        "multi_select",
        "启用运行时功能开关；未知名称会被保留给扩展。",
        (),
        options=(),
    ),
)

KNOB_BY_ID = MappingProxyType({knob.id: knob for knob in KNOBS})


@dataclass(frozen=True)
class ExperimentProfile:
    id: str
    name: str
    description: str
    knobs: Mapping[str, object] = field(default_factory=dict)
    schema_version: int = 1
    updated_at: float = field(default_factory=utc_timestamp)

    def __post_init__(self) -> None:
        object.__setattr__(self, "knobs", MappingProxyType(dict(self.knobs)))

    @property
    def profile_hash(self) -> str:
        return stable_hash({
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "knobs": dict(self.knobs),
        })

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "knobs": dict(self.knobs),
            "updated_at": self.updated_at,
            "profile_hash": self.profile_hash,
        }


@dataclass(frozen=True)
class AppliedExperiment:
    profile: ExperimentProfile
    warnings: tuple[str, ...] = ()

    @property
    def profile_hash(self) -> str:
        return self.profile.profile_hash


def _safe_profile_id(value: object) -> str:
    text = str(value or "").strip()
    filtered = "".join(
        char for char in text if char.isalnum() or char in {"-", "_"})
    return filtered[:80] or f"profile-{uuid.uuid4().hex[:12]}"


def _string_list(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    values = (value,) if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)):
        raise ExperimentError(f"{field_name} 必须是字符串或字符串数组")
    output: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise ExperimentError(f"{field_name} 只能包含非空字符串")
        normalized = item.strip()
        if normalized not in output:
            output.append(normalized)
    return tuple(output)


def validate_knobs(raw: Mapping[str, object]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, value in raw.items():
        knob_id = str(key).strip()
        knob = KNOB_BY_ID.get(knob_id)
        if knob is None:
            raise ExperimentError(f"不允许的实验开关: {knob_id}")
        if knob.kind == "boolean":
            if not isinstance(value, bool):
                raise ExperimentError(f"{knob_id} 必须是布尔值")
            normalized[knob_id] = value
        elif knob.kind == "select":
            selected = str(value or "").strip()
            if selected not in knob.options:
                raise ExperimentError(
                    f"{knob_id} 必须是 {', '.join(knob.options)} 之一")
            normalized[knob_id] = selected
        elif knob.kind == "multi_select":
            normalized[knob_id] = list(
                _string_list(value, knob_id))
        else:
            raise ExperimentError(f"未知开关类型: {knob.kind}")
    return normalized


def profile_from_mapping(raw: Mapping[str, object]) -> ExperimentProfile:
    raw_version = raw.get("schema_version") or 1
    if isinstance(raw_version, bool) or not isinstance(raw_version, (int, str)):
        raise ExperimentError("schema_version 必须是整数")
    try:
        version = int(raw_version)
    except ValueError as exc:
        raise ExperimentError("schema_version 必须是整数") from exc
    if version != 1:
        raise ExperimentError(f"不支持的实验 profile 版本: {version}")
    knobs_raw = raw.get("knobs") or {}
    if not isinstance(knobs_raw, Mapping):
        raise ExperimentError("knobs 必须是 object")
    profile_id = _safe_profile_id(raw.get("id"))
    name = str(raw.get("name") or "Untitled experiment").strip()[:120]
    description = str(raw.get("description") or "").strip()[:1_000]
    updated = raw.get("updated_at")
    updated_at = float(updated) if isinstance(updated, (int, float)) else utc_timestamp()
    return ExperimentProfile(
        id=profile_id,
        name=name,
        description=description,
        knobs=validate_knobs({
            str(key): value for key, value in knobs_raw.items()}),
        updated_at=updated_at,
    )


def load_profile(path: Path) -> ExperimentProfile:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ExperimentError(f"实验 profile 不存在: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentError(f"实验 profile 读取失败: {path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ExperimentError("实验 profile 顶层必须是 object")
    return profile_from_mapping({str(key): value for key, value in raw.items()})


def save_profile(path: Path, profile: ExperimentProfile) -> None:
    atomic_write_json(Path(path), profile.to_dict())


def profile_warnings(profile: ExperimentProfile) -> tuple[str, ...]:
    warnings: list[str] = []
    if profile.knobs.get("context.enabled") is False and (
        profile.knobs.get("context.compression") is True
        or profile.knobs.get("context.directory_rules") is True
    ):
        warnings.append("Context 已关闭；compression/directory_rules 在本轮不会生效。")
    if profile.knobs.get("memory.mode") == "auto":
        warnings.append("Auto Memory 为 auto；建议同时记录 Memory 事件用于审计。")
    return tuple(warnings)


def _set_config(config: MutableMapping[str, Any], key: str, value: object) -> None:
    config[key] = copy.deepcopy(value)


def apply_profile(
    profile: ExperimentProfile,
    args: argparse.Namespace,
    config: MutableMapping[str, Any],
) -> AppliedExperiment:
    """Apply validated values to one build-local args/config copy."""

    knobs = profile.knobs
    if "context.enabled" in knobs:
        args.no_context = not bool(knobs["context.enabled"])
    if "context.compression" in knobs:
        args.no_compress = not bool(knobs["context.compression"])
    if "context.directory_rules" in knobs:
        section = config.get("context")
        context_section = dict(section) if isinstance(section, Mapping) else {}
        context_section["include_directory_rules"] = bool(
            knobs["context.directory_rules"])
        _set_config(config, "context", context_section)
    if "memory.mode" in knobs:
        args.memory_mode = str(knobs["memory.mode"])
    if "prompt.authoring" in knobs:
        section = config.get("prompt")
        prompt_section = dict(section) if isinstance(section, Mapping) else {}
        prompt_section["authoring"] = {"enabled": bool(
            knobs["prompt.authoring"])}
        _set_config(config, "prompt", prompt_section)
    if "mcp.enabled" in knobs:
        args.no_mcp = not bool(knobs["mcp.enabled"])
    if "tools.disabled" in knobs:
        section = config.get("tools")
        tools_section = dict(section) if isinstance(section, Mapping) else {}
        tools_section["disabled"] = list(
            _string_list(knobs["tools.disabled"], "tools.disabled"))
        _set_config(config, "tools", tools_section)
    if "features.enabled" in knobs:
        section = config.get("features")
        feature_section = dict(section) if isinstance(section, Mapping) else {}
        feature_section["enabled"] = list(
            _string_list(knobs["features.enabled"], "features.enabled"))
        _set_config(config, "features", feature_section)
    return AppliedExperiment(profile=profile, warnings=profile_warnings(profile))


def configured_profile_path(
    workspace: Path,
    config: Mapping[str, object],
) -> Path | None:
    section = config.get("observability")
    raw: object = None
    if isinstance(section, Mapping):
        raw = section.get("experiment_profile")
    if raw is False or raw == "":
        return None
    if raw is None:
        default = Path(workspace) / ".krelcode" / "observability" / "active-experiment.json"
        return default if default.is_file() else None
    path = Path(str(raw)).expanduser()
    return path if path.is_absolute() else Path(workspace) / path


def load_and_apply_profile(
    args: argparse.Namespace,
    config: MutableMapping[str, Any],
    *,
    workspace: Path,
) -> AppliedExperiment | None:
    explicit = getattr(args, "experiment_profile", None)
    selected_path: Path | None
    if explicit:
        selected_path = Path(str(explicit)).expanduser()
    else:
        selected_path = configured_profile_path(workspace, config)
        if selected_path is None or not selected_path.is_file():
            return None
    profile = load_profile(selected_path)
    return apply_profile(profile, args, config)


def default_profile_path(workspace: Path) -> Path:
    return Path(workspace) / ".krelcode" / "observability" / "active-experiment.json"


def empty_profile() -> ExperimentProfile:
    return ExperimentProfile(
        id=f"profile-{uuid.uuid4().hex[:12]}",
        name="Baseline",
        description="All experimental capabilities use their configured baseline.",
        knobs={knob.id: knob.default for knob in KNOBS},
        updated_at=utc_timestamp(),
    )


__all__ = [
    "AppliedExperiment",
    "ExperimentError",
    "ExperimentKnob",
    "ExperimentProfile",
    "KNOBS",
    "KNOB_BY_ID",
    "apply_profile",
    "configured_profile_path",
    "default_profile_path",
    "empty_profile",
    "load_and_apply_profile",
    "load_profile",
    "profile_from_mapping",
    "profile_warnings",
    "save_profile",
    "validate_knobs",
]
