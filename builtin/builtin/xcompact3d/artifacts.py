"""Generated-artifact semantics for the builtin Xcompact3D package."""

from __future__ import annotations

import hashlib
import os
import posixpath
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from jarvis_cd.artifacts import (
    ArtifactLocation,
    ArtifactObservation,
    ArtifactOwnership,
    ArtifactRole,
    ArtifactState,
    ArtifactStructure,
    new_artifact_id,
)
from jarvis_cd.artifacts.schema import JsonValue
from jarvis_cd.input_bundle import extract_input_bundle

_MAX_DISCOVERED_ENTRIES = 4096
_MAX_REPORTED_MEMBERS = 256
_MAX_CHECKSUM_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _DiscoveredEntry:
    """One safe output member below the package-owned result root."""

    relative_path: PurePosixPath
    is_file: bool
    is_directory: bool
    size_bytes: int | None


@dataclass
class Xcompact3dArtifactAdapter:
    """Discover bounded Xcompact3D logs, checkpoints, fields, and statistics."""

    output_dir: PurePosixPath
    input_paths: frozenset[PurePosixPath] = frozenset()
    _finalized: bool = False
    _collection_artifact_id: str = field(default_factory=new_artifact_id)

    def observe_artifacts(self, text: str) -> list[ArtifactObservation]:
        """Wait for process completion before inspecting mutable outputs."""

        del text
        return []

    def finalize_artifacts(self) -> list[ArtifactObservation]:
        """Report outputs after a successful process completion."""

        return self._finalize(return_code=0)

    def finalize_artifacts_for_exit(
        self, return_code: int
    ) -> list[ArtifactObservation]:
        """Report outputs with the authoritative process completion state."""

        return self._finalize(return_code=return_code)

    def reset_artifacts(self) -> None:
        """Permit discovery after an execution stream is replaced."""

        self._finalized = False

    def _finalize(self, *, return_code: int) -> list[ArtifactObservation]:
        if self._finalized:
            return []
        self._finalized = True
        entries, truncated = self._discover_entries()
        outputs = self._output_entries(entries)
        if not outputs and not truncated:
            return []
        state = (
            ArtifactState.FINALIZED
            if return_code == 0 and not truncated
            else ArtifactState.INCOMPLETE
        )
        observations = [
            self._collection_observation(outputs, state=state, truncated=truncated)
        ]
        observations.extend(
            self._member_observation(entry, entries=outputs, state=state)
            for entry in outputs
            if self._is_concrete_product(entry)
        )
        return observations

    def _discover_entries(self) -> tuple[list[_DiscoveredEntry], bool]:
        """Walk the owned output tree without following links and with a bound."""

        root = self._local_output_path()
        if not root.exists():
            return [], False
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError(
                f"Xcompact3D output root is not a real directory: {root}"
            )
        discovered: list[_DiscoveredEntry] = []
        truncated = False
        try:
            for current, directory_names, file_names in os.walk(
                root, topdown=True, followlinks=False
            ):
                current_path = Path(current)
                safe_directories: list[str] = []
                for name in sorted(directory_names):
                    path = current_path / name
                    if path.is_symlink():
                        continue
                    if len(discovered) >= _MAX_DISCOVERED_ENTRIES:
                        truncated = True
                        break
                    discovered.append(
                        _DiscoveredEntry(
                            PurePosixPath(path.relative_to(root).as_posix()),
                            False,
                            True,
                            None,
                        )
                    )
                    safe_directories.append(name)
                directory_names[:] = [] if truncated else safe_directories
                if truncated:
                    break
                for name in sorted(file_names):
                    path = current_path / name
                    if path.is_symlink() or not path.is_file():
                        continue
                    if len(discovered) >= _MAX_DISCOVERED_ENTRIES:
                        truncated = True
                        break
                    discovered.append(
                        _DiscoveredEntry(
                            PurePosixPath(path.relative_to(root).as_posix()),
                            True,
                            False,
                            path.stat().st_size,
                        )
                    )
                if truncated:
                    break
        except OSError as exc:
            raise RuntimeError(
                f"cannot inspect Xcompact3D output directory {root}: {exc}"
            ) from exc
        return discovered, truncated

    def _local_output_path(self) -> Path:
        """Project the declared cluster path into the current host filesystem."""

        return Path(self.output_dir.as_posix())

    def _output_entries(
        self, entries: list[_DiscoveredEntry]
    ) -> list[_DiscoveredEntry]:
        """Exclude exact inputs and directories that contain only staged inputs."""

        files = [
            entry
            for entry in entries
            if entry.is_file and entry.relative_path not in self.input_paths
        ]
        retained_paths = {entry.relative_path for entry in files}
        directories = [
            entry
            for entry in entries
            if entry.is_directory
            and any(
                candidate != entry.relative_path
                and candidate.is_relative_to(entry.relative_path)
                for candidate in retained_paths
            )
        ]
        selected = {entry.relative_path: entry for entry in files}
        selected.update({entry.relative_path: entry for entry in directories})
        return [selected[path] for path in sorted(selected)]

    @staticmethod
    def _is_concrete_product(entry: _DiscoveredEntry) -> bool:
        """Recognize stable, high-confidence Xcompact3D products."""

        name = entry.relative_path.name.casefold()
        if entry.is_directory:
            return name in {"data", "statistics"} or name.endswith(".bp")
        return name in {"checkpoint", "restart.info", "xcompact3d.log"}

    def _collection_observation(
        self,
        outputs: list[_DiscoveredEntry],
        *,
        state: ArtifactState,
        truncated: bool,
    ) -> ArtifactObservation:
        """Describe the bounded package-owned result tree."""

        names: list[JsonValue] = [
            entry.relative_path.as_posix() for entry in outputs[:_MAX_REPORTED_MEMBERS]
        ]
        size_bytes = sum(entry.size_bytes or 0 for entry in outputs if entry.is_file)
        return ArtifactObservation(
            artifact_id=self._collection_artifact_id,
            logical_name="xcompact3d-results",
            kind="scientific_dataset",
            role=ArtifactRole.OUTPUT,
            structure=ArtifactStructure.COLLECTION,
            ownership=ArtifactOwnership.SHARED,
            state=state,
            location=ArtifactLocation.cluster_path(self.output_dir),
            format="xcompact3d-output-tree",
            size_bytes=size_bytes,
            message=(
                "Xcompact3D output discovery reached its safety bound"
                if truncated
                else "Xcompact3D output tree finalized"
            ),
            metadata={
                "application": "xcompact3d",
                "completion_signal": "process_exit",
                "discovery_truncated": truncated,
                "member_count_observed": len(outputs),
                "member_names": names,
                "member_names_truncated": len(outputs) > len(names),
            },
        )

    def _member_observation(
        self,
        entry: _DiscoveredEntry,
        *,
        entries: list[_DiscoveredEntry],
        state: ArtifactState,
    ) -> ArtifactObservation:
        """Describe one solver log, checkpoint, field, or statistics product."""

        name = entry.relative_path.name.casefold()
        logical_name, kind, format_name, media_type = _product_identity(name)
        size_bytes = entry.size_bytes
        if entry.is_directory:
            size_bytes = sum(
                item.size_bytes or 0
                for item in entries
                if item.is_file
                and item.relative_path.is_relative_to(entry.relative_path)
            )
        return ArtifactObservation(
            logical_name=logical_name,
            kind=kind,
            role=ArtifactRole.OUTPUT,
            structure=(
                ArtifactStructure.FILE
                if entry.is_file
                else ArtifactStructure.COLLECTION
            ),
            ownership=ArtifactOwnership.SHARED,
            state=state,
            location=ArtifactLocation.cluster_path(
                self.output_dir / entry.relative_path
            ),
            media_type=media_type,
            format=format_name,
            size_bytes=size_bytes,
            checksum=self._checksum(entry),
            message="Xcompact3D product finalized",
            metadata={"application": "xcompact3d"},
        )

    def _checksum(self, entry: _DiscoveredEntry) -> str | None:
        """Hash bounded files while avoiding unbounded finalization work."""

        if (
            not entry.is_file
            or entry.size_bytes is None
            or entry.size_bytes > _MAX_CHECKSUM_BYTES
        ):
            return None
        path = self._local_output_path() / entry.relative_path
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"


