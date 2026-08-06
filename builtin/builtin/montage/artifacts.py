"""Generated-artifact semantics for the builtin Montage package."""

from __future__ import annotations

import json
import os
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

_RESULT_SCHEMA = "jarvis.montage-result.v1"


@dataclass(frozen=True, slots=True)
class _Product:
    logical_name: str
    filename: str
    kind: str
    role: ArtifactRole
    media_type: str
    format_name: str


_PRODUCTS = (
    _Product(
        "montage-j-mosaic",
        "montage-j.fits",
        "scientific_dataset",
        ArtifactRole.OUTPUT,
        "image/fits",
        "fits",
    ),
    _Product(
        "montage-h-mosaic",
        "montage-h.fits",
        "scientific_dataset",
        ArtifactRole.OUTPUT,
        "image/fits",
        "fits",
    ),
    _Product(
        "montage-k-mosaic",
        "montage-k.fits",
        "scientific_dataset",
        ArtifactRole.OUTPUT,
        "image/fits",
        "fits",
    ),
    _Product(
        "montage-three-band-composite",
        "montage-jhk.png",
        "scientific_visualization",
        ArtifactRole.OUTPUT,
        "image/png",
        "png",
    ),
    _Product(
        "montage-result",
        "montage-result.json",
        "validation_report",
        ArtifactRole.VALIDATION,
        "application/json",
        _RESULT_SCHEMA,
    ),
)


@dataclass
class MontageArtifactAdapter:
    """Report the exact offline three-band product set at process exit."""

    output_dir: PurePosixPath
    region: str
    _finalized: bool = False
    _artifact_ids: dict[str, str] = field(
        default_factory=lambda: {
            product.logical_name: new_artifact_id() for product in _PRODUCTS
        }
    )

    def observe_artifacts(self, text: str) -> list[ArtifactObservation]:
        """Return no provisional records for bounded batch outputs."""
        del text
        return []

    def finalize_artifacts(self) -> list[ArtifactObservation]:
        """Finalize products using successful-exit semantics."""
        return self.finalize_artifacts_for_exit(0)

    def finalize_artifacts_for_exit(
        self,
        return_code: int,
    ) -> list[ArtifactObservation]:
        """Report present, validated products or explicit incomplete records."""
        if self._finalized:
            return []
        self._finalized = True
        output = Path(self.output_dir.as_posix())
        return [
            self._observation(product, output, return_code) for product in _PRODUCTS
        ]

    def reset_artifacts(self) -> None:
        """Allow one later process lifecycle to rediscover the product set."""
        self._finalized = False

    def _observation(
        self,
        product: _Product,
        output: Path,
        return_code: int,
    ) -> ArtifactObservation:
        path = output / product.filename
        valid, size_bytes = _validate_product(path, product)
        finalized = return_code == 0 and valid
        state = ArtifactState.FINALIZED if finalized else ArtifactState.INCOMPLETE
        return ArtifactObservation(
            artifact_id=self._artifact_ids[product.logical_name],
            logical_name=product.logical_name,
            kind=product.kind,
            role=product.role,
            structure=ArtifactStructure.FILE,
            ownership=ArtifactOwnership.SHARED,
            state=state,
            location=(
                ArtifactLocation.cluster_path(self.output_dir / product.filename)
                if valid
                else None
            ),
            media_type=product.media_type,
            format=product.format_name,
            size_bytes=size_bytes,
            message=(
                f"{product.logical_name} finalized after successful Montage exit"
                if finalized
                else f"{product.logical_name} is missing or incomplete"
            ),
            metadata={
                "application": "montage",
                "region": self.region,
                "return_code": return_code,
            },
        )


def _validate_product(path: Path, product: _Product) -> tuple[bool, int | None]:
    """Validate one regular product without following symlinks."""
    try:
        status = path.lstat()
        if path.is_symlink() or not path.is_file():
            return False, None
        size = status.st_size
        if product.format_name == "fits":
            with path.open("rb") as stream:
                valid = (
                    2880 <= size <= 512 * 1024 * 1024 and stream.read(8) == b"SIMPLE  "
                )
        elif product.format_name == "png":
            with path.open("rb") as stream:
                valid = (
                    1024 < size <= 128 * 1024 * 1024
                    and stream.read(8) == b"\x89PNG\r\n\x1a\n"
                )
        else:
            valid = False
            if 0 < size <= 1024 * 1024:
                payload = json.loads(path.read_text(encoding="utf-8", errors="strict"))
                valid = (
                    isinstance(payload, dict)
                    and payload.get("schema_version") == _RESULT_SCHEMA
                )
        return valid, size if valid else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, None


def adapter_from_package(
    package: dict[str, Any],
) -> MontageArtifactAdapter | None:
    """Create artifacts only for the complete offline builtin profile."""
    if package.get("pkg_type") != "builtin.montage":
        return None
    configured = tuple(package.get(f"{band}_bundle") for band in ("j", "h", "k"))
    if not all(isinstance(value, str) and value for value in configured):
        return None
    output_dir = _configured_output_dir(
        package.get("out"),
        shared_dir=package.get("shared_dir"),
        runtime_cwd=package.get("runtime_cwd"),
    )
    region = package.get("region", "M17")
    if not isinstance(region, str) or not region:
        raise ValueError("Montage artifacts require a region label")
    return MontageArtifactAdapter(output_dir=output_dir, region=region)


def _configured_output_dir(
    value: object,
    *,
    shared_dir: object,
    runtime_cwd: object,
) -> PurePosixPath:
    """Resolve output below JARVIS-owned shared or runtime context."""
    raw = os.path.expandvars(str(value or "."))
    path = PurePosixPath(raw)
    if not path.is_absolute():
        base = _absolute_path(shared_dir) or _absolute_path(runtime_cwd)
        if base is None:
            raise ValueError(
                "relative Montage output requires a shared directory or runtime cwd"
            )
        path = base / path
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Montage artifact output must be a normalized absolute path")
    return path


def _absolute_path(value: object) -> PurePosixPath | None:
    """Return one normalized absolute POSIX path when supplied."""
    if not isinstance(value, str) or not value:
        return None
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        return None
    return path


__all__ = ["MontageArtifactAdapter", "adapter_from_package"]
