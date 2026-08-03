"""Generated-artifact semantics for the LBM-CFD stencil comparison."""

from __future__ import annotations

import hashlib
import json
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

from builtin.lbm_cfd.contract import (
    FINAL_STEP,
    LATTICES,
    RESULT_NAME,
    RESULT_SCHEMA,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(slots=True)
class LbmStencilArtifactAdapter:
    """Report only validated bounded products from one LBM-CFD run."""

    output_dir: Path
    _finalized: bool = False

    def observe_artifacts(self, text: str) -> list[ArtifactObservation]:
        """Wait for the comparison to complete before reporting products."""

        del text
        return []

    def _observation(
        self,
        path: Path,
        *,
        logical_name: str,
        kind: str,
        role: ArtifactRole,
        media_type: str,
        format_name: str,
        metadata: dict[str, Any],
        maximum_bytes: int,
    ) -> ArtifactObservation:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"LBM-CFD artifact is missing or unsafe: {path.name}")
        size = path.stat().st_size
        if size <= 0 or size > maximum_bytes:
            raise RuntimeError(
                f"LBM-CFD artifact size is outside its bound: {path.name}"
            )
        return ArtifactObservation(
            logical_name=logical_name,
            kind=kind,
            role=role,
            structure=ArtifactStructure.FILE,
            ownership=ArtifactOwnership.SHARED,
            state=ArtifactState.FINALIZED,
            location=ArtifactLocation.cluster_path(PurePosixPath(path.as_posix())),
            media_type=media_type,
            format=format_name,
            size_bytes=size,
            checksum=f"sha256:{_sha256(path)}",
            message="Validated LBM-CFD stencil benchmark product",
            metadata=metadata,
        )

    def finalize_artifacts(self) -> list[ArtifactObservation]:
        """Validate and report the result, fields, and input provenance."""

        if self._finalized:
            return []
        self._finalized = True
        result_path = self.output_dir / RESULT_NAME
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if (
            not isinstance(result, dict)
            or result.get("schema_version") != RESULT_SCHEMA
        ):
            raise RuntimeError("LBM-CFD result document has the wrong schema")
        common = {"application": "lbm-cfd", "benchmark_case": "lbm-stencil-response"}
        observations = [
            self._observation(
                result_path,
                logical_name="lbm-stencil-result",
                kind="scientific_result",
                role=ArtifactRole.OUTPUT,
                media_type="application/json",
                format_name=RESULT_SCHEMA,
                metadata={**common, "benchmark_role": "stencil_comparison"},
                maximum_bytes=1024 * 1024,
            ),
            self._observation(
                self.output_dir / "input-provenance.json",
                logical_name="lbm-input-provenance",
                kind="provenance",
                role=ArtifactRole.PROVENANCE,
                media_type="application/json",
                format_name="scientific-benchmark-lbm-input-provenance-v1",
                metadata=common,
                maximum_bytes=1024 * 1024,
            ),
        ]
        for lattice in LATTICES:
            observations.append(
                self._observation(
                    self.output_dir
                    / lattice
                    / f"simulation_state_t{FINAL_STEP:05d}.vts",
                    logical_name=f"lbm-{lattice.upper()}-vorticity",
                    kind="scientific_field",
                    role=ArtifactRole.OUTPUT,
                    media_type="application/vnd.vtk",
                    format_name="vtk-structured-grid",
                    metadata={
                        **common,
                        "lattice": lattice.upper(),
                        "benchmark_role": "vorticity_field",
                    },
                    maximum_bytes=64 * 1024 * 1024,
                )
            )
        return observations

    def finalize_artifacts_for_exit(
        self, return_code: int
    ) -> list[ArtifactObservation]:
        """Publish study products only after a successful process exit."""

        if return_code != 0:
            self._finalized = True
            return []
        return self.finalize_artifacts()

    def reset_artifacts(self) -> None:
        """Allow a fresh finalization after a JARVIS stream reset."""

        self._finalized = False


def adapter_from_package(package: dict[str, Any]) -> LbmStencilArtifactAdapter | None:
    """Create artifact semantics only for the benchmark LBM-CFD package."""

    if package.get("pkg_type") != "builtin.lbm_cfd":
        return None
    configured = package.get("out")
    if not isinstance(configured, str) or not configured:
        raise ValueError("LBM-CFD artifact provider requires its output directory")
    output_dir = Path(configured).resolve(strict=False)
    shared = package.get("shared_dir")
    if not isinstance(shared, str) or not shared:
        raise ValueError("LBM-CFD artifact provider requires its shared root")
    shared_root = Path(shared).resolve(strict=False)
    if not output_dir.is_relative_to(shared_root):
        raise ValueError("LBM-CFD output directory escaped its shared root")
    return LbmStencilArtifactAdapter(output_dir)
