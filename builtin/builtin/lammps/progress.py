"""LAMMPS progress semantics owned beside the builtin package launcher."""

from __future__ import annotations

import math
import os
import statistics
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, cast

from jarvis_cd.progress.provider import ProgressObservation
from jarvis_cd.progress.schema import JsonValue, ProgressState


@dataclass
class LammpsThermoProgressAdapter:
    """Parse live LAMMPS thermo records under the JARVIS package boundary."""

    package_name: str = "builtin.lammps"
    package_id: str = "lammps"
    package_version: str = "builtin"
    run_id: str = ""
    adapter_name: str = "lammps"
    application_profile: str | None = "jarvis-cd.builtin.lammps"
    log_visibility: str = "scoped_stdout"
    total_steps: float | None = None
    output_dir: Path | None = None
    warmup_samples: int = 2
    sample_window: int = 8
    active_columns: list[str] = field(default_factory=list)
    active_step_column: int | None = None
    active_time_column: int | None = None
    active_time_column_name: str | None = None
    samples: list[tuple[float, float]] = field(default_factory=list)
    last_step: float | None = None
    completed_steps: float = 0.0
    active_run_steps: float | None = None
    active_run_start_step: float | None = None
    active_package_stdout: bool = False
    authoritative_source: str | None = None
    stdout_fragment: str = ""
    jarvis_stdout_fragment: str = ""
    last_emitted_key: tuple[float, float, float, float | None] | None = None

    def observe_progress(self, text: str) -> list[ProgressObservation]:
        """Interpret application stdout for the JARVIS-owned typed SPI."""
        return [_record_to_observation(record) for record in self.observe_stdout(text)]

    def finalize_progress(self) -> list[ProgressObservation]:
        """Flush the final application-output fragment for JARVIS core."""
        return [_record_to_observation(record) for record in self.finalize_stdout()]

    def reset_progress(self) -> None:
        """Reset the JARVIS-owned application stream parser."""
        self.reset_stdout()

    def observe_stdout(self, text: str) -> list[dict[str, object]]:
        """Extract progress from an already trusted package-owned log."""
        return self._observe_stdout(text, finalize=False)

    def finalize_stdout(self) -> list[dict[str, object]]:
        """Flush the final package-log fragment at end of stream."""
        return self._observe_stdout("", finalize=True)

    def reset_stdout(self) -> None:
        """Reset package-log parser state after file replacement or truncation."""
        if self.authoritative_source not in (None, "package_log"):
            return
        self.authoritative_source = None
        self.stdout_fragment = ""
        self.active_columns = []
        self.active_step_column = None
        self.active_time_column = None
        self.active_time_column_name = None
        self.samples = []
        self.last_step = None
        self.completed_steps = 0.0
        self.active_run_steps = None
        self.active_run_start_step = None
        self.last_emitted_key = None

    def _observe_stdout(
        self,
        text: str,
        *,
        finalize: bool,
    ) -> list[dict[str, object]]:
        if self.authoritative_source == "jarvis_stdout":
            if finalize:
                self.stdout_fragment = ""
            return []
        if text and self.authoritative_source is None:
            self.authoritative_source = "package_log"
        records: list[dict[str, object]] = []
        for line in self._complete_lines(
            text,
            fragment_name="stdout_fragment",
            finalize=finalize,
        ):
            record = self.observe_line(line)
            if record is not None:
                records.append(record)
        return records

    def observe_jarvis_stdout(self, text: str) -> list[dict[str, object]]:
        """Extract records only while JARVIS identifies ``builtin.lammps``."""
        return self._observe_jarvis_stdout(text, finalize=False)

    def finalize_jarvis_stdout(self) -> list[dict[str, object]]:
        """Flush the final JARVIS-scoped fragment at end of stream."""
        return self._observe_jarvis_stdout("", finalize=True)

    def _observe_jarvis_stdout(
        self,
        text: str,
        *,
        finalize: bool,
    ) -> list[dict[str, object]]:
        if self.authoritative_source == "package_log":
            if finalize:
                self.jarvis_stdout_fragment = ""
                self.active_package_stdout = False
            return []
        records: list[dict[str, object]] = []
        for line in self._complete_lines(
            text,
            fragment_name="jarvis_stdout_fragment",
            finalize=finalize,
        ):
            stripped = line.strip()
            if stripped == f"[{self.package_name}] [START] BEGIN":
                if self.authoritative_source is None:
                    self.authoritative_source = "jarvis_stdout"
                self.active_package_stdout = True
                continue
            if stripped == f"[{self.package_name}] [START] END":
                self.active_package_stdout = False
                continue
            if not self.active_package_stdout:
                continue
            record = self.observe_line(line)
            if record is not None:
                records.append(record)
        if finalize:
            self.active_package_stdout = False
        return records

    def _complete_lines(
        self,
        text: str,
        *,
        fragment_name: str,
        finalize: bool,
    ) -> list[str]:
        fragment = cast(str, getattr(self, fragment_name))
        lines = (fragment + text).splitlines(keepends=True)
        if not finalize and lines and not lines[-1].endswith(("\n", "\r")):
            setattr(self, fragment_name, lines.pop())
        else:
            setattr(self, fragment_name, "")
        return [line.rstrip("\r\n") for line in lines]

    def progress_log_paths(self) -> list[Path]:
        """Return the LAMMPS thermo log owned by this package execution."""
        if self.output_dir is None:
            return []
        return [self.output_dir / "log.lammps"]

    def package_load_probe_python(self) -> str:
        """Return a probe that locates the installed builtin LAMMPS package."""
        return (
            "from pathlib import Path\n"
            "import jarvis_cd\n"
            "root = Path(jarvis_cd.__file__).resolve().parent.parent\n"
            "path = root / 'builtin' / 'builtin' / 'lammps' / 'pkg.py'\n"
            "if not path.is_file():\n"
            "    raise SystemExit(f'JARVIS builtin LAMMPS package missing: {path}')\n"
            "print(path)"
        )

    def acceptance_progress_valid(self, metadata: dict[str, Any]) -> bool:
        """Require observed LAMMPS timing rather than a claimed percentage."""
        eta = metadata.get("eta_seconds")
        absolute_step = metadata.get("absolute_step")
        eta_value = _finite_number(eta)
        absolute_step_value = _finite_number(absolute_step)
        return (
            metadata.get("adapter") == self.adapter_name
            and metadata.get("prediction_status") == "observed_lammps_timing"
            and metadata.get("timing_source") == "lammps_thermo_cpu"
            and eta_value is not None
            and eta_value >= 0
            and absolute_step_value is not None
            and absolute_step_value >= 0
        )

    def observe_line(self, line: str) -> dict[str, object] | None:
        """Extract one progress observation from a LAMMPS output line."""
        stripped = line.strip()
        if stripped == "":
            return None
        reset_step = _parse_reset_timestep(stripped)
        if reset_step is not None:
            self.active_run_start_step = reset_step
            self.last_step = reset_step
            return None
        run_command = _parse_run_command(stripped)
        if run_command is not None:
            run_steps, run_upto = run_command
            self.active_run_steps = None if run_upto else run_steps
            self.active_run_start_step = None
            return None
        if _looks_like_thermo_header(stripped):
            self.active_columns = stripped.split()
            self.active_step_column = self.active_columns.index("Step")
            self.active_time_column = None
            self.active_time_column_name = None
            for candidate in ("CPU", "Cpu", "cpu"):
                if candidate in self.active_columns:
                    self.active_time_column = self.active_columns.index(candidate)
                    self.active_time_column_name = candidate
                    break
            return None
        if stripped.startswith("Loop time of "):
            completed_steps = self.completed_steps
            if self.active_run_steps is not None:
                completed_steps += self.active_run_steps
            elif self.active_run_start_step is not None and self.last_step is not None:
                completed_steps += max(0.0, self.last_step - self.active_run_start_step)
            if math.isfinite(completed_steps):
                self.completed_steps = completed_steps
            self.active_run_steps = None
            self.active_run_start_step = None
            self.active_columns = []
            self.active_step_column = None
            self.active_time_column = None
            self.active_time_column_name = None
            return None
        if self.active_step_column is None:
            return None
        if (
            self.total_steps is not None
            and self.completed_steps >= self.total_steps
            and self.active_run_steps is None
        ):
            return None
        parts = stripped.split()
        if len(parts) != len(self.active_columns):
            return None
        step = _optional_float(parts[self.active_step_column])
        if step is None:
            return None
        if step < 0:
            return None
        elapsed_seconds = None
        if self.active_time_column is not None:
            elapsed_seconds = _optional_float(parts[self.active_time_column])
            if elapsed_seconds is not None and elapsed_seconds < 0:
                elapsed_seconds = None
        if self.active_run_start_step is None:
            self.active_run_start_step = step
        self.last_step = step
        current = self.completed_steps + max(0.0, step - self.active_run_start_step)
        if self.active_run_steps is not None:
            current = min(self.completed_steps + self.active_run_steps, current)
        if not math.isfinite(current):
            return None
        progress_key = (self.completed_steps, current, step, elapsed_seconds)
        if progress_key == self.last_emitted_key:
            return None
        self.last_emitted_key = progress_key
        if elapsed_seconds is not None:
            self.samples.append((current, elapsed_seconds))
            self.samples = self.samples[-self.sample_window :]
        prediction = self._prediction(current, elapsed_seconds=elapsed_seconds)
        return _drop_none(
            {
                "label": "timestep",
                "current": current,
                "total": self.total_steps,
                "unit": "step",
                "message": _lammps_message(current, self.total_steps),
                "metadata": {
                    "adapter": self.adapter_name,
                    "columns": self.active_columns,
                    "step_column": "Step",
                    "absolute_step": step,
                    "run_start_step": self.active_run_start_step,
                    "run_steps": self.active_run_steps,
                    "completed_prior_runs": self.completed_steps,
                    "timing_column": self.active_time_column_name,
                    **prediction,
                    "source": "jarvis_package",
                    "progress_source": self.authoritative_source,
                    "log_visibility": self.log_visibility,
                    "package_name": self.package_name,
                    "package_id": self.package_id,
                    "package_version": self.package_version,
                    "run_id": self.run_id,
                    "execution_id": self.run_id,
                },
            }
        )

    def _prediction(
        self,
        current_step: float,
        *,
        elapsed_seconds: float | None,
    ) -> dict[str, object]:
        if elapsed_seconds is None:
            return {
                "confidence": "timing_unavailable",
                "samples": len(self.samples),
                "prediction_status": "no_lammps_timing_column",
            }
        if self.total_steps is None or len(self.samples) <= self.warmup_samples:
            return {
                "confidence": "warming_up",
                "samples": len(self.samples),
                "prediction_status": "warming_up",
                "elapsed_seconds": elapsed_seconds,
            }
        rates: list[float] = []
        for (previous_step, previous_time), (step, timestamp) in zip(
            self.samples,
            self.samples[1:],
        ):
            step_delta = step - previous_step
            time_delta = timestamp - previous_time
            if step_delta <= 0 or time_delta <= 0:
                continue
            rate = time_delta / step_delta
            if math.isfinite(rate):
                rates.append(rate)
        if not rates:
            return {
                "confidence": "warming_up",
                "samples": len(self.samples),
                "prediction_status": "warming_up",
                "elapsed_seconds": elapsed_seconds,
            }
        ordered = sorted(rates)
        trimmed = ordered[1:-1] if len(ordered) > 2 else ordered
        seconds_per_step = statistics.mean(trimmed)
        remaining_steps = max(0.0, self.total_steps - current_step)
        eta_seconds = remaining_steps * seconds_per_step
        if not all(
            math.isfinite(value)
            for value in (seconds_per_step, remaining_steps, eta_seconds)
        ):
            return {
                "confidence": "timing_unavailable",
                "samples": len(self.samples),
                "prediction_status": "nonfinite_prediction_rejected",
                "elapsed_seconds": elapsed_seconds,
            }
        return {
            "prediction_method": "trimmed_mean_step_time_after_warmup",
            "rate_samples": len(rates),
            "trimmed_rate_samples": len(trimmed),
            "min_seconds_per_step": min(trimmed),
            "max_seconds_per_step": max(trimmed),
            "seconds_per_step": seconds_per_step,
            "eta_seconds": eta_seconds,
            "elapsed_seconds": elapsed_seconds,
            "remaining_steps": remaining_steps,
            "samples": len(self.samples),
            "prediction_status": "observed_lammps_timing",
            "timing_source": "lammps_thermo_cpu",
            "confidence": "observed" if len(rates) >= 2 else "low_sample",
        }


