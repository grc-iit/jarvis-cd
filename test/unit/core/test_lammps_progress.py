"""Tests for the package-owned builtin LAMMPS progress provider."""

from __future__ import annotations

from pathlib import Path

from jarvis_cd.progress.lammps import (
    LammpsThermoProgressAdapter,
    adapter_from_package,
)


def test_adapter_is_owned_by_builtin_lammps(tmp_path: Path) -> None:
    """The factory must activate only for the JARVIS builtin package."""
    assert adapter_from_package({"pkg_type": "builtin.echo"}) is None

    adapter = adapter_from_package(
        {
            "pkg_type": "builtin.lammps",
            "pkg_version": "1.2",
            "out": str(tmp_path),
            "progress": {"total_steps": 100},
        }
    )

    assert isinstance(adapter, LammpsThermoProgressAdapter)
    assert adapter.package_name == "builtin.lammps"
    assert adapter.package_version == "1.2"
    assert adapter.total_steps == 100
    assert adapter.progress_log_paths() == [tmp_path / "log.lammps"]


def test_adapter_observes_only_lammps_jarvis_scope() -> None:
    """Unscoped or unrelated stdout must not create trusted progress."""
    adapter = LammpsThermoProgressAdapter(total_steps=100, run_id="job_1")
    thermo = "run 100\nStep Temp CPU\n0 1.0 0.0\n25 1.1 1.0\n50 1.2 2.0\n75 1.3 3.0\n"

    assert adapter.observe_jarvis_stdout(thermo) == []
    assert (
        adapter.observe_jarvis_stdout(
            "[builtin.echo] [START] BEGIN\n" + thermo + "[builtin.echo] [START] END\n"
        )
        == []
    )

    records = adapter.observe_jarvis_stdout(
        "[builtin.lammps] [START] BEGIN\n" + thermo + "[builtin.lammps] [START] END\n"
    )

    assert [record["current"] for record in records] == [0.0, 25.0, 50.0, 75.0]
    metadata = records[-1]["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["source"] == "jarvis_package"
    assert metadata["package_name"] == "builtin.lammps"
    assert metadata["run_id"] == "job_1"
    assert metadata["execution_id"] == "job_1"
    assert adapter.acceptance_progress_valid(metadata)


def test_adapter_rejects_unobserved_acceptance_claims() -> None:
    """Acceptance requires LAMMPS-owned timing and a real step record."""
    adapter = LammpsThermoProgressAdapter()

    assert not adapter.acceptance_progress_valid(
        {
            "adapter": "lammps",
            "prediction_status": "claimed",
            "timing_source": "lammps_thermo_cpu",
            "eta_seconds": 1.0,
            "absolute_step": 10.0,
        }
    )
    assert not adapter.acceptance_progress_valid(
        {
            "adapter": "lammps",
            "prediction_status": "observed_lammps_timing",
            "timing_source": "lammps_thermo_cpu",
            "eta_seconds": -1.0,
            "absolute_step": 10.0,
        }
    )
