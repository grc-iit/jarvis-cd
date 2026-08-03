"""Generated-artifact semantics for the builtin Gadget2 package."""

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

_MAX_DISCOVERED_FILES = 4096
_MAX_REPORTED_MEMBERS = 512
_MAX_CHECKSUM_BYTES = 256 * 1024 * 1024
_LOG_PRODUCTS = {
    "energy.txt": (
        "gadget2-energy",
        "scientific_dataset",
        "gadget2-energy-table",
        "text/plain",
    ),
    "info.txt": (
        "gadget2-info",
        "scientific_log",
        "gadget2-timestep-log",
        "text/plain",
    ),
    "cpu.txt": (
        "gadget2-cpu",
        "performance_log",
        "gadget2-cpu-log",
        "text/plain",
    ),
    "timings.txt": (
        "gadget2-timings",
        "performance_log",
        "gadget2-timing-log",
        "text/plain",
    ),
}


@dataclass(frozen=True, slots=True)
class _OutputFile:
    """One safe regular file below the package-owned result root."""

    relative_path: PurePosixPath
    size_bytes: int


@dataclass
class Gadget2ArtifactAdapter:
    """Discover bounded Gadget2 logs, snapshots, and restart state."""

    output_dir: PurePosixPath
    input_paths: frozenset[PurePosixPath] = frozenset()
    _local_root: Path | None = None
    _finalized: bool = False
    _collection_artifact_id: str = field(default_factory=new_artifact_id)

    def observe_artifacts(self, text: str) -> list[ArtifactObservation]:
        """Wait for authoritative process completion before inspecting outputs."""

        del text
        return []

    def finalize_artifacts(self) -> list[ArtifactObservation]:
        """Finalize outputs for a successful legacy completion callback."""

        return self._finalize(return_code=0)

    def finalize_artifacts_for_exit(
        self, return_code: int
    ) -> list[ArtifactObservation]:
        """Finalize outputs with the authoritative process completion state."""

        return self._finalize(return_code=return_code)

    def reset_artifacts(self) -> None:
        """Permit discovery after an execution stream is replaced."""

        self._finalized = False

    def _local_output_path(self) -> Path:
        """Project the declared cluster directory into this host filesystem."""

        if self._local_root is not None:
            return self._local_root
        return Path(self.output_dir.as_posix())

    def _discover_files(self) -> tuple[list[_OutputFile], bool]:
        """Walk the owned output tree without following links and with a bound."""

        root = self._local_output_path()
        if not root.exists():
            return [], False
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError(f"Gadget2 output root is not a real directory: {root}")
        files: list[_OutputFile] = []
        truncated = False
        try:
            for current, directory_names, file_names in os.walk(
                root, topdown=True, followlinks=False
            ):
                current_path = Path(current)
                directory_names[:] = [
                    name
                    for name in sorted(directory_names)
                    if not (current_path / name).is_symlink()
                ]
                for name in sorted(file_names):
                    path = current_path / name
                    if path.is_symlink() or not path.is_file():
                        continue
                    relative = PurePosixPath(path.relative_to(root).as_posix())
                    if relative in self.input_paths:
                        continue
                    if len(files) >= _MAX_DISCOVERED_FILES:
                        truncated = True
                        break
                    files.append(_OutputFile(relative, path.stat().st_size))
                if truncated:
                    break
        except OSError as exc:
            raise RuntimeError(
                f"cannot inspect Gadget2 output directory {root}: {exc}"
            ) from exc
        return files, truncated

    def _finalize(self, *, return_code: int) -> list[ArtifactObservation]:
        if self._finalized:
            return []
        self._finalized = True
        files, truncated = self._discover_files()
        if not files and not truncated:
            return []
        by_basename: dict[str, list[_OutputFile]] = {}
        for item in files:
            by_basename.setdefault(item.relative_path.name.casefold(), []).append(item)
        snapshots = [
            item
            for item in files
            if item.relative_path.name.casefold().startswith("snapshot_")
        ]
        restarts = [
            item
            for item in files
            if item.relative_path.name.casefold().startswith("restart.")
        ]
        missing = [
            name
            for name in ("energy.txt", "info.txt")
            if len(by_basename.get(name, ())) != 1
        ]
        if not snapshots:
            missing.append("snapshot")
        valid = return_code == 0 and not truncated and not missing
        state = ArtifactState.FINALIZED if valid else ArtifactState.INCOMPLETE
        if missing:
            message = "Gadget2 result is missing required products: " + ", ".join(
                missing
            )
        elif truncated:
            message = "Gadget2 output discovery reached its safety bound"
        elif return_code != 0:
            message = f"Gadget2 process exited with status {return_code}"
        else:
            message = "Gadget2 result tree finalized"
        observations = [
            self._collection_observation(files, state=state, message=message)
        ]
        for basename, identity in _LOG_PRODUCTS.items():
            matches = by_basename.get(basename, ())
            if len(matches) == 1:
                observations.append(
                    self._file_observation(matches[0], identity=identity, state=state)
                )
        if snapshots:
            observations.append(
                self._set_observation(
                    snapshots,
                    logical_name="gadget2-snapshots",
                    kind="scientific_dataset",
                    format_name="gadget2-snapshot-set",
                    media_type="application/x-gadget2-snapshot",
                    state=state,
                )
            )
        if restarts:
            observations.append(
                self._set_observation(
                    restarts,
                    logical_name="gadget2-restarts",
                    kind="checkpoint",
                    format_name="gadget2-restart-set",
                    media_type="application/x-gadget2-restart",
                    state=state,
                )
            )
        return observations

    def _collection_observation(
        self,
        files: list[_OutputFile],
        *,
        state: ArtifactState,
        message: str,
    ) -> ArtifactObservation:
        """Describe the bounded package-owned result tree."""

        names: list[JsonValue] = [
            item.relative_path.as_posix() for item in files[:_MAX_REPORTED_MEMBERS]
        ]
        return ArtifactObservation(
            artifact_id=self._collection_artifact_id,
            logical_name="gadget2-results",
            kind="scientific_dataset",
            role=ArtifactRole.OUTPUT,
            structure=ArtifactStructure.COLLECTION,
            ownership=ArtifactOwnership.SHARED,
            state=state,
            location=ArtifactLocation.cluster_path(self.output_dir),
            format="gadget2-output-tree",
            size_bytes=sum(item.size_bytes for item in files),
            message=message,
            metadata={
                "application": "gadget2",
                "completion_signal": "process_exit",
                "member_count_observed": len(files),
                "member_names": names,
                "member_names_truncated": len(files) > len(names),
            },
        )

    def _file_observation(
        self,
        item: _OutputFile,
        *,
        identity: tuple[str, str, str, str],
        state: ArtifactState,
    ) -> ArtifactObservation:
        """Describe one exact Gadget2 log or table."""

        logical_name, kind, format_name, media_type = identity
        return ArtifactObservation(
            logical_name=logical_name,
            kind=kind,
            role=ArtifactRole.OUTPUT,
            structure=ArtifactStructure.FILE,
            ownership=ArtifactOwnership.SHARED,
            state=state,
            location=ArtifactLocation.cluster_path(
                self.output_dir / item.relative_path
            ),
            media_type=media_type,
            format=format_name,
            size_bytes=item.size_bytes,
            checksum=self._checksum(item),
            message="Gadget2 product finalized",
            metadata={"application": "gadget2"},
        )

    def _set_observation(
        self,
        items: list[_OutputFile],
        *,
        logical_name: str,
        kind: str,
        format_name: str,
        media_type: str,
        state: ArtifactState,
    ) -> ArtifactObservation:
        """Describe one bounded set of snapshots or per-rank restart files."""

        paths: list[JsonValue] = [
            item.relative_path.as_posix() for item in items[:_MAX_REPORTED_MEMBERS]
        ]
        parents = {item.relative_path.parent for item in items}
        location = self.output_dir
        if len(parents) == 1:
            location /= next(iter(parents))
        return ArtifactObservation(
            logical_name=logical_name,
            kind=kind,
            role=ArtifactRole.OUTPUT,
            structure=ArtifactStructure.COLLECTION,
            ownership=ArtifactOwnership.SHARED,
            state=state,
            location=ArtifactLocation.cluster_path(location),
            media_type=media_type,
            format=format_name,
            size_bytes=sum(item.size_bytes for item in items),
            message="Gadget2 product set finalized",
            metadata={
                "application": "gadget2",
                "member_count": len(items),
                "member_names": paths,
                "member_names_truncated": len(items) > len(paths),
            },
        )

    def _checksum(self, item: _OutputFile) -> str | None:
        """Hash one bounded output file."""

        if item.size_bytes > _MAX_CHECKSUM_BYTES:
            return None
        path = self._local_output_path() / item.relative_path
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"


