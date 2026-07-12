"""Dependency-free reporter for package scripts and ParaView ``pvbatch``."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TextIO

from .schema import JsonValue, ProgressEvent, ProgressState
from .store import ProgressStore

PROGRESS_LINE_PREFIX = "JARVIS_PROGRESS "
PROGRESS_PATH_ENV = "JARVIS_PROGRESS_PATH"
PROGRESS_TRANSPORT_ENV = "JARVIS_PROGRESS_TRANSPORT"
EXECUTION_ID_ENV = "JARVIS_EXECUTION_ID"
PACKAGE_NAME_ENV = "JARVIS_PACKAGE_NAME"
PACKAGE_ID_ENV = "JARVIS_PACKAGE_ID"


class ProgressReporter:
    """Emit structured progress to stdout or a durable JSONL sidecar.

    This JARVIS-side module uses only the Python standard library. Applications
    with an older embedded Python may emit the same line schema independently.
    """

    def __init__(
        self,
        *,
        package_name: str | None = None,
        package_id: str | None = None,
        execution_id: str | None = None,
        path: str | os.PathLike[str] | None = None,
        stream: TextIO | None = None,
    ) -> None:
        self.package_name = package_name or os.environ.get(PACKAGE_NAME_ENV, "")
        self.package_id = package_id or os.environ.get(PACKAGE_ID_ENV, "")
        self.execution_id = execution_id or os.environ.get(EXECUTION_ID_ENV, "")
        configured_path = (
            path if path is not None else os.environ.get(PROGRESS_PATH_ENV)
        )
        configured_transport = (
            None if path is not None else os.environ.get(PROGRESS_TRANSPORT_ENV)
        )
        if configured_transport not in {None, "sidecar", "stdout"}:
            raise ValueError("JARVIS_PROGRESS_TRANSPORT must be 'sidecar' or 'stdout'")
        if configured_transport == "sidecar" and not configured_path:
            raise ValueError("sidecar progress transport requires JARVIS_PROGRESS_PATH")
        self.transport = configured_transport or (
            "sidecar" if configured_path else "stdout"
        )
        self.path = (
            Path(configured_path)
            if configured_path and self.transport == "sidecar"
            else None
        )
        self.stream = stream or sys.stdout
        latest = ProgressStore(self.path).latest() if self.path is not None else None
        if latest is not None and (
            latest.execution_id,
            latest.package_name,
            latest.package_id,
        ) != (self.execution_id, self.package_name, self.package_id):
            raise ValueError(
                "existing progress store identity does not match this reporter"
            )
        self.sequence = latest.sequence if latest is not None else 0

    def emit(
        self,
        *,
        label: str,
        state: ProgressState = ProgressState.RUNNING,
        current: float | None = None,
        total: float | None = None,
        unit: str | None = None,
        message: str | None = None,
        metadata: dict[str, JsonValue] | None = None,
    ) -> ProgressEvent:
        """Validate and emit one progress event, returning the exact event."""
        self.sequence += 1
        event = ProgressEvent(
            package_name=self.package_name,
            package_id=self.package_id,
            execution_id=self.execution_id,
            label=label,
            state=state,
            current=current,
            total=total,
            unit=unit,
            message=message,
            sequence=self.sequence,
            metadata=metadata or {},
        )
        if self.path is not None:
            ProgressStore(self.path).append(event)
        else:
            self.stream.write(f"{PROGRESS_LINE_PREFIX}{event.to_json()}\n")
            self.stream.flush()
        return event


def event_from_progress_line(line: str) -> ProgressEvent | None:
    """Parse one structured stdout or raw JSONL sidecar line."""
    stripped = line.strip()
    if not stripped:
        return None
    if stripped.startswith(PROGRESS_LINE_PREFIX):
        stripped = stripped[len(PROGRESS_LINE_PREFIX) :]
    elif not stripped.startswith("{"):
        return None
    return ProgressEvent.from_json(stripped)