def adapter_from_package(
    package: dict[str, Any],
) -> LammpsThermoProgressAdapter | None:
    """Create the provider only for the builtin JARVIS LAMMPS package."""
    package_type = package.get("pkg_type")
    if package_type != "builtin.lammps":
        return None
    output = package.get("out")
    deploy_mode = package.get("effective_deploy_mode") or package.get("deploy_mode")
    progress = package.get("progress")
    shared_log = _nested_progress_value(progress, "log_visibility") == "shared"
    output_dir = (
        Path(os.path.expandvars(output))
        if deploy_mode != "container"
        and shared_log
        and isinstance(output, str)
        and output.strip()
        else None
    )
    return LammpsThermoProgressAdapter(
        package_name=str(package_type),
        package_id=str(package.get("pkg_id") or "lammps"),
        package_version=_distribution_version(),
        log_visibility="shared" if output_dir is not None else "scoped_stdout",
        total_steps=_optional_float(
            package.get("total_steps")
            or package.get("steps")
            or (
                package.get("io_run_steps")
                if _optional_float(package.get("io_dump_interval"))
                else None
            )
            or _nested_progress_total(progress)
        ),
        output_dir=output_dir,
    )


def _nested_progress_total(value: object) -> object:
    return _nested_progress_value(value, "total_steps") or _nested_progress_value(
        value, "total"
    )


