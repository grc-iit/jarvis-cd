"""Artifact semantics for paired Gray-Scott morphology analysis."""

from __future__ import annotations

import json
import math
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

_RESULT_SCHEMA = "jarvis.gray-scott-morphology.v1"
_CASE_FIELDS = {
    "configuration",
    "element_count",
    "final_simulation_step",
    "output_steps",
    "shape",
    "u",
    "v",
}
_METRIC_FIELDS = {
    "active_fraction",
    "active_threshold",
    "component_count",
    "interface_density",
    "largest_component_fraction_of_active",
    "max",
    "mean",
    "min",
    "standard_deviation",
    "surface_to_active",
}
_COMPARISON_FIELDS = {
    "max_absolute_v_difference",
    "pearson_v_correlation",
    "relative_v_l2_difference",
    "v_rms_difference",
}


@dataclass
class GrayScottAnalysisArtifactAdapter:
    """Report the closed paired morphology comparison after process exit."""

    output_path: PurePosixPath
    active_threshold: float
    low_input: str
    high_input: str
    artifact_id: str = field(default_factory=new_artifact_id)
    _terminal: bool = False

    def observe_artifacts(self, text: str) -> list[ArtifactObservation]:
        """Return no provisional record for the bounded JSON output."""
        del text
        return []

    def finalize_artifacts(self) -> list[ArtifactObservation]:
        """Finalize using successful-exit semantics."""
        return self.finalize_artifacts_for_exit(0)

    def finalize_artifacts_for_exit(
        self, return_code: int
    ) -> list[ArtifactObservation]:
        """Emit one finalized or explicitly incomplete comparison record."""
        if self._terminal:
            return []
        self._terminal = True
        valid, size_bytes = _valid_result(
            Path(self.output_path.as_posix()), self.active_threshold
        )
        finalized = return_code == 0 and valid
        return [
            ArtifactObservation(
                artifact_id=self.artifact_id,
                logical_name="gray-scott-morphology-comparison",
                kind="scientific_analysis",
                role=ArtifactRole.VALIDATION,
                structure=ArtifactStructure.FILE,
                ownership=ArtifactOwnership.SHARED,
                state=ArtifactState.FINALIZED
                if finalized
                else ArtifactState.INCOMPLETE,
                location=ArtifactLocation.cluster_path(self.output_path)
                if valid
                else None,
                media_type="application/json",
                format=_RESULT_SCHEMA,
                size_bytes=size_bytes,
                message=(
                    "Gray-Scott morphology comparison finalized"
                    if finalized
                    else "Gray-Scott morphology comparison is missing or incomplete"
                ),
                metadata={
                    "active_threshold": self.active_threshold,
                    "application": "gray_scott",
                    "analysis": "paired_morphology",
                    "high_input": self.high_input,
                    "low_input": self.low_input,
                    "return_code": return_code,
                },
            )
        ]

    def reset_artifacts(self) -> None:
        """Allow one later execution lifecycle to report a fresh result."""
        self._terminal = False


def _finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _valid_case(value: object, active_threshold: float | None) -> bool:
    if not isinstance(value, dict) or set(value) != _CASE_FIELDS:
        return False
    shape = value["shape"]
    if (
        not isinstance(value["configuration"], dict)
        or isinstance(value["element_count"], bool)
        or not isinstance(value["element_count"], int)
        or value["element_count"] <= 0
        or isinstance(value["final_simulation_step"], bool)
        or not isinstance(value["final_simulation_step"], int)
        or value["final_simulation_step"] < 0
        or isinstance(value["output_steps"], bool)
        or not isinstance(value["output_steps"], int)
        or value["output_steps"] <= 0
        or not isinstance(shape, list)
        or len(shape) != 3
        or any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in shape
        )
    ):
        return False
    if math.prod(shape) != value["element_count"]:
        return False
    for field_name in ("u", "v"):
        metrics = value[field_name]
        if not isinstance(metrics, dict) or set(metrics) != _METRIC_FIELDS:
            return False
        if (
            isinstance(metrics["component_count"], bool)
            or not isinstance(metrics["component_count"], int)
            or metrics["component_count"] < 0
            or not all(
                _finite_number(metrics[name])
                for name in _METRIC_FIELDS - {"component_count"}
            )
        ):
            return False
        if (
            active_threshold is not None
            and metrics["active_threshold"] != active_threshold
        ):
            return False
    return True


def _valid_result(
    path: Path,
    active_threshold: float | None = None,
) -> tuple[bool, int | None]:
    try:
        status = path.lstat()
        if (
            path.is_symlink()
            or not path.is_file()
            or not 0 < status.st_size <= 16 * 1024 * 1024
        ):
            return False, None
        value = json.loads(path.read_text(encoding="utf-8", errors="strict"))
        valid = (
            isinstance(value, dict)
            and set(value) == {"schema_version", "cases", "comparison"}
            and value.get("schema_version") == _RESULT_SCHEMA
            and isinstance(value.get("cases"), dict)
            and isinstance(value.get("comparison"), dict)
        )
        if valid:
            cases = value["cases"]
            comparison = value["comparison"]
            valid = (
                set(cases) == {"feed_low", "feed_high"}
                and set(comparison) == _COMPARISON_FIELDS
                and all(_finite_number(comparison[name]) for name in comparison)
                and all(
                    _valid_case(cases[name], active_threshold)
                    for name in ("feed_low", "feed_high")
                )
            )
        return valid, status.st_size if valid else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False, None


def adapter_from_package(
    package: dict[str, Any],
) -> GrayScottAnalysisArtifactAdapter | None:
    """Create artifact semantics only for the maintained analysis package."""
    if package.get("pkg_type") != "builtin.adios2_gray_scott_analysis":
        return None
    output = _absolute_posix_path(package.get("output_file"))
    threshold = package.get("active_threshold", 0.1)
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(float(threshold))
        or float(threshold) < 0
    ):
        raise ValueError("Gray-Scott analysis threshold must be non-negative")
    low_input = package.get("low_input")
    high_input = package.get("high_input")
    if not isinstance(low_input, str) or not isinstance(high_input, str):
        raise ValueError("Gray-Scott analysis artifact inputs must be paths")
    return GrayScottAnalysisArtifactAdapter(
        output, float(threshold), low_input, high_input
    )


def _absolute_posix_path(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ValueError("Gray-Scott analysis artifact output must be an absolute path")
    path = PurePosixPath(value)
    if not path.is_absolute() or path.as_posix() != value or ".." in path.parts:
        raise ValueError(
            "Gray-Scott analysis artifact output must be a normalized absolute path"
        )
    return path


__all__ = ["GrayScottAnalysisArtifactAdapter", "adapter_from_package"]
