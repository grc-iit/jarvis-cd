"""Generated-artifact semantics for the OpenFOAM airfoil incidence study."""

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

from builtin.openfoam.contract import (
    ANGLES,
    CASE_NAMES,
    RESULT_FILE_SCHEMA,
    RESULT_NAME,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(slots=True)
class OpenfoamAirfoilArtifactAdapter:
    """Report only validated bounded products from one airfoil run directory."""

    output_dir: Path
    _finalized: bool = False

    def observe_artifacts(self, text: str) -> list[ArtifactObservation]:
        """Wait for all three cases before reporting immutable products."""

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
    ) -> ArtifactObservation:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"OpenFOAM artifact is missing or unsafe: {path.name}")
        size = path.stat().st_size
        if size <= 0 or size > 16 * 1024 * 1024:
            raise RuntimeError(
                f"OpenFOAM artifact size is outside its bound: {path.name}"
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
            message="Validated OpenFOAM airfoil benchmark product",
            metadata=metadata,
        )

    def finalize_artifacts(self) -> list[ArtifactObservation]:
        """Validate and report the result, provenance, and coefficient series."""

        if self._finalized:
            return []
        self._finalized = True
        result_path = self.output_dir / RESULT_NAME
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if (
            not isinstance(result, dict)
            or result.get("schema_version") != RESULT_FILE_SCHEMA
        ):
            raise RuntimeError("OpenFOAM result document has the wrong schema")
        observations = [
            self._observation(
                result_path,
                logical_name="airfoil-incidence-comparison",
                kind="scientific_result",
                role=ArtifactRole.OUTPUT,
                media_type="application/json",
                format_name=RESULT_FILE_SCHEMA,
                metadata={
                    "application": "openfoam",
                    "benchmark_case": "openfoam-airfoil-incidence",
                    "benchmark_role": "force_coefficients",
                },
            ),
            self._observation(
                self.output_dir / "input-provenance.json",
                logical_name="airfoil-input-provenance",
                kind="provenance",
                role=ArtifactRole.PROVENANCE,
                media_type="application/json",
                format_name="scientific-benchmark-openfoam-input-provenance-v1",
                metadata={"application": "openfoam"},
            ),
        ]
        for angle in ANGLES:
            case_name = CASE_NAMES[angle]
            matches = list(
                (self.output_dir / case_name).glob(
                    "postProcessing/forceCoeffs/*/coefficient.dat"
                )
            )
            if len(matches) != 1:
                raise RuntimeError(
                    f"OpenFOAM {angle}-degree coefficient artifact is ambiguous"
                )
            observations.append(
                self._observation(
                    matches[0],
                    logical_name=f"airfoil-force-coefficients-{angle:02d}-degrees",
                    kind="scientific_result",
                    role=ArtifactRole.OUTPUT,
                    media_type="text/tab-separated-values",
                    format_name="openfoam-force-coefficients",
                    metadata={
                        "angle_degrees": angle,
                        "application": "openfoam",
                        "benchmark_role": "force_coefficients",
                    },
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


def adapter_from_package(
    package: dict[str, Any],
) -> OpenfoamAirfoilArtifactAdapter | None:
    """Create artifact semantics only for the benchmark OpenFOAM package."""

    if package.get("pkg_type") != "builtin.openfoam" or not package.get("input_bundle"):
        return None
    configured = package.get("out")
    if not isinstance(configured, str) or not configured:
        raise ValueError("OpenFOAM artifact provider requires its output directory")
    output_dir = Path(configured).resolve(strict=False)
    shared = package.get("shared_dir")
    if not isinstance(shared, str) or not shared:
        raise ValueError("OpenFOAM artifact provider requires its shared root")
    shared_root = Path(shared).resolve(strict=False)
    if not output_dir.is_relative_to(shared_root):
        raise ValueError("OpenFOAM output directory escaped its shared root")
    return OpenfoamAirfoilArtifactAdapter(output_dir)
