"""Atomic workspace-local storage for Prompt release evidence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from krelcode.agent.prompt.prompt_release import ModelProfileCalibration
from krelcode.agent.prompt.prompt_release_models import (
    PROMPT_RELEASE_PIPELINE_SCHEMA_VERSION,
    PromptHumanApproval,
    PromptPipelineError,
    PromptReleaseProposal,
    _text,
)


class PromptReleaseProposalStore:
    """Atomic, workspace-local, content-light proposal and approval store."""

    def __init__(self, workspace_root: Path | str) -> None:
        self.root = (
            Path(workspace_root).expanduser().resolve()
            / ".krelcode" / "prompt-releases"
        )
        self.proposals_dir = self.root / "proposals"
        self.approvals_dir = self.root / "approvals"
        self.calibrations_dir = self.root / "calibrations"
        self.artifacts_dir = self.root / "artifacts"

    def _safe_id(self, proposal_id: str) -> str:
        value = _text(proposal_id, 240)
        if (
            not value
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or "\x00" in value
        ):
            raise PromptPipelineError("非法 proposal_id")
        return value

    def _atomic_write(self, path: Path, payload: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def save_proposal(self, proposal: PromptReleaseProposal) -> Path:
        path = self.proposals_dir / f"{self._safe_id(proposal.proposal_id)}.json"
        if path.exists():
            existing = self.load_proposal(proposal.proposal_id)
            if existing.proposal_hash != proposal.proposal_hash:
                raise PromptPipelineError("同一 proposal_id 的证据身份发生漂移")
        self._atomic_write(path, proposal.as_dict())
        return path

    def load_proposal(self, proposal_id: str) -> PromptReleaseProposal:
        path = self.proposals_dir / f"{self._safe_id(proposal_id)}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PromptPipelineError(f"无法读取 proposal: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise PromptPipelineError(f"proposal JSON 无效: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise PromptPipelineError("proposal 根节点必须是 object")
        return PromptReleaseProposal.from_dict(raw)

    def list_proposals(self, limit: int = 50) -> tuple[PromptReleaseProposal, ...]:
        bounded = max(1, min(int(limit), 500))
        if not self.proposals_dir.is_dir():
            return ()
        proposals: list[PromptReleaseProposal] = []
        for path in self.proposals_dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, Mapping):
                    proposals.append(PromptReleaseProposal.from_dict(raw))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        proposals.sort(
            key=lambda item: (item.created_at, item.proposal_id),
            reverse=True,
        )
        return tuple(proposals[:bounded])

    def save_approval(self, approval: PromptHumanApproval) -> Path:
        path = self.approvals_dir / f"{self._safe_id(approval.proposal_id)}.json"
        self._atomic_write(path, approval.as_dict())
        return path

    def load_approval(self, proposal_id: str) -> PromptHumanApproval:
        path = self.approvals_dir / f"{self._safe_id(proposal_id)}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PromptPipelineError(f"无法读取 approval: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise PromptPipelineError("approval 根节点必须是 object")
        return PromptHumanApproval.from_dict(raw)

    def save_calibration(
        self, proposal_id: str, calibration: ModelProfileCalibration,
    ) -> Path:
        path = (
            self.calibrations_dir
            / f"{self._safe_id(proposal_id)}.json"
        )
        self._atomic_write(path, calibration.as_dict())
        return path

    def save_release_artifact(
        self, proposal: PromptReleaseProposal, approval: PromptHumanApproval,
    ) -> Path:
        payload = {
            "schema_version": PROMPT_RELEASE_PIPELINE_SCHEMA_VERSION,
            "kind": "approved_prompt_release_artifact",
            "proposal_id": proposal.proposal_id,
            "proposal_hash": proposal.proposal_hash,
            "candidate_hash": proposal.candidate_hash,
            "approval": approval.as_dict(),
            "release_id": proposal.release_id,
            "release_hash": proposal.release_hash,
            "profile_name": proposal.profile_name,
            "profile_hash": proposal.profile_hash,
            "calibration_hash": (
                proposal.calibration.calibration_hash
                if proposal.calibration is not None else ""),
            "production_modified": False,
            "applied": False,
            "contains_prompt_content": False,
        }
        path = self.artifacts_dir / f"{self._safe_id(proposal.proposal_id)}.json"
        self._atomic_write(path, payload)
        return path


__all__ = ["PromptReleaseProposalStore"]
