"""Artifact semantics owned by the builtin ADIOS2 Gray-Scott package."""

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

_STEPS_RE = re.compile(r"^steps:\s+(?P<steps>\d+)\s*$")
_OUTPUT_RE = re.compile(
    r"^Simulation at step\s+(?P<step>\d+)\s+"
    r"writing output step\s+(?P<output_step>\d+)\s*$"
)
_CHECKPOINT_RE = re.compile(
    r"^checkpoint at step\s+(?P<step>\d+)\s+create file\s+(?P<path>\S+)\s*$"
)
_COMPLETED_RE = re.compile(
    r"^Rank\s+0\s+-\s+ET\s+(?P<elapsed_ms>\d+)\s+-\s+milliseconds\s*$"
)
_EPHEMERAL_ENGINES = frozenset({"sst"})


@dataclass
class Adios2GrayScottArtifactAdapter:
    """Describe durable simulation output and restart checkpoints."""

    output_path: PurePosixPath | None
    checkpoint_path: PurePosixPath | None
    engine: str
    output_artifact_id: str = field(default_factory=new_artifact_id)
    checkpoint_artifact_id: str = field(default_factory=new_artifact_id)
    _lines: LineBuffer = field(default_factory=LineBuffer)
    _output_announced: bool = False
    _output_steps: int = 0
    _latest_step: int = 0
    _checkpoint_announced: bool = False
    _latest_checkpoint_step: int = 0
    _finalized: bool = False

    def observe_artifacts(self, text: str) -> list[ArtifactObservation]:
        """Return artifact revisions from native Gray-Scott output."""
        return self._observe(text, finalize=False)

    def finalize_artifacts(self) -> list[ArtifactObservation]:
        """Flush one final unterminated application output line."""
        return self._observe("", finalize=True)

    def reset_artifacts(self) -> None:
        """Reset parsing after the application output stream is replaced."""
        self._lines.reset()
        self._output_announced = False
        self._output_steps = 0
        self._latest_step = 0
        self._checkpoint_announced = False
        self._latest_checkpoint_step = 0
        self._finalized = False

    def _observe(self, text: str, *, finalize: bool) -> list[ArtifactObservation]:
        observations: list[ArtifactObservation] = []
        for line in self._lines.feed(text, finalize=finalize):
            observations.extend(self._observe_line(line.strip()))
        return observations

    def _observe_line(self, line: str) -> list[ArtifactObservation]:
        if self._finalized:
            return []

        if _STEPS_RE.fullmatch(line) is not None:
            if self.output_path is None or self._output_announced:
                return []
            self._output_announced = True
            return [self._output_observation(ArtifactState.PRODUCING)]

        output_match = _OUTPUT_RE.fullmatch(line)
        if output_match is not None:
            if self.output_path is None:
                return []
            step = int(output_match.group("step"))
            if step <= self._latest_step:
                return []
            self._output_announced = True
            self._latest_step = step
            self._output_steps = int(output_match.group("output_step"))
            return [self._output_observation(ArtifactState.PRODUCING)]

        checkpoint_match = _CHECKPOINT_RE.fullmatch(line)
        if checkpoint_match is not None:
            if self.checkpoint_path is None:
                return []
            observed_path = _absolute_posix_path(checkpoint_match.group("path"))
            if observed_path != self.checkpoint_path:
                raise ValueError(
                    "Gray-Scott checkpoint is outside its configured artifact path"
                )
            step = int(checkpoint_match.group("step"))
            if step <= self._latest_checkpoint_step:
                return []
            self._checkpoint_announced = True
            self._latest_checkpoint_step = step
            return [self._checkpoint_observation(ArtifactState.PRODUCING)]

        completed_match = _COMPLETED_RE.fullmatch(line)
        if completed_match is None:
            return []
        self._finalized = True
        elapsed_ms = int(completed_match.group("elapsed_ms"))
        observations: list[ArtifactObservation] = []
        if self._output_announced:
            observations.append(
                self._output_observation(
                    ArtifactState.FINALIZED,
                    elapsed_milliseconds=elapsed_ms,
                )
            )
        if self._checkpoint_announced:
            observations.append(
                self._checkpoint_observation(
                    ArtifactState.FINALIZED,
                    elapsed_milliseconds=elapsed_ms,
                )
            )
        return observations

    def _output_observation(
        self,
        state: ArtifactState,
        *,
        elapsed_milliseconds: int | None = None,
    ) -> ArtifactObservation:
        if self.output_path is None:
            raise RuntimeError("ADIOS2 Gray-Scott output location is unavailable")
        metadata: dict[str, str | int] = {
            "application": "gray_scott",
            "io_backend": "adios2",
            "engine": self.engine,
            "output_steps_observed": self._output_steps,
            "latest_timestep": self._latest_step,
        }
        if elapsed_milliseconds is not None:
            metadata["elapsed_milliseconds"] = elapsed_milliseconds
            metadata["completion_signal"] = "writer_closed_and_timing_reported"
        return ArtifactObservation(
            artifact_id=self.output_artifact_id,
            logical_name="gray-scott-simulation-output",
            kind="scientific_dataset",
            role=ArtifactRole.OUTPUT,
            structure=ArtifactStructure.COLLECTION,
            ownership=ArtifactOwnership.SHARED,
            state=state,
            location=ArtifactLocation.cluster_path(self.output_path),
            media_type="application/x-adios2-bp",
            format=_output_format(self.engine),
            message=(
                "Gray-Scott ADIOS2 output finalized"
                if state is ArtifactState.FINALIZED
                else "Gray-Scott ADIOS2 output is being produced"
            ),
            metadata=metadata,
        )

    def _checkpoint_observation(
        self,
        state: ArtifactState,
        *,
        elapsed_milliseconds: int | None = None,
    ) -> ArtifactObservation:
        if self.checkpoint_path is None:
            raise RuntimeError("ADIOS2 Gray-Scott checkpoint location is unavailable")
        metadata: dict[str, str | int] = {
            "application": "gray_scott",
            "io_backend": "adios2",
            "checkpoint_timestep": self._latest_checkpoint_step,
        }
        if elapsed_milliseconds is not None:
            metadata["elapsed_milliseconds"] = elapsed_milliseconds
            metadata["completion_signal"] = "application_completed_after_checkpoint"
        return ArtifactObservation(
            artifact_id=self.checkpoint_artifact_id,
            logical_name="gray-scott-restart-checkpoint",
            kind="restart_checkpoint",
            role=ArtifactRole.CHECKPOINT,
            structure=ArtifactStructure.COLLECTION,
            ownership=ArtifactOwnership.SHARED,
            state=state,
            location=ArtifactLocation.cluster_path(self.checkpoint_path),
            media_type="application/x-adios2-bp",
            format="adios2-bp5",
            message=(
                "Gray-Scott restart checkpoint finalized"
                if state is ArtifactState.FINALIZED
                else "Gray-Scott is writing a restart checkpoint"
            ),
            metadata=metadata,
        )


