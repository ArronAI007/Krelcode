"""Read-only artifact identity registry."""

from krelcode.contracts.runtime import ArtifactIdentity


class ArtifactRegistry:
    def __init__(self) -> None:
        self._artifacts: dict[str, ArtifactIdentity] = {}

    def register(self, artifact: ArtifactIdentity) -> None:
        self._artifacts[artifact.id] = artifact

    def get(self, artifact_id: str) -> ArtifactIdentity | None:
        return self._artifacts.get(str(artifact_id or "").strip())

    def list(self) -> tuple[ArtifactIdentity, ...]:
        return tuple(
            self._artifacts[key] for key in sorted(self._artifacts))


__all__ = ["ArtifactIdentity", "ArtifactRegistry"]
