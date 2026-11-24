"""Prompt/Context Fragment Registry。

Registry 统一登记带来源、作用域、层级、稳定性和信任级别的 fragment，并负责
手动启停与兼容快照。请求级最终排序和消息生成由 PromptAssembler 独占；
快照仍暴露稳定前缀结束位置，供旧路径和观测逻辑兼容使用。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, cast

from ..core.messages import Message
from .context import ContextSection, WorkspaceContext
from .evidence import SourceRole, instruction_source_for
from .prompts import (
    BASE_SYSTEM_PROMPT,
    ModelPromptProfile,
    PromptProfileResolution,
    PROMPT_BASE_SECTIONS,
    PROMPT_PROFILE_AUTO,
    base_prompt_hash,
    resolve_prompt_profile,
)
from .rules import RuleLayer, RuleScope
from .tokens import estimate_text_tokens

if TYPE_CHECKING: from .prompt_snapshot_ports import PromptSnapshotHostPort, RegistryT

GROUP_SYSTEM = "system"
GROUP_WORKSPACE = "workspace_context"
GROUP_RUNTIME = "runtime"
GROUP_ORDER = (GROUP_SYSTEM, GROUP_WORKSPACE, GROUP_RUNTIME)

STABILITY_STATIC = "static"
STABILITY_SESSION = "session"
STABILITY_TURN = "turn"
PROMPT_STABILITY_LEVELS = (
    STABILITY_STATIC,
    STABILITY_SESSION,
    STABILITY_TURN,
)
STABILITY_ORDER = {
    STABILITY_STATIC: 0,
    STABILITY_SESSION: 1,
    STABILITY_TURN: 2,
}

TRUST_MANAGED = "managed"
TRUST_USER = "user"
TRUST_PROJECT = "project"
TRUST_DATA = "data"
TRUST_UNTRUSTED = "untrusted"
PROMPT_TRUST_LEVELS = (
    TRUST_MANAGED,
    TRUST_USER,
    TRUST_PROJECT,
    TRUST_DATA,
    TRUST_UNTRUSTED,
)

LAYER_L0_MANAGED_POLICY = "L0_MANAGED_POLICY"
LAYER_L1_BASE_AGENT = "L1_BASE_AGENT_PROMPT"
LAYER_L2_USER_GLOBAL = "L2_USER_GLOBAL_INSTRUCTIONS"
LAYER_L3_PROJECT_RULES = "L3_PROJECT_DIRECTORY_RULES"
LAYER_L4_AGENT_STYLE = "L4_AGENT_PERSONA_OUTPUT_STYLE"
LAYER_L5_MODE_CONTRACT = "L5_MODE_PERMISSION_TOOL_CONTRACT"
LAYER_L6_CATALOG = "L6_SKILL_AGENT_MCP_CATALOG"
LAYER_L7_MEMORY = "L7_MEMORY_EVIDENCE"
# Semantic alias: keep the persisted string stable for checkpoint compatibility,
# while new code names the layer by its actual authority class.
LAYER_L7_EVIDENCE = LAYER_L7_MEMORY
LAYER_L8_ENVIRONMENT = "L8_ENVIRONMENT_DATE_SESSION"
LAYER_L9_WORKING = "L9_PLAN_WORKING_CONTEXT"
LAYER_L10_HISTORY = "L10_RECENT_RETRIEVED_HISTORY"
LAYER_L11_USER_REQUEST = "L11_CURRENT_USER_REQUEST"
LAYER_L12_REMINDER = "L12_PER_TURN_REMINDER"
PROMPT_LAYERS = (
    LAYER_L0_MANAGED_POLICY,
    LAYER_L1_BASE_AGENT,
    LAYER_L2_USER_GLOBAL,
    LAYER_L3_PROJECT_RULES,
    LAYER_L4_AGENT_STYLE,
    LAYER_L5_MODE_CONTRACT,
    LAYER_L6_CATALOG,
    LAYER_L7_MEMORY,
    LAYER_L8_ENVIRONMENT,
    LAYER_L9_WORKING,
    LAYER_L10_HISTORY,
    LAYER_L11_USER_REQUEST,
    LAYER_L12_REMINDER,
)
LAYER_ORDER = {layer: index for index, layer in enumerate(PROMPT_LAYERS)}
SYSTEM_PROMPT_LAYERS = frozenset(PROMPT_LAYERS[:10])
TURN_ONLY_LAYERS = frozenset((
    LAYER_L10_HISTORY,
    LAYER_L11_USER_REQUEST,
    LAYER_L12_REMINDER,
))

WORKSPACE_OPEN = "<workspace_context>"
WORKSPACE_CLOSE = "</workspace_context>"


@dataclass(frozen=True)
class PromptFragment:
    id: str
    source: str
    title: str
    content: str
    group: str = GROUP_SYSTEM
    priority: int = 50
    cacheable: bool = False
    compactable: bool = True
    toggleable: bool = True
    metadata: Mapping[str, object] = field(default_factory=dict)
    scope: str = "core"
    layer: str = LAYER_L1_BASE_AGENT
    stability: str = ""
    trust: str = TRUST_MANAGED

    def __post_init__(self) -> None:
        if not self.stability:
            object.__setattr__(
                self, "stability",
                STABILITY_STATIC if self.cacheable else STABILITY_SESSION,
            )
        if self.stability == STABILITY_TURN and self.cacheable:
            raise ValueError(
                f"turn fragment 不允许进入稳定缓存前缀: {self.id}")
        if self.trust == TRUST_UNTRUSTED and self.cacheable:
            raise ValueError(
                f"untrusted fragment 不允许 cacheable: {self.id}")

    @property
    def tokens(self) -> int:
        return estimate_text_tokens(self.content)


@dataclass(frozen=True)
class PromptCacheBoundary:
    offset: int
    stable_tokens: int
    stable_fragments: Tuple[str, ...]
    volatile_fragments: Tuple[str, ...]
    stable_hash: str
    full_hash: str
    dynamic_hash: str = ""

    @property
    def enabled(self) -> bool:
        return self.offset > 0


@dataclass(frozen=True)
class PromptSnapshot:
    text: str
    fragments: Tuple[PromptFragment, ...]
    cache: PromptCacheBoundary
    generation: int
    total_tokens: int

    @property
    def cache_boundary(self) -> int:
        return self.cache.offset

    @property
    def stable_prefix(self) -> str:
        return self.text[:self.cache.offset]

    def provider_meta(self) -> Optional[Dict[str, object]]:
        if self.cache.offset <= 0:
            return None
        return {"prompt_cache_boundaries": [self.cache.offset]}

    def system_message(self) -> Message:
        return Message(
            role="system",
            content=self.text,
            provider_meta=self.provider_meta(),
        )

    def summary_lines(self) -> List[str]:
        enabled = len(self.fragments)
        cacheable = len(self.cache.stable_fragments)
        volatile = len(self.cache.volatile_fragments)
        return [
            f"system prompt {self.total_tokens} tokens · fragment {enabled} 个"
            f"（稳定 {cacheable} / 动态 {volatile}）",
            f"cache boundary: {self.cache.offset} chars"
            f" · stable hash {self.cache.stable_hash[:12]}"
            if self.cache.enabled else "cache boundary: 未启用",
        ]


@dataclass(frozen=True)
class FragmentSourceSummary:
    source: str
    total: int
    enabled: int
    tokens: int
    cacheable: int
    fragment_ids: Tuple[str, ...]


def _validate_fragment(fragment: PromptFragment) -> None:
    if not fragment.id or not fragment.source:
        raise ValueError("fragment 必须包含非空 id/source")
    if fragment.group not in GROUP_ORDER:
        raise ValueError(f"未知 fragment group: {fragment.group}")
    if not fragment.scope:
        raise ValueError(f"fragment 必须包含非空 scope: {fragment.id}")
    if fragment.layer not in LAYER_ORDER:
        raise ValueError(f"未知 fragment layer: {fragment.layer}")
    if fragment.stability not in PROMPT_STABILITY_LEVELS:
        raise ValueError(f"未知 fragment stability: {fragment.stability}")
    if fragment.trust not in PROMPT_TRUST_LEVELS:
        raise ValueError(f"未知 fragment trust: {fragment.trust}")
    if fragment.layer in TURN_ONLY_LAYERS and fragment.stability != STABILITY_TURN:
        raise ValueError(
            f"{fragment.layer} 只能承载 turn fragment: {fragment.id}")
    if (
        fragment.layer in {
            LAYER_L0_MANAGED_POLICY,
            LAYER_L1_BASE_AGENT,
            LAYER_L2_USER_GLOBAL,
            LAYER_L3_PROJECT_RULES,
            LAYER_L4_AGENT_STYLE,
            LAYER_L5_MODE_CONTRACT,
            LAYER_L6_CATALOG,
        }
        and fragment.stability == STABILITY_TURN
    ):
        raise ValueError(
            f"turn fragment 不能进入稳定/会话层 {fragment.layer}: {fragment.id}")


class PromptFragmentRegistry:
    def __init__(self, fragments: Iterable[PromptFragment] = ()) -> None:
        self._fragments: Dict[str, PromptFragment] = {}
        self._id_overrides: Dict[str, bool] = {}
        self._source_overrides: Dict[str, bool] = {}
        self._generation = 0
        self._snapshot: Optional[PromptSnapshot] = None
        for fragment in fragments:
            self.register(fragment)

    @property
    def generation(self) -> int:
        return self._generation

    def register(self, fragment: PromptFragment) -> PromptFragment:
        _validate_fragment(fragment)
        if fragment.id in self._fragments:
            raise ValueError(f"fragment 已存在: {fragment.id}")
        self._fragments[fragment.id] = fragment
        self._invalidate()
        return fragment

    def upsert(self, fragment: PromptFragment) -> PromptFragment:
        _validate_fragment(fragment)
        existing = self._fragments.get(fragment.id)
        if existing is not None and existing.toggleable != fragment.toggleable:
            raise ValueError(f"不能动态改变 fragment 的 toggleable 属性: {fragment.id}")
        self._fragments[fragment.id] = fragment
        self._invalidate()
        return fragment

    def remove(self, fragment_id: str) -> bool:
        existed = self._fragments.pop(fragment_id, None) is not None
        if existed:
            self._invalidate()
        return existed

    def replace_group(
        self,
        group: str,
        fragments: Sequence[PromptFragment],
    ) -> None:
        if group not in GROUP_ORDER:
            raise ValueError(f"未知 fragment group: {group}")
        for fragment in fragments:
            _validate_fragment(fragment)
            if fragment.group != group:
                raise ValueError("replace_group 收到其他 group 的 fragment")
        ids = {fragment.id for fragment in fragments}
        if len(ids) != len(fragments):
            raise ValueError("replace_group 包含重复 fragment id")
        retained = {
            fragment_id: fragment
            for fragment_id, fragment in self._fragments.items()
            if fragment.group != group
        }
        for fragment in fragments:
            existing = retained.get(fragment.id)
            if existing is not None:
                raise ValueError(f"fragment 已存在: {fragment.id}")
            retained[fragment.id] = fragment
        self._fragments = retained
        self._invalidate()

    def replace_source(self, source: str, fragments: Sequence[PromptFragment]) -> None:
        for fragment in fragments:
            _validate_fragment(fragment)
            if fragment.source != source:
                raise ValueError("replace_source 收到其他 source 的 fragment")
        ids = {fragment.id for fragment in fragments}
        if len(ids) != len(fragments):
            raise ValueError("replace_source 包含重复 fragment id")
        retained = {
            fragment_id: fragment
            for fragment_id, fragment in self._fragments.items()
            if fragment.source != source
        }
        for fragment in fragments:
            if fragment.id in retained:
                raise ValueError(f"fragment 已存在: {fragment.id}")
            retained[fragment.id] = fragment
        self._fragments = retained
        self._invalidate()

    def get(self, fragment_id: str) -> Optional[PromptFragment]:
        return self._fragments.get(fragment_id)

    def list(
        self,
        *,
        source: str = "",
        include_disabled: bool = True,
    ) -> Tuple[PromptFragment, ...]:
        fragments = sorted(self._fragments.values(), key=_fragment_sort_key)
        if source:
            fragments = [f for f in fragments if f.source == source]
        if not include_disabled:
            fragments = [f for f in fragments if self.is_enabled(f)]
        return tuple(fragments)

    def is_enabled(self, fragment: PromptFragment | str) -> bool:
        item = self.get(fragment) if isinstance(fragment, str) else fragment
        if item is None:
            return False
        if item.id in self._id_overrides:
            return self._id_overrides[item.id]
        if item.source in self._source_overrides:
            return self._source_overrides[item.source]
        return True

    def set_enabled(self, target: str, enabled: bool) -> Tuple[str, int]:
        """按精确 id 或 source 启停；精确 id 覆盖 source 级设置。"""
        if target in self._fragments:
            fragment = self._fragments[target]
            if not fragment.toggleable:
                raise ValueError(f"fragment 不可手动启停: {target}")
            self._id_overrides[target] = enabled
            self._invalidate()
            return ("fragment", 1)
        matched = [f for f in self._fragments.values() if f.source == target]
        if not matched:
            raise KeyError(f"未知 fragment/source: {target}")
        if any(not f.toggleable for f in matched):
            raise ValueError(f"source 包含不可手动启停的 fragment: {target}")
        self._source_overrides[target] = enabled
        self._invalidate()
        return ("source", len(matched))

    def toggle(self, target: str) -> Tuple[str, int, bool]:
        if target in self._fragments:
            fragment = self._fragments[target]
            enabled = not self.is_enabled(fragment)
        else:
            matched = [f for f in self._fragments.values() if f.source == target]
            if not matched:
                raise KeyError(f"未知 fragment/source: {target}")
            enabled = not all(self.is_enabled(f) for f in matched)
        kind, count = self.set_enabled(target, enabled)
        return kind, count, enabled

    def reset_overrides(self) -> None:
        self._id_overrides.clear()
        self._source_overrides.clear()
        self._invalidate()

    def source_summaries(self) -> Tuple[FragmentSourceSummary, ...]:
        sources = sorted({fragment.source for fragment in self._fragments.values()})
        summaries = []
        for source in sources:
            fragments = [f for f in self.list() if f.source == source]
            enabled = [f for f in fragments if self.is_enabled(f)]
            summaries.append(FragmentSourceSummary(
                source=source,
                total=len(fragments),
                enabled=len(enabled),
                tokens=sum(f.tokens for f in enabled),
                cacheable=sum(1 for f in enabled if f.cacheable),
                fragment_ids=tuple(f.id for f in fragments),
            ))
        return tuple(summaries)

    def snapshot(self) -> PromptSnapshot:
        if self._snapshot is not None:
            return self._snapshot
        enabled = [f for f in self.list() if self.is_enabled(f)]
        text, boundary = _render_fragments(enabled)
        stable_list = []
        for fragment in enabled:
            if not fragment.cacheable:
                break
            stable_list.append(fragment)
        stable = tuple(f.id for f in stable_list)
        stable_ids = set(stable)
        volatile = tuple(f.id for f in enabled if f.id not in stable_ids)
        stable_text = text[:boundary]
        snapshot = PromptSnapshot(
            text=text,
            fragments=tuple(enabled),
            cache=PromptCacheBoundary(
                offset=boundary,
                stable_tokens=estimate_text_tokens(stable_text),
                stable_fragments=stable,
                volatile_fragments=volatile,
                stable_hash=hashlib.sha256(
                    stable_text.encode("utf-8")).hexdigest(),
                full_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                dynamic_hash=hashlib.sha256(
                    text[boundary:].encode("utf-8")).hexdigest()
                if text[boundary:] else "",
            ),
            generation=self._generation,
            total_tokens=estimate_text_tokens(text),
        )
        self._snapshot = snapshot
        return snapshot

    def _invalidate(self) -> None:
        self._generation += 1
        self._snapshot = None


def _base_fragment_metadata(
    resolution: PromptProfileResolution,
    *,
    section_id: str,
    required: bool,
) -> Dict[str, object]:
    return {
        "kind": "base",
        "prompt_section": section_id,
        "prompt_section_required": required,
        "prompt_profile": resolution.profile.name,
        "prompt_profile_requested": resolution.requested,
        "prompt_profile_match": resolution.matched_by,
        "prompt_provider": resolution.provider,
        "prompt_model": resolution.model,
        "base_prompt_hash": resolution.base_prompt_hash,
        "prompt_release_id": resolution.release_id,
        "prompt_release_hash": resolution.release_hash,
        "prompt_profile_hash": resolution.profile_hash,
        "profile_calibration_id": resolution.calibration_id,
        "profile_calibration_hash": resolution.calibration_hash,
        "profile_calibration_status": resolution.calibration_status,
        "profile_calibration_reason": resolution.calibration_reason,
        "source_role": SourceRole.INSTRUCTION.value,
        "instruction_source": "managed",
        "instruction": True,
    }


def _coerce_base_profile(
    profile: str | ModelPromptProfile | PromptProfileResolution,
    provider: object,
    model: object,
) -> PromptProfileResolution:
    if isinstance(profile, PromptProfileResolution):
        return profile
    if isinstance(profile, ModelPromptProfile):
        provider_name = str(getattr(provider, "name", provider) or "").strip().lower()
        model_name = str(
            model if isinstance(model, str) else getattr(model, "model", "")
        ).strip()
        return PromptProfileResolution(
            profile=profile,
            requested=profile.name,
            matched_by="direct",
            provider=provider_name,
            model=model_name,
            base_prompt_hash=base_prompt_hash(profile),
        )
    return resolve_prompt_profile(
        provider,
        model,
        configured=str(profile or PROMPT_PROFILE_AUTO),
    )


def base_fragment() -> PromptFragment:
    """Compatibility view containing the default production base prompt."""

    resolution = resolve_prompt_profile(configured=PROMPT_PROFILE_AUTO)
    return PromptFragment(
        id="core:base",
        source="core",
        title="Krelcode base system prompt (aggregate compatibility view)",
        content=BASE_SYSTEM_PROMPT,
        group=GROUP_SYSTEM,
        priority=0,
        cacheable=True,
        compactable=False,
        toggleable=False,
        metadata=_base_fragment_metadata(
            resolution, section_id="core:base", required=True),
        scope="managed",
        layer=LAYER_L1_BASE_AGENT,
        stability=STABILITY_STATIC,
        trust=TRUST_MANAGED,
    )


def base_fragments(
    profile: str | ModelPromptProfile | PromptProfileResolution = PROMPT_PROFILE_AUTO,
    *,
    provider: object = "",
    model: object = "",
) -> Tuple[PromptFragment, ...]:
    """Build independently attributable L1 fragments for production assembly."""

    resolution = _coerce_base_profile(profile, provider, model)
    sections = (*PROMPT_BASE_SECTIONS, *resolution.profile.additional_sections)
    fragments: List[PromptFragment] = []
    for section in sections:
        is_profile_section = section in resolution.profile.additional_sections
        fragments.append(PromptFragment(
            id=section.id,
            source=(
                f"model_profile:{resolution.profile.name}"
                if is_profile_section else "core"
            ),
            title=section.title,
            content=section.content,
            group=GROUP_SYSTEM,
            priority=section.priority,
            cacheable=True,
            compactable=False,
            toggleable=False,
            metadata=_base_fragment_metadata(
                resolution,
                section_id=section.id,
                required=False if is_profile_section else section.required,
            ),
            scope="managed",
            layer=LAYER_L1_BASE_AGENT,
            stability=STABILITY_STATIC,
            trust=TRUST_MANAGED,
        ))
    return tuple(fragments)


def replace_base_fragments(
    registry: PromptFragmentRegistry,
    profile: str | ModelPromptProfile | PromptProfileResolution = PROMPT_PROFILE_AUTO,
    *,
    provider: object = "",
    model: object = "",
) -> Tuple[PromptFragment, ...]:
    """Atomically replace the reserved ``core:base*`` namespace."""

    fragments = base_fragments(profile, provider=provider, model=model)
    ids = {fragment.id for fragment in fragments}
    if len(ids) != len(fragments):
        raise ValueError("base prompt 包含重复 fragment id")
    retained = {
        fragment.id: fragment for fragment in registry.list()
        if fragment.id != "core:base" and not fragment.id.startswith("core:base:")
    }
    conflicts = sorted(ids & set(retained))
    if conflicts:
        raise ValueError(
            "base prompt fragment 与保留 fragment 冲突: " + ", ".join(conflicts))
    for fragment in fragments:
        _validate_fragment(fragment)
    # Validation completes before mutation, so a profile refresh cannot leave a
    # half-installed base prompt behind.
    registry._fragments = retained  # noqa: SLF001 - registry owns this reserved swap
    for fragment in fragments:
        registry._fragments[fragment.id] = fragment  # noqa: SLF001
    registry._invalidate()  # noqa: SLF001
    return fragments


def skills_fragment(index: str) -> Optional[PromptFragment]:
    if not index.strip():
        return None
    return PromptFragment(
        id="skills:index",
        source="skills",
        title="Available Skills index",
        content=index.strip(),
        group=GROUP_SYSTEM,
        priority=20,
        cacheable=True,
        compactable=False,
        scope="project",
        layer=LAYER_L6_CATALOG,
        stability=STABILITY_SESSION,
        trust=TRUST_PROJECT,
    )


RULE_FRAGMENT_IDS = {
    RuleScope.MANAGED: "managed:rules",
    RuleScope.USER: "user:rules",
    RuleScope.PROJECT: "project:rules",
    RuleScope.DIRECTORY: "directory:rules",
}
_RULE_PRECEDENCE = {
    RuleScope.MANAGED: 0,
    RuleScope.USER: 10,
    RuleScope.PROJECT: 20,
    RuleScope.DIRECTORY: 30,
}
_RULE_SPECS = {
    RuleScope.MANAGED: (
        LAYER_L0_MANAGED_POLICY,
        STABILITY_STATIC,
        TRUST_MANAGED,
        "managed",
        True,
    ),
    RuleScope.USER: (
        LAYER_L2_USER_GLOBAL,
        STABILITY_SESSION,
        TRUST_USER,
        "user",
        True,
    ),
    RuleScope.PROJECT: (
        LAYER_L3_PROJECT_RULES,
        STABILITY_SESSION,
        TRUST_PROJECT,
        "project",
        True,
    ),
    RuleScope.DIRECTORY: (
        LAYER_L3_PROJECT_RULES,
        STABILITY_SESSION,
        TRUST_PROJECT,
        "project",
        True,
    ),
}


def rule_fragment(layer: RuleLayer) -> Optional[PromptFragment]:
    """Convert one rule authority layer into its canonical fragment."""
    bundle = layer.bundle
    content = bundle.content.strip()
    if not content:
        return None
    layer_name, stability, trust, scope, cacheable = _RULE_SPECS[layer.scope]
    fragment_id = RULE_FRAGMENT_IDS[layer.scope]
    precedence = _RULE_PRECEDENCE[layer.scope]
    lower_layers = tuple(
        RULE_FRAGMENT_IDS[item]
        for item, rank in _RULE_PRECEDENCE.items()
        if rank < precedence and item is not RuleScope.MANAGED
    )
    authority_source = instruction_source_for(
        trust, source=fragment_id, origin=layer.scope.value)
    metadata = {
        "source_role": SourceRole.INSTRUCTION.value,
        "instruction": True,
        "instruction_source": (
            authority_source.value
            if authority_source is not None else "managed"
        ),
        "rule_scope": layer.scope.value,
        "rule_precedence": precedence,
        "rule_overrides": lower_layers,
        "rule_conflict_policy": (
            "同一规则冲突时 managed 约束始终保留；"
            "user → project → directory 中后者更具体并优先"
        ),
        "rule_sources": tuple(
            source.display_path for source in bundle.sources),
        "rule_entry_sources": tuple(
            source.display_path for source in bundle.sources
            if not source.imported),
        "rule_import_count": bundle.import_count,
        "rule_issues": tuple({
            "code": issue.code,
            "path": issue.path,
            "message": issue.message,
        } for issue in bundle.issues),
        "rule_targets": tuple(str(path) for path in bundle.targets),
        "truncated": bundle.truncated,
    }
    return PromptFragment(
        id=fragment_id,
        source=fragment_id,
        title=f"{layer.scope.value.title()} rules",
        content=content,
        group=GROUP_SYSTEM,
        priority=precedence,
        cacheable=cacheable,
        compactable=False,
        toggleable=False,
        metadata=metadata,
        scope=scope,
        layer=layer_name,
        stability=stability,
        trust=trust,
    )


def agent_definition_fragment(
    name: str,
    content: str,
) -> Optional[PromptFragment]:
    """Build the canonical L4 agent-definition instruction fragment."""
    fragment_id = f"agent_definition:{str(name).strip()}"
    body = content.strip()
    if not body or fragment_id == "agent_definition:":
        return None
    return PromptFragment(
        id=fragment_id,
        source=fragment_id,
        title=f"Agent definition {name}",
        content=body,
        group=GROUP_SYSTEM,
        priority=40,
        cacheable=True,
        compactable=False,
        toggleable=False,
        metadata={
            "source_role": SourceRole.INSTRUCTION.value,
            "instruction": True,
            "instruction_source": "agent_definition",
            "rule_scope": "agent_definition",
            "rule_precedence": 40,
            "rule_conflict_policy": (
                "Agent Definition 是当前子代理的角色契约，不覆盖托管安全策略"),
        },
        scope="project",
        layer=LAYER_L4_AGENT_STYLE,
        stability=STABILITY_SESSION,
        trust=TRUST_PROJECT,
    )


def is_rule_fragment(fragment: PromptFragment) -> bool:
    return bool(fragment.metadata.get("rule_scope")) or fragment.id in {
        *RULE_FRAGMENT_IDS.values(), "workspace:rules",
    }


def sync_rule_fragments(
    registry: PromptFragmentRegistry,
    bundle: WorkspaceContext,
) -> tuple[PromptFragment, ...]:
    """Replace only rule fragments while preserving unrelated registry state."""
    for fragment in registry.list():
        if is_rule_fragment(fragment):
            registry.remove(fragment.id)
    fragments = workspace_rule_fragments(bundle)
    for fragment in fragments:
        registry.register(fragment)
    return fragments


def workspace_fragments(bundle: WorkspaceContext) -> Tuple[PromptFragment, ...]:
    """Expand real rule stacks; retain the compatibility section adapter."""
    if bundle.rule_layers:
        rules = workspace_rule_fragments(bundle)
        return rules + tuple(
            _workspace_fragment(section)
            for section in bundle.sections
            if section.name != "rules"
        )
    fragments = []
    for section in bundle.sections:
        fragments.append(_workspace_fragment(section))
    return tuple(fragments)


def workspace_rule_fragments(bundle: WorkspaceContext) -> Tuple[PromptFragment, ...]:
    """Return canonical per-scope rule fragments for main and subagents."""
    if bundle.rule_layers:
        return tuple(
            fragment for layer in bundle.rule_layers
            for fragment in (rule_fragment(layer),)
            if fragment is not None
        )
    # Compatibility for hand-built WorkspaceContext values without a RuleStack.
    return tuple(
        _workspace_fragment(section)
        for section in bundle.sections
        if section.name == "rules"
    )


def _workspace_fragment(section: ContextSection) -> PromptFragment:
    # rules 在一次会话内稳定；git/tree 随工作区变化，必须位于缓存边界之后。
    cacheable = section.name == "rules"
    return PromptFragment(
        id=f"workspace:{section.name}",
        source=f"workspace:{section.name}",
        title=f"Workspace {section.name}",
        content=section.content,
        group=GROUP_WORKSPACE,
        priority=section.priority,
        cacheable=cacheable,
        compactable=True,
        metadata={"original_source": section.source},
        scope="project",
        layer=(LAYER_L3_PROJECT_RULES if cacheable else LAYER_L8_ENVIRONMENT),
        stability=(STABILITY_SESSION if cacheable else STABILITY_TURN),
        trust=(TRUST_PROJECT if cacheable else TRUST_DATA),
    )


def apply_prompt_snapshot(loop: "PromptSnapshotHostPort[PromptSnapshot, RegistryT]", snapshot: PromptSnapshot) -> None:
    """把 registry 快照写回 loop.history 的 system 消息。"""
    loop.prompt_snapshot = snapshot
    loop.skill_base_prompt = snapshot.text
    message = snapshot.system_message()
    if loop.history and loop.history[0].role == "system":
        loop.history[0] = message
    else:
        loop.history.insert(0, message)
def refresh_registry_snapshot(loop: "PromptSnapshotHostPort[PromptSnapshot, RegistryT]") -> Optional[PromptSnapshot]:
    registry = loop.prompt_registry
    if registry is None:
        return None
    snapshot_method = getattr(registry, "snapshot")
    snapshot = cast(PromptSnapshot, snapshot_method())
    apply_prompt_snapshot(loop, snapshot)
    return snapshot
def _render_fragments(fragments: Sequence[PromptFragment]) -> Tuple[str, int]:
    text = ""
    boundary: Optional[int] = None

    def append(content: str, cacheable: bool) -> None:
        nonlocal text, boundary
        if not content:
            return
        separator = "\n\n" if text else ""
        if boundary is None and not cacheable:
            boundary = len(text)
        text += separator + content

    # 先渲染所有 cacheable fragment，再渲染动态 fragment。这样即使调用方注册了
    # 动态 system fragment，也不会阻断后续 workspace stable rules 的缓存前缀。
    for cacheable_bucket in (True, False):
        for group in GROUP_ORDER:
            grouped = [
                fragment for fragment in fragments
                if fragment.group == group
                and fragment.cacheable is cacheable_bucket
            ]
            if not grouped:
                continue
            if group != GROUP_WORKSPACE:
                for fragment in grouped:
                    append(fragment.content, cacheable_bucket)
                continue
            append(WORKSPACE_OPEN, cacheable_bucket)
            for fragment in grouped:
                append(fragment.content, cacheable_bucket)
            append(WORKSPACE_CLOSE, cacheable_bucket)
    if boundary is None:
        boundary = len(text)
    return text, boundary


def _fragment_sort_key(fragment: PromptFragment) -> Tuple[int, int, int, str]:
    try:
        group_index = GROUP_ORDER.index(fragment.group)
    except ValueError:
        group_index = len(GROUP_ORDER)
    # cacheable 内容必须形成全局连续前缀；group/priority 只负责同层排序。
    return 0 if fragment.cacheable else 1, group_index, fragment.priority, fragment.id
