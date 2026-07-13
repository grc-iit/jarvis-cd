"""Progress semantics owned by the builtin ADIOS2 Gray-Scott package."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis_cd.progress import LineBuffer, ProgressObservation, ProgressState

_RESTART_RE = re.compile(r"^restart:\s+from step\s+(?P<step>\d+)\s*$")
_STEPS_RE = re.compile(r"^steps:\s+(?P<steps>\d+)\s*$")
_OUTPUT_RE = re.compile(
    r"^Simulation at step\s+(?P<step>\d+)\s+"
    r"writing output step\s+(?P<output_step>\d+)\s*$"
)
_COMPLETED_RE = re.compile(
    r"^Rank\s+0\s+-\s+ET\s+(?P<elapsed_ms>\d+)\s+-\s+milliseconds\s*$"
)


@dataclass
class Adios2GrayScottProgressAdapter:
    """Interpret ADIOS2 Gray-Scott simulation lifecycle output."""

    package_name: str = "builtin.adios2_gray_scott"
    package_id: str = "adios2_gray_scott"
    total_steps: int | None = None
    output_every: int | None = None
    output_path: Path | None = None
    checkpoint_path: Path | None = None
    _lines: LineBuffer = field(default_factory=LineBuffer)
    _restart_step: int = 0
    _last_step: int = 0
    _started: bool = False
    _completed: bool = False
    _failed: bool = False
    _pending_elapsed_milliseconds: int | None = None

    def observe_progress(self, text: str) -> list[ProgressObservation]:
        """Return progress based on native Gray-Scott lifecycle messages."""
        return self._observe(text, finalize=False)

    def finalize_progress(self) -> list[ProgressObservation]:
        """Flush one final unterminated Gray-Scott output line."""
        observations = self._observe("", finalize=True)
        completed = self._pending_completed_observation()
        if completed is not None:
            observations.append(completed)
        return observations

    def finalize_progress_for_exit(self, return_code: int) -> list[ProgressObservation]:
        """Finalize progress from the authoritative producer process status."""
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
        self._restart_step = 0
        self._last_step = 0
        self._started = False
        self._completed = False
        self._failed = False
        self._pending_elapsed_milliseconds = None

    def _observe(self, text: str, *, finalize: bool) -> list[ProgressObservation]:
        observations: list[ProgressObservation] = []
        for line in self._lines.feed(text, finalize=finalize):
            observation = self._observe_line(line.strip())
            if observation is not None:
                observations.append(observation)
        return observations

    def _observe_line(self, line: str) -> ProgressObservation | None:
        if self._completed or self._failed:
            return None

        restart_match = _RESTART_RE.fullmatch(line)
        if restart_match is not None:
            self._restart_step = int(restart_match.group("step"))
            self._last_step = self._restart_step
            return None

        steps_match = _STEPS_RE.fullmatch(line)
        if steps_match is not None:
            observed_total = int(steps_match.group("steps"))
            if self.total_steps is not None and observed_total != self.total_steps:
                raise ValueError(
                    "ADIOS2 Gray-Scott reported a step total that differs from "
                    "its JARVIS package configuration"
                )
            if self._restart_step > observed_total:
                raise ValueError("Gray-Scott restart timestep exceeds configured total")
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
                message="ADIOS2 Gray-Scott simulation started",
                metadata={
                    "application": "gray_scott",
                    "io_backend": "adios2",
                    "progress_kind": "simulation_timestep",
                    "completion_signal": "application_settings_reported",
                    "restart_step": self._restart_step,
                },
            )

        output_match = _OUTPUT_RE.fullmatch(line)
        if output_match is not None:
            step = int(output_match.group("step"))
            output_step = int(output_match.group("output_step"))
            if step <= self._last_step:
                return None
            if self.total_steps is not None and step > self.total_steps:
                raise ValueError("Gray-Scott output timestep exceeds configured total")
            if (
                self.output_every is not None
                and step // self.output_every != output_step
            ):
                raise ValueError("Gray-Scott output sequence contradicts plotgap")
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
                    "output_step": output_step,
                },
            )

        completed_match = _COMPLETED_RE.fullmatch(line)
        if completed_match is not None:
            self._pending_elapsed_milliseconds = int(
                completed_match.group("elapsed_ms")
            )
        return None

    def _pending_completed_observation(self) -> ProgressObservation | None:
        """Commit a deferred writer-close marker for legacy callers."""
        if self._pending_elapsed_milliseconds is None:
            return None
        return self._completed_observation(
            completion_signal="writer_closed_and_timing_reported",
            elapsed_milliseconds=self._pending_elapsed_milliseconds,
        )

    def _completed_observation(
        self,
        *,
        completion_signal: str,
        elapsed_milliseconds: int | None = None,
    ) -> ProgressObservation | None:
        """Return one terminal success after its process status is accepted."""
        if self._completed or self._failed:
            return None
        self._completed = True
        current = self.total_steps if self.total_steps is not None else self._last_step
        metadata: dict[str, str | int] = {
            "application": "gray_scott",
            "io_backend": "adios2",
            "progress_kind": "simulation_timestep",
            "completion_signal": completion_signal,
        }
        if elapsed_milliseconds is not None:
            metadata["elapsed_milliseconds"] = elapsed_milliseconds
        return ProgressObservation(
            label="simulation",
            state=ProgressState.COMPLETED,
            current=float(current),
            total=(float(self.total_steps) if self.total_steps is not None else None),
            unit="timestep",
            message="ADIOS2 Gray-Scott simulation completed",
            metadata=metadata,
        )

    def _failed_observation(self, return_code: int) -> ProgressObservation | None:
        """Return one terminal failure from an authoritative process status."""
        if self._completed or self._failed:
            return None
        self._failed = True
        metadata: dict[str, str | int] = {
            "application": "gray_scott",
            "io_backend": "adios2",
            "progress_kind": "simulation_timestep",
            "completion_signal": "process_exit_nonzero",
            "return_code": return_code,
        }
        if self._pending_elapsed_milliseconds is not None:
            metadata["application_completion_signal"] = (
                "writer_closed_and_timing_reported"
            )
            metadata["elapsed_milliseconds"] = self._pending_elapsed_milliseconds
        return ProgressObservation(
            label="simulation",
            state=ProgressState.FAILED,
            current=float(self._last_step),
            total=(float(self.total_steps) if self.total_steps is not None else None),
            unit="timestep",
            message=(
                f"ADIOS2 Gray-Scott simulation failed with exit status {return_code}"
            ),
            metadata=metadata,
        )


def adapter_from_package(
    package: dict[str, Any],
) -> Adios2GrayScottProgressAdapter | None:
    """Create progress semantics for the builtin ADIOS2 Gray-Scott package."""
    if package.get("pkg_type") != "builtin.adios2_gray_scott":
        return None
    return Adios2GrayScottProgressAdapter(
        package_id=str(package.get("pkg_id") or "adios2_gray_scott"),
        total_steps=_positive_int(package.get("steps")),
        output_every=_positive_int(package.get("plotgap")),
        output_path=_optional_path(package.get("out_file")),
        checkpoint_path=_optional_path(package.get("checkpoint_output")),
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
    """Return a configured non-empty artifact path when one is present."""
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value)
