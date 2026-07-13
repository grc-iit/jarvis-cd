"""Tests for builtin LAMMPS generated-artifact semantics."""

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
        / "lammps"
        / "artifacts.py"
    )
    return load_artifacts_module(path)


def test_lammps_finalization_discovers_only_bounded_known_outputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The provider does not crawl or register unrelated output-root content."""
    script = tmp_path / "in.simulation"
    script.write_text(
        "write_data atoms.snapshot\n"
        "write_restart state.chk\n"
        "write_dump all custom viz.bp id type x y z\n"
        "write_data /outside/foreign.data\n",
        encoding="utf-8",
    )
    for name, payload in {
        "log.lammps": "thermo output\n",
        "dump.0.lammpstrj": "frame zero\n",
        "dump.100.lammpstrj": "frame one\n",
        "restart.100": "restart\n",
        "state.chk": "checkpoint\n",
        "atoms.snapshot": "atoms\n",
        "foreign.data": "not package-declared here\n",
        "notes.txt": "unrelated\n",
    }.items():
        (tmp_path / name).write_text(payload, encoding="utf-8")
    (tmp_path / "viz.bp").mkdir()
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "dump.999.lammpstrj").write_text("nested\n", encoding="utf-8")

    module = _module()
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.lammps",
            "out": "/scratch/run",
            "script": str(script),
        }
    )
    assert adapter is not None
    monkeypatch.setattr(module, "Path", lambda _value: tmp_path)

    assert adapter.observe_artifacts("LAMMPS stdout is not filesystem truth\n") == []
    observations = adapter.finalize_artifacts()

    assert adapter.finalize_artifacts() == []
    assert {item.ownership for item in observations} == {ArtifactOwnership.SHARED}
    logical_names = {item.logical_name for item in observations}
    assert "log.lammps" in logical_names
    assert "atoms.snapshot" in logical_names
    assert "viz.bp" in logical_names
    assert "foreign.data" not in logical_names
    assert "notes.txt" not in logical_names

    dumps = next(
        item for item in observations if item.logical_name == "lammps-trajectory-dumps"
    )
    assert dumps.role is ArtifactRole.OUTPUT
    assert dumps.structure is ArtifactStructure.COLLECTION
    assert dumps.state is ArtifactState.FINALIZED
    assert dumps.metadata["member_count_observed"] == 2
    assert "nested/dump.999.lammpstrj" not in dumps.metadata["member_names"]

    checkpoints = next(
        item for item in observations if item.logical_name == "lammps-restarts"
    )
    assert checkpoints.role is ArtifactRole.CHECKPOINT
    assert checkpoints.metadata["member_count_observed"] == 2
    assert set(checkpoints.metadata["member_names"]) == {
        "restart.100",
        "state.chk",
    }
    viz = next(item for item in observations if item.logical_name == "viz.bp")
    assert viz.structure is ArtifactStructure.COLLECTION
    assert viz.format == "adios2-bp"


def test_lammps_discovery_reports_safety_bound_as_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A bounded scan cannot silently claim a complete collection manifest."""
    for index in range(3):
        (tmp_path / f"dump.{index}.lammpstrj").write_text(str(index), encoding="utf-8")
    module = _module()
    adapter = module.adapter_from_package(
        {"pkg_type": "builtin.lammps", "out": "/scratch/bounded"}
    )
    assert adapter is not None
    monkeypatch.setattr(module, "Path", lambda _value: tmp_path)
    monkeypatch.setattr(module, "_MAX_DIRECTORY_ENTRIES", 2)

    observations = adapter.finalize_artifacts()

    assert len(observations) == 1
    assert observations[0].state is ArtifactState.INCOMPLETE
    assert observations[0].metadata["discovery_truncated"] is True


def test_lammps_factory_rejects_unscoped_output_authority() -> None:
    """Relative output is resolved only with JARVIS-owned runtime context."""
    module = _module()
    assert module.adapter_from_package({"pkg_type": "builtin.paraview"}) is None
    with pytest.raises(ValueError, match="runtime working directory"):
        module.adapter_from_package(
            {"pkg_type": "builtin.lammps", "out": "relative/output"}
        )

    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.lammps",
            "out": "relative/output",
            "script": "in.lj",
            "runtime_cwd": "/work",
        }
    )

    assert adapter is not None
    assert adapter.output_dir.as_posix() == "/work/relative/output"
    assert adapter.script_path == Path("/work/relative/output/in.lj")


def test_container_lammps_reports_only_host_visible_output() -> None:
    """Container-private paths are not claimed as durable cluster artifacts."""
    module = _module()
    hidden = module.adapter_from_package(
        {
            "pkg_type": "builtin.lammps",
            "out": "/tmp/lammps_out",
            "effective_deploy_mode": "container",
            "shared_dir": "/shared/execution",
            "private_dir": "/private/execution",
            "runtime_cwd": "/work",
        }
    )
    visible = module.adapter_from_package(
        {
            "pkg_type": "builtin.lammps",
            "out": "/shared/execution/lammps",
            "effective_deploy_mode": "container",
            "shared_dir": "/shared/execution",
            "private_dir": "/private/execution",
            "runtime_cwd": "/work",
        }
    )

    assert hidden is None
    assert visible is not None
