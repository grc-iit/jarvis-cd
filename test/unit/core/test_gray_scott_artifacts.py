"""Tests for builtin Gray-Scott artifact semantics."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from xml.etree import ElementTree

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


def test_gray_scott_adios_config_pins_output_and_checkpoint_to_bp5() -> None:
    """Artifact format claims are enforced for both direct application writers."""
    path = (
        Path(__file__).resolve().parents[3]
        / "builtin"
        / "builtin"
        / "gray_scott"
        / "config"
        / "adios2.xml"
    )
    document = ElementTree.parse(path)
    engines = {
        io.attrib["name"]: engine.attrib.get("type")
        for io in document.getroot().findall("io")
        if (engine := io.find("engine")) is not None
    }

    assert engines["SimulationOutput"] == "BP5"
    assert engines["SimulationCheckpoint"] == "BP5"


def test_gray_scott_checkpoint_fingerprint_tracks_metadata_only(
    tmp_path: Path,
) -> None:
    """Checkpoint detection notices BP metadata without reading data payloads."""
    module = _module()
    checkpoint = tmp_path / "restart.bp"
    checkpoint.mkdir()
    configured_path = module.PurePosixPath(checkpoint.as_posix())

    before = module._path_fingerprint(configured_path)
    (checkpoint / "md.idx").write_bytes(b"metadata")
    after = module._path_fingerprint(configured_path)

    assert before is not None
    assert after is not None
    assert after != before


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
    finalized += adapter.finalize_artifacts()

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
    observations += adapter.finalize_artifacts()

    assert observations[-1].state is ArtifactState.FINALIZED
    assert observations[-1].format == "adios2-bp5"
    assert observations[-1].metadata["io_backend"] == "adios2"


def test_gray_scott_direct_iowarp_artifact_finalizes_on_zero_exit() -> None:
    """A successful writer process closes the directly produced BP5 dataset."""
    adapter = _module().adapter_from_package(
        {
            "pkg_type": "builtin.gray_scott",
            "effective_deploy_mode": "default",
            "outdir": "/scratch/run/gray-scott.bp",
        }
    )
    assert adapter is not None

    observations = adapter.observe_artifacts(
        "steps: 20\n"
        "Simulation at step 10 writing output step 1\n"
        "Simulation at step 20 writing output step 2\n"
    )
    observations += adapter.finalize_artifacts_for_exit(0)

    assert observations[-1].state is ArtifactState.FINALIZED
    assert observations[-1].metadata["members_observed"] == 2
    assert observations[-1].metadata["latest_timestep"] == 20
    assert observations[-1].metadata["completion_signal"] == (
        "process_exit_zero_after_final_output"
    )


def test_gray_scott_failed_process_marks_artifact_incomplete() -> None:
    """A nonzero process exit terminally marks the partial BP5 dataset."""
    adapter = _module().adapter_from_package(
        {
            "pkg_type": "builtin.gray_scott",
            "effective_deploy_mode": "default",
            "outdir": "/scratch/run/gray-scott.bp",
        }
    )
    assert adapter is not None

    observations = adapter.observe_artifacts(
        "steps: 20\nSimulation at step 10 writing output step 1\n"
    )
    observations += adapter.finalize_artifacts_for_exit(9)

    assert observations[-1].state is ArtifactState.INCOMPLETE
    assert observations[-1].metadata["return_code"] == 9
    assert observations[-1].metadata["completion_signal"] == "process_exit_nonzero"


def test_gray_scott_done_marker_cannot_finalize_after_failed_exit() -> None:
    """A provisional native success marker cannot hide process failure."""
    adapter = _module().adapter_from_package(
        {
            "pkg_type": "builtin.gray_scott",
            "effective_deploy_mode": "default",
            "outdir": "/scratch/run/gray-scott.bp",
        }
    )
    assert adapter is not None

    observations = adapter.observe_artifacts(
        "steps: 20\n"
        "Simulation at step 10 writing output step 1\n"
        "Rank 0 - ET 123 - milliseconds\n"
    )
    observations += adapter.finalize_artifacts_for_exit(23)

    assert all(item.state is not ArtifactState.FINALIZED for item in observations)
    assert observations[-1].state is ArtifactState.INCOMPLETE
    assert observations[-1].metadata["return_code"] == 23
    assert observations[-1].metadata["application_completion_signal"] == (
        "writer_closed_and_timing_reported"
    )


def test_gray_scott_reports_checkpoint_created_by_owned_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured checkpoint is reported only when its BP path changes."""
    module = _module()
    changed = (
        (".", 16877, 1, 64, 2, 2),
        (("md.idx", 33188, 2, 128, 2, 2),),
    )
    fingerprints = iter([None, changed])
    monkeypatch.setattr(
        module,
        "_path_fingerprint",
        lambda _path: next(fingerprints),
    )
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.gray_scott",
            "effective_deploy_mode": "default",
            "outdir": "/scratch/run/gray-scott.bp",
            "checkpoint": True,
            "checkpoint_output": "/scratch/checkpoints/restart.bp",
        }
    )
    assert adapter is not None

    observations = adapter.observe_artifacts(
        "steps: 20\nSimulation at step 20 writing output step 2\n"
    )
    observations += adapter.finalize_artifacts_for_exit(0)

    checkpoint = next(
        item for item in observations if item.role is ArtifactRole.CHECKPOINT
    )
    assert checkpoint.logical_name == "gray-scott-restart-checkpoint"
    assert checkpoint.state is ArtifactState.FINALIZED
    assert checkpoint.location is not None
    assert checkpoint.location.value == "/scratch/checkpoints/restart.bp"
    assert checkpoint.metadata["physical_path_observed"] is True
    assert checkpoint.metadata["completion_signal"] == (
        "process_exit_zero_after_final_output"
    )