def _nested_progress_value(value: object, key: str) -> object:
    if not isinstance(value, dict):
        return None
    typed = cast("dict[str, object]", value)
    return typed.get(key)


def _distribution_version() -> str:
    try:
        return version("jarvis_cd")
    except PackageNotFoundError:
        return "source-checkout"


def _optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _finite_number(value)
    if isinstance(value, str) and value != "":
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _looks_like_thermo_header(line: str) -> bool:
    columns = line.split()
    return "Step" in columns and len(columns) >= 2


def _parse_run_command(line: str) -> tuple[float, bool] | None:
    parts = line.split()
    if len(parts) < 2 or parts[0] != "run":
        return None
    run_value = _optional_float(parts[1])
    if run_value is None or run_value < 0:
        return None
    return run_value, "upto" in parts[2:]


def _parse_reset_timestep(line: str) -> float | None:
    parts = line.split()
    if len(parts) < 2 or parts[0] != "reset_timestep":
        return None
    return _optional_float(parts[1])


def _lammps_message(step: float, total_steps: float | None) -> str:
    if total_steps is None:
        return f"LAMMPS step {int(step)}"
    return f"LAMMPS step {int(step)} of {int(total_steps)}"


def _drop_none(value: dict[str, object | None]) -> dict[str, object]:
    return {key: item for key, item in value.items() if item is not None}


def _record_to_observation(record: dict[str, object]) -> ProgressObservation:
    """Project the legacy relay record into the typed JARVIS core contract."""
    metadata = record.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("LAMMPS progress metadata must be an object")
    state_value = metadata.get("progress_state", ProgressState.RUNNING.value)
    try:
        state = ProgressState(str(state_value))
    except ValueError as exc:
        raise ValueError(f"invalid LAMMPS progress state: {state_value!r}") from exc
    label = record.get("label")
    if not isinstance(label, str) or not label:
        raise ValueError("LAMMPS progress requires a non-empty label")
    return ProgressObservation(
        label=label,
        state=state,
        current=cast(float | None, record.get("current")),
        total=cast(float | None, record.get("total")),
        unit=cast(str | None, record.get("unit")),
        message=cast(str | None, record.get("message")),
        metadata=cast(dict[str, JsonValue], metadata),
    )
