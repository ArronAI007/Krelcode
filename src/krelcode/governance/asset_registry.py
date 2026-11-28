"""In-memory dependency graph for governed asset deletion."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from ..contracts.asset import (
    AssetDeletionMode,
    AssetDeletionPlan,
    AssetEnvelope,
)


class AssetDependencyGraph:
    def __init__(self, assets: Iterable[AssetEnvelope] = ()) -> None:
        self._assets: dict[str, AssetEnvelope] = {}
        for asset in assets:
            self.add(asset)

    def add(self, asset: AssetEnvelope) -> None:
        self._assets[asset.asset_id] = asset

    def plan_delete(
        self,
        asset_id: str,
        *,
        mode: AssetDeletionMode | str = AssetDeletionMode.QUARANTINE,
    ) -> AssetDeletionPlan:
        root = str(asset_id or "").strip()
        children: dict[str, set[str]] = defaultdict(set)
        for asset in self._assets.values():
            for parent in asset.derived_from:
                children[parent].add(asset.asset_id)
        affected: list[str] = []
        blocked: list[str] = []
        pending = [root]
        seen: set[str] = set()
        while pending:
            current = pending.pop(0)
            if current in seen:
                continue
            seen.add(current)
            if current in self._assets:
                affected.append(current)
            blocked.extend(sorted(children.get(current, ())))
            pending.extend(sorted(children.get(current, ())))
        return AssetDeletionPlan(
            root_asset_id=root,
            mode=mode,
            affected_asset_ids=tuple(affected),
            blocked_asset_ids=tuple(blocked),
            evidence={
                "asset_count": len(self._assets),
                "derived_count": max(0, len(affected) - 1),
            },
        )


__all__ = ["AssetDependencyGraph"]
