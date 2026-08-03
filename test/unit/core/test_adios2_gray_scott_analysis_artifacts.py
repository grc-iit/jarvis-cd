"""Tests for paired Gray-Scott analysis artifact semantics."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest

from jarvis_cd.artifacts import (
    ArtifactOwnership,
    ArtifactRole,
    ArtifactState,
    ArtifactStructure,
    load_artifacts_module,
)


def _module() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[3]
        / "builtin"
        / "builtin"
        / "adios2_gray_scott_analysis"
        / "artifacts.py"
    )
    return load_artifacts_module(path)


def _result(path: Path, schema: str = "jarvis.gray-scott-morphology.v1") -> None:
    metrics = {
        "active_fraction": 0.2,
        "active_threshold": 0.1,
        "component_count": 2,
        "interface_density": 0.1,
        "largest_component_fraction_of_active": 0.8,
        "max": 0.7,
        "mean": 0.2,
        "min": 0.0,
        "standard_deviation": 0.1,
        "surface_to_active": 1.0,
    }
    case = {
        "configuration": {"F": 0.02},
        "element_count": 8,
        "final_simulation_step": 100,
        "output_steps": 10,
        "shape": [2, 2, 2],
        "u": metrics,
        "v": metrics,
    }
    path.write_text(
        json.dumps(
            {
                "cases": {"feed_high": case, "feed_low": case},
                "comparison": {
                    "max_absolute_v_difference": 0.5,
                    "pearson_v_correlation": 0.2,
                    "relative_v_l2_difference": 0.8,
                    "v_rms_difference": 0.3,
                },
                "schema_version": schema,
            }
        ),
        encoding="utf-8",
    )


def test_analysis_finalizes_one_typed_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Successful exit exposes the validated JSON result as scientific analysis."""
    output = tmp_path / "result.json"
    _result(output)
    module = _module()
    assert module._valid_result(output, 0.1) == (True, output.stat().st_size)
    monkeypatch.setattr(
        module,
        "_valid_result",
        lambda _path, _threshold: (True, output.stat().st_size),
    )
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.adios2_gray_scott_analysis",
            "output_file": "/execution/shared/result.json",
            "active_threshold": 0.1,
            "low_input": "/datasets/low.bp",
            "high_input": "/datasets/high.bp",
        }
    )
    assert adapter is not None

    observations = adapter.finalize_artifacts_for_exit(0)

    assert len(observations) == 1
    result = observations[0]
    assert result.state is ArtifactState.FINALIZED
    assert result.logical_name == "gray-scott-morphology-comparison"
    assert result.role is ArtifactRole.VALIDATION
    assert result.structure is ArtifactStructure.FILE
    assert result.ownership is ArtifactOwnership.SHARED
    assert result.location is not None
    assert result.location.value == "/execution/shared/result.json"
    assert result.format == "jarvis.gray-scott-morphology.v1"
    assert result.metadata["active_threshold"] == 0.1


def test_analysis_rejects_missing_or_wrong_schema_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Missing, malformed, or wrong-schema bytes remain explicitly incomplete."""
    output = tmp_path / "result.json"
    module = _module()
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.adios2_gray_scott_analysis",
            "output_file": "/execution/shared/result.json",
            "active_threshold": 0.1,
            "low_input": "/datasets/low.bp",
            "high_input": "/datasets/high.bp",
        }
    )
    assert adapter is not None
    assert module._valid_result(output, 0.1) == (False, None)

    _result(output, schema="wrong")
    assert module._valid_result(output, 0.1) == (False, None)
    _result(output)
    changed = json.loads(output.read_text(encoding="utf-8"))
    changed["cases"]["feed_high"]["v"]["active_threshold"] = 0.2
    output.write_text(json.dumps(changed), encoding="utf-8")
    assert module._valid_result(output, 0.1) == (False, None)
    monkeypatch.setattr(
        module, "_valid_result", lambda _path, _threshold: (False, None)
    )
    assert adapter.finalize_artifacts_for_exit(0)[0].state is ArtifactState.INCOMPLETE


def test_analysis_factory_is_package_local_and_requires_absolute_output() -> None:
    """Artifact authority is exact and never inferred from another package."""
    module = _module()
    assert (
        module.adapter_from_package({"pkg_type": "builtin.adios2_gray_scott"}) is None
    )
    try:
        module.adapter_from_package(
            {
                "pkg_type": "builtin.adios2_gray_scott_analysis",
                "output_file": "relative.json",
            }
        )
    except ValueError as error:
        assert "absolute" in str(error)
    else:
        raise AssertionError("relative analysis output was accepted")
