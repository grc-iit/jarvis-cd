"""Artifact semantics owned by the builtin Gray-Scott package."""

from __future__ import annotations

import re
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
from jarvis_cd.progress import LineBuffer

_OUTPUT_RE = re.compile(r"^\s*wrote\s+(?P<path>.+?gs_(?P<step>\d+)\.h5)\s*$")
_ADIOS_STEPS_RE = re.compile(r"^steps:\s+(?P<steps>\d+)\s*$")
_ADIOS_OUTPUT_RE = re.compile(
    r"^Simulation at step\s+(?P<step>\d+)\s+"
    r"writing output step\s+(?P<output_step>\d+)\s*$"
)
_ADIOS_COMPLETED_RE = re.compile(
    r"^Rank\s+0\s+-\s+ET\s+(?P<elapsed_ms>\d+)\s+-\s+milliseconds\s*$"
)


@dataclass
class GrayScottArtifactAdapter:
    """Describe Gray-Scott's HDF5 or ADIOS2 timestep collection."""

    output_dir: PurePosixPath | None
    deploy_mode: str = "default"
    artifact_id: str = field(default_factory=new_artifact_id)
    _lines: LineBuffer = field(default_factory=LineBuffer)
    _member_count: int = 0
    _latest_step: int = 0
    _announced: bool = False
    _finalized: bool = False

    def observe_artifacts(self, text: str) -> list[ArtifactObservation]:
        """Return collection revisions from completed HDF5 writes."""
        return self._observe(text, finalize=False)

    def finalize_artifacts(self) -> list[ArtifactObservation]:
        """Flush one final unterminated application output line."""
        return self._observe("", finalize=True)

    def reset_artifacts(self) -> None:
        """Reset parsing after the application output stream is replaced."""
        self._lines.reset()
        self._member_count = 0
        self._latest_step = 0
        self._announced = False
        self._finalized = False

    def _observe(self, text: str, *, finalize: bool) -> list[ArtifactObservation]:
        observations: list[ArtifactObservation] = []
        for line in self._lines.feed(text, finalize=finalize):
            observation = self._observe_line(line)
            if observation is not None:
                observations.append(observation)
        return observations

    def _observe_line(self, line: str) -> ArtifactObservation | None:
        stripped = line.strip()
        if (
            self.deploy_mode != "container"
            and _ADIOS_STEPS_RE.fullmatch(stripped) is not None
            and not self._announced
            and self.output_dir is not None
        ):
            self._announced = True
            return self._observation(ArtifactState.PRODUCING)

        adios_output_match = _ADIOS_OUTPUT_RE.fullmatch(stripped)
        if adios_output_match is not None:
            if self.output_dir is None:
                return None
            step = int(adios_output_match.group("step"))
            if step <= self._latest_step:
                return None
            self._announced = True
            self._latest_step = step
            self._member_count = int(adios_output_match.group("output_step"))
            return self._observation(ArtifactState.PRODUCING)

        output_match = _OUTPUT_RE.fullmatch(line)
        if output_match is not None:
            if self._finalized:
                raise ValueError(
                    "Gray-Scott emitted output after finalizing its dataset"
                )
            if self.output_dir is None:
                return None
            output_path = _absolute_posix_path(output_match.group("path"))
            if output_path.parent != self.output_dir:
                raise ValueError(
                    "Gray-Scott output is outside its configured artifact directory"
                )
            step = int(output_match.group("step"))
            if step <= self._latest_step:
                return None
            self._latest_step = step
            self._member_count += 1
            self._announced = True
            return self._observation(ArtifactState.PRODUCING)

        if stripped == "Done." or _ADIOS_COMPLETED_RE.fullmatch(stripped) is not None:
            if self._finalized or not self._announced:
                return None
            self._finalized = True
            return self._observation(ArtifactState.FINALIZED)
        return None

    def _observation(self, state: ArtifactState) -> ArtifactObservation:
        if self.output_dir is None:
            raise RuntimeError("Gray-Scott artifact location is unavailable")
        is_hdf5 = self.deploy_mode == "container"
        return ArtifactObservation(
            artifact_id=self.artifact_id,
            logical_name="gray-scott-timesteps",
            kind="scientific_dataset",
            role=ArtifactRole.OUTPUT,
            structure=ArtifactStructure.COLLECTION,
            ownership=ArtifactOwnership.SHARED,
            state=state,
            location=ArtifactLocation.cluster_path(self.output_dir),
            media_type=("application/x-hdf5" if is_hdf5 else "application/x-adios2-bp"),
            format=("hdf5-time-series" if is_hdf5 else "adios2-bp5"),
            message=(
                "Gray-Scott timestep collection finalized"
                if state is ArtifactState.FINALIZED
                else "Gray-Scott timestep collection is being produced"
            ),
            metadata={
                "application": "gray_scott",
                "io_backend": "hdf5" if is_hdf5 else "adios2",
                "member_pattern": "gs_*.h5" if is_hdf5 else "adios2-steps",
                "members_observed": self._member_count,
                "latest_timestep": self._latest_step,
            },
        )


def adapter_from_package(
    package: dict[str, Any],
) -> GrayScottArtifactAdapter | None:
    """Create artifact semantics for the builtin Gray-Scott package."""
    if package.get("pkg_type") != "builtin.gray_scott":
        return None
    return GrayScottArtifactAdapter(
        output_dir=_configured_cluster_path(package.get("outdir")),
        deploy_mode=str(package.get("effective_deploy_mode") or "default").casefold(),
    )


def _configured_cluster_path(value: object) -> PurePosixPath | None:
    """Return a normalized absolute cluster path from trusted configuration."""
    if not isinstance(value, str) or not value.strip():
        return None
    if not PurePosixPath(value).is_absolute():
        return None
    return _absolute_posix_path(value)


def _absolute_posix_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not path.is_absolute() or path.as_posix() != value or ".." in path.parts:
        raise ValueError("Gray-Scott artifacts require a normalized absolute path")
    return path
