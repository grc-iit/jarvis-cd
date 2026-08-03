"""Artifact semantics owned by builtin DLIO Benchmark."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from jarvis_cd.artifacts.provider import ArtifactObservation
from jarvis_cd.artifacts.schema import (
    ArtifactLocation,
    ArtifactOwnership,
    ArtifactRole,
    ArtifactState,
    ArtifactStructure,
    new_artifact_id,
)


@dataclass(slots=True)
class DlioBenchmarkArtifactAdapter:
    """Track native DLIO datasets, measurements, and checkpoints."""

    workload: str
    cache_policy: str
    data_path: PurePosixPath | None
    output_path: PurePosixPath
    checkpoint_path: PurePosixPath | None
    data_artifact_id: str = field(default_factory=new_artifact_id)
    output_artifact_id: str = field(default_factory=new_artifact_id)
    checkpoint_artifact_id: str = field(default_factory=new_artifact_id)
    _started: bool = False
    _terminal: bool = False

    def observe_artifacts(self, text: str) -> list[ArtifactObservation]:
        """Announce configured products when the native process starts writing."""
        if self._started or self._terminal or not text:
            return []
        self._started = True
        return self._observations(ArtifactState.PRODUCING)

    def finalize_artifacts(self) -> list[ArtifactObservation]:
        """Wait for the authoritative process status before terminalization."""
        return []

    def finalize_artifacts_for_exit(
        self, return_code: int
    ) -> list[ArtifactObservation]:
        """Finalize or mark every declared product incomplete from process exit."""
        if self._terminal:
            return []
        self._terminal = True
        state = (
            ArtifactState.FINALIZED if return_code == 0 else ArtifactState.INCOMPLETE
        )
        return self._observations(state, return_code=return_code)

    def reset_artifacts(self) -> None:
        """Reset stream state when JARVIS replaces the owned output stream."""
        self._started = False
        self._terminal = False

    def _observations(
        self,
        state: ArtifactState,
        *,
        return_code: int | None = None,
    ) -> list[ArtifactObservation]:
        metadata: dict[str, str | int] = {
            "application": "dlio",
            "workload": self.workload,
            "cache_policy": self.cache_policy,
        }
        if return_code is not None:
            metadata["return_code"] = return_code
        observations: list[ArtifactObservation] = []
        if self.data_path is not None:
            observations.append(
                self._observation(
                    artifact_id=self.data_artifact_id,
                    logical_name="dlio-generated-dataset",
                    kind="scientific_dataset",
                    role=ArtifactRole.INTERMEDIATE,
                    path=self.data_path,
                    state=state,
                    format_name="dlio-generated-dataset",
                    metadata=metadata,
                )
            )
        observations.append(
            self._observation(
                artifact_id=self.output_artifact_id,
                logical_name="dlio-native-results",
                kind="scientific_measurement",
                role=ArtifactRole.OUTPUT,
                path=self.output_path,
                state=state,
                format_name="dlio-native-output-directory",
                metadata=metadata,
            )
        )
        if self.checkpoint_path is not None:
            observations.append(
                self._observation(
                    artifact_id=self.checkpoint_artifact_id,
                    logical_name="dlio-checkpoints",
                    kind="model_checkpoint",
                    role=ArtifactRole.CHECKPOINT,
                    path=self.checkpoint_path,
                    state=state,
                    format_name="dlio-checkpoint-collection",
                    metadata=metadata,
                )
            )
        return observations

    @staticmethod
    def _observation(
        *,
        artifact_id: str,
        logical_name: str,
        kind: str,
        role: ArtifactRole,
        path: PurePosixPath,
        state: ArtifactState,
        format_name: str,
        metadata: dict[str, str | int],
    ) -> ArtifactObservation:
        """Build one stable lifecycle revision."""
        if state is ArtifactState.FINALIZED:
            message = f"{logical_name} finalized after successful DLIO exit"
        elif state is ArtifactState.INCOMPLETE:
            message = f"{logical_name} incomplete after DLIO process failure"
        else:
            message = f"{logical_name} is being produced"
        return ArtifactObservation(
            artifact_id=artifact_id,
            logical_name=logical_name,
            kind=kind,
            role=role,
            structure=ArtifactStructure.COLLECTION,
            ownership=ArtifactOwnership.SHARED,
            state=state,
            location=ArtifactLocation.cluster_path(path),
            media_type="application/octet-stream",
            format=format_name,
            message=message,
            metadata=metadata,
        )


def adapter_from_package(
    package: dict[str, Any],
) -> DlioBenchmarkArtifactAdapter | None:
    """Create native DLIO artifact semantics from normalized package config."""
    if package.get("pkg_type") != "builtin.dlio_benchmark":
        return None
    workload = package.get("workload")
    if not isinstance(workload, str) or not workload:
        raise ValueError("DLIO artifact provider requires a workload")
    cache_policy = package.get("cache_policy", "none")
    if not isinstance(cache_policy, str) or not cache_policy:
        raise ValueError("DLIO artifact provider requires an explicit cache policy")
    output_path = _absolute_path(package.get("output_path"), field="output_path")
    data_path = (
        _absolute_path(package.get("data_path"), field="data_path")
        if package.get("generate_data") is True
        else None
    )
    checkpoint_path = (
        _absolute_path(package.get("checkpoint_path"), field="checkpoint_path")
        if package.get("checkpoint_supported") is True
        and package.get("checkpoint") is True
        else None
    )
    return DlioBenchmarkArtifactAdapter(
        workload=workload,
        cache_policy=cache_policy,
        data_path=data_path,
        output_path=output_path,
        checkpoint_path=checkpoint_path,
    )


def _absolute_path(value: object, *, field: str) -> PurePosixPath:
    """Require one normalized absolute cluster artifact path."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"DLIO {field} must be a non-empty absolute path")
    path = PurePosixPath(value)
    if not path.is_absolute() or path.as_posix() != value or ".." in path.parts:
        raise ValueError(f"DLIO {field} must be a normalized absolute path")
    return path


__all__ = ["DlioBenchmarkArtifactAdapter", "adapter_from_package"]
