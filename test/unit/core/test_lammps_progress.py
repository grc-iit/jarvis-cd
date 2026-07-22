"""Tests for the package-owned builtin LAMMPS progress provider."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis_cd.progress import (
    PackageProgressProvider,
    ProcessExitProgressProvider,
    ProgressObservation,
    ProgressState,
    RelayProgressAdapter,
)
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
            "progress": {"log_visibility": "shared", "total_steps": 100},
        }
    )

    assert isinstance(adapter, LammpsThermoProgressAdapter)
    assert adapter.package_name == "builtin.lammps"
    assert adapter.package_id == "lammps"
    assert adapter.package_version
    assert adapter.application_profile == "jarvis-cd.builtin.lammps"
    assert adapter.log_visibility == "shared"
    assert adapter.total_steps == 100
    assert adapter.progress_log_paths() == [tmp_path / "log.lammps"]
    assert isinstance(adapter, PackageProgressProvider)
    assert isinstance(adapter, ProcessExitProgressProvider)
    assert isinstance(adapter, RelayProgressAdapter)

    container_adapter = adapter_from_package(
        {
            "deploy_mode": "container",
            "out": "/tmp/container-only",
            "pkg_type": "builtin.lammps",
        }
    )
    assert isinstance(container_adapter, LammpsThermoProgressAdapter)
    assert container_adapter.progress_log_paths() == []
    assert container_adapter.log_visibility == "scoped_stdout"

    inherited_container_adapter = adapter_from_package(
        {
            "effective_deploy_mode": "container",
            "out": "/tmp/container-only",
            "pkg_type": "builtin.lammps",
        }
    )
    assert isinstance(inherited_container_adapter, LammpsThermoProgressAdapter)
    assert inherited_container_adapter.progress_log_paths() == []

    node_local_adapter = adapter_from_package(
        {
            "out": "/tmp/node-local-lammps",
            "pkg_type": "builtin.lammps",
            "progress": {"total_steps": 100},
        }
    )
    assert isinstance(node_local_adapter, LammpsThermoProgressAdapter)
    assert node_local_adapter.progress_log_paths() == []
    assert node_local_adapter.log_visibility == "scoped_stdout"


def test_default_output_progress_uses_execution_package_shared_log(
    tmp_path: Path,
) -> None:
    """The portable default exposes the execution-owned log without stdout scraping."""
    shared_dir = (tmp_path / "execution" / "shared" / "lammps").resolve()

    adapter = adapter_from_package(
        {
            "pkg_type": "builtin.lammps",
            "out": ".",
            "shared_dir": str(shared_dir),
            "io_dump_interval": 100,
            "io_run_steps": 5000,
        }
    )

    assert isinstance(adapter, LammpsThermoProgressAdapter)
    assert adapter.log_visibility == "shared"
    assert adapter.progress_log_paths() == [shared_dir / "log.lammps"]
    assert adapter.total_steps == 5000


def test_caller_script_does_not_inherit_generated_workload_total(
    tmp_path: Path,
) -> None:
    """An arbitrary input script cannot inherit the built-in smoke-test size."""
    shared_dir = (tmp_path / "execution" / "shared" / "lammps").resolve()

    adapter = adapter_from_package(
        {
            "pkg_type": "builtin.lammps",
            "out": ".",
            "shared_dir": str(shared_dir),
            "script": str(tmp_path / "staged" / "in.research"),
            "io_dump_interval": 100,
            "io_run_steps": 5000,
        }
    )

    assert isinstance(adapter, LammpsThermoProgressAdapter)
    assert adapter.progress_log_paths() == [shared_dir / "log.lammps"]
    assert adapter.total_steps is None


def test_caller_script_success_completes_indeterminate_progress() -> None:
    """Process exit completes custom work without inventing a timestep total."""
    provider = LammpsThermoProgressAdapter(total_steps=None)
    observed = provider.observe_progress(
        "run 250\nStep Temp CPU\n0 1.0 0.0\n125 1.1 1.0\n250 1.2 2.0\n"
    )

    terminal = provider.finalize_progress_for_exit(0)

    assert [item.current for item in observed] == [0.0, 125.0, 250.0]
    assert all(item.total is None for item in observed)
    assert len(terminal) == 1
    assert terminal[0].state is ProgressState.COMPLETED
    assert terminal[0].current == 250.0
    assert terminal[0].total is None
    assert terminal[0].metadata["completion_signal"] == (
        "process_exit_zero_with_unknown_total"
    )
    assert terminal[0].metadata["return_code"] == 0
    assert provider.finalize_progress_for_exit(0) == []


def test_caller_script_success_without_thermo_is_still_terminal() -> None:
    """A quiet custom script retains authoritative process completion state."""
    provider = LammpsThermoProgressAdapter(total_steps=None)

    terminal = provider.finalize_progress_for_exit(0)

    assert len(terminal) == 1
    assert terminal[0].state is ProgressState.COMPLETED
    assert terminal[0].current is None
    assert terminal[0].total is None
    assert terminal[0].message == "LAMMPS completed successfully"
    assert terminal[0].metadata["progress_source"] == "process_exit"


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
    assert metadata["package_id"] == "lammps"
    assert metadata["run_id"] == "job_1"
    assert metadata["execution_id"] == "job_1"
    assert adapter.acceptance_progress_valid(metadata)


def test_lammps_provider_exposes_typed_jarvis_observations() -> None:
    """JARVIS core consumes a typed SPI independently of the relay projection."""
    provider = LammpsThermoProgressAdapter(total_steps=10)

    observations = provider.observe_progress(
        "run 10\nStep Temp CPU\n0 1.0 0.0\n5 1.1 1.0\n"
    )

    assert all(isinstance(item, ProgressObservation) for item in observations)
    assert [item.current for item in observations] == [0.0, 5.0]
    assert [item.total for item in observations] == [10, 10]


def test_lammps_successful_exit_completes_observed_final_timestep() -> None:
    """A zero process exit commits an observed final thermo row exactly once."""
    provider = LammpsThermoProgressAdapter(
        total_steps=100,
        package_version="test-version",
    )
    observed = provider.observe_progress(
        "run 100\nStep Temp CPU\n0 1.0 0.0\n50 1.1 1.0\n100 1.2 2.0"
    )

    terminal = provider.finalize_progress_for_exit(0)

    assert [item.current for item in observed] == [0.0, 50.0]
    assert [item.current for item in terminal] == [100.0, 100.0]
    assert terminal[-1].state is ProgressState.COMPLETED
    assert terminal[-1].total == 100.0
    assert terminal[-1].unit == "step"
    assert terminal[-1].metadata["source"] == "jarvis_package"
    assert terminal[-1].metadata["package_version"] == "test-version"
    assert terminal[-1].metadata["completion_signal"] == (
        "process_exit_zero_after_final_timestep"
    )
    assert terminal[-1].metadata["return_code"] == 0
    assert provider.finalize_progress_for_exit(0) == []


def test_lammps_successful_exit_does_not_invent_unobserved_progress() -> None:
    """Process success alone cannot fabricate a missing final thermo step."""
    provider = LammpsThermoProgressAdapter(total_steps=100)
    observed = provider.observe_progress(
        "run 100\nStep Temp CPU\n0 1.0 0.0\n50 1.1 1.0\n"
    )

    terminal = provider.finalize_progress_for_exit(0)

    assert [item.current for item in observed] == [0.0, 50.0]
    assert terminal == []
    assert all(item.state is ProgressState.RUNNING for item in observed)


def test_lammps_failed_exit_terminates_at_last_observed_timestep() -> None:
    """A nonzero process exit records failure without advancing progress."""
    provider = LammpsThermoProgressAdapter(total_steps=100)
    observed = provider.observe_progress(
        "run 100\nStep Temp CPU\n0 1.0 0.0\n25 1.1 1.0\n"
    )

    terminal = provider.finalize_progress_for_exit(7)

    assert [item.current for item in observed] == [0.0, 25.0]
    assert len(terminal) == 1
    assert terminal[0].state is ProgressState.FAILED
    assert terminal[0].current == 25.0
    assert terminal[0].total == 100.0
    assert terminal[0].metadata["completion_signal"] == "process_exit_nonzero"
    assert terminal[0].metadata["return_code"] == 7
    assert provider.finalize_progress_for_exit(7) == []


def test_lammps_process_exit_rejects_non_integer_status() -> None:
    """The package provider accepts only an actual integer process result."""
    provider = LammpsThermoProgressAdapter(total_steps=100)

    with pytest.raises(TypeError, match="process return code must be an integer"):
        provider.finalize_progress_for_exit(True)


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


def test_adapter_buffers_partial_stdout_lines() -> None:
    """Arbitrary pipe and log chunk boundaries must not lose thermo records."""
    adapter = LammpsThermoProgressAdapter(total_steps=100, run_id="job_chunked")

    assert adapter.observe_stdout("run 100\nStep Temp C") == []
    first = adapter.observe_stdout("PU\n0 1.0 0.0\n25 1.1 ")
    second = adapter.observe_stdout("1.0\n50 1.2 2.0\n75 1.3 3.0\n")

    assert [record["current"] for record in first + second] == [0.0, 25.0, 50.0, 75.0]
    metadata = second[-1]["metadata"]
    assert isinstance(metadata, dict)
    assert adapter.acceptance_progress_valid(metadata)


def test_adapter_flushes_unterminated_fragments_at_eof() -> None:
    """The stream owner can explicitly flush a final unterminated thermo row."""
    adapter = LammpsThermoProgressAdapter(total_steps=100)

    assert adapter.observe_stdout("run 100\nStep Temp CPU\n0 1.0 0.0") == []
    records = adapter.finalize_stdout()

    assert [record["current"] for record in records] == [0.0]
    assert adapter.stdout_fragment == ""


def test_adapter_flushes_unterminated_jarvis_scope_at_eof() -> None:
    """EOF flushing preserves a final row and closes JARVIS package scope."""
    adapter = LammpsThermoProgressAdapter(total_steps=100)

    assert (
        adapter.observe_jarvis_stdout(
            "[builtin.lammps] [START] BEGIN\nrun 100\nStep Temp CPU\n0 1.0 0.0"
        )
        == []
    )
    records = adapter.finalize_jarvis_stdout()

    assert [record["current"] for record in records] == [0.0]
    assert adapter.jarvis_stdout_fragment == ""
    assert adapter.active_package_stdout is False


def test_adapter_rejects_nonfinite_input_and_predictions(tmp_path: Path) -> None:
    """NaN and infinity must never escape into progress JSON or messages."""
    adapter = adapter_from_package(
        {
            "pkg_type": "builtin.lammps",
            "out": str(tmp_path),
            "progress": {"total_steps": float("inf")},
        }
    )
    assert isinstance(adapter, LammpsThermoProgressAdapter)
    assert adapter.total_steps is None
    assert adapter.observe_stdout("Step Temp CPU\nnan 1.0 0.0\ninf 1.0 1.0\n") == []

    huge = adapter_from_package(
        {
            "pkg_type": "builtin.lammps",
            "progress": {"total_steps": 10**10000},
        }
    )
    assert isinstance(huge, LammpsThermoProgressAdapter)
    assert huge.total_steps is None

    overflow = LammpsThermoProgressAdapter(total_steps=1e308)
    records = overflow.observe_stdout(
        "run 1e308\nStep Temp CPU\n0 1.0 0\n1 1.0 5e307\n2 1.0 1e308\n3 1.0 1.5e308\n"
    )

    assert records[-1]["metadata"]["prediction_status"] == (
        "nonfinite_prediction_rejected"
    )
    json.dumps(records, allow_nan=False)
    assert not overflow.acceptance_progress_valid(
        {
            "adapter": "lammps",
            "prediction_status": "observed_lammps_timing",
            "timing_source": "lammps_thermo_cpu",
            "eta_seconds": float("inf"),
            "absolute_step": 3.0,
        }
    )
    assert not overflow.acceptance_progress_valid(
        {
            "adapter": "lammps",
            "prediction_status": "observed_lammps_timing",
            "timing_source": "lammps_thermo_cpu",
            "eta_seconds": 10**10000,
            "absolute_step": 3.0,
        }
    )


@pytest.mark.parametrize("first_source", ["package_log", "jarvis_stdout"])
def test_adapter_never_double_counts_dual_source_replay(first_source: str) -> None:
    """A thermo run replayed from the second channel must not advance twice."""
    adapter = LammpsThermoProgressAdapter(total_steps=100)
    thermo = (
        "run 100\nStep Temp CPU\n"
        "0 1.0 0.0\n50 1.1 1.0\n100 1.2 2.0\n"
        "Loop time of 2.0 on 1 procs for 100 steps\n"
    )
    jarvis = f"[builtin.lammps] [START] BEGIN\n{thermo}[builtin.lammps] [START] END\n"

    if first_source == "package_log":
        first = adapter.observe_stdout(thermo)
        replay = adapter.observe_jarvis_stdout(jarvis)
    else:
        first = adapter.observe_jarvis_stdout(jarvis)
        replay = adapter.observe_stdout(thermo)

    assert [record["current"] for record in first] == [0.0, 50.0, 100.0]
    assert replay == []
    assert adapter.authoritative_source == first_source
    assert adapter.completed_steps == 100.0


def test_adapter_resets_package_log_after_replacement() -> None:
    """A replaced or truncated log begins a fresh execution at step zero."""
    adapter = LammpsThermoProgressAdapter(total_steps=100)
    thermo = (
        "run 100\nStep Temp CPU\n"
        "0 1.0 0.0\n50 1.1 1.0\n100 1.2 2.0\n"
        "Loop time of 2.0 on 1 procs for 100 steps\n"
    )
    assert [record["current"] for record in adapter.observe_stdout(thermo)] == [
        0.0,
        50.0,
        100.0,
    ]

    adapter.reset_stdout()
    replacement = adapter.observe_stdout(
        "run 100\nStep Temp CPU\n0 1.0 0.0\n25 1.1 1.0\n"
    )

    assert [record["current"] for record in replacement] == [0.0, 25.0]
    assert adapter.authoritative_source == "package_log"
    assert adapter.completed_steps == 0.0


def test_adapter_counts_run_upto_from_observed_timestep_delta() -> None:
    """LAMMPS ``run N upto`` uses N as a target, not a relative run length."""
    adapter = LammpsThermoProgressAdapter(total_steps=150)

    records = adapter.observe_stdout(
        "reset_timestep 100\nrun 200 upto\nStep Temp CPU\n"
        "100 1.0 0.0\n150 1.1 1.0\n200 1.2 2.0\n"
        "Loop time of 2.0 on 1 procs for 100 steps\n"
        "run 50\nStep Temp CPU\n"
        "200 1.2 0.0\n225 1.3 1.0\n250 1.4 2.0\n"
        "Loop time of 2.0 on 1 procs for 50 steps\n"
    )

    assert [record["current"] for record in records] == [
        0.0,
        50.0,
        100.0,
        100.0,
        125.0,
        150.0,
    ]
    assert adapter.completed_steps == 150.0


def test_adapter_rejects_zero_elapsed_rate_for_eta() -> None:
    """Rounded duplicate CPU timestamps cannot claim an observed zero ETA."""
    adapter = LammpsThermoProgressAdapter(total_steps=100)

    records = adapter.observe_stdout(
        "run 100\nStep Temp CPU\n0 1.0 0.0\n25 1.1 0.0\n50 1.2 0.0\n75 1.3 0.0\n"
    )

    metadata = records[-1]["metadata"]
    assert metadata["prediction_status"] == "warming_up"
    assert "eta_seconds" not in metadata
    assert not adapter.acceptance_progress_valid(metadata)
