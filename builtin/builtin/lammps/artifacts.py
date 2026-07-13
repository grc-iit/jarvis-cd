"""Generated-artifact semantics for the builtin LAMMPS package."""

from __future__ import annotations

import fnmatch
import os
import shlex
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

_MAX_DIRECTORY_ENTRIES = 4096
_MAX_SCRIPT_BYTES = 1024 * 1024
_MAX_REPORTED_MEMBERS = 256


@dataclass(frozen=True, slots=True)
class _DiscoveredEntry:
    """One non-symlink direct child of the configured output directory."""

    name: str
    is_file: bool
    is_directory: bool
    size_bytes: int | None


@dataclass
class LammpsArtifactAdapter:
    """Discover only known LAMMPS products in its configured output root.

    LAMMPS does not provide a structured artifact stream. The package therefore
    performs a bounded, non-recursive scan at finalization. It recognizes
    package-owned logs, dump series, restart/checkpoint series, and concrete
    outputs declared by static ``write_data`` or ``write_dump`` commands.
    """

    output_dir: PurePosixPath
    script_path: Path | None = None
    _finalized: bool = False
    _dump_artifact_id: str = field(default_factory=new_artifact_id)
    _checkpoint_artifact_id: str = field(default_factory=new_artifact_id)

    def observe_artifacts(self, text: str) -> list[ArtifactObservation]:
        """Return no observations until LAMMPS has finished writing files."""
        del text
        return []

    def finalize_artifacts(self) -> list[ArtifactObservation]:
        """Return a bounded manifest of package-owned LAMMPS products."""
        if self._finalized:
            return []
        self._finalized = True
        entries, truncated = self._discover_entries()
        declared_outputs = self._declared_output_patterns()

        logs: list[_DiscoveredEntry] = []
        dumps: list[_DiscoveredEntry] = []
        checkpoints: list[_DiscoveredEntry] = []
        outputs: list[_DiscoveredEntry] = []
        for entry in entries:
            category = _classify_entry(entry.name, declared_outputs)
            if category == "log":
                logs.append(entry)
            elif category == "dump":
                dumps.append(entry)
            elif category == "checkpoint":
                checkpoints.append(entry)
            elif category == "output":
                outputs.append(entry)

        observations: list[ArtifactObservation] = []
        observations.extend(self._log_observation(entry) for entry in logs)
        if dumps:
            observations.append(
                self._collection_observation(
                    artifact_id=self._dump_artifact_id,
                    logical_name="lammps-trajectory-dumps",
                    kind="scientific_dataset",
                    role=ArtifactRole.OUTPUT,
                    format_name="lammps-dump-series",
                    members=dumps,
                    truncated=truncated,
                )
            )
        if checkpoints:
            observations.append(
                self._collection_observation(
                    artifact_id=self._checkpoint_artifact_id,
                    logical_name="lammps-restarts",
                    kind="checkpoint",
                    role=ArtifactRole.CHECKPOINT,
                    format_name="lammps-restart-series",
                    members=checkpoints,
                    truncated=truncated,
                )
            )
        observations.extend(self._output_observation(entry) for entry in outputs)
        return observations

    def reset_artifacts(self) -> None:
        """Allow final discovery to run again after an execution-stream reset."""
        self._finalized = False

    def _discover_entries(self) -> tuple[list[_DiscoveredEntry], bool]:
        """Inspect direct children without following links or walking subtrees."""
        output_path = Path(self.output_dir.as_posix())
        try:
            if not output_path.exists():
                return [], False
            if not output_path.is_dir() or output_path.is_symlink():
                raise RuntimeError(
                    f"LAMMPS output root is not a real directory: {self.output_dir}"
                )
            discovered: list[_DiscoveredEntry] = []
            truncated = False
            with os.scandir(output_path) as iterator:
                for index, entry in enumerate(iterator):
                    if index >= _MAX_DIRECTORY_ENTRIES:
                        truncated = True
                        break
                    if entry.is_symlink():
                        continue
                    is_file = entry.is_file(follow_symlinks=False)
                    is_directory = entry.is_dir(follow_symlinks=False)
                    if not is_file and not is_directory:
                        continue
                    size_bytes = (
                        entry.stat(follow_symlinks=False).st_size if is_file else None
                    )
                    discovered.append(
                        _DiscoveredEntry(
                            name=entry.name,
                            is_file=is_file,
                            is_directory=is_directory,
                            size_bytes=size_bytes,
                        )
                    )
        except OSError as exc:
            raise RuntimeError(
                f"cannot inspect LAMMPS output directory {self.output_dir}: {exc}"
            ) from exc
        return sorted(discovered, key=lambda item: item.name), truncated

    def _declared_output_patterns(self) -> dict[str, str]:
        """Read bounded static output directives from the configured input."""
        if self.script_path is None:
            return {}
        try:
            status = self.script_path.stat()
            if not self.script_path.is_file() or status.st_size > _MAX_SCRIPT_BYTES:
                return {}
            lines = self.script_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            return {}

        patterns: dict[str, str] = {}
        for line in lines:
            try:
                tokens = shlex.split(line, comments=True, posix=True)
            except ValueError:
                continue
            if not tokens:
                continue
            command = tokens[0].casefold()
            output_tokens: list[tuple[str, str]] = []
            if command == "write_data" and len(tokens) >= 2:
                output_tokens.append((tokens[1], "output"))
            elif command == "write_restart" and len(tokens) >= 2:
                output_tokens.append((tokens[1], "checkpoint"))
            elif command == "restart" and len(tokens) >= 3:
                output_tokens.extend((token, "checkpoint") for token in tokens[2:4])
            elif command == "dump" and len(tokens) >= 6:
                output_tokens.append((tokens[5], "dump"))
            elif command == "write_dump" and len(tokens) >= 4:
                output_tokens.append((tokens[3], "output"))
            for output_token, category in output_tokens:
                if "$" in output_token:
                    continue
                normalized = _direct_output_pattern(output_token, self.output_dir)
                if normalized is not None:
                    patterns[normalized] = category
        return patterns

    def _log_observation(self, entry: _DiscoveredEntry) -> ArtifactObservation:
        """Describe one finalized LAMMPS text log."""
        return ArtifactObservation(
            logical_name=entry.name,
            kind="log",
            role=ArtifactRole.LOG,
            structure=ArtifactStructure.FILE,
            ownership=ArtifactOwnership.SHARED,
            state=ArtifactState.FINALIZED,
            location=ArtifactLocation.cluster_path(self.output_dir / entry.name),
            media_type="text/plain",
            format="lammps-log",
            size_bytes=entry.size_bytes,
            message="LAMMPS application log finalized",
            metadata={"application": "lammps"},
        )

    def _collection_observation(
        self,
        *,
        artifact_id: str,
        logical_name: str,
        kind: str,
        role: ArtifactRole,
        format_name: str,
        members: list[_DiscoveredEntry],
        truncated: bool,
    ) -> ArtifactObservation:
        """Describe one bounded collection without claiming unscanned members."""
        names: list[JsonValue] = [
            entry.name for entry in members[:_MAX_REPORTED_MEMBERS]
        ]
        size_bytes = sum(entry.size_bytes or 0 for entry in members if entry.is_file)
        return ArtifactObservation(
            artifact_id=artifact_id,
            logical_name=logical_name,
            kind=kind,
            role=role,
            structure=ArtifactStructure.COLLECTION,
            ownership=ArtifactOwnership.SHARED,
            state=(ArtifactState.INCOMPLETE if truncated else ArtifactState.FINALIZED),
            location=ArtifactLocation.cluster_path(self.output_dir),
            format=format_name,
            size_bytes=size_bytes,
            message=(
                "LAMMPS artifact discovery reached its safety bound"
                if truncated
                else f"LAMMPS {logical_name} finalized"
            ),
            metadata={
                "application": "lammps",
                "member_count_observed": len(members),
                "member_names": names,
                "member_names_truncated": len(members) > len(names),
                "discovery_truncated": truncated,
            },
        )

    def _output_observation(self, entry: _DiscoveredEntry) -> ArtifactObservation:
        """Describe one concrete output selected by LAMMPS semantics."""
        structure = (
            ArtifactStructure.COLLECTION
            if entry.is_directory
            else ArtifactStructure.FILE
        )
        format_name, media_type = _output_format(entry.name)
        return ArtifactObservation(
            logical_name=entry.name,
            kind="scientific_dataset",
            role=ArtifactRole.OUTPUT,
            structure=structure,
            ownership=ArtifactOwnership.SHARED,
            state=ArtifactState.FINALIZED,
            location=ArtifactLocation.cluster_path(self.output_dir / entry.name),
            media_type=media_type,
            format=format_name,
            size_bytes=entry.size_bytes,
            message="LAMMPS output finalized",
            metadata={"application": "lammps"},
        )


