"""Prompt/Context Fragment Registry。

Registry 是 system prompt 的唯一组装点：fragment 带来源、作用域、稳定排序、
手动启停和缓存属性。稳定 fragment 永远排在动态 fragment 前面，快照暴露
稳定前缀结束位置，Provider 可据此建立真实 prompt-cache 断点。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..core.messages import Message
from .context import ContextSection, WorkspaceContext
from .prompts import BASE_SYSTEM_PROMPT
from .tokens import estimate_text_tokens

GROUP_SYSTEM = "system"
GROUP_WORKSPACE = "workspace_context"
GROUP_RUNTIME = "runtime"
GROUP_ORDER = (GROUP_SYSTEM, GROUP_WORKSPACE, GROUP_RUNTIME)

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
            ),
            generation=self._generation,
            total_tokens=estimate_text_tokens(text),
        )
        self._snapshot = snapshot
        return snapshot

    def _invalidate(self) -> None:
        self._generation += 1
        self._snapshot = None


def base_fragment() -> PromptFragment:
    return PromptFragment(
        id="core:base",
        source="core",
        title="Krelcode base system prompt",
        content=BASE_SYSTEM_PROMPT,
        group=GROUP_SYSTEM,
        priority=0,
        cacheable=True,
        compactable=False,
        toggleable=False,
    )


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
    )


def workspace_fragments(bundle: WorkspaceContext) -> Tuple[PromptFragment, ...]:
    fragments = []
    for section in bundle.sections:
        fragments.append(_workspace_fragment(section))
    return tuple(fragments)


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
    )


def apply_prompt_snapshot(loop, snapshot: PromptSnapshot) -> None:
    """把 registry 快照写回 loop.history 的 system 消息。"""
    loop.prompt_snapshot = snapshot
    loop.skill_base_prompt = snapshot.text
    message = snapshot.system_message()
    if loop.history and loop.history[0].role == "system":
        loop.history[0] = message
    else:
        loop.history.insert(0, message)


def refresh_registry_snapshot(loop) -> Optional[PromptSnapshot]:
    registry = getattr(loop, "prompt_registry", None)
    if registry is None:
        return None
    snapshot = registry.snapshot()
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
