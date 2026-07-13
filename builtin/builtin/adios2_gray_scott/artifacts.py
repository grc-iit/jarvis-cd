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
    _terminal: bool = False
    _pending_elapsed_milliseconds: int | None = None
    _completion_signal: str | None = None
    _return_code: int | None = None

    def observe_artifacts(self, text: str) -> list[ArtifactObservation]:
        """Return artifact revisions from native Gray-Scott output."""
        return self._observe(text, finalize=False)

    def finalize_artifacts(self) -> list[ArtifactObservation]:
        """Flush one final unterminated application output line."""
        observations = self._observe("", finalize=True)
        if self._pending_elapsed_milliseconds is not None and not self._terminal:
            self._terminal = True
            self._completion_signal = "writer_closed_and_timing_reported"
            observations.extend(self._terminal_observations(ArtifactState.FINALIZED))
        return observations

    def finalize_artifacts_for_exit(
        self, return_code: int
    ) -> list[ArtifactObservation]:
        """Finalize durable outputs from the producer's owned process status."""
        observations = self._observe("", finalize=True)
        if self._terminal or not (self._output_announced or self._checkpoint_announced):
            return observations

        self._terminal = True
        self._return_code = return_code
        if return_code == 0:
            self._completion_signal = (
                "writer_closed_and_timing_reported"
                if self._pending_elapsed_milliseconds is not None
                else "process_exit_zero_after_final_output"
            )
            terminal_state = ArtifactState.FINALIZED
        else:
            self._completion_signal = "process_exit_nonzero"
            terminal_state = ArtifactState.INCOMPLETE
        observations.extend(self._terminal_observations(terminal_state))
        return observations

    def reset_artifacts(self) -> None:
        """Reset parsing after the application output stream is replaced."""
        self._lines.reset()
        self._output_announced = False
        self._output_steps = 0
        self._latest_step = 0
        self._checkpoint_announced = False
        self._latest_checkpoint_step = 0
        self._terminal = False
        self._pending_elapsed_milliseconds = None
        self._completion_signal = None
        self._return_code = None

    def _observe(self, text: str, *, finalize: bool) -> list[ArtifactObservation]:
        observations: list[ArtifactObservation] = []
        for line in self._lines.feed(text, finalize=finalize):
            observations.extend(self._observe_line(line.strip()))
        return observations

    def _observe_line(self, line: str) -> list[ArtifactObservation]:
        if self._terminal:
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
        self._pending_elapsed_milliseconds = int(completed_match.group("elapsed_ms"))
        return []

    def _terminal_observations(self, state: ArtifactState) -> list[ArtifactObservation]:
        """Return terminal revisions for every artifact seen in this run."""
        observations: list[ArtifactObservation] = []
        if self._output_announced:
            observations.append(self._output_observation(state))
        if self._checkpoint_announced:
            observations.append(self._checkpoint_observation(state))
        return observations

    def _output_observation(
        self,
        state: ArtifactState,
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
        self._add_terminal_metadata(metadata, state)
        if state is ArtifactState.FINALIZED:
            message = "Gray-Scott ADIOS2 output finalized"
        elif state is ArtifactState.INCOMPLETE:
            message = "Gray-Scott ADIOS2 output is incomplete after process failure"
        else:
            message = "Gray-Scott ADIOS2 output is being produced"
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
            message=message,
            metadata=metadata,
        )

    def _checkpoint_observation(
        self,
        state: ArtifactState,
    ) -> ArtifactObservation:
        if self.checkpoint_path is None:
            raise RuntimeError("ADIOS2 Gray-Scott checkpoint location is unavailable")
        metadata: dict[str, str | int] = {
            "application": "gray_scott",
            "io_backend": "adios2",
            "checkpoint_timestep": self._latest_checkpoint_step,
        }
        self._add_terminal_metadata(metadata, state)
        if state is ArtifactState.FINALIZED:
            message = "Gray-Scott restart checkpoint finalized"
        elif state is ArtifactState.INCOMPLETE:
            message = (
                "Gray-Scott restart checkpoint is incomplete after process failure"
            )
        else:
            message = "Gray-Scott is writing a restart checkpoint"
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
            message=message,
            metadata=metadata,
        )

    def _add_terminal_metadata(
        self,
        metadata: dict[str, str | int],
        state: ArtifactState,
    ) -> None:
        """Attach process-aware metadata only to terminal revisions."""
        if state not in {ArtifactState.FINALIZED, ArtifactState.INCOMPLETE}:
            return
        if self._completion_signal is not None:
            metadata["completion_signal"] = self._completion_signal
        if self._pending_elapsed_milliseconds is not None:
            metadata["elapsed_milliseconds"] = self._pending_elapsed_milliseconds
        if (
            self._completion_signal == "process_exit_nonzero"
            and self._pending_elapsed_milliseconds is not None
        ):
            metadata["application_completion_signal"] = (
                "writer_closed_and_timing_reported"
            )
        if self._return_code is not None:
            metadata["return_code"] = self._return_code


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