def adapter_from_package(package: dict[str, Any]) -> LammpsArtifactAdapter | None:
    """Create artifact semantics for the builtin LAMMPS package."""
    if package.get("pkg_type") != "builtin.lammps":
        return None
    output_dir = _configured_output_dir(
        package.get("out"),
        runtime_cwd=package.get("runtime_cwd"),
    )
    deploy_mode = str(package.get("effective_deploy_mode") or "default").casefold()
    if deploy_mode == "container":
        allowed_roots = tuple(
            root
            for root in (
                _optional_absolute_path(package.get("shared_dir")),
                _optional_absolute_path(package.get("private_dir")),
            )
            if root is not None
        )
        if not any(output_dir.is_relative_to(root) for root in allowed_roots):
            return None
    script_base = (
        output_dir
        if deploy_mode != "container"
        else _optional_absolute_path(package.get("runtime_cwd"))
    )
    script_path = _configured_script_path(package.get("script"), script_base)
    return LammpsArtifactAdapter(output_dir=output_dir, script_path=script_path)


def _configured_output_dir(
    value: object,
    *,
    runtime_cwd: object,
) -> PurePosixPath:
    """Return one normalized absolute POSIX output directory."""
    raw = os.path.expandvars(str(value or "."))
    path = PurePosixPath(raw)
    if not path.is_absolute():
        if not isinstance(runtime_cwd, str) or not runtime_cwd:
            raise ValueError(
                "relative LAMMPS artifact output requires a runtime working directory"
            )
        runtime_path = PurePosixPath(runtime_cwd)
        if not runtime_path.is_absolute():
            raise ValueError("LAMMPS runtime working directory must be absolute")
        path = runtime_path / path
        raw = path.as_posix()
    if not path.is_absolute() or path.as_posix() != raw or ".." in path.parts:
        raise ValueError("LAMMPS artifacts require a normalized absolute output path")
    return path


