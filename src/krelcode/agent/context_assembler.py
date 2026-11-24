"""Deterministic ContextSource collection, ordering and cache-boundary assembly."""

from __future__ import annotations

import hashlib
import inspect
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .context_contracts import (
    CATEGORY_ORDER,
    CONTEXT_TRUST_LEVELS,
    ContextLayer,
    ContextTokenContribution,
)
from .context_source import (
    ContextAssembly,
    ContextRequest,
    ContextSource,
    ContextSourceIssue,
)


class ContextAssembler:
    """Own the only path from ContextSources to the provider-bound system text."""

    def __init__(
        self,
        sources: Iterable[ContextSource] = (),
        *,
        engine: str = "v3",
    ) -> None:
        self._sources: List[ContextSource] = []
        self._names: Dict[str, ContextSource] = {}
        normalized = str(engine or "v3").strip().lower()
        self.engine = normalized if normalized in {"v3", "legacy"} else "v3"
        for source in sources:
            self.register(source)

    @property
    def source_names(self) -> Tuple[str, ...]:
        return tuple(self._names)

    def register(self, source: ContextSource, *, replace: bool = False) -> None:
        raw_name = getattr(source, "name", "")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError("ContextSource 必须声明非空字符串 name")
        name = raw_name.strip()
        if name in self._names:
            if not replace:
                raise ValueError(f"ContextSource 已存在: {name}")
            self.unregister(name)
        self._sources.append(source)
        self._names[name] = source

    def unregister(self, name: str) -> bool:
        source = self._names.pop(name, None)
        if source is None:
            return False
        self._sources.remove(source)
        return True

    async def assemble(self, request: ContextRequest) -> ContextAssembly:
        collected: List[
            Tuple[int, int, int, int, str, ContextLayer]
        ] = []
        issues: List[ContextSourceIssue] = []
        seen_ids: set[str] = set()
        contributing_sources: set[str] = set()
        category_rank = {
            category: index for index, category in enumerate(CATEGORY_ORDER)}

        for source_index, source in enumerate(self._sources):
            try:
                result = source.collect(request)
                if inspect.isawaitable(result):
                    result = await result
                layers = tuple(result or ())
            except Exception as exc:
                # A third-party/optional source must not break the whole request.
                # Only the exception type is retained: messages may contain secrets.
                issues.append(ContextSourceIssue(source.name, type(exc).__name__))
                continue
            for layer_index, layer in enumerate(layers):
                if not isinstance(layer, ContextLayer):
                    issues.append(ContextSourceIssue(source.name, "invalid-layer"))
                    continue
                issue = _validate_layer(layer, seen_ids)
                if issue is not None:
                    issues.append(ContextSourceIssue(source.name, issue))
                    continue
                if not layer.content:
                    continue
                seen_ids.add(layer.id)
                contributing_sources.add(source.name)
                collected.append((
                    category_rank[layer.category],
                    layer.priority,
                    source_index,
                    layer_index,
                    layer.id,
                    layer,
                ))

        collected.sort(key=lambda item: item[:5])
        ordered_layers = tuple(item[5] for item in collected)
        text, stable_prefix = _render_layers(ordered_layers)
        stable_prefix = max(0, min(stable_prefix, len(text)))
        stable_text = text[:stable_prefix]
        dynamic_text = text[stable_prefix:]
        contributions = _merge_contributions(ordered_layers)
        provider_meta = dict(request.system_provider_meta or {})
        if stable_prefix > 0:
            provider_meta["prompt_cache_boundaries"] = [stable_prefix]
        else:
            provider_meta.pop("prompt_cache_boundaries", None)
        fragment_count = sum(
            max(0, int(value))
            for layer in ordered_layers
            if isinstance(
                (value := layer.metadata.get("fragment_count", 0)),
                (int, float, str, bytes, bytearray),
            )
        )
        return ContextAssembly(
            text=text,
            layers=ordered_layers,
            stable_prefix_chars=stable_prefix,
            stable_hash=hashlib.sha256(stable_text.encode("utf-8")).hexdigest(),
            dynamic_hash=hashlib.sha256(
                dynamic_text.encode("utf-8")).hexdigest() if dynamic_text else "",
            full_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            token_contributions=contributions,
            engine=self.engine,
            source_count=len(contributing_sources),
            fragment_count=fragment_count,
            issues=tuple(issues),
            provider_meta=provider_meta or None,
            prompt_snapshot=request.prompt_snapshot,
        )


def _validate_layer(
    layer: ContextLayer, seen_ids: set[str],
) -> Optional[str]:
    """Validate untrusted source output before it reaches rendering/accounting."""
    if not isinstance(layer.id, str) or not layer.id:
        return "invalid-id"
    if not isinstance(layer.source, str) or not layer.source:
        return "invalid-id"
    if layer.id in seen_ids:
        return "duplicate-id"
    if layer.category not in CATEGORY_ORDER:
        return "invalid-category"
    if layer.trust not in CONTEXT_TRUST_LEVELS:
        return "invalid-trust"
    if not isinstance(layer.origin, str):
        return "invalid-origin"
    if not isinstance(layer.content, str):
        return "invalid-content"
    if not isinstance(layer.priority, int) or isinstance(layer.priority, bool):
        return "invalid-priority"
    if (
        layer.stable_prefix_chars is not None
        and (
            not isinstance(layer.stable_prefix_chars, int)
            or isinstance(layer.stable_prefix_chars, bool)
        )
    ):
        return "invalid-cache-boundary"
    if not hasattr(layer.metadata, "get"):
        return "invalid-metadata"
    raw_fragment_count = layer.metadata.get("fragment_count", 0)
    if raw_fragment_count is not None:
        if not isinstance(raw_fragment_count, int) or isinstance(
            raw_fragment_count, bool
        ) or raw_fragment_count < 0:
            return "invalid-metadata"
    try:
        contributions = layer.effective_token_contributions()
    except Exception:
        return "invalid-token-contribution"
    for contribution in contributions:
        if not isinstance(contribution, ContextTokenContribution):
            return "invalid-token-contribution"
        if (
            not isinstance(contribution.source, str)
            or not contribution.source
            or contribution.category not in CATEGORY_ORDER
        ):
            return "invalid-token-contribution"
        if (
            not isinstance(contribution.tokens, int)
            or isinstance(contribution.tokens, bool)
            or contribution.tokens < 0
        ):
            return "invalid-token-contribution"
    return None


def _render_layers(layers: Sequence[ContextLayer]) -> Tuple[str, int]:
    """Join layers and compute the last valid cacheable prefix."""
    text = ""
    stable_end = 0
    stable_open = True
    for layer in layers:
        separator = "\n\n" if text else ""
        start = len(text) + len(separator)
        text += separator + layer.content
        if not stable_open:
            continue
        if layer.stable_prefix_chars is not None:
            local = max(0, min(layer.stable_prefix_chars, len(layer.content)))
            stable_end = start + local
            if local < len(layer.content) or not layer.cacheable:
                stable_open = False
            continue
        if layer.cacheable:
            stable_end = len(text)
        else:
            stable_open = False
    return text, stable_end


def _merge_contributions(
    layers: Sequence[ContextLayer],
) -> Tuple[ContextTokenContribution, ...]:
    merged: Dict[Tuple[str, str], int] = defaultdict(int)
    for layer in layers:
        for contribution in layer.effective_token_contributions():
            if contribution.category not in CATEGORY_ORDER:
                continue
            merged[(contribution.source, contribution.category)] += max(
                0, int(contribution.tokens))
    return tuple(
        ContextTokenContribution(source, category, tokens)
        for (source, category), tokens in sorted(merged.items())
    )
