"""Generated-artifact semantics for the builtin BioBB MD-setup package."""

from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from jarvis_cd.artifacts import (
    ArtifactLocation,
    ArtifactObservation,
    ArtifactOwnership,
    ArtifactRole,
    ArtifactState,
    ArtifactStructure,
)

RESULT_SCHEMA = "jarvis.biobb-md-setup-result.v1"
RESULT_NAME = "biobb-result.json"
_MAX_RESULT_BYTES = 1024 * 1024
_MAX_PRODUCT_BYTES = 512 * 1024 * 1024
_PRODUCTS = {
    "fixed.pdb": (
        "biobb-fixed-structure",
        "molecular_structure",
        "pdb",
        "chemical/x-pdb",
    ),
    "processed.gro": (
        "biobb-processed-coordinates",
        "molecular_structure",
        "gromacs-gro",
        "chemical/x-gro",
    ),
    "processed_topology.zip": (
        "biobb-processed-topology",
        "scientific_dataset",
        "gromacs-topology-zip",
        "application/zip",
    ),
    "boxed.gro": (
        "biobb-boxed-coordinates",
        "molecular_structure",
        "gromacs-gro",
        "chemical/x-gro",
    ),
    "solvated.gro": (
        "biobb-solvated-coordinates",
        "molecular_structure",
        "gromacs-gro",
        "chemical/x-gro",
    ),
    "solvated_topology.zip": (
        "biobb-solvated-topology",
        "scientific_dataset",
        "gromacs-topology-zip",
        "application/zip",
    ),
}


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest of one bounded file."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(slots=True)
class BiobbMdSetupArtifactAdapter:
    """Report one closed BioBB result and its exact declared products."""

    output_dir: PurePosixPath
    _local_root: Path | None = None
    _finalized: bool = False

    def observe_artifacts(self, text: str) -> list[ArtifactObservation]:
        """Wait for authoritative process completion before reporting files."""

        del text
        return []

    def finalize_artifacts(self) -> list[ArtifactObservation]:
        """Finalize products for a successful legacy completion callback."""

        return self._finalize(return_code=0)

    def finalize_artifacts_for_exit(
        self, return_code: int
    ) -> list[ArtifactObservation]:
        """Finalize products using the authoritative driver exit status."""

        return self._finalize(return_code=return_code)

    def reset_artifacts(self) -> None:
        """Permit discovery after an execution stream is replaced."""

        self._finalized = False

    def _local_output_path(self) -> Path:
        """Project the declared cluster directory into this host filesystem."""

        if self._local_root is not None:
            return self._local_root
        return Path(self.output_dir.as_posix())

    def _result(self) -> tuple[Path, dict[str, Any]]:
        """Load and validate the bounded package-owned result document."""

        raw_root = self._local_output_path()
        if raw_root.is_symlink():
            raise RuntimeError("BioBB output root is not a real directory")
        try:
            root = raw_root.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("BioBB output root is unavailable") from exc
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError("BioBB output root is not a real directory")
        path = root / RESULT_NAME
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size <= 0
            or path.stat().st_size > _MAX_RESULT_BYTES
        ):
            raise RuntimeError("BioBB result is missing, unsafe, or oversized")
        try:
            document = json.loads(path.read_text(encoding="utf-8", errors="strict"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("BioBB result is not valid JSON") from exc
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != RESULT_SCHEMA
            or document.get("status") not in {"completed", "failed"}
        ):
            raise RuntimeError("BioBB result has the wrong schema")
        return path, document

    def _members(
        self, document: dict[str, Any]
    ) -> list[tuple[Path, PurePosixPath, tuple[str, str, str, str]]]:
        """Resolve and digest-check every exact result-declared product."""

        values = document.get("artifacts")
        if not isinstance(values, list) or len(values) > len(_PRODUCTS):
            raise RuntimeError("BioBB result has an invalid artifact list")
        root = self._local_output_path().resolve(strict=True)
        members: list[tuple[Path, PurePosixPath, tuple[str, str, str, str]]] = []
        seen: set[str] = set()
        for item in values:
            if not isinstance(item, dict):
                raise RuntimeError("BioBB result has an invalid artifact member")
            relative = item.get("path")
            expected_format = item.get("format")
            expected_size = item.get("size_bytes")
            expected_hash = item.get("sha256")
            if not isinstance(relative, str):
                raise RuntimeError("BioBB result declared an unknown artifact")
            candidate = Path(relative)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise RuntimeError(
                    f"BioBB artifact escaped its output root: {relative}"
                )
            if relative not in _PRODUCTS:
                raise RuntimeError("BioBB result declared an unknown artifact")
            if relative in seen:
                raise RuntimeError("BioBB result declared a duplicate artifact")
            seen.add(relative)
            identity = _PRODUCTS[relative]
            if expected_format != identity[2]:
                raise RuntimeError("BioBB artifact format differs from its contract")
            unresolved = root / candidate
            if unresolved.is_symlink():
                raise RuntimeError(f"BioBB artifact is missing or unsafe: {relative}")
            path = unresolved.resolve(strict=False)
            if not path.is_relative_to(root):
                raise RuntimeError(
                    f"BioBB artifact escaped its output root: {relative}"
                )
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"BioBB artifact is missing or unsafe: {relative}")
            size = path.stat().st_size
            if (
                isinstance(expected_size, bool)
                or not isinstance(expected_size, int)
                or expected_size != size
                or not 0 < size <= _MAX_PRODUCT_BYTES
            ):
                raise RuntimeError(f"BioBB artifact size differs: {relative}")
            if (
                not isinstance(expected_hash, str)
                or len(expected_hash) != 64
                or _sha256(path) != expected_hash
            ):
                raise RuntimeError(f"BioBB artifact hash differs: {relative}")
            members.append((path, PurePosixPath(relative), identity))
        if document.get("status") == "completed" and seen != set(_PRODUCTS):
            raise RuntimeError("completed BioBB result omitted required products")
        return members

    @staticmethod
    def _metadata(document: dict[str, Any]) -> dict[str, Any]:
        """Project bounded scientific values into each artifact observation."""

        parameters = document.get("parameters")
        metrics = document.get("metrics")
        if not isinstance(parameters, dict) or not isinstance(metrics, dict):
            raise RuntimeError("BioBB result omitted parameters or metrics")
        return {
            "application": "biobb_wf_md_setup",
            "box_type": parameters.get("box_type"),
            "distance_to_molecule": parameters.get("distance_to_molecule"),
            "force_field": parameters.get("force_field"),
            "water_type": parameters.get("water_type"),
            "boxed_volume_nm3": metrics.get("boxed_volume_nm3"),
            "solvated_atom_count": metrics.get("solvated_atom_count"),
            "solvent_molecule_count": metrics.get("solvent_molecule_count"),
        }

    def _observation(
        self,
        path: Path,
        cluster_path: PurePosixPath,
        *,
        logical_name: str,
        kind: str,
        format_name: str,
        media_type: str,
        state: ArtifactState,
        metadata: dict[str, Any],
    ) -> ArtifactObservation:
        """Create one exact file observation."""

        return ArtifactObservation(
            logical_name=logical_name,
            kind=kind,
            role=ArtifactRole.OUTPUT,
            structure=ArtifactStructure.FILE,
            ownership=ArtifactOwnership.SHARED,
            state=state,
            location=ArtifactLocation.cluster_path(cluster_path),
            media_type=media_type,
            format=format_name,
            size_bytes=path.stat().st_size,
            checksum=f"sha256:{_sha256(path)}",
            message="Closed BioBB molecular-dynamics setup product",
            metadata=metadata,
        )

    def _finalize(self, *, return_code: int) -> list[ArtifactObservation]:
        """Validate and report the exact result-declared files once."""

        if self._finalized:
            return []
        self._finalized = True
        result_path, document = self._result()
        declared_return_code = document.get("return_code")
        if isinstance(declared_return_code, bool) or not isinstance(
            declared_return_code, int
        ):
            raise RuntimeError("BioBB result omitted its process return code")
        members = self._members(document)
        state = (
            ArtifactState.FINALIZED
            if return_code == 0
            and declared_return_code == 0
            and document["status"] == "completed"
            else ArtifactState.INCOMPLETE
        )
        metadata = self._metadata(document)
        observations = [
            self._observation(
                result_path,
                self.output_dir / RESULT_NAME,
                logical_name="biobb-md-setup-result",
                kind="scientific_result",
                format_name=RESULT_SCHEMA,
                media_type="application/json",
                state=state,
                metadata=metadata,
            )
        ]
        observations.extend(
            self._observation(
                path,
                self.output_dir / relative,
                logical_name=identity[0],
                kind=identity[1],
                format_name=identity[2],
                media_type=identity[3],
                state=state,
                metadata=metadata,
            )
            for path, relative, identity in members
        )
        return observations


