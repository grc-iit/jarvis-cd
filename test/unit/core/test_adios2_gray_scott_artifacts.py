"""Tests for builtin ADIOS2 Gray-Scott artifact semantics."""

from __future__ import annotations

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
        / "adios2_gray_scott"
        / "artifacts.py"
    )
    return load_artifacts_module(path)


def test_adios2_gray_scott_exposes_output_and_checkpoint_lifecycles() -> None:
    """BP output and restart state have distinct stable artifact identities."""
    adapter = _module().adapter_from_package(
        {
            "pkg_type": "builtin.adios2_gray_scott",
            "engine": "bp5",
            "out_file": "/scratch/run/gs.bp",
            "checkpoint_output": "/scratch/run/ckpt.bp",
        }
    )
    assert adapter is not None

    observations = adapter.observe_artifacts(
        "steps: 100\n"
        "Simulation at step 10 writing output step 1\n"
        "checkpoint at step 70 create file /scratch/run/ckpt.bp\n"
        "checkpoint at step 70 create file /scratch/run/ckpt.bp\n"
        "Rank 0 - ET 2345 - milliseconds\n"
    )

    outputs = [item for item in observations if item.role is ArtifactRole.OUTPUT]
    checkpoints = [
        item for item in observations if item.role is ArtifactRole.CHECKPOINT
    ]
    assert len({item.artifact_id for item in outputs}) == 1
    assert len({item.artifact_id for item in checkpoints}) == 1
    assert outputs[-1].state is ArtifactState.FINALIZED
    assert outputs[-1].structure is ArtifactStructure.COLLECTION
    assert outputs[-1].ownership is ArtifactOwnership.SHARED
    assert outputs[-1].location is not None
    assert outputs[-1].location.value == "/scratch/run/gs.bp"
    assert outputs[-1].format == "adios2-bp5"
    assert checkpoints[-1].state is ArtifactState.FINALIZED
    assert checkpoints[-1].ownership is ArtifactOwnership.SHARED
    assert checkpoints[-1].metadata["checkpoint_timestep"] == 70
    assert len(checkpoints) == 2


def test_adios2_gray_scott_does_not_claim_ephemeral_sst_as_durable() -> None:
    """An SST stream name is not misrepresented as a persistent cluster path."""
    adapter = _module().adapter_from_package(
        {
            "pkg_type": "builtin.adios2_gray_scott",
            "engine": "sst",
            "out_file": "/scratch/run/stream.bp",
            "checkpoint_output": "relative-ckpt.bp",
        }
    )
    assert adapter is not None

    observations = adapter.observe_artifacts(
        "steps: 100\nSimulation at step 10 writing output step 1\n"
    )

    assert observations == []


def test_adios2_gray_scott_rejects_checkpoint_path_mismatch() -> None:
    """Native output cannot redirect a declared checkpoint artifact."""
    adapter = _module().adapter_from_package(
        {
            "pkg_type": "builtin.adios2_gray_scott",
            "checkpoint_output": "/scratch/run/ckpt.bp",
        }
    )
    assert adapter is not None

    with pytest.raises(ValueError, match="outside.*configured"):
        adapter.observe_artifacts(
            "checkpoint at step 70 create file /scratch/other/ckpt.bp\n"
        )


def test_adios2_gray_scott_resolves_default_relative_checkpoint() -> None:
    """The default checkpoint path is qualified by the real runtime cwd."""
    adapter = _module().adapter_from_package(
        {
            "pkg_type": "builtin.adios2_gray_scott",
            "engine": "bp5",
            "out_file": "/scratch/run/gs.bp",
            "checkpoint_output": "ckpt.bp",
            "runtime_cwd": "/work/pipeline",
        }
    )
    assert adapter is not None

    observations = adapter.observe_artifacts(
        "checkpoint at step 70 create file /work/pipeline/ckpt.bp\n"
        "Rank 0 - ET 2345 - milliseconds\n"
    )

    checkpoint = next(
        item for item in observations if item.role is ArtifactRole.CHECKPOINT
    )
    assert checkpoint.location is not None
    assert checkpoint.location.value == "/work/pipeline/ckpt.bp"