def adapter_from_package(
    package: dict[str, Any],
) -> Adios2GrayScottArtifactAdapter | None:
    """Create artifact semantics for builtin ADIOS2 Gray-Scott."""
    if package.get("pkg_type") != "builtin.adios2_gray_scott":
        return None
    engine = str(package.get("engine") or "bp5").casefold()
    output_path = (
        None
        if engine in _EPHEMERAL_ENGINES
        else _configured_cluster_path(
            package.get("out_file"),
            runtime_cwd=package.get("runtime_cwd"),
        )
    )
    return Adios2GrayScottArtifactAdapter(
        output_path=output_path,
        checkpoint_path=_configured_cluster_path(
            package.get("checkpoint_output"),
            runtime_cwd=package.get("runtime_cwd"),
        ),
        engine=engine,
    )


def _configured_cluster_path(
    value: object,
    *,
    runtime_cwd: object,
) -> PurePosixPath | None:
    """Resolve configured output using JARVIS-owned runtime cwd context."""
    if not isinstance(value, str) or not value.strip():
        return None
    path = PurePosixPath(value)
    if not path.is_absolute():
        if not isinstance(runtime_cwd, str) or not runtime_cwd:
            return None
        runtime_path = PurePosixPath(runtime_cwd)
        if not runtime_path.is_absolute() or ".." in runtime_path.parts:
            raise ValueError("Gray-Scott runtime working directory must be absolute")
        path = runtime_path / path
    return _absolute_posix_path(path.as_posix())


def _absolute_posix_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not path.is_absolute() or path.as_posix() != value or ".." in path.parts:
        raise ValueError("Gray-Scott artifacts require a normalized absolute path")
    return path


def _output_format(engine: str) -> str:
    base_engine = engine.removesuffix("_derived")
    return f"adios2-{base_engine}"
