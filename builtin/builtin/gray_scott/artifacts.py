"""Artifact semantics owned by the builtin Gray-Scott package."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
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

_PathStat = tuple[str, int, int, int, int, int]
_PathFingerprint = tuple[_PathStat, tuple[_PathStat, ...]]


def _path_fingerprint(path: PurePosixPath) -> _PathFingerprint | None:
    """Return metadata-only evidence that a configured path changed.

    The direct Clio Core writer does not print a checkpoint-completion line.
    Snapshotting the configured BP path before and after the owned process lets
    JARVIS report only a checkpoint that appeared or changed during that run,
    without reading or hashing potentially large scientific data files.
    """
    concrete = Path(path.as_posix())
    try:
        root_stat = concrete.lstat()
        children: list[_PathStat] = []
        if stat.S_ISDIR(root_stat.st_mode):
            for child in concrete.iterdir():
                child_stat = child.lstat()
                children.append(_stat_signature(child.name, child_stat))
    except OSError:
        return None
    return (
        _stat_signature(".", root_stat),
        tuple(sorted(children, key=lambda item: item[0])),
    )


def _stat_signature(name: str, value: os.stat_result) -> _PathStat:
    """Return stable filesystem metadata without reading artifact contents."""
    return (
        name,
        int(value.st_mode),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


@dataclass
class GrayScottArtifactAdapter:
    """Describe Gray-Scott's HDF5 or ADIOS2 timestep collection."""

    output_dir: PurePosixPath | None
    checkpoint_path: PurePosixPath | None = None
    deploy_mode: str = "default"
    artifact_id: str = field(default_factory=new_artifact_id)
    checkpoint_artifact_id: str = field(default_factory=new_artifact_id)
    _lines: LineBuffer = field(default_factory=LineBuffer)
    _member_count: int = 0
    _latest_step: int = 0
    _announced: bool = False
    _terminal: bool = False
    _completion_signal: str | None = None
    _pending_completion_signal: str | None = None
    _pending_elapsed_milliseconds: int | None = None
    _return_code: int | None = None
    _checkpoint_baseline: _PathFingerprint | None = field(init=False, default=None)
    _checkpoint_detected: bool = False

    def __post_init__(self) -> None:
        """Snapshot a configured checkpoint before the owned process starts."""
        if self.checkpoint_path is not None:
            self._checkpoint_baseline = _path_fingerprint(self.checkpoint_path)

    def observe_artifacts(self, text: str) -> list[ArtifactObservation]:
        """Return collection revisions from completed HDF5 writes."""
        return self._observe(text, finalize=False)

    def finalize_artifacts(self) -> list[ArtifactObservation]:
        """Flush one final unterminated application output line."""
        observations = self._observe("", finalize=True)
        finalized = self._pending_finalized_observation()
        if finalized is not None:
            observations.append(finalized)
        return observations

    def finalize_artifacts_for_exit(
        self, return_code: int
    ) -> list[ArtifactObservation]:
        """Finalize direct outputs from an owned process and filesystem state."""
        observations = self._observe("", finalize=True)
        if self._terminal:
            return observations

        self._checkpoint_detected = self._checkpoint_changed_during_process()
        if not self._announced and not self._checkpoint_detected:
            return observations

        self._terminal = True
        self._return_code = return_code
        if return_code == 0:
            self._completion_signal = (
                self._pending_completion_signal
                or "process_exit_zero_after_final_output"
            )
            terminal_state = ArtifactState.FINALIZED
        else:
            self._completion_signal = "process_exit_nonzero"
            terminal_state = ArtifactState.INCOMPLETE
        if self._announced:
            observations.append(self._observation(terminal_state))
        if self._checkpoint_detected:
            observations.append(self._checkpoint_observation(terminal_state))
        return observations

    def reset_artifacts(self) -> None:
        """Reset parsing after the application output stream is replaced."""
        self._lines.reset()
        self._member_count = 0
        self._latest_step = 0
        self._announced = False
        self._terminal = False
        self._completion_signal = None
        self._pending_completion_signal = None
        self._pending_elapsed_milliseconds = None
        self._return_code = None
        self._checkpoint_detected = False
        self._checkpoint_baseline = (
            None
            if self.checkpoint_path is None
            else _path_fingerprint(self.checkpoint_path)
        )

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
            if self._terminal:
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
            if self._terminal or not self._announced:
                return None
            self._pending_completion_signal = (
                "application_reported_done"
                if stripped == "Done."
                else "writer_closed_and_timing_reported"
            )
            completed_match = _ADIOS_COMPLETED_RE.fullmatch(stripped)
            if completed_match is not None:
                self._pending_elapsed_milliseconds = int(
                    completed_match.group("elapsed_ms")
                )
        return None

    def _pending_finalized_observation(self) -> ArtifactObservation | None:
        """Commit a deferred application success marker for legacy callers."""
        if (
            self._terminal
            or not self._announced
            or self._pending_completion_signal is None
        ):
            return None
        self._terminal = True
        self._completion_signal = self._pending_completion_signal
        return self._observation(ArtifactState.FINALIZED)

    def _observation(self, state: ArtifactState) -> ArtifactObservation:
        if self.output_dir is None:
            raise RuntimeError("Gray-Scott artifact location is unavailable")
        is_hdf5 = self.deploy_mode == "container"
        metadata: dict[str, str | int] = {
            "application": "gray_scott",
            "io_backend": "hdf5" if is_hdf5 else "adios2",
            "member_pattern": "gs_*.h5" if is_hdf5 else "adios2-steps",
            "members_observed": self._member_count,
            "latest_timestep": self._latest_step,
        }
        if self._completion_signal is not None:
            metadata["completion_signal"] = self._completion_signal
        if self._pending_elapsed_milliseconds is not None:
            metadata["elapsed_milliseconds"] = self._pending_elapsed_milliseconds
        if (
            self._completion_signal == "process_exit_nonzero"
            and self._pending_completion_signal is not None
        ):
            metadata["application_completion_signal"] = self._pending_completion_signal
        if self._return_code is not None:
            metadata["return_code"] = self._return_code

        if state is ArtifactState.FINALIZED:
            message = "Gray-Scott timestep collection finalized"
        elif state is ArtifactState.INCOMPLETE:
            message = (
                "Gray-Scott timestep collection is incomplete after process failure"
            )
        else:
            message = "Gray-Scott timestep collection is being produced"
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
            message=message,
            metadata=metadata,
        )

    def _checkpoint_changed_during_process(self) -> bool:
        """Return whether the configured checkpoint became durable this run."""
        if self.checkpoint_path is None:
            return False
        current = _path_fingerprint(self.checkpoint_path)
        return current is not None and current != self._checkpoint_baseline

    def _checkpoint_observation(
        self,
        state: ArtifactState,
    ) -> ArtifactObservation:
        """Describe the checkpoint observed at owned-process completion."""
        if self.checkpoint_path is None:
            raise RuntimeError("Gray-Scott checkpoint location is unavailable")
        metadata: dict[str, str | int | bool] = {
            "application": "gray_scott",
            "io_backend": "adios2",
            "detection_signal": "configured_path_created_or_changed_during_process",
            "physical_path_observed": True,
        }
        if self._latest_step > 0:
            metadata["latest_output_timestep_observed"] = self._latest_step
        if self._completion_signal is not None:
            metadata["completion_signal"] = self._completion_signal
        if self._return_code is not None:
            metadata["return_code"] = self._return_code
        message = (
            "Gray-Scott restart checkpoint finalized"
            if state is ArtifactState.FINALIZED
            else "Gray-Scott restart checkpoint is incomplete after process failure"
        )
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


def adapter_from_package(
    package: dict[str, Any],
) -> GrayScottArtifactAdapter | None:
    """Create artifact semantics for the builtin Gray-Scott package."""
    if package.get("pkg_type") != "builtin.gray_scott":
        return None
    deploy_mode = str(package.get("effective_deploy_mode") or "default").casefold()
    checkpoint_path = None
    if deploy_mode != "container" and package.get("checkpoint") is True:
        checkpoint_path = _configured_cluster_path(package.get("checkpoint_output"))
    return GrayScottArtifactAdapter(
        output_dir=_configured_cluster_path(package.get("outdir")),
        checkpoint_path=checkpoint_path,
        deploy_mode=deploy_mode,
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
