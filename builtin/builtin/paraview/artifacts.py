"""Generated-artifact semantics for the generic builtin ParaView package."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, cast

from jarvis_cd.artifacts import (
    ArtifactLocation,
    ArtifactObservation,
    ArtifactOwnership,
    ArtifactRole,
    ArtifactState,
    ArtifactStructure,
    new_artifact_id,
)
from jarvis_cd.progress import LineBuffer, ProgressState, event_from_progress_line
from jarvis_cd.progress.schema import JsonValue, ProgressEvent


@dataclass
class _TrackedArtifact:
    """Stable identity and latest metadata for one reported output path."""

    artifact_id: str
    location: ArtifactLocation
    logical_name: str
    kind: str
    structure: ArtifactStructure
    media_type: str | None
    format_name: str
    state: ArtifactState
    metadata: dict[str, JsonValue]


@dataclass
class ParaViewArtifactAdapter:
    """Translate structured ``pvbatch`` output paths into typed artifacts."""

    package_id: str
    cwd: PurePosixPath | None = None
    _lines: LineBuffer = field(default_factory=LineBuffer)
    _last_sequence: int = 0
    _tracked: dict[str, _TrackedArtifact] = field(default_factory=dict)

    def observe_artifacts(self, text: str) -> list[ArtifactObservation]:
        """Return artifact revisions from completed ParaView progress units."""
        return self._observe(text, finalize=False)

    def finalize_artifacts(self) -> list[ArtifactObservation]:
        """Flush a final line and finalize every still-available output."""
        observations = self._observe("", finalize=True)
        for tracked in self._tracked.values():
            if tracked.state is ArtifactState.FINALIZED:
                continue
            tracked.state = ArtifactState.FINALIZED
            tracked.metadata = {**tracked.metadata, "finalized_at_execution_end": True}
            observations.append(self._observation(tracked))
        return observations

    def reset_artifacts(self) -> None:
        """Reset parsing and generated-output identity after stream replacement."""
        self._lines.reset()
        self._last_sequence = 0
        self._tracked.clear()

    def _observe(self, text: str, *, finalize: bool) -> list[ArtifactObservation]:
        observations: list[ArtifactObservation] = []
        for line in self._lines.feed(text, finalize=finalize):
            event = event_from_progress_line(line)
            if event is None:
                continue
            observation = self._observe_event(event)
            if observation is not None:
                observations.append(observation)
        return observations

    def _observe_event(self, event: ProgressEvent) -> ArtifactObservation | None:
        """Validate one package event and translate its optional output path."""
        if event.package_name != "builtin.paraview":
            raise ValueError("ParaView artifact package identity does not match")
        if event.package_id != self.package_id:
            raise ValueError("ParaView artifact package ID does not match")
        if event.sequence <= self._last_sequence:
            raise ValueError("ParaView artifact progress sequences must increase")
        self._last_sequence = event.sequence

        output_value = event.metadata.get("output_path")
        if output_value is None:
            return None
        if not isinstance(output_value, str) or not output_value.strip():
            raise ValueError("ParaView output_path must be a non-empty string")
        path = _resolve_output_path(output_value, self.cwd)
        key = path.as_posix()
        tracked = self._tracked.get(key)
        final = event.state is ProgressState.COMPLETED
        metadata = _artifact_metadata(event, final=final)
        if tracked is None:
            kind, structure, media_type, format_name = _classify_output(path)
            tracked = _TrackedArtifact(
                artifact_id=new_artifact_id(),
                location=ArtifactLocation.cluster_path(path),
                logical_name=path.name,
                kind=kind,
                structure=structure,
                media_type=media_type,
                format_name=format_name,
                state=(ArtifactState.FINALIZED if final else ArtifactState.AVAILABLE),
                metadata=metadata,
            )
            self._tracked[key] = tracked
        else:
            if tracked.state is ArtifactState.FINALIZED:
                raise ValueError("ParaView reported output after artifact finalization")
            tracked.state = (
                ArtifactState.FINALIZED if final else ArtifactState.AVAILABLE
            )
            tracked.metadata = metadata
        return self._observation(tracked)

    @staticmethod
    def _observation(tracked: _TrackedArtifact) -> ArtifactObservation:
        """Project tracked package state into the generic artifact SPI."""
        return ArtifactObservation(
            artifact_id=tracked.artifact_id,
            logical_name=tracked.logical_name,
            kind=tracked.kind,
            role=ArtifactRole.OUTPUT,
            structure=tracked.structure,
            ownership=ArtifactOwnership.SHARED,
            state=tracked.state,
            location=tracked.location,
            media_type=tracked.media_type,
            format=tracked.format_name,
            message=(
                "ParaView output finalized"
                if tracked.state is ArtifactState.FINALIZED
                else "ParaView completed an output write"
            ),
            metadata=tracked.metadata,
        )


def adapter_from_package(
    package: dict[str, Any],
) -> ParaViewArtifactAdapter | None:
    """Create output semantics for ``pvbatch``; ``pvserver`` has no outputs."""
    if package.get("pkg_type") != "builtin.paraview":
        return None
    if package.get("mode", "server") != "batch":
        return None
    package_id = package.get("pkg_id")
    if not isinstance(package_id, str) or not package_id:
        package_id = "paraview"
    return ParaViewArtifactAdapter(
        package_id=package_id,
        cwd=_configured_cwd(package.get("cwd") or package.get("runtime_cwd")),
    )


def _configured_cwd(value: object) -> PurePosixPath | None:
    """Return a normalized absolute POSIX working directory when configured."""
    if not isinstance(value, str) or not value.strip():
        return None
    path = PurePosixPath(value)
    if not path.is_absolute() or path.as_posix() != value or ".." in path.parts:
        raise ValueError("ParaView artifacts require a normalized absolute cwd")
    return path


def _resolve_output_path(value: str, cwd: PurePosixPath | None) -> PurePosixPath:
    """Resolve one explicit renderer path without granting traversal authority."""
    if "\\" in value:
        raise ValueError("ParaView output_path must use POSIX separators")
    path = PurePosixPath(value)
    if ".." in path.parts or path.as_posix() != value:
        raise ValueError("ParaView output_path must be normalized")
    if not path.is_absolute():
        if cwd is None:
            raise ValueError(
                "relative ParaView output_path requires a configured absolute cwd"
            )
        path = cwd / path
    return path


def _artifact_metadata(event: ProgressEvent, *, final: bool) -> dict[str, JsonValue]:
    """Keep provenance needed to relate an artifact to package progress."""
    metadata: dict[str, JsonValue] = {
        "application": "paraview",
        "progress_sequence": event.sequence,
        "progress_label": event.label,
        "generation_stage": "final" if final else "intermediate",
    }
    for name in ("timestep", "completion_signal"):
        value = event.metadata.get(name)
        if value is not None:
            metadata[name] = cast(JsonValue, value)
    if event.current is not None:
        metadata["completed_units"] = event.current
    if event.total is not None:
        metadata["total_units"] = event.total
    if event.unit is not None:
        metadata["unit"] = event.unit
    return metadata


def _classify_output(
    path: PurePosixPath,
) -> tuple[str, ArtifactStructure, str | None, str]:
    """Map common ParaView render and dataset suffixes to stable semantics."""
    suffix = path.suffix.casefold()
    images = {
        ".png": ("image/png", "png"),
        ".jpg": ("image/jpeg", "jpeg"),
        ".jpeg": ("image/jpeg", "jpeg"),
        ".tif": ("image/tiff", "tiff"),
        ".tiff": ("image/tiff", "tiff"),
        ".bmp": ("image/bmp", "bmp"),
        ".webp": ("image/webp", "webp"),
        ".exr": ("image/x-exr", "openexr"),
    }
    videos = {
        ".mp4": ("video/mp4", "mp4"),
        ".webm": ("video/webm", "webm"),
        ".avi": ("video/x-msvideo", "avi"),
        ".mov": ("video/quicktime", "quicktime"),
        ".mkv": ("video/x-matroska", "matroska"),
    }
    if suffix in images:
        media_type, format_name = images[suffix]
        return "image", ArtifactStructure.FILE, media_type, format_name
    if suffix in videos:
        media_type, format_name = videos[suffix]
        return "video", ArtifactStructure.FILE, media_type, format_name
    if suffix == ".bp":
        return (
            "scientific_dataset",
            ArtifactStructure.COLLECTION,
            "application/x-adios2",
            "adios2-bp",
        )
    if suffix in {".pvd", ".pvtu", ".pvti", ".pvtp", ".pvtr", ".pvts"}:
        return (
            "scientific_dataset",
            ArtifactStructure.COLLECTION,
            "application/xml",
            "paraview-data-collection",
        )
    if suffix in {".vtk", ".vtu", ".vti", ".vtp", ".vtr", ".vts"}:
        return (
            "scientific_dataset",
            ArtifactStructure.FILE,
            "application/vnd.vtk",
            "vtk",
        )
    if suffix in {".h5", ".hdf5"}:
        return (
            "scientific_dataset",
            ArtifactStructure.FILE,
            "application/x-hdf5",
            "hdf5",
        )
    if suffix in {".xdmf", ".xmf"}:
        return (
            "scientific_dataset",
            ArtifactStructure.COLLECTION,
            "application/xml",
            "xdmf",
        )
    if suffix == ".nc":
        return (
            "scientific_dataset",
            ArtifactStructure.FILE,
            "application/x-netcdf",
            "netcdf",
        )
    if suffix == ".csv":
        return "tabular_dataset", ArtifactStructure.FILE, "text/csv", "csv"
    return "file", ArtifactStructure.FILE, None, "paraview-output"


__all__ = ["ParaViewArtifactAdapter", "adapter_from_package"]
