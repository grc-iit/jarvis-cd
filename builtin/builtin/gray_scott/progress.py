"""Progress semantics owned by the builtin Gray-Scott package."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis_cd.progress import LineBuffer, ProgressObservation, ProgressState

_START_RE = re.compile(
    r"^Gray-Scott\s+(?P<width>\d+)x(?P<height>\d+)\s+"
    r"(?P<ranks>\d+)\s+ranks\s+(?P<steps>\d+)\s+steps\s*$"
)
_OUTPUT_RE = re.compile(r"^\s*wrote\s+(?P<path>.+?gs_(?P<step>\d+)\.h5)\s*$")
_ADIOS_STEPS_RE = re.compile(r"^steps:\s+(?P<steps>\d+)\s*$")
_ADIOS_RESTART_RE = re.compile(r"^restart:\s+from step\s+(?P<step>\d+)\s*$")
_ADIOS_OUTPUT_RE = re.compile(
    r"^Simulation at step\s+(?P<step>\d+)\s+"
    r"writing output step\s+(?P<output_step>\d+)\s*$"
)
_ADIOS_COMPLETED_RE = re.compile(
    r"^Rank\s+0\s+-\s+ET\s+(?P<elapsed_ms>\d+)\s+-\s+milliseconds\s*$"
)


@dataclass
class GrayScottProgressAdapter:
    """Interpret completed Gray-Scott timesteps from package-owned stdout."""

    package_name: str = "builtin.gray_scott"
    package_id: str = "gray_scott"
    total_steps: int | None = None
    output_every: int | None = None
    output_dir: Path | None = None
    _lines: LineBuffer = field(default_factory=LineBuffer)
    _last_step: int = 0
    _restart_step: int = 0
    _started: bool = False
    _completed: bool = False
    _failed: bool = False
    _pending_completion_signal: str | None = None
    _pending_elapsed_milliseconds: int | None = None

    def observe_progress(self, text: str) -> list[ProgressObservation]:
        """Return truthful progress observations from complete output lines."""
        return self._observe(text, finalize=False)

    def finalize_progress(self) -> list[ProgressObservation]:
        """Flush one final unterminated Gray-Scott output line."""
        observations = self._observe("", finalize=True)
        completed = self._pending_completed_observation()
        if completed is not None:
            observations.append(completed)
        return observations

    def finalize_progress_for_exit(self, return_code: int) -> list[ProgressObservation]:
        """Finalize direct IOWarp runs from JARVIS's owned process result."""
        observations = self._observe("", finalize=True)
        if return_code != 0:
            failed = self._failed_observation(return_code)
            if failed is not None:
                observations.append(failed)
            return observations

        completed = self._pending_completed_observation()
        if completed is None and (
            self._started
            and self.total_steps is not None
            and self._last_step == self.total_steps
        ):
            completed = self._completed_observation(
                completion_signal="process_exit_zero_after_final_output"
            )
        if completed is not None:
            observations.append(completed)
        return observations

    def reset_progress(self) -> None:
        """Reset parsing when the application output stream is replaced."""
        self._lines.reset()
        self._last_step = 0
        self._restart_step = 0
        self._started = False
        self._completed = False
        self._failed = False
        self._pending_completion_signal = None
        self._pending_elapsed_milliseconds = None

    def _observe(self, text: str, *, finalize: bool) -> list[ProgressObservation]:
        observations: list[ProgressObservation] = []
        for line in self._lines.feed(text, finalize=finalize):
            observation = self._observe_line(line)
            if observation is not None:
                observations.append(observation)
        return observations

    def _observe_line(self, line: str) -> ProgressObservation | None:
        stripped = line.strip()
        if self._completed or self._failed:
            return None

        restart_match = _ADIOS_RESTART_RE.fullmatch(stripped)
        if restart_match is not None:
            self._restart_step = int(restart_match.group("step"))
            self._last_step = self._restart_step
            return None

        adios_steps_match = _ADIOS_STEPS_RE.fullmatch(stripped)
        if adios_steps_match is not None:
            observed_total = int(adios_steps_match.group("steps"))
            if self.total_steps is not None and observed_total != self.total_steps:
                raise ValueError(
                    "Gray-Scott reported a step total that differs from its "
                    "JARVIS package configuration"
                )
            self.total_steps = observed_total
            if self._started:
                return None
            self._started = True
            return ProgressObservation(
                label="simulation",
                state=ProgressState.RUNNING,
                current=float(self._restart_step),
                total=float(observed_total),
                unit="timestep",
                message="Gray-Scott simulation started",
                metadata={
                    "application": "gray_scott",
                    "io_backend": "adios2",
                    "progress_kind": "simulation_timestep",
                    "completion_signal": "application_settings_reported",
                    "restart_step": self._restart_step,
                },
            )

        start_match = _START_RE.fullmatch(stripped)
        if start_match is not None:
            observed_total = int(start_match.group("steps"))
            if self.total_steps is not None and observed_total != self.total_steps:
                raise ValueError(
                    "Gray-Scott reported a step total that differs from its "
                    "JARVIS package configuration"
                )
            self.total_steps = observed_total
            if self._started:
                return None
            self._started = True
            return ProgressObservation(
                label="simulation",
                state=ProgressState.RUNNING,
                current=0.0,
                total=float(observed_total),
                unit="timestep",
                message="Gray-Scott simulation started",
                metadata={
                    "application": "gray_scott",
                    "progress_kind": "simulation_timestep",
                    "completion_signal": "application_started",
                    "grid_width": int(start_match.group("width")),
                    "grid_height": int(start_match.group("height")),
                    "mpi_ranks": int(start_match.group("ranks")),
                },
            )

        adios_output_match = _ADIOS_OUTPUT_RE.fullmatch(stripped)
        if adios_output_match is not None:
            step = int(adios_output_match.group("step"))
            if step <= self._last_step:
                return None
            if self.total_steps is not None and step > self.total_steps:
                raise ValueError("Gray-Scott output timestep exceeds configured total")
            self._last_step = step
            return ProgressObservation(
                label="simulation",
                state=ProgressState.RUNNING,
                current=float(step),
                total=(
                    float(self.total_steps) if self.total_steps is not None else None
                ),
                unit="timestep",
                message=f"Gray-Scott completed compute timestep {step}",
                metadata={
                    "application": "gray_scott",
                    "io_backend": "adios2",
                    "progress_kind": "simulation_timestep",
                    "completion_signal": "compute_step_completed",
                    "output_write_state": "started",
                    "output_step": int(adios_output_match.group("output_step")),
                },
            )

        output_match = _OUTPUT_RE.fullmatch(line)
        if output_match is not None:
            step = int(output_match.group("step"))
            if step <= self._last_step:
                return None
            if self.total_steps is not None and step > self.total_steps:
                raise ValueError("Gray-Scott output timestep exceeds configured total")
            self._last_step = step
            output_path = output_match.group("path")
            return ProgressObservation(
                label="simulation",
                state=ProgressState.RUNNING,
                current=float(step),
                total=(
                    float(self.total_steps) if self.total_steps is not None else None
                ),
                unit="timestep",
                message=f"Gray-Scott completed timestep {step}",
                metadata={
                    "application": "gray_scott",
                    "progress_kind": "simulation_timestep",
                    "completion_signal": "hdf5_write_returned",
                    "output_path": output_path,
                    "output_format": "hdf5",
                },
            )

        adios_completed_match = _ADIOS_COMPLETED_RE.fullmatch(stripped)
        if adios_completed_match is not None:
            self._remember_completion(
                "writer_closed_and_timing_reported",
                elapsed_milliseconds=int(adios_completed_match.group("elapsed_ms")),
            )
            return None
        if stripped == "Done.":
            self._remember_completion("application_reported_done")
        return None

    def _remember_completion(
        self,
        completion_signal: str,
        *,
        elapsed_milliseconds: int | None = None,
    ) -> None:
        """Hold an application success marker until process exit is known."""
        if self._pending_completion_signal is not None:
            return
        self._pending_completion_signal = completion_signal
        self._pending_elapsed_milliseconds = elapsed_milliseconds

    def _pending_completed_observation(self) -> ProgressObservation | None:
        """Commit a deferred application success marker for legacy callers."""
        if self._pending_completion_signal is None:
            return None
        return self._completed_observation(
            completion_signal=self._pending_completion_signal,
            elapsed_milliseconds=self._pending_elapsed_milliseconds,
        )

    def _completed_observation(
        self,
        *,
        completion_signal: str,
        elapsed_milliseconds: int | None = None,
    ) -> ProgressObservation | None:
        """Return one terminal observation from a real application signal."""
        if self._completed or self._failed:
            return None
        self._completed = True
        current = self.total_steps if self.total_steps is not None else self._last_step
        metadata: dict[str, str | int] = {
            "application": "gray_scott",
            "progress_kind": "simulation_timestep",
            "completion_signal": completion_signal,
        }
        if elapsed_milliseconds is not None:
            metadata["io_backend"] = "adios2"
            metadata["elapsed_milliseconds"] = elapsed_milliseconds
        return ProgressObservation(
            label="simulation",
            state=ProgressState.COMPLETED,
            current=float(current),
            total=(float(self.total_steps) if self.total_steps is not None else None),
            unit="timestep",
            message="Gray-Scott simulation completed",
            metadata=metadata,
        )

    def _failed_observation(self, return_code: int) -> ProgressObservation | None:
        """Return one terminal failure from an authoritative process status."""
        if self._completed or self._failed:
            return None
        self._failed = True
        metadata: dict[str, str | int] = {
            "application": "gray_scott",
            "progress_kind": "simulation_timestep",
            "completion_signal": "process_exit_nonzero",
            "return_code": return_code,
        }
        if self._pending_completion_signal is not None:
            metadata["application_completion_signal"] = self._pending_completion_signal
        return ProgressObservation(
            label="simulation",
            state=ProgressState.FAILED,
            current=float(self._last_step),
            total=(float(self.total_steps) if self.total_steps is not None else None),
            unit="timestep",
            message=f"Gray-Scott simulation failed with exit status {return_code}",
            metadata=metadata,
        )


def adapter_from_package(
    package: dict[str, Any],
) -> GrayScottProgressAdapter | None:
    """Create progress semantics for the builtin Gray-Scott package."""
    if package.get("pkg_type") != "builtin.gray_scott":
        return None
    return GrayScottProgressAdapter(
        package_id=str(package.get("pkg_id") or "gray_scott"),
        total_steps=_positive_int(package.get("steps")),
        output_every=_positive_int(package.get("out_every")),
        output_dir=_optional_path(package.get("outdir")),
    )


def _positive_int(value: object) -> int | None:
    """Return a finite positive integer without accepting booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or parsed <= 0 or not parsed.is_integer():
        return None
    return int(parsed)


def _optional_path(value: object) -> Path | None:
    """Return a configured non-empty output path when one is present."""
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value)