def _configured_script_path(
    value: object,
    base_dir: PurePosixPath | None,
) -> Path | None:
    """Resolve an input script using the same cwd as the LAMMPS launcher."""
    if not isinstance(value, str) or not value.strip():
        return None
    expanded_value = os.path.expandvars(value)
    host_path = Path(expanded_value)
    if host_path.is_absolute():
        return host_path
    expanded = PurePosixPath(expanded_value)
    if not expanded.is_absolute():
        if base_dir is None:
            return None
        expanded = base_dir / expanded
    return Path(expanded.as_posix())


def _optional_absolute_path(value: object) -> PurePosixPath | None:
    """Return one normalized absolute POSIX path when available."""
    if not isinstance(value, str) or not value:
        return None
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        return None
    return path


def _direct_output_pattern(value: str, output_dir: PurePosixPath) -> str | None:
    """Return a direct-child pattern only when it stays inside ``output_dir``."""
    path = PurePosixPath(value)
    if not path.is_absolute():
        path = output_dir / path
    if ".." in path.parts or path.parent != output_dir:
        return None
    name = path.name.replace("%", "*")
    return name if name and "/" not in name else None


def _classify_entry(name: str, declared_outputs: dict[str, str]) -> str | None:
    """Classify only package-specific, high-confidence LAMMPS products."""
    lowered = name.casefold()
    if lowered == "log.lammps" or lowered.startswith("log.lammps."):
        return "log"
    if (
        lowered.startswith("restart")
        or lowered.startswith("checkpoint")
        or ".restart" in lowered
    ):
        return "checkpoint"
    if (
        lowered.startswith("dump.")
        or lowered.endswith(".lammpstrj")
        or lowered.endswith(".dump")
    ):
        return "dump"
    for pattern, category in declared_outputs.items():
        if fnmatch.fnmatchcase(name, pattern):
            return category
    if lowered.startswith(("output.", "output_", "result.", "result_", "final.")):
        return "output"
    return None


def _output_format(name: str) -> tuple[str, str | None]:
    """Return a conservative format and media type for a LAMMPS output."""
    lowered = name.casefold()
    if lowered.endswith((".h5", ".hdf5")):
        return "hdf5", "application/x-hdf5"
    if lowered.endswith(".bp"):
        return "adios2-bp", "application/x-adios2"
    if lowered.endswith((".data", ".dat")) or lowered.startswith("data."):
        return "lammps-data", "text/plain"
    return "lammps-output", None


__all__ = ["LammpsArtifactAdapter", "adapter_from_package"]
