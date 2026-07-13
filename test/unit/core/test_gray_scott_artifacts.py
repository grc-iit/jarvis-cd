"""Tests for builtin Gray-Scott artifact semantics."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest

from jarvis_cd.artifacts import (
    ArtifactLocationKind,
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
        / "gray_scott"
        / "artifacts.py"
    )
    return load_artifacts_module(path)


def test_gray_scott_exposes_one_evolving_hdf5_collection() -> None:
    """A timestep series is one artifact with producing and final revisions."""
    adapter = _module().adapter_from_package(
        {
            "pkg_type": "builtin.gray_scott",
            "outdir": "/scratch/run/gray-scott",
        }
    )
    assert adapter is not None

    producing = adapter.observe_artifacts(
        "wrote /scratch/run/gray-scott/gs_000100.h5\n"
        "wrote /scratch/run/gray-scott/gs_000200.h5\n"
    )
    finalized = adapter.observe_artifacts("Done.\n")

    observations = producing + finalized
    assert len({item.artifact_id for item in observations}) == 1
    assert [item.state for item in observations] == [
        ArtifactState.PRODUCING,
        ArtifactState.PRODUCING,
        ArtifactState.FINALIZED,
    ]
    assert observations[-1].role is ArtifactRole.OUTPUT
    assert observations[-1].structure is ArtifactStructure.COLLECTION
    assert observations[-1].ownership is ArtifactOwnership.SHARED
    assert observations[-1].location is not None
    assert observations[-1].location.kind is ArtifactLocationKind.CLUSTER_PATH
    assert observations[-1].location.value == "/scratch/run/gray-scott"
    assert observations[-1].metadata["members_observed"] == 2
    assert observations[-1].metadata["latest_timestep"] == 200


def test_gray_scott_rejects_output_outside_configured_collection() -> None:
    """Application output cannot widen configured cluster-path authority."""
    adapter = _module().adapter_from_package(
        {"pkg_type": "builtin.gray_scott", "outdir": "/scratch/owned"}
    )
    assert adapter is not None

    with pytest.raises(ValueError, match="outside.*configured"):
        adapter.observe_artifacts("wrote /scratch/other/gs_000100.h5\n")


def test_gray_scott_relative_output_is_not_exposed_as_cluster_authority() -> None:
    """A relative package path is not silently resolved on the desktop host."""
    adapter = _module().adapter_from_package(
        {"pkg_type": "builtin.gray_scott", "outdir": "relative/output"}
    )
    assert adapter is not None

    assert adapter.observe_artifacts("wrote relative/output/gs_000100.h5\n") == []


def test_gray_scott_default_mode_exposes_adios_collection() -> None:
    """The package's default ADIOS2 branch has the same artifact lifecycle."""
    adapter = _module().adapter_from_package(
        {
            "pkg_type": "builtin.gray_scott",
            "effective_deploy_mode": "default",
            "outdir": "/scratch/run/gray-scott.bp",
        }
    )
    assert adapter is not None

    observations = adapter.observe_artifacts(
        "steps: 100\n"
        "Simulation at step 10 writing output step 1\n"
        "Rank 0 - ET 123 - milliseconds\n"
    )

    assert observations[-1].state is ArtifactState.FINALIZED
    assert observations[-1].format == "adios2-bp5"
    assert observations[-1].metadata["io_backend"] == "adios2"