def test_gray_scott_does_not_claim_unchanged_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkpoint left by an earlier run is not attributed to this run."""
    module = _module()
    unchanged = ((".", 16877, 1, 64, 2, 2), ())
    monkeypatch.setattr(module, "_path_fingerprint", lambda _path: unchanged)
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.gray_scott",
            "effective_deploy_mode": "default",
            "outdir": "/scratch/run/gray-scott.bp",
            "checkpoint": True,
            "checkpoint_output": "/scratch/checkpoints/restart.bp",
        }
    )
    assert adapter is not None

    observations = adapter.observe_artifacts(
        "steps: 20\nSimulation at step 20 writing output step 2\n"
    )
    observations += adapter.finalize_artifacts_for_exit(0)

    assert all(item.role is not ArtifactRole.CHECKPOINT for item in observations)


def test_gray_scott_failed_process_leaves_changed_checkpoint_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A physical checkpoint cannot be finalized after producer failure."""
    module = _module()
    changed = ((".", 16877, 1, 64, 2, 2), ())
    fingerprints = iter([None, changed])
    monkeypatch.setattr(
        module,
        "_path_fingerprint",
        lambda _path: next(fingerprints),
    )
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.gray_scott",
            "effective_deploy_mode": "default",
            "outdir": "/scratch/run/gray-scott.bp",
            "checkpoint": True,
            "checkpoint_output": "/scratch/checkpoints/restart.bp",
        }
    )
    assert adapter is not None

    observations = adapter.observe_artifacts(
        "steps: 20\nSimulation at step 10 writing output step 1\n"
    )
    observations += adapter.finalize_artifacts_for_exit(12)

    checkpoint = next(
        item for item in observations if item.role is ArtifactRole.CHECKPOINT
    )
    assert checkpoint.state is ArtifactState.INCOMPLETE
    assert checkpoint.metadata["return_code"] == 12
    assert checkpoint.metadata["completion_signal"] == "process_exit_nonzero"


def test_gray_scott_disabled_checkpoint_is_never_probed_or_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured filename alone does not imply checkpoint production."""
    module = _module()

    def unexpected_probe(_path: object) -> object:
        raise AssertionError("disabled checkpoints must not touch the filesystem")

    monkeypatch.setattr(module, "_path_fingerprint", unexpected_probe)
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.gray_scott",
            "effective_deploy_mode": "default",
            "outdir": "/scratch/run/gray-scott.bp",
            "checkpoint": False,
            "checkpoint_output": "/scratch/checkpoints/restart.bp",
        }
    )
    assert adapter is not None

    observations = adapter.observe_artifacts(
        "steps: 20\nSimulation at step 20 writing output step 2\n"
    )
    observations += adapter.finalize_artifacts_for_exit(0)

    assert all(item.role is not ArtifactRole.CHECKPOINT for item in observations)
