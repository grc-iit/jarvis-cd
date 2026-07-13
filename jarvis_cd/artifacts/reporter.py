"""Dependency-free reporter for generated JARVIS artifacts."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TextIO

from .schema import (
    JsonValue,
    ArtifactEvent,
    ArtifactLocation,
    ArtifactOwnership,
    ArtifactRole,
    ArtifactState,
    ArtifactStructure,
    new_artifact_id,
    validate_artifact_history,
)
from .store import ArtifactStore

ARTIFACT_LINE_PREFIX = "JARVIS_ARTIFACT "
ARTIFACT_PATH_ENV = "JARVIS_ARTIFACT_PATH"
ARTIFACT_TRANSPORT_ENV = "JARVIS_ARTIFACT_TRANSPORT"
EXECUTION_ID_ENV = "JARVIS_EXECUTION_ID"
PACKAGE_NAME_ENV = "JARVIS_PACKAGE_NAME"
PACKAGE_ID_ENV = "JARVIS_PACKAGE_ID"


class ArtifactReporter:
    """Emit structured artifact lifecycle events to stdout or owned JSONL."""

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
            path if path is not None else os.environ.get(ARTIFACT_PATH_ENV)
        )
        configured_transport = (
            None if path is not None else os.environ.get(ARTIFACT_TRANSPORT_ENV)
        )
        if configured_transport not in {None, "sidecar", "stdout"}:
            raise ValueError("JARVIS_ARTIFACT_TRANSPORT must be 'sidecar' or 'stdout'")
        if configured_transport == "sidecar" and not configured_path:
            raise ValueError("sidecar artifact transport requires JARVIS_ARTIFACT_PATH")
        self.transport = configured_transport or (
            "sidecar" if configured_path else "stdout"
        )
        self.path = (
            Path(configured_path)
            if configured_path and self.transport == "sidecar"
            else None
        )
        self.stream = stream or sys.stdout
        events = ArtifactStore(self.path).read_all() if self.path is not None else []
        if events and (
            events[0].execution_id,
            events[0].package_name,
            events[0].package_id,
        ) != (self.execution_id, self.package_name, self.package_id):
            raise ValueError(
                "existing artifact store identity does not match this reporter"
            )
        self._events = list(events)
        self._current: dict[str, ArtifactEvent] = {}
        for event in events:
            self._current[event.artifact_id] = event
        self.sequence = events[-1].sequence if events else 0

    def emit(
        self,
        *,
        logical_name: str,
        kind: str,
        role: ArtifactRole,
        structure: ArtifactStructure,
        ownership: ArtifactOwnership,
        state: ArtifactState = ArtifactState.AVAILABLE,
        artifact_id: str | None = None,
        location: ArtifactLocation | str | None = None,
        media_type: str | None = None,
        format: str | None = None,
        size_bytes: int | None = None,
        checksum: str | None = None,
        message: str | None = None,
        metadata: dict[str, JsonValue] | None = None,
    ) -> ArtifactEvent:
        """Validate and emit one artifact event, returning the exact event."""

        def build_event(history: tuple[ArtifactEvent, ...]) -> ArtifactEvent:
            if history and (
                history[0].execution_id,
                history[0].package_name,
                history[0].package_id,
            ) != (self.execution_id, self.package_name, self.package_id):
                raise ValueError(
                    "existing artifact store identity does not match this reporter"
                )
            current = {event.artifact_id: event for event in history}
            resolved_artifact_id = artifact_id
            if resolved_artifact_id is None:
                resolved_artifact_id = new_artifact_id()
                while resolved_artifact_id in current:
                    resolved_artifact_id = new_artifact_id()
            previous = current.get(resolved_artifact_id)
            typed_location = (
                ArtifactLocation.execution_relative(location)
                if isinstance(location, str)
                else location
            )
            resolved_media_type = media_type
            resolved_format = format
            resolved_size = size_bytes
            resolved_checksum = checksum
            if previous is not None:
                typed_location = typed_location or previous.location
                resolved_media_type = resolved_media_type or previous.media_type
                resolved_format = resolved_format or previous.format
                resolved_size = (
                    previous.size_bytes if resolved_size is None else resolved_size
                )
                resolved_checksum = resolved_checksum or previous.checksum
            return ArtifactEvent(
                package_name=self.package_name,
                package_id=self.package_id,
                execution_id=self.execution_id,
                artifact_id=resolved_artifact_id,
                logical_name=logical_name,
                kind=kind,
                role=role,
                structure=structure,
                ownership=ownership,
                state=state,
                location=typed_location,
                media_type=resolved_media_type,
                format=resolved_format,
                size_bytes=resolved_size,
                checksum=resolved_checksum,
                message=message,
                revision=(previous.revision + 1 if previous is not None else 1),
                sequence=(history[-1].sequence + 1 if history else 1),
                metadata=metadata or {},
            )

        if self.path is not None:
            event = ArtifactStore(self.path).append_next(build_event)
        else:
            event = build_event(tuple(self._events))
            validate_artifact_history([*self._events, event])
            self.stream.write(f"{ARTIFACT_LINE_PREFIX}{event.to_json()}\n")
            self.stream.flush()
        self.sequence = event.sequence
        self._events.append(event)
        self._current[event.artifact_id] = event
        return event

    def finalize_execution(
        self,
        state_for_open: ArtifactState = ArtifactState.INCOMPLETE,
    ) -> list[ArtifactEvent]:
        """Seal producing artifacts without changing merely available output."""
        if state_for_open not in {
            ArtifactState.FINALIZED,
            ArtifactState.INCOMPLETE,
            ArtifactState.FAILED,
        }:
            raise ValueError("open artifacts require a terminal sealing state")
        if self.path is not None:
            sealed = ArtifactStore(self.path).finalize_open(state_for_open)
            if sealed:
                self._events.extend(sealed)
                self.sequence = sealed[-1].sequence
                for event in sealed:
                    self._current[event.artifact_id] = event
            return sealed
        producing = [
            event
            for event in tuple(self._current.values())
            if event.state is ArtifactState.PRODUCING
        ]
        sealed: list[ArtifactEvent] = []
        for event in producing:
            sealed.append(
                self.emit(
                    artifact_id=event.artifact_id,
                    logical_name=event.logical_name,
                    kind=event.kind,
                    role=event.role,
                    structure=event.structure,
                    ownership=event.ownership,
                    state=state_for_open,
                    location=event.location,
                    media_type=event.media_type,
                    format=event.format,
                    size_bytes=event.size_bytes,
                    checksum=event.checksum,
                    message=event.message,
                    metadata=dict(event.metadata),
                )
            )
        return sealed


def event_from_artifact_line(line: str) -> ArtifactEvent | None:
    """Parse one structured stdout or raw JSONL artifact line."""
    stripped = line.strip()
    if not stripped:
        return None
    if stripped.startswith(ARTIFACT_LINE_PREFIX):
        stripped = stripped[len(ARTIFACT_LINE_PREFIX) :]
    elif not stripped.startswith("{"):
        return None
    return ArtifactEvent.from_json(stripped)