def adapter_from_package(package: dict[str, Any]) -> BiobbMdSetupArtifactAdapter | None:
    """Create artifact semantics only for builtin BioBB MD setup."""

    if package.get("pkg_type") != "builtin.biobb_wf_md_setup":
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
    return BiobbMdSetupArtifactAdapter(output_dir)


def _configured_output_dir(
    value: object,
    *,
    shared_dir: object,
    runtime_cwd: object,
) -> PurePosixPath:
    """Resolve the normalized absolute output directory used by the package."""

    raw = "run" if value in (None, "") else value
    if not isinstance(raw, str) or not raw:
        raise ValueError("BioBB artifacts require a printable output path")
    path = PurePosixPath(raw)
    if not path.is_absolute():
        base = _optional_absolute_path(shared_dir)
        if base is None:
            base = _optional_absolute_path(runtime_cwd)
        if base is None:
            raise ValueError(
                "relative BioBB artifact output requires shared_dir or runtime_cwd"
            )
        path = base / path
    normalized = PurePosixPath(posixpath.normpath(path.as_posix()))
    if not normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError("BioBB artifacts require a normalized absolute output path")
    return normalized


def _optional_absolute_path(value: object) -> PurePosixPath | None:
    """Return a normalized absolute POSIX path when one is available."""

    if not isinstance(value, str) or not value:
        return None
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        return None
    return path


__all__ = ["BiobbMdSetupArtifactAdapter", "adapter_from_package"]
