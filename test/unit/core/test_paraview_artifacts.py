"""Tests for generic builtin ParaView generated-artifact semantics."""

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
from jarvis_cd.progress import PROGRESS_LINE_PREFIX, ProgressEvent, ProgressState


def _module() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[3]
        / "builtin"
        / "builtin"
        / "paraview"
        / "artifacts.py"
    )
    return load_artifacts_module(path)


def _line(
    *,
    sequence: int,
    output_path: str | None,
    state: ProgressState = ProgressState.RUNNING,
    current: float = 1,
    total: float | None = 2,
) -> str:
    metadata: dict[str, str | float | bool] = {
        "progress_kind": "pvbatch_completed_unit",
        "renderer": "paraview",
        "completion_signal": "render_returned",
        "completed_after_render": True,
        "timestep": current - 1,
    }
    if output_path is not None:
        metadata["output_path"] = output_path
    event = ProgressEvent(
        package_name="builtin.paraview",
        package_id="asteroid_render",
        execution_id="exec_asteroid",
        label="frame",
        state=state,
        current=current,
        total=total,
        unit="frame",
        sequence=sequence,
        metadata=metadata,
    )
    return f"{PROGRESS_LINE_PREFIX}{event.to_json()}\n"


def test_paraview_translates_progress_output_paths_and_finalizes_revisions() -> None:
    """Distinct render paths become typed, queryable output artifacts."""
    module = _module()
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.paraview",
            "pkg_id": "asteroid_render",
            "mode": "batch",
            "cwd": "/scratch/render",
        }
    )
    assert adapter is not None

    first = adapter.observe_artifacts(
        _line(sequence=1, output_path="frames/frame-0001.png")
    )
    final = adapter.observe_artifacts(
        _line(
            sequence=2,
            output_path="/scratch/render/asteroid.mp4",
            state=ProgressState.COMPLETED,
            current=2,
        )
    )
    sealed = adapter.finalize_artifacts()

    assert len(first) == 1
    assert first[0].state is ArtifactState.AVAILABLE
    assert first[0].ownership is ArtifactOwnership.SHARED
    assert first[0].location is not None
    assert first[0].location.value == "/scratch/render/frames/frame-0001.png"
    assert first[0].kind == "image"
    assert first[0].media_type == "image/png"
    assert first[0].metadata["generation_stage"] == "intermediate"

    assert len(final) == 1
    assert final[0].state is ArtifactState.FINALIZED
    assert final[0].kind == "video"
    assert final[0].format == "mp4"
    assert final[0].metadata["generation_stage"] == "final"

    assert len(sealed) == 1
    assert sealed[0].artifact_id == first[0].artifact_id
    assert sealed[0].state is ArtifactState.FINALIZED
    assert sealed[0].metadata["finalized_at_execution_end"] is True
    assert adapter.finalize_artifacts() == []


def test_paraview_dataset_mapping_and_stable_path_identity() -> None:
    """Repeated reports revise one ADIOS collection rather than duplicating it."""
    module = _module()
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.paraview",
            "pkg_id": "asteroid_render",
            "mode": "batch",
            "cwd": "/scratch/render",
        }
    )
    assert adapter is not None

    producing = adapter.observe_artifacts(
        _line(sequence=1, output_path="processed.bp")
    )[0]
    finalized = adapter.observe_artifacts(
        _line(
            sequence=2,
            output_path="processed.bp",
            state=ProgressState.COMPLETED,
            current=2,
        )
    )[0]

    assert producing.artifact_id == finalized.artifact_id
    assert finalized.structure is ArtifactStructure.COLLECTION
    assert finalized.kind == "scientific_dataset"
    assert finalized.format == "adios2-bp"
    with pytest.raises(ValueError, match="after artifact finalization"):
        adapter.observe_artifacts(
            _line(sequence=3, output_path="processed.bp", current=2)
        )


def test_paraview_server_and_progress_without_output_claim_no_artifacts() -> None:
    """A server readiness/progress event is not fabricated into an output."""
    module = _module()
    assert (
        module.adapter_from_package({"pkg_type": "builtin.paraview", "mode": "server"})
        is None
    )
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.paraview",
            "pkg_id": "asteroid_render",
            "mode": "batch",
            "cwd": "/scratch/render",
        }
    )
    assert adapter is not None
    assert adapter.observe_artifacts(_line(sequence=1, output_path=None)) == []


