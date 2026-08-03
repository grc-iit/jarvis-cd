"""Generated-artifact semantics for the WRF tropical-cyclone comparison."""

from __future__ import annotations

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

from builtin.wrf.contract import (
    FORMULATIONS,
    RESULT_NAME,
    RESULT_SCHEMA,
    sha256_file,
)


@dataclass(slots=True)
class WrfTropicalCycloneArtifactAdapter:
    """Report only closed WRF comparison products."""

    output_dir: Path
    _finalized: bool = False

    def observe_artifacts(self, text: str) -> list[ArtifactObservation]:
        """Wait for terminal validation before reporting products."""

        del text
        return []

    def _file(
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
            raise RuntimeError(f"WRF artifact is missing or unsafe: {path.name}")
        size = path.stat().st_size
        if size <= 0 or size > maximum_bytes:
            raise RuntimeError(f"WRF artifact size is outside its bound: {path.name}")
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
            checksum=f"sha256:{sha256_file(path)}",
            message="Validated WRF tropical-cyclone product",
            metadata=metadata,
        )

    def finalize_artifacts(self) -> list[ArtifactObservation]:
        """Report the comparison, histories, diagnostics, and input provenance."""

        if self._finalized:
            return []
        self._finalized = True
        result_path = self.output_dir / RESULT_NAME
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if (
            not isinstance(result, dict)
            or result.get("schema_version") != RESULT_SCHEMA
        ):
            raise RuntimeError("WRF result document has the wrong schema")
        common = {"application": "wrf", "benchmark_case": "tc-surface-exchange"}
        observations = [
            self._file(
                result_path,
                logical_name="wrf-tropical-cyclone-result",
                kind="scientific_result",
                role=ArtifactRole.OUTPUT,
                media_type="application/json",
                format_name=RESULT_SCHEMA,
                metadata={**common, "benchmark_role": "surface_exchange_comparison"},
                maximum_bytes=1024 * 1024,
            ),
            self._file(
                self.output_dir / "input-provenance.json",
                logical_name="wrf-tropical-cyclone-input-provenance",
                kind="provenance",
                role=ArtifactRole.PROVENANCE,
                media_type="application/json",
                format_name="scientific-benchmark-wrf-input-provenance-v1",
                metadata=common,
                maximum_bytes=1024 * 1024,
            ),
        ]
        for name, option in FORMULATIONS:
            case_root = self.output_dir / name
            outputs = sorted(case_root.glob("wrfout_d01_*"))
            if len(outputs) != 1:
                raise RuntimeError(f"WRF {name} history artifact is ambiguous")
            observations.extend(
                (
                    self._file(
                        outputs[0],
                        logical_name=f"wrf-{name}-history",
                        kind="scientific_field",
                        role=ArtifactRole.OUTPUT,
                        media_type="application/x-netcdf",
                        format_name="netcdf",
                        metadata={**common, "formulation": name, "isftcflx": option},
                        maximum_bytes=4 * 1024 * 1024 * 1024,
                    ),
                    self._file(
                        case_root / "surface-diagnostics.cdl",
                        logical_name=f"wrf-{name}-surface-diagnostics",
                        kind="scientific_result",
                        role=ArtifactRole.OUTPUT,
                        media_type="text/plain",
                        format_name="netcdf-cdl",
                        metadata={**common, "formulation": name, "isftcflx": option},
                        maximum_bytes=128 * 1024 * 1024,
                    ),
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
) -> WrfTropicalCycloneArtifactAdapter | None:
    """Create artifact semantics only for the benchmark WRF package."""

    if package.get("pkg_type") != "builtin.wrf" or not package.get("input_bundle"):
        return None
    configured = package.get("out")
    if not isinstance(configured, str) or not configured:
        raise ValueError("WRF artifact provider requires its output directory")
    output_dir = Path(configured).resolve(strict=False)
    shared = package.get("shared_dir")
    if not isinstance(shared, str) or not shared:
        raise ValueError("WRF artifact provider requires its shared root")
    shared_root = Path(shared).resolve(strict=False)
    if not output_dir.is_relative_to(shared_root):
        raise ValueError("WRF output directory escaped its shared root")
    return WrfTropicalCycloneArtifactAdapter(output_dir)
