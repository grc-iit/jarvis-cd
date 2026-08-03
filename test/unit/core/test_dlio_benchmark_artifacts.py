"""Artifact lifecycle tests for builtin DLIO Benchmark."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

from jarvis_cd.artifacts import (
    ArtifactObservation,
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
        / "dlio_benchmark"
        / "artifacts.py"
    )
    return load_artifacts_module(path)


def test_dlio_outputs_and_checkpoints_follow_process_lifecycle() -> None:
    """Native output groups retain identity through successful completion."""
    adapter = _module().adapter_from_package(
        {
            "pkg_type": "builtin.dlio_benchmark",
            "workload": "resnet50_v100",
            "generate_data": True,
            "checkpoint_supported": True,
            "checkpoint": True,
            "data_path": "/scratch/run/dataset",
            "output_path": "/scratch/run/output",
            "checkpoint_path": "/scratch/run/checkpoints",
            "cache_policy": "sync",
        }
    )
    assert adapter is not None

    observations = adapter.observe_artifacts("Starting DLIO benchmark\n")
    observations += adapter.finalize_artifacts_for_exit(0)

    assert {item.role for item in observations[:3]} == {
        ArtifactRole.INTERMEDIATE,
        ArtifactRole.OUTPUT,
        ArtifactRole.CHECKPOINT,
    }
    assert all(item.state is ArtifactState.PRODUCING for item in observations[:3])
    assert all(item.state is ArtifactState.FINALIZED for item in observations[-3:])
    assert all(item.structure is ArtifactStructure.COLLECTION for item in observations)
    by_role: dict[ArtifactRole, list[ArtifactObservation]] = {}
    for item in observations:
        by_role.setdefault(item.role, []).append(item)
    assert all(
        len({item.artifact_id for item in items}) == 1 for items in by_role.values()
    )
    output = next(
        item for item in observations[-3:] if item.role is ArtifactRole.OUTPUT
    )
    assert output.location is not None
    assert output.location.value == "/scratch/run/output"
    assert output.metadata["cache_policy"] == "sync"


def test_dlio_failure_marks_every_declared_product_incomplete() -> None:
    """A nonzero owned process cannot leave finalized DLIO artifacts."""
    adapter = _module().adapter_from_package(
        {
            "pkg_type": "builtin.dlio_benchmark",
            "workload": "unet3d_a100",
            "generate_data": False,
            "checkpoint_supported": True,
            "checkpoint": True,
            "output_path": "/scratch/run/output",
            "checkpoint_path": "/scratch/run/checkpoints",
            "cache_policy": "none",
        }
    )
    assert adapter is not None

    observations = adapter.finalize_artifacts_for_exit(17)

    assert {item.role for item in observations} == {
        ArtifactRole.OUTPUT,
        ArtifactRole.CHECKPOINT,
    }
    assert all(item.state is ArtifactState.INCOMPLETE for item in observations)
    assert all(item.metadata["return_code"] == 17 for item in observations)


def test_dlio_artifacts_reject_relative_or_unsafe_paths() -> None:
    """Artifact providers cannot turn relative settings into cluster authority."""
    module = _module()

    try:
        module.adapter_from_package(
            {
                "pkg_type": "builtin.dlio_benchmark",
                "workload": "unet3d_a100",
                "output_path": "relative/output",
            }
        )
    except ValueError as error:
        assert "absolute" in str(error)
    else:
        raise AssertionError("relative DLIO output path was accepted")
