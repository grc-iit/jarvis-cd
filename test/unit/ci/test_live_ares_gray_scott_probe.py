"""Focused tests for the machine-readable Ares Gray-Scott live probe."""

from __future__ import annotations

import argparse
import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


def _probe_module() -> ModuleType:
    """Load the standalone CI probe without requiring an editable install."""
    path = Path(__file__).resolve().parents[3] / "ci" / "live_ares_gray_scott_probe.py"
    spec = spec_from_file_location("test_live_ares_gray_scott_probe_module", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load live probe: {path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROBE = _probe_module()
REPORT_SCHEMA = PROBE.REPORT_SCHEMA
add_assertion = PROBE.add_assertion
atomic_write_json = PROBE.atomic_write_json
evaluate_semantics = PROBE.evaluate_semantics
sha256_file = PROBE.sha256_file
spack_hashes = PROBE._spack_hashes
parse_ldd_output = PROBE._parse_ldd_output
assert_installed_import = PROBE._assert_installed_import
assert_installed_package_source = PROBE._assert_installed_package_source
assert_fresh_install_root = PROBE._assert_fresh_install_root
configured_pipeline = PROBE._configured_pipeline
assert_expected_release_artifact = PROBE._assert_expected_release_artifact


def _record() -> dict[str, Any]:
    return {
        "state": "completed",
        "terminal": True,
        "return_code": 0,
        "scheduler_provider": "slurm",
        "scheduler_native_id": "12345",
    }


def _progress() -> dict[str, Any]:
    return {
        "packages": [
            {
                "package_id": "gray_scott_bp5",
                "event_count": 4,
                "latest": {
                    "state": "completed",
                    "current": 20.0,
                    "total": 20.0,
                    "unit": "timestep",
                    "metadata": {
                        "completion_signal": "process_exit_zero_after_final_output"
                    },
                },
            }
        ]
    }


def _artifact(
    *,
    artifact_id: str,
    logical_name: str,
    kind: str,
    role: str,
    revision: int,
    path: Path,
    metadata: dict[str, object],
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "logical_name": logical_name,
        "package_id": "gray_scott_bp5",
        "state": "finalized",
        "kind": kind,
        "role": role,
        "structure": "collection",
        "ownership": "shared",
        "media_type": "application/x-adios2-bp",
        "format": "adios2-bp5",
        "revision": revision,
        "location": {"kind": "cluster_path", "value": path.as_posix()},
        "metadata": metadata,
    }


def _artifacts(output_path: Path, checkpoint_path: Path) -> dict[str, Any]:
    return {
        "artifacts": [
            _artifact(
                artifact_id="art_0123456789abcdef0123456789abcdef",
                logical_name="gray-scott-timesteps",
                kind="scientific_dataset",
                role="output",
                revision=4,
                path=output_path,
                metadata={
                    "application": "gray_scott",
                    "io_backend": "adios2",
                    "member_pattern": "adios2-steps",
                    "members_observed": 2,
                    "latest_timestep": 20,
                    "completion_signal": "process_exit_zero_after_final_output",
                },
            ),
            _artifact(
                artifact_id="art_fedcba9876543210fedcba9876543210",
                logical_name="gray-scott-restart-checkpoint",
                kind="restart_checkpoint",
                role="checkpoint",
                revision=1,
                path=checkpoint_path,
                metadata={
                    "application": "gray_scott",
                    "io_backend": "adios2",
                    "detection_signal": (
                        "configured_path_created_or_changed_during_process"
                    ),
                    "physical_path_observed": True,
                    "latest_output_timestep_observed": 20,
                    "completion_signal": "process_exit_zero_after_final_output",
                    "return_code": 0,
                },
            ),
        ]
    }


def _bpls() -> dict[str, dict[str, object]]:
    return {
        "output_list": {"return_code": 0, "stdout": "double U\ndouble V\nint step\n"},
        "output_steps": {"return_code": 0, "stdout": "step: 10 20\n"},
        "checkpoint_list": {
            "return_code": 0,
            "stdout": "double U\ndouble V\nint step\n",
        },
        "checkpoint_steps": {"return_code": 0, "stdout": "step: 20\n"},
    }


def test_digest_and_atomic_report_are_deterministic(tmp_path: Path) -> None:
    """Provenance hashes and reports use exact bytes and valid JSON."""
    artifact = tmp_path / "artifact.whl"
    artifact.write_bytes(b"jarvis-wheel")
    assert sha256_file(artifact) == (
        "a2698bbe900f78e274e491b58c8f0afaad67482b93bccd4b5105e77d9101cb66"
    )

    report = tmp_path / "reports" / "live.json"
    atomic_write_json(report, {"schema_version": REPORT_SCHEMA, "success": True})
    assert json.loads(report.read_text(encoding="utf-8")) == {
        "schema_version": REPORT_SCHEMA,
        "success": True,
    }


def test_release_artifact_assertions_bind_digest_and_version(tmp_path: Path) -> None:
    """A passing report names the exact wheel bytes and installed version."""
    wheel = tmp_path / "jarvis_cd-1.2.1-py3-none-any.whl"
    wheel.write_bytes(b"released-wheel")
    expected_digest = sha256_file(wheel)
    args = SimpleNamespace(
        wheel=wheel,
        expected_version="1.2.1",
        expected_wheel_sha256=expected_digest,
    )
    report: dict[str, Any] = {"assertions": []}

    assert_expected_release_artifact(
        args,
        report,
        installed_version="1.2.1",
    )

    assert [item["name"] for item in report["assertions"]] == [
        "release_artifact.wheel_sha256",
        "release_artifact.installed_version",
    ]
    assert all(item["passed"] is True for item in report["assertions"])


def test_release_artifact_assertions_reject_wrong_digest(tmp_path: Path) -> None:
    """The probe fails before submission when wheel bytes do not match."""
    wheel = tmp_path / "jarvis_cd-1.2.1-py3-none-any.whl"
    wheel.write_bytes(b"different-wheel")
    args = SimpleNamespace(
        wheel=wheel,
        expected_version="1.2.1",
        expected_wheel_sha256="0" * 64,
    )
    report: dict[str, Any] = {"assertions": []}

    with pytest.raises(AssertionError, match="wheel_sha256"):
        assert_expected_release_artifact(args, report)

    assert report["assertions"][0]["passed"] is False


def test_assertion_helper_preserves_machine_readable_evidence() -> None:
    """Each acceptance decision retains expected and actual values."""
    assertions: list[dict[str, object]] = []
    add_assertion(
        assertions,
        "example",
        passed=False,
        expected=20,
        actual=10,
        detail="the final timestep was missing",
    )
    assert assertions == [
        {
            "name": "example",
            "passed": False,
            "expected": 20,
            "actual": 10,
            "detail": "the final timestep was missing",
        }
    ]


def test_spack_hash_discovery_covers_nested_json() -> None:
    """The report records concrete IDs emitted by common Spack schemas."""
    assert spack_hashes(
        [
            {"name": "adios2", "hash": "27nopqaa"},
            {"deps": [{"full_hash": "og56sxz"}]},
        ]
    ) == {"27nopqaa", "og56sxz"}


def test_ldd_parser_retains_exact_runtime_paths_and_missing_libraries() -> None:
    """Binary provenance distinguishes resolved paths from missing libraries."""
    libraries, missing = parse_ldd_output(
        "libadios2_cxx_mpi.so.2.10 => /spack/adios2/lib/libadios2.so (0x1)\n"
        "libmpi.so.40 => /spack/openmpi/lib/libmpi.so.40 (0x2)\n"
        "liboptional.so => not found\n"
    )
    assert libraries == {
        "libadios2_cxx_mpi.so.2.10": "/spack/adios2/lib/libadios2.so",
        "libmpi.so.40": "/spack/openmpi/lib/libmpi.so.40",
    }
    assert missing == ["liboptional.so"]


def test_installed_import_accepts_uv_managed_python_symlink(tmp_path: Path) -> None:
    """A uv venv remains isolated when its interpreter target lives outside it."""
    venv = tmp_path / ".venv"
    provenance = {
        "module_path": str(venv / "lib/python3.12/site-packages/jarvis_cd/__init__.py"),
        "python_executable": str(venv / "bin/python"),
        "python_executable_resolved": "/opt/uv/python/cpython-3.12/bin/python3.12",
        "python_prefix": str(venv),
        "python_base_prefix": "/opt/uv/python/cpython-3.12",
    }

    assert_installed_import(provenance, venv)


def test_installed_import_rejects_non_venv_prefix(tmp_path: Path) -> None:
    """An import through a different Python prefix fails the release gate."""
    venv = tmp_path / ".venv"
    provenance = {
        "module_path": str(venv / "lib/python3.12/site-packages/jarvis_cd/__init__.py"),
        "python_executable": str(venv / "bin/python"),
        "python_prefix": str(tmp_path / "different-prefix"),
    }

    with pytest.raises(RuntimeError, match="python_prefix"):
        assert_installed_import(provenance, venv)


def test_installed_package_source_rejects_stale_user_checkout(tmp_path: Path) -> None:
    """A global built-in repository cannot satisfy an installed-wheel probe."""
    venv = tmp_path / ".venv"
    with pytest.raises(RuntimeError, match="package source_path"):
        assert_installed_package_source(
            {"package": {"source_path": "/home/operator/.ppi-jarvis/builtin/pkg.py"}},
            venv,
        )


def test_installed_package_source_accepts_wheel_package(tmp_path: Path) -> None:
    """A built-in module below the isolated site-packages prefix is accepted."""
    venv = tmp_path / ".venv"
    assert_installed_package_source(
        {
            "package": {
                "source_path": str(
                    venv
                    / "lib/python3.12/site-packages/builtin/builtin/gray_scott/pkg.py"
                )
            }
        },
        venv,
    )


def test_live_probe_refuses_to_replace_an_existing_venv(tmp_path: Path) -> None:
    """Concurrent or repeated acceptance runs require distinct owned roots."""
    (tmp_path / ".jarvis-live-probe-venv").mkdir()

    with pytest.raises(RuntimeError, match="already contains owned state"):
        assert_fresh_install_root(tmp_path)


def test_live_pipeline_uses_base_default_instead_of_private_package_argument(
    tmp_path: Path,
) -> None:
    """The direct probe configures only the package's public argument contract."""
    args = argparse.Namespace(
        root=tmp_path / "probe",
        partition="compute",
        account=None,
        executable=Path("/opt/iowarp/bin/gray-scott"),
    )
    report: dict[str, Any] = {}

    pipeline = configured_pipeline(
        args,
        report,
        environment={},
        output_path=tmp_path / "gray-scott.bp",
        mpi_executable=Path("/opt/mpi/bin/mpiexec"),
        run_id="gray-scott-contract-test",
    )

    assert pipeline.base_deploy_mode is None
    assert "deploy_mode" not in pipeline.packages[-1]["config"]
    assert report["pipeline"]["package"]["effective_deploy_mode"] == "default"
    assert report["pipeline"]["package"]["config"]["checkpoint"] is True
    assert report["pipeline"]["package"]["config"]["checkpoint_freq"] == 1
    source_path = Path(report["pipeline"]["package"]["source_path"])
    assert source_path.parts[-4:] == ("builtin", "builtin", "gray_scott", "pkg.py")


def test_exact_gray_scott_contract_passes() -> None:
    """The expected progress, artifacts, and physical datasets all pass."""
    output_path = Path("/mnt/common/probe/gray-scott.bp")
    checkpoint_path = Path(f"{output_path}.checkpoint.bp")
    assertions = evaluate_semantics(
        record=_record(),
        progress=_progress(),
        artifacts=_artifacts(output_path, checkpoint_path),
        output_path=output_path,
        checkpoint_path=checkpoint_path,
        bpls_output=_bpls(),
    )
    assert assertions
    assert all(item["passed"] is True for item in assertions)


def test_missing_physical_timestep_fails_explicit_assertion() -> None:
    """JARVIS metadata cannot hide an incomplete physical ADIOS2 dataset."""
    output_path = Path("/mnt/common/probe/gray-scott.bp")
    checkpoint_path = Path(f"{output_path}.checkpoint.bp")
    bpls = _bpls()
    bpls["output_steps"]["stdout"] = "step: 10\n"
    assertions = evaluate_semantics(
        record=_record(),
        progress=_progress(),
        artifacts=_artifacts(output_path, checkpoint_path),
        output_path=output_path,
        checkpoint_path=checkpoint_path,
        bpls_output=bpls,
    )
    failures = {item["name"] for item in assertions if item["passed"] is False}
    assert failures == {"bpls.output.step.20"}


def test_missing_native_terminal_signal_fails_production_contract() -> None:
    """Two writes alone cannot claim completed progress or a finalized dataset."""
    output_path = Path("/mnt/common/probe/gray-scott.bp")
    checkpoint_path = Path(f"{output_path}.checkpoint.bp")
    progress = _progress()
    package = progress["packages"][0]
    package["event_count"] = 3
    package["latest"]["state"] = "running"
    package["latest"]["metadata"]["completion_signal"] = "compute_step_completed"
    artifacts = _artifacts(output_path, checkpoint_path)
    artifact = artifacts["artifacts"][0]
    artifact["state"] = "incomplete"
    artifact["metadata"].pop("completion_signal")

    assertions = evaluate_semantics(
        record=_record(),
        progress=progress,
        artifacts=artifacts,
        output_path=output_path,
        checkpoint_path=checkpoint_path,
        bpls_output=_bpls(),
    )
    failures = {item["name"] for item in assertions if item["passed"] is False}
    assert {
        "progress.event_count",
        "progress.latest.state",
        "progress.completion_signal",
        "artifacts.output.state",
        "artifacts.output.metadata.completion_signal",
    }.issubset(failures)


def test_invalid_invocation_still_writes_failure_report(tmp_path: Path) -> None:
    """A pre-install validation error still produces the report contract."""
    root = tmp_path / "shared-root"
    report = tmp_path / "reports" / "failure.json"
    exit_code = PROBE.main(
        [
            "--wheel",
            str(tmp_path / "missing.whl"),
            "--root",
            str(root),
            "--report",
            str(report),
            "--partition",
            "compute",
        ]
    )

    document = json.loads(report.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert document["schema_version"] == REPORT_SCHEMA
    assert document["success"] is False
    assert document["finished_at"] is not None
    assert document["error"]["type"] == "ValueError"
