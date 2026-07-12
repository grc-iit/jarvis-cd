"""Truthful ParaView progress semantics for pvbatch and pvserver."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, cast

from jarvis_cd.progress import (
    LineBuffer,
    PackageScopeFilter,
    ProgressEvent,
    ProgressObservation,
    ProgressState,
    event_from_progress_line,
)
from jarvis_cd.progress.schema import JsonValue

from .progress_reporter import ParaViewProgressReporter

__all__ = [
    "ParaViewProgressAdapter",
    "ParaViewProgressReporter",
    "adapter_from_package",
]


@dataclass
class ParaViewProgressAdapter:
    """Parse package-scoped structured pvbatch events and pvserver readiness."""

    package_name: str = "builtin.paraview"
    package_id: str = "paraview"
    package_version: str = "builtin"
    run_id: str = ""
    adapter_name: str = "paraview"
    application_profile: str | None = "jarvis-cd.builtin.paraview"
    output_dir: Path | None = None
    progress_path: Path | None = None
    authoritative_source: str | None = None
    _stdout: LineBuffer = field(default_factory=LineBuffer)
    _jarvis_stdout: LineBuffer = field(default_factory=LineBuffer)
    _scope: PackageScopeFilter = field(init=False)
    _readiness_sequence: int = 0
    _last_sequence: int = 0

    def __post_init__(self) -> None:
        self._scope = PackageScopeFilter(self.package_name)

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
        """Read a trusted package JSONL sidecar or stdout stream."""
        return self._observe_package_stream(text, finalize=False)

    def finalize_stdout(self) -> list[dict[str, object]]:
        """Flush an unterminated package-owned progress line."""
        return self._observe_package_stream("", finalize=True)

    def reset_stdout(self) -> None:
        """Reset state after a sidecar replacement or truncation."""
        if self.authoritative_source not in (None, "package_log"):
            return
        self.authoritative_source = None
        self._stdout.reset()
        self._last_sequence = 0

    def observe_jarvis_stdout(self, text: str) -> list[dict[str, object]]:
        """Read only output inside this ParaView package's JARVIS markers."""
        return self._observe_jarvis_stream(text, finalize=False)

    def finalize_jarvis_stdout(self) -> list[dict[str, object]]:
        """Flush a final JARVIS-scoped line and close its scope."""
        return self._observe_jarvis_stream("", finalize=True)

    def progress_log_paths(self) -> list[Path]:
        """Return only an explicitly shared package progress sidecar."""
        return [self.progress_path] if self.progress_path is not None else []

    def package_load_probe_python(self) -> str:
        """Return a probe for the installed builtin ParaView package provider."""
        return (
            "from pathlib import Path\n"
            "import jarvis_cd\n"
            "root = Path(jarvis_cd.__file__).resolve().parent.parent\n"
            "path = root / 'builtin' / 'builtin' / 'paraview' / 'progress.py'\n"
            "if not path.is_file():\n"
            "    raise SystemExit(f'JARVIS builtin ParaView progress missing: {path}')\n"
            "print(path)"
        )

    def acceptance_progress_valid(self, metadata: dict[str, Any]) -> bool:
        """Accept only real completed ParaView units or observed server readiness."""
        if metadata.get("adapter") != self.adapter_name:
            return False
        kind = metadata.get("progress_kind")
        if kind == "pvserver_ready":
            return (
                metadata.get("readiness_signal")
                in {
                    "instrumented_server_ready",
                    "pvserver_accepting_connections",
                    "pvserver_waiting_for_client",
                }
                and metadata.get("determinate") is False
            )
        if kind != "pvbatch_completed_unit":
            return False
        current = _finite_number(metadata.get("completed_units"))
        total = _finite_number(metadata.get("total_units"))
        if current is None or current < 1:
            return False
        if total is not None and (total <= 0 or current > total):
            return False
        unit = metadata.get("unit")
        if unit == "frame":
            completion_valid = (
                metadata.get("completion_signal") == "render_returned"
                and metadata.get("completed_after_render") is True
            )
        elif unit == "timestep":
            completion_valid = (
                metadata.get("completion_signal") == "pipeline_update_returned"
                and metadata.get("completed_after_update") is True
            )
        else:
            return False
        return (
            metadata.get("renderer") == "paraview"
            and completion_valid
            and metadata.get("determinate") is (total is not None)
        )

    def _observe_package_stream(
        self, text: str, *, finalize: bool
    ) -> list[dict[str, object]]:
        if self.authoritative_source == "jarvis_stdout":
            if finalize:
                self._stdout.reset()
            return []
        if text and self.authoritative_source is None:
            self.authoritative_source = "package_log"
        records: list[dict[str, object]] = []
        for line in self._stdout.feed(text, finalize=finalize):
            record = self._observe_line(line)
            if record is not None:
                records.append(record)
        return records

    def _observe_jarvis_stream(
        self, text: str, *, finalize: bool
    ) -> list[dict[str, object]]:
        if self.authoritative_source == "package_log":
            if finalize:
                self._jarvis_stdout.reset()
                self._scope.reset()
            return []
        records: list[dict[str, object]] = []
        for line in self._jarvis_stdout.feed(text, finalize=finalize):
            was_active = self._scope.active
            scoped = self._scope.observe(line)
            if (
                not was_active
                and self._scope.active
                and self.authoritative_source is None
            ):
                self.authoritative_source = "jarvis_stdout"
            if scoped is None:
                continue
            record = self._observe_line(scoped)
            if record is not None:
                records.append(record)
        if finalize:
            self._scope.reset()
        return records

    def _observe_line(self, line: str) -> dict[str, object] | None:
        lowered = line.casefold()
        readiness_signal = None
        if "accepting connection(s)" in lowered:
            readiness_signal = "pvserver_accepting_connections"
        elif "waiting for client" in lowered:
            readiness_signal = "pvserver_waiting_for_client"
        if readiness_signal is not None:
            if not self.run_id:
                raise ValueError("ParaView readiness requires a JARVIS execution ID")
            self._readiness_sequence += 1
            readiness_message = (
                "ParaView server is accepting client connections"
                if readiness_signal == "pvserver_accepting_connections"
                else "ParaView server is waiting for a client"
            )
            event = ProgressEvent(
                package_name=self.package_name,
                package_id=self.package_id,
                execution_id=self.run_id,
                label="pvserver",
                state=ProgressState.READY,
                sequence=self._readiness_sequence,
                message=readiness_message,
                metadata={
                    "progress_kind": "pvserver_ready",
                    "renderer": "paraview",
                    "readiness_signal": readiness_signal,
                },
            )
            return self._adapter_record(event)
        structured_event = event_from_progress_line(line)
        if structured_event is None:
            return None
        if structured_event.package_name != self.package_name:
            raise ValueError(
                "ParaView progress package identity does not match provider"
            )
        if structured_event.package_id != self.package_id:
            raise ValueError("ParaView progress package ID does not match provider")
        if self.run_id and structured_event.execution_id != self.run_id:
            raise ValueError(
                "ParaView progress execution identity does not match provider"
            )
        if structured_event.sequence <= self._last_sequence:
            raise ValueError("ParaView progress sequences must increase strictly")
        self._last_sequence = structured_event.sequence
        return self._adapter_record(structured_event)

    def _adapter_record(self, event: ProgressEvent) -> dict[str, object]:
        if event.label not in {"frame", "timestep", "pvserver"}:
            raise ValueError(f"unsupported ParaView progress label: {event.label!r}")
        if event.label == "pvserver":
            if event.state is not ProgressState.READY or event.determinate:
                raise ValueError("pvserver readiness must be indeterminate and ready")
        else:
            if event.current is None or event.current < 1:
                raise ValueError("pvbatch progress requires a completed unit")
            if event.unit != event.label:
                raise ValueError("pvbatch progress unit must match its label")
        record = event.as_adapter_record()
        metadata = cast(dict[str, object], record["metadata"])
        metadata.update(
            {
                "adapter": self.adapter_name,
                "source": "jarvis_package",
                "package_name": self.package_name,
                "package_id": self.package_id,
                "package_version": self.package_version,
                "run_id": self.run_id or event.execution_id,
                "execution_id": self.run_id or event.execution_id,
                "progress_source": self.authoritative_source,
                "completed_units": event.current,
                "total_units": event.total,
                "unit": event.unit,
            }
        )
        if event.label == "pvserver":
            record["current"] = 0.0
            metadata["compatibility_projection"] = "indeterminate_state_ordinal"
        return record