def test_paraview_rejects_output_path_traversal() -> None:
    """Structured metadata cannot escape path normalization checks."""
    module = _module()
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.paraview",
            "pkg_id": "asteroid_render",
            "mode": "batch",
            "cwd": "/scratch/render",
        }
    )
    assert adapter is not None
    with pytest.raises(ValueError, match="normalized"):
        adapter.observe_artifacts(_line(sequence=1, output_path="../outside/frame.png"))


def test_paraview_finalization_discovers_render_frames_never_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A batch script that never calls the progress reporter still registers frames.

    This mirrors the LAMMPS trajectory recognizer: a bounded, non-recursive
    scan of the package-configured working directory at finalization time, so
    composed simulate-then-render pipelines expose their rendered images even
    when the render script issues no structured progress lines (issue #209).
    """
    frame_names = [
        "frame_0000_step000000000.png",
        "frame_0001_step000000100.png",
        "frame_0002_step000000200.png",
        "frame_0003_step000000300.png",
        "frame_0004_step000000400.png",
        "frame_0005_step000000500.png",
    ]
    for name in frame_names:
        (tmp_path / name).write_bytes(b"not-a-real-png")

    module = _module()
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.paraview",
            "pkg_id": "asteroid_render",
            "mode": "batch",
            "cwd": "/scratch/render",
        }
    )
    assert adapter is not None
    monkeypatch.setattr(module, "Path", lambda _value: tmp_path)

    observations = adapter.finalize_artifacts()

    assert len(observations) == len(frame_names)
    assert {item.logical_name for item in observations} == set(frame_names)
    for item in observations:
        assert item.kind == "image"
        assert item.role is ArtifactRole.OUTPUT
        assert item.structure is ArtifactStructure.FILE
        assert item.ownership is ArtifactOwnership.SHARED
        assert item.state is ArtifactState.FINALIZED
        assert item.media_type == "image/png"
        assert item.format == "png"
        assert item.location is not None
        assert item.location.value == f"/scratch/render/{item.logical_name}"

    # A second finalization does not re-discover or re-register the frames.
    assert adapter.finalize_artifacts() == []


def test_paraview_finalization_with_empty_render_directory_registers_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Zero rendered frames must not fabricate artifacts or raise errors."""
    module = _module()
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.paraview",
            "pkg_id": "asteroid_render",
            "mode": "batch",
            "cwd": "/scratch/render",
        }
    )
    assert adapter is not None
    monkeypatch.setattr(module, "Path", lambda _value: tmp_path)

    assert adapter.finalize_artifacts() == []


def test_paraview_finalization_ignores_unrecognized_files_in_render_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Non-render files sitting beside the frames are never claimed as outputs."""
    (tmp_path / "frame_0000_step000000000.png").write_bytes(b"not-a-real-png")
    (tmp_path / "render.py").write_text(
        "# the pvbatch script itself\n", encoding="utf-8"
    )
    (tmp_path / "notes.txt").write_text("unrelated\n", encoding="utf-8")
    (tmp_path / "state.pvsm").write_text("<xml/>\n", encoding="utf-8")

    module = _module()
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.paraview",
            "pkg_id": "asteroid_render",
            "mode": "batch",
            "cwd": "/scratch/render",
        }
    )
    assert adapter is not None
    monkeypatch.setattr(module, "Path", lambda _value: tmp_path)

    observations = adapter.finalize_artifacts()

    logical_names = {item.logical_name for item in observations}
    assert logical_names == {"frame_0000_step000000000.png"}


def test_paraview_finalization_does_not_duplicate_progress_reported_frames(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A frame already reported through progress lines is not rediscovered."""
    (tmp_path / "frame_0000_step000000000.png").write_bytes(b"not-a-real-png")
    (tmp_path / "frame_0001_step000000100.png").write_bytes(b"not-a-real-png")

    module = _module()
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.paraview",
            "pkg_id": "asteroid_render",
            "mode": "batch",
            "cwd": "/scratch/render",
        }
    )
    assert adapter is not None
    reported = adapter.observe_artifacts(
        _line(
            sequence=1,
            output_path="frame_0000_step000000000.png",
            state=ProgressState.COMPLETED,
        )
    )
    assert len(reported) == 1
    monkeypatch.setattr(module, "Path", lambda _value: tmp_path)

    observations = adapter.finalize_artifacts()

    logical_names = [item.logical_name for item in observations]
    assert logical_names == ["frame_0001_step000000100.png"]


def test_paraview_finalization_without_configured_cwd_skips_discovery() -> None:
    """No configured working directory means no filesystem authority to scan."""
    module = _module()
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.paraview",
            "pkg_id": "asteroid_render",
            "mode": "batch",
        }
    )
    assert adapter is not None
    assert adapter.finalize_artifacts() == []