def adapter_from_package(package: dict[str, Any]) -> Gadget2ArtifactAdapter | None:
    """Create artifact semantics for the builtin Gadget2 package."""

    if package.get("pkg_type") != "builtin.gadget2":
        return None
    input_bundle = package.get("input_bundle")
    if not isinstance(input_bundle, str) or not input_bundle:
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
    shared_dir = _optional_absolute_path(package.get("shared_dir"))
    if shared_dir is None:
        raise ValueError("Gadget2 input-bundle artifacts require shared_dir")
    materialized = extract_input_bundle(
        input_bundle, Path(shared_dir.as_posix()) / "input-bundles"
    )
    input_paths = frozenset(
        PurePosixPath(item.path) for item in materialized.manifest.files
    )
    return Gadget2ArtifactAdapter(output_dir, input_paths)


def _configured_output_dir(
    value: object,
    *,
    shared_dir: object,
    runtime_cwd: object,
) -> PurePosixPath:
    """Resolve the normalized absolute output directory used by the package."""

    raw = "run" if value in (None, "") else value
    if not isinstance(raw, str) or not raw:
        raise ValueError("Gadget2 artifacts require a printable output path")
    path = PurePosixPath(raw)
    if not path.is_absolute():
        base = _optional_absolute_path(shared_dir)
        if base is None:
            base = _optional_absolute_path(runtime_cwd)
        if base is None:
            raise ValueError(
                "relative Gadget2 artifact output requires shared_dir or runtime_cwd"
            )
        path = base / path
    normalized = PurePosixPath(posixpath.normpath(path.as_posix()))
    if not normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError("Gadget2 artifacts require a normalized absolute output path")
    return normalized


def _optional_absolute_path(value: object) -> PurePosixPath | None:
    """Return a normalized absolute POSIX path when one is available."""

    if not isinstance(value, str) or not value:
        return None
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        return None
    return path