def adapter_from_package(
    package: dict[str, Any],
) -> ParaViewProgressAdapter | None:
    """Create a provider only for the generic builtin ParaView package."""
    if package.get("pkg_type") != "builtin.paraview":
        return None
    return ParaViewProgressAdapter(
        package_id=str(package.get("pkg_id") or "paraview"),
        package_version=_distribution_version(),
    )


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _distribution_version() -> str:
    try:
        return version("jarvis_cd")
    except PackageNotFoundError:
        return "source-checkout"


def _record_to_observation(record: dict[str, object]) -> ProgressObservation:
    """Project the legacy relay record into the typed JARVIS core contract."""
    metadata = record.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("ParaView progress metadata must be an object")
    state_value = metadata.get("progress_state", ProgressState.RUNNING.value)
    try:
        state = ProgressState(str(state_value))
    except ValueError as exc:
        raise ValueError(f"invalid ParaView progress state: {state_value!r}") from exc
    label = record.get("label")
    if not isinstance(label, str) or not label:
        raise ValueError("ParaView progress requires a non-empty label")
    return ProgressObservation(
        label=label,
        state=state,
        current=cast(float | None, record.get("current")),
        total=cast(float | None, record.get("total")),
        unit=cast(str | None, record.get("unit")),
        message=cast(str | None, record.get("message")),
        metadata=cast(dict[str, JsonValue], metadata),
    )
