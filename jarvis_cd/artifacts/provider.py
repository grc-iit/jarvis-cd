"""Generic package artifact provider protocol."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from .schema import (
    JsonValue,
    ArtifactEvent,
    ArtifactLocation,
    ArtifactOwnership,
    ArtifactRole,
    ArtifactState,
    ArtifactStructure,
    new_artifact_id,
)


@dataclass(frozen=True, slots=True)
class ArtifactObservation:
    """Package interpretation of an artifact without authoritative run identity.

    Providers may retain an opaque ``artifact_id`` returned by a reporter when
    they need to describe later lifecycle revisions. JARVIS supplies package
    and execution identity when the observation is persisted.
    """

    logical_name: str
    kind: str
    role: ArtifactRole
    structure: ArtifactStructure
    ownership: ArtifactOwnership
    state: ArtifactState = ArtifactState.AVAILABLE
    artifact_id: str | None = None
    location: ArtifactLocation | None = None
    media_type: str | None = None
    format: str | None = None
    size_bytes: int | None = None
    checksum: str | None = None
    message: str | None = None
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate the observation with the persisted schema rules."""
        ArtifactEvent(
            package_name="provider",
            package_id="provider",
            execution_id="provider",
            artifact_id=self.artifact_id or new_artifact_id(),
            logical_name=self.logical_name,
            kind=self.kind,
            role=self.role,
            structure=self.structure,
            ownership=self.ownership,
            state=self.state,
            location=self.location,
            media_type=self.media_type,
            format=self.format,
            size_bytes=self.size_bytes,
            checksum=self.checksum,
            message=self.message,
            metadata=self.metadata,
        ).to_json()


@runtime_checkable
class PackageArtifactProvider(Protocol):
    """Minimal artifact contract implemented beside a JARVIS package."""

    def observe_artifacts(self, text: str) -> list[ArtifactObservation]:
        """Interpret application output and return typed artifact observations."""
        ...

    def finalize_artifacts(self) -> list[ArtifactObservation]:
        """Flush final observations after application output ends."""
        ...

    def reset_artifacts(self) -> None:
        """Reset provider state when the underlying output stream is replaced."""
        ...


ArtifactProviderFactory = Callable[[dict[str, Any]], PackageArtifactProvider | None]