def adapter_from_package(package: dict[str, Any]) -> Xcompact3dArtifactAdapter | None:
    """Create artifact semantics for the builtin Xcompact3D package."""

    if package.get("pkg_type") != "builtin.xcompact3d":
        return None
    output_dir = _configured_output_dir(
        package.get("out"),
        shared_dir=package.get("shared_dir"),
        runtime_cwd=package.get("runtime_cwd"),
    )
    deploy_mode = str(package.get("effective_deploy_mode") or "default").casefold()
    if deploy_mode == "container":
        roots = tuple(
            root
            for root in (
                _optional_absolute_path(package.get("shared_dir")),
                _optional_absolute_path(package.get("private_dir")),
            )
            if root is not None
        )
        if not any(output_dir.is_relative_to(root) for root in roots):
            return None
    input_paths: set[PurePosixPath] = set()
    input_bundle = package.get("input_bundle")
    if isinstance(input_bundle, str) and input_bundle:
        shared_dir = _optional_absolute_path(package.get("shared_dir"))
        if shared_dir is None:
            raise ValueError("Xcompact3D input-bundle artifacts require shared_dir")
        materialized = extract_input_bundle(
            input_bundle, Path(shared_dir.as_posix()) / "input-bundles"
        )
        input_paths.update(
            PurePosixPath(item.path) for item in materialized.manifest.files
        )
    configured_input = package.get("inputs")
    if isinstance(configured_input, str) and configured_input:
        input_paths.add(PurePosixPath(Path(configured_input).name))
    return Xcompact3dArtifactAdapter(output_dir, frozenset(input_paths))


