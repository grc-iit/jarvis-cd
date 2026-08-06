"""Tests for builtin Xcompact3D generated-artifact semantics."""

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
        / "xcompact3d"
        / "artifacts.py"
    )
    return load_artifacts_module(path)


def _write_products(root: Path) -> None:
    (root / "channel").mkdir(parents=True)
    (root / "channel" / "input.i3d").write_text("input\n", encoding="utf-8")
    (root / "channel" / "jarvis-input.i3d").write_text("input\n", encoding="utf-8")
    (root / "channel" / "adios2_config.xml").write_text("<xml/>\n", encoding="utf-8")
    (root / "channel" / "xcompact3d.log").write_text(
        "UT 6.666662e-1 -4.6e-7\n", encoding="utf-8"
    )
    (root / "channel" / "checkpoint").write_bytes(b"checkpoint")
    (root / "channel" / "restart.info").write_text("100\n", encoding="utf-8")
    (root / "channel" / "data").mkdir()
    (root / "channel" / "data" / "snapshot-1.xdmf").write_text(
        "<Xdmf/>\n", encoding="utf-8"
    )
    (root / "channel" / "statistics").mkdir()
    (root / "channel" / "statistics" / "umean.dat100").write_bytes(b"stats")


def test_finalization_reports_owned_results_and_excludes_staged_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scientific outputs are visible without reclassifying exact inputs."""

    _write_products(tmp_path)
    module = _module()
    adapter = module.Xcompact3dArtifactAdapter(
        module.PurePosixPath("/scratch/xcompact3d"),
        frozenset(
            {
                module.PurePosixPath("channel/input.i3d"),
                module.PurePosixPath("channel/adios2_config.xml"),
                module.PurePosixPath("channel/jarvis-input.i3d"),
            }
        ),
    )
    monkeypatch.setattr(adapter, "_local_output_path", lambda: tmp_path)

    observations = adapter.finalize_artifacts_for_exit(0)

    assert adapter.finalize_artifacts_for_exit(0) == []
    collection = observations[0]
    assert collection.logical_name == "xcompact3d-results"
    assert collection.role is ArtifactRole.OUTPUT
    assert collection.structure is ArtifactStructure.COLLECTION
    assert collection.state is ArtifactState.FINALIZED
    assert "channel/input.i3d" not in collection.metadata["member_names"]
    assert "channel/jarvis-input.i3d" not in collection.metadata["member_names"]
    names = {item.logical_name: item for item in observations[1:]}
    assert names["xcompact3d-log"].format == "xcompact3d-runtime-log"
    assert names["xcompact3d-checkpoint"].checksum is not None
    assert names["xcompact3d-data"].structure is ArtifactStructure.COLLECTION
    assert names["xcompact3d-statistics"].structure is ArtifactStructure.COLLECTION


def test_nonzero_exit_marks_discovered_outputs_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Filesystem presence cannot turn a failed solver run into success."""

    _write_products(tmp_path)
    module = _module()
    adapter = module.Xcompact3dArtifactAdapter(
        module.PurePosixPath("/scratch/xcompact3d")
    )
    monkeypatch.setattr(adapter, "_local_output_path", lambda: tmp_path)

    observations = adapter.finalize_artifacts_for_exit(17)

    assert observations
    assert {item.state for item in observations} == {ArtifactState.INCOMPLETE}


def test_discovery_bound_cannot_claim_complete_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A truncated output walk is visibly incomplete."""

    _write_products(tmp_path)
    module = _module()
    adapter = module.Xcompact3dArtifactAdapter(
        module.PurePosixPath("/scratch/xcompact3d")
    )
    monkeypatch.setattr(adapter, "_local_output_path", lambda: tmp_path)
    monkeypatch.setattr(module, "_MAX_DISCOVERED_ENTRIES", 2)

    observations = adapter.finalize_artifacts_for_exit(0)

    assert observations[0].state is ArtifactState.INCOMPLETE
    assert observations[0].metadata["discovery_truncated"] is True


def test_factory_scopes_relative_output_to_package_shared_root() -> None:
    """Artifact discovery receives the same execution-owned root as launch."""

    module = _module()

    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.xcompact3d",
            "out": "run",
            "shared_dir": "/execution/shared/xcompact3d",
            "runtime_cwd": "/execution/runtime",
        }
    )

    assert adapter is not None
    assert adapter.output_dir.as_posix() == "/execution/shared/xcompact3d/run"
    assert module.adapter_from_package({"pkg_type": "builtin.lammps"}) is None


def test_container_private_output_is_not_claimed() -> None:
    """A host cannot report artifacts from an unmounted container path."""

    module = _module()
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.xcompact3d",
            "out": "/tmp/xcompact3d",
            "effective_deploy_mode": "container",
            "shared_dir": "/execution/shared",
            "private_dir": "/execution/private",
            "runtime_cwd": "/execution/runtime",
        }
    )

    assert adapter is None
