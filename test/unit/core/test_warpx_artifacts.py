"""Tests for builtin WarpX generated-artifact semantics."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest

from jarvis_cd.artifacts import (
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
        / "warpx"
        / "artifacts.py"
    )
    return load_artifacts_module(path)


def test_finalization_reports_bounded_plotfiles_and_diagnostics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exact staged inputs are excluded while native products remain visible."""

    (tmp_path / "density-low").mkdir()
    (tmp_path / "density-low" / "inputs").write_text("input\n", encoding="utf-8")
    diagnostic = tmp_path / "density-low" / "electron_energy.txt"
    diagnostic.write_text("40 1e-13 0.0 1.0\n", encoding="utf-8")
    plotfile = tmp_path / "density-low" / "plt00040"
    plotfile.mkdir()
    (plotfile / "Header").write_text("WarpX\n", encoding="utf-8")
    (tmp_path / "density-low" / "unclassified.bin").write_bytes(b"result")

    module = _module()
    adapter = module.WarpxArtifactAdapter(
        module.PurePosixPath("/scratch/warpx"),
        frozenset({module.PurePosixPath("density-low/inputs")}),
    )
    monkeypatch.setattr(adapter, "_local_output_path", lambda: tmp_path)

    observations = adapter.finalize_artifacts_for_exit(0)

    assert adapter.finalize_artifacts_for_exit(0) == []
    collection = observations[0]
    assert collection.logical_name == "warpx-results"
    assert collection.role is ArtifactRole.OUTPUT
    assert collection.structure is ArtifactStructure.COLLECTION
    assert collection.state is ArtifactState.FINALIZED
    names = collection.metadata["member_names"]
    assert "density-low/inputs" not in names
    assert "density-low/electron_energy.txt" in names
    assert "density-low/unclassified.bin" in names
    concrete = {item.logical_name: item for item in observations[1:]}
    histogram = concrete["warpx-density-low-electron_energy.txt"]
    assert histogram.structure is ArtifactStructure.FILE
    assert histogram.checksum is not None
    assert concrete["warpx-density-low-plt00040"].format == "amrex-plotfile"
    assert "warpx-density-low-unclassified.bin" not in concrete


def test_nonzero_exit_marks_discovered_outputs_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Filesystem presence cannot turn a failed WarpX run into success."""

    (tmp_path / "electron_energy.txt").write_text("partial\n", encoding="utf-8")
    module = _module()
    adapter = module.WarpxArtifactAdapter(module.PurePosixPath("/scratch/warpx"))
    monkeypatch.setattr(adapter, "_local_output_path", lambda: tmp_path)

    observations = adapter.finalize_artifacts_for_exit(17)

    assert observations
    assert {item.state for item in observations} == {ArtifactState.INCOMPLETE}


def test_discovery_bound_cannot_claim_complete_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A truncated output walk is visibly incomplete."""

    for index in range(3):
        (tmp_path / f"diag-{index}.txt").write_text(str(index), encoding="utf-8")
    module = _module()
    adapter = module.WarpxArtifactAdapter(module.PurePosixPath("/scratch/warpx"))
    monkeypatch.setattr(adapter, "_local_output_path", lambda: tmp_path)
    monkeypatch.setattr(module, "_MAX_DISCOVERED_ENTRIES", 2)

    observations = adapter.finalize_artifacts_for_exit(0)

    assert observations[0].state is ArtifactState.INCOMPLETE
    assert observations[0].metadata["discovery_truncated"] is True


def test_factory_scopes_relative_output_to_package_shared_root() -> None:
    """Artifact discovery receives the same execution-owned output as launch."""

    module = _module()

    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.warpx",
            "out": ".",
            "shared_dir": "/execution/shared/warpx",
            "runtime_cwd": "/execution/runtime",
        }
    )

    assert adapter is not None
    assert adapter.output_dir.as_posix() == "/execution/shared/warpx"
    assert module.adapter_from_package({"pkg_type": "builtin.lammps"}) is None


def test_container_private_output_is_not_claimed() -> None:
    """A host cannot report artifacts from an unmounted container path."""

    module = _module()
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.warpx",
            "out": "/tmp/warpx",
            "effective_deploy_mode": "container",
            "shared_dir": "/execution/shared",
            "private_dir": "/execution/private",
            "runtime_cwd": "/execution/runtime",
        }
    )

    assert adapter is None