def _configured_output_dir(
    value: object,
    *,
    shared_dir: object,
    runtime_cwd: object,
) -> PurePosixPath:
    """Resolve the normalized absolute output directory used by the package."""

    raw = "run" if value in (None, "") else value
    if not isinstance(raw, str) or not raw:
        raise ValueError("Xcompact3D artifacts require a printable output path")
    path = PurePosixPath(raw)
    if not path.is_absolute():
        base = _optional_absolute_path(shared_dir)
        if base is None:
            base = _optional_absolute_path(runtime_cwd)
        if base is None:
            raise ValueError(
                "relative Xcompact3D artifact output requires shared_dir or runtime_cwd"
            )
        path = base / path
    normalized = PurePosixPath(posixpath.normpath(path.as_posix()))
    if not normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError(
            "Xcompact3D artifacts require a normalized absolute output path"
        )
    return normalized


def _optional_absolute_path(value: object) -> PurePosixPath | None:
    """Return a normalized absolute POSIX path when one is available."""

    if not isinstance(value, str) or not value:
        return None
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        return None
    return path


def _product_identity(name: str) -> tuple[str, str, str, str | None]:
    """Return the stable artifact identity for a recognized product name."""

    if name == "xcompact3d.log":
        return (
            "xcompact3d-log",
            "scientific_log",
            "xcompact3d-runtime-log",
            "text/plain",
        )
    if name == "checkpoint":
        return (
            "xcompact3d-checkpoint",
            "checkpoint",
            "xcompact3d-checkpoint",
            "application/octet-stream",
        )
    if name == "restart.info":
        return (
            "xcompact3d-restart-info",
            "checkpoint_metadata",
            "xcompact3d-restart-info",
            "text/plain",
        )
    if name == "data":
        return (
            "xcompact3d-data",
            "scientific_dataset",
            "xcompact3d-field-output",
            "application/x-xcompact3d-fields",
        )
    if name == "statistics":
        return (
            "xcompact3d-statistics",
            "scientific_dataset",
            "xcompact3d-statistics",
            "application/x-xcompact3d-statistics",
        )
    return (
        "xcompact3d-adios2",
        "scientific_dataset",
        "adios2-bp",
        "application/x-adios2",
    )
