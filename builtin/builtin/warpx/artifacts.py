"""Generated-artifact semantics for the builtin WarpX package."""

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
_DIAGNOSTIC_SUFFIXES = frozenset({".csv", ".h5", ".hdf5", ".json", ".txt"})


@dataclass(frozen=True, slots=True)
class _DiscoveredEntry:
    """One safe output member below the package-owned WarpX root."""

    relative_path: PurePosixPath
    is_file: bool
    is_directory: bool
    size_bytes: int | None


@dataclass
class WarpxArtifactAdapter:
    """Discover bounded WarpX plotfiles and reduced diagnostics at exit."""

    output_dir: PurePosixPath
    input_paths: frozenset[PurePosixPath] = frozenset()
    _finalized: bool = False
    _collection_artifact_id: str = field(default_factory=new_artifact_id)

    def observe_artifacts(self, text: str) -> list[ArtifactObservation]:
        """Wait for process completion before inspecting mutable outputs."""

        del text
        return []

    def finalize_artifacts(self) -> list[ArtifactObservation]:
        """Report successfully completed WarpX outputs."""

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
        if not outputs:
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
            self._member_observation(entry, state=state)
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
            raise RuntimeError(f"WarpX output root is not a real directory: {root}")
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
                    relative = PurePosixPath(path.relative_to(root).as_posix())
                    discovered.append(_DiscoveredEntry(relative, False, True, None))
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
                    relative = PurePosixPath(path.relative_to(root).as_posix())
                    discovered.append(
                        _DiscoveredEntry(
                            relative,
                            True,
                            False,
                            path.stat().st_size,
                        )
                    )
                if truncated:
                    break
        except OSError as exc:
            raise RuntimeError(
                f"cannot inspect WarpX output directory {root}: {exc}"
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
        concrete_directories = [
            entry
            for entry in entries
            if entry.is_directory and self._is_concrete_product(entry)
        ]
        retained_paths = {
            entry.relative_path for entry in (*files, *concrete_directories)
        }
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
        selected.update({entry.relative_path: entry for entry in concrete_directories})
        selected.update({entry.relative_path: entry for entry in directories})
        return [selected[path] for path in sorted(selected)]

    @staticmethod
    def _is_concrete_product(entry: _DiscoveredEntry) -> bool:
        """Recognize high-confidence WarpX diagnostics and output collections."""

        name = entry.relative_path.name.casefold()
        if entry.is_directory:
            return name.startswith(
                ("diag", "plt", "chk", "checkpoint")
            ) or name.endswith(".bp")
        return PurePosixPath(name).suffix in _DIAGNOSTIC_SUFFIXES

    def _collection_observation(
        self,
        outputs: list[_DiscoveredEntry],
        *,
        state: ArtifactState,
        truncated: bool,
    ) -> ArtifactObservation:
        """Describe the bounded package-owned WarpX result tree."""

        names: list[JsonValue] = [
            entry.relative_path.as_posix() for entry in outputs[:_MAX_REPORTED_MEMBERS]
        ]
        size_bytes = sum(entry.size_bytes or 0 for entry in outputs if entry.is_file)
        return ArtifactObservation(
            artifact_id=self._collection_artifact_id,
            logical_name="warpx-results",
            kind="scientific_dataset",
            role=ArtifactRole.OUTPUT,
            structure=ArtifactStructure.COLLECTION,
            ownership=ArtifactOwnership.SHARED,
            state=state,
            location=ArtifactLocation.cluster_path(self.output_dir),
            format="warpx-output-tree",
            size_bytes=size_bytes,
            message=(
                "WarpX output discovery reached its safety bound"
                if truncated
                else "WarpX output tree finalized"
            ),
            metadata={
                "application": "warpx",
                "completion_signal": "process_exit",
                "discovery_truncated": truncated,
                "member_count_observed": len(outputs),
                "member_names": names,
                "member_names_truncated": len(outputs) > len(names),
            },
        )

    def _member_observation(
        self, entry: _DiscoveredEntry, *, state: ArtifactState
    ) -> ArtifactObservation:
        """Describe one concrete WarpX plotfile or reduced diagnostic."""

        format_name, media_type = _output_format(entry)
        checksum = self._checksum(entry)
        logical_suffix = entry.relative_path.as_posix().replace("/", "-")
        return ArtifactObservation(
            logical_name=f"warpx-{logical_suffix}"[:256],
            kind="scientific_dataset",
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
            size_bytes=entry.size_bytes,
            checksum=checksum,
            message="WarpX diagnostic finalized",
            metadata={"application": "warpx"},
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


def adapter_from_package(package: dict[str, Any]) -> WarpxArtifactAdapter | None:
    """Create artifact semantics for the builtin WarpX package."""

    if package.get("pkg_type") != "builtin.warpx":
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
            raise ValueError("WarpX input-bundle artifacts require shared_dir")
        materialized = extract_input_bundle(
            input_bundle, Path(shared_dir.as_posix()) / "input-bundles"
        )
        input_paths.update(
            PurePosixPath(item.path) for item in materialized.manifest.files
        )
    configured_input = package.get("inputs")
    if isinstance(configured_input, str) and configured_input:
        input_paths.add(PurePosixPath(Path(configured_input).name))
    return WarpxArtifactAdapter(output_dir, frozenset(input_paths))


def _configured_output_dir(
    value: object,
    *,
    shared_dir: object,
    runtime_cwd: object,
) -> PurePosixPath:
    """Resolve the normalized absolute output directory used by the package."""

    raw = "run" if value in (None, "") else value
    if not isinstance(raw, str) or not raw:
        raise ValueError("WarpX artifacts require a printable output path")
    path = PurePosixPath(raw)
    if not path.is_absolute():
        base = _optional_absolute_path(shared_dir)
        if base is None:
            base = _optional_absolute_path(runtime_cwd)
        if base is None:
            raise ValueError(
                "relative WarpX artifact output requires shared_dir or runtime_cwd"
            )
        path = base / path
    normalized = PurePosixPath(posixpath.normpath(path.as_posix()))
    if not normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError("WarpX artifacts require a normalized absolute output path")
    return normalized


def _optional_absolute_path(value: object) -> PurePosixPath | None:
    """Return a normalized absolute POSIX path when one is available."""

    if not isinstance(value, str) or not value:
        return None
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        return None
    return path


def _output_format(entry: _DiscoveredEntry) -> tuple[str, str | None]:
    """Return a conservative format and media type for one WarpX product."""

    name = entry.relative_path.name.casefold()
    if entry.is_directory:
        if name.endswith(".bp"):
            return "adios2-bp", "application/x-adios2"
        if name.startswith(("plt", "diag")):
            return "amrex-plotfile", "application/x-amrex-plotfile"
        return "warpx-output-collection", None
    suffix = PurePosixPath(name).suffix
    if suffix == ".json":
        return "json", "application/json"
    if suffix == ".csv":
        return "csv", "text/csv"
    if suffix in {".h5", ".hdf5"}:
        return "hdf5", "application/x-hdf5"
    return "warpx-reduced-diagnostic", "text/plain"


__all__ = ["WarpxArtifactAdapter", "adapter_from_package"]
