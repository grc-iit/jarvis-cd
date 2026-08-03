"""Tests for builtin ADIOS2 PDF Calc artifact semantics."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

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
        / "adios2_pdf_calc"
        / "artifacts.py"
    )
    return load_artifacts_module(path)


def test_pdf_calc_finalizes_one_typed_analysis_dataset() -> None:
    """Successful process exit promotes the declared BP output to finalized."""
    adapter = _module().adapter_from_package(
        {
            "pkg_type": "builtin.adios2_pdf_calc",
            "output_file": "/scratch/run/feed-low-pdf.bp",
            "engine": "bp5",
            "nbins": 128,
        }
    )
    assert adapter is not None

    observations = adapter.observe_artifacts(
        "PDF analysis writes using engine type: BP5\n"
    )
    observations += adapter.finalize_artifacts_for_exit(0)

    assert [item.state for item in observations] == [
        ArtifactState.PRODUCING,
        ArtifactState.FINALIZED,
    ]
    final = observations[-1]
    assert final.logical_name == "gray-scott-pdf-analysis"
    assert final.role is ArtifactRole.OUTPUT
    assert final.structure is ArtifactStructure.COLLECTION
    assert final.ownership is ArtifactOwnership.SHARED
    assert final.location is not None
    assert final.location.value == "/scratch/run/feed-low-pdf.bp"
    assert final.media_type == "application/x-adios2-bp"
    assert final.format == "adios2-bp5-pdf"
    assert final.metadata["bins"] == 128


def test_pdf_calc_nonzero_exit_is_incomplete_and_idempotent() -> None:
    """A process failure cannot leave a finalized analysis claim."""
    adapter = _module().adapter_from_package(
        {
            "pkg_type": "builtin.adios2_pdf_calc",
            "output_file": "/scratch/run/pdf.bp",
            "engine": "bp5",
            "nbins": 64,
        }
    )
    assert adapter is not None

    observations = adapter.finalize_artifacts_for_exit(7)

    assert len(observations) == 1
    assert observations[0].state is ArtifactState.INCOMPLETE
    assert observations[0].metadata["return_code"] == 7
    assert adapter.finalize_artifacts_for_exit(7) == []


def test_pdf_calc_factory_is_package_local_and_requires_absolute_output() -> None:
    """Artifact authority is exact and cannot be inferred from a relative path."""
    module = _module()

    assert (
        module.adapter_from_package({"pkg_type": "builtin.adios2_gray_scott"}) is None
    )
    try:
        module.adapter_from_package(
            {
                "pkg_type": "builtin.adios2_pdf_calc",
                "output_file": "relative.bp",
            }
        )
    except ValueError as exc:
        assert "absolute" in str(exc)
    else:
        raise AssertionError("relative PDF Calc output was accepted")
