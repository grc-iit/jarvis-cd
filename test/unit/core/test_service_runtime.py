"""Durable execution-owned service-runtime contract tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from jarvis_cd.core.execution import (
    SERVICE_RUNTIME_SNAPSHOT_SCHEMA,
    ExecutionStore,
)
from jarvis_cd.core.pkg import Pkg
from jarvis_cd.service_runtime import (
    DatasetArray,
    DatasetDescriptor,
    DatasetMember,
    ServiceLifecycle,
    ServiceProtocol,
    ServiceRuntimeReport,
    ServiceRuntimeReporter,
    ServiceRuntimeStore,
    calculate_dataset_fingerprint,
)


def _descriptor() -> DatasetDescriptor:
    members = tuple(
        DatasetMember(
            index=index,
            location=f"/mnt/common/asteroid/frame-{index:04d}.vti",
            timestep=float(index * 10),
        )
        for index in range(5)
    )
    arrays = (DatasetArray("pressure", "point", 1, "Pa"),)
    bounds = (0.0, 10.0, -2.0, 2.0, 0.0, 1.0)
    return DatasetDescriptor(
        dataset_id="asteroid-2018-subset",
        kind="temporal-volume-series",
        format="vtk-image-data",
        members=members,
        arrays=arrays,
        bounds=bounds,
        fingerprint=calculate_dataset_fingerprint(
            dataset_id="asteroid-2018-subset",
            kind="temporal-volume-series",
            format="vtk-image-data",
            members=members,
            arrays=arrays,
            bounds=bounds,
        ),
    )


def _report(
    *,
    revision: int = 1,
    lifecycle: ServiceLifecycle = ServiceLifecycle.STARTING,
) -> ServiceRuntimeReport:
    return ServiceRuntimeReport(
        execution_id="exec-render",
        package_name="builtin.paraview",
        package_id="viewer",
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
        revision=revision,
        lifecycle=lifecycle,
        host="compute-01.cluster.example",
        port=18080,
        protocol=ServiceProtocol.HTTP,
        health_path="/healthz",
        live_data_path="/live-data",
        events_path="/events",
        state_path="/state",
        command_path="/commands",
        dataset_descriptor=_descriptor(),
    )


def test_descriptor_round_trip_has_only_intrinsic_bounded_facts() -> None:
    """Descriptor identity is stable and cannot absorb a hidden scene recipe."""
    descriptor = _descriptor()

    decoded = DatasetDescriptor.from_json(descriptor.to_json())

    assert decoded == descriptor
    assert decoded.canonical_digest == descriptor.canonical_digest
    document = descriptor.to_dict()
    assert set(document) == {
        "schema_version",
        "dataset_id",
        "kind",
        "format",
        "members",
        "arrays",
        "bounds",
        "fingerprint",
        "source_artifact",
    }
    document["camera"] = {"position": [1, 2, 3]}
    with pytest.raises(ValueError, match="unknown dataset descriptor fields"):
        DatasetDescriptor.from_dict(document)
    forged = descriptor.to_dict()
    forged["members"][0]["location"] = "/mnt/common/asteroid/other.vti"
    with pytest.raises(ValueError, match="fingerprint does not match"):
        DatasetDescriptor.from_dict(forged)


def test_report_requires_reachable_host_command_path_and_push_mode() -> None:
    """A runtime report contains every relay-facing endpoint explicitly."""
    report = _report()
    decoded = ServiceRuntimeReport.from_json(report.to_json())

    assert decoded == report
    assert decoded.base_url == "http://compute-01.cluster.example:18080"
    assert decoded.to_dict()["command_path"] == "/commands"
    with pytest.raises(ValueError, match="reachable host"):
        replace(report, host="0.0.0.0")


def test_store_enforces_revision_identity_and_terminal_lifecycle(
    tmp_path: Path,
) -> None:
    """Append-only history cannot fork identity or reopen a stopped service."""
    store = ServiceRuntimeStore(tmp_path / "service-runtimes" / "viewer.jsonl")
    starting = _report()
    ready = _report(revision=2, lifecycle=ServiceLifecycle.READY)
    stopped = _report(revision=3, lifecycle=ServiceLifecycle.STOPPED)

    store.append(starting)
    store.append(ready)
    store.append(stopped)

    assert store.latest() == stopped
    assert store.current() == {stopped.service_instance_id: stopped}
    with pytest.raises(ValueError, match="lifecycle transition"):
        store.append(_report(revision=4, lifecycle=ServiceLifecycle.READY))


def test_reporter_uses_bound_identity_and_assigns_revisions(tmp_path: Path) -> None:
    """Packages cannot choose another execution identity through report fields."""
    path = tmp_path / "execution" / "service-runtimes" / "viewer.jsonl"
    reporter = ServiceRuntimeReporter.from_environment(
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
        host="compute-01.cluster.example",
        port=18080,
        dataset_descriptor=_descriptor(),
        environ={
            "JARVIS_EXECUTION_ID": "exec-render",
            "JARVIS_PACKAGE_NAME": "builtin.paraview",
            "JARVIS_PACKAGE_ID": "viewer",
            "JARVIS_SERVICE_RUNTIME_PATH": str(path.resolve()),
        },
    )

    starting = reporter.report(ServiceLifecycle.STARTING)
    ready = reporter.report(ServiceLifecycle.READY)

    assert (starting.revision, ready.revision) == (1, 2)
    assert ready.execution_id == "exec-render"
    assert ServiceRuntimeStore(path).latest() == ready


@pytest.mark.parametrize(
    ("execution_state", "return_code", "expected_lifecycle"),
    [
        ("completed", 0, ServiceLifecycle.STOPPED),
        ("failed", 7, ServiceLifecycle.FAILED),
    ],
)
def test_execution_terminalization_reconciles_active_service(
    tmp_path: Path,
    execution_state: str,
    return_code: int,
    expected_lifecycle: ServiceLifecycle,
) -> None:
    """A killed supervisor cannot leave a terminal execution claiming ready."""
    store = ExecutionStore(tmp_path / "executions", "visualization")
    record = store.create("exec-render", mode="direct")
    runtime_path = (
        store.executions_dir / record.execution_id / "service-runtimes" / "viewer.jsonl"
    )
    store.update(
        record.execution_id,
        state="running",
        metadata={
            "service_runtime_files": {
                "package-viewer": {
                    "filename": runtime_path.name,
                    "package_id": "viewer",
                    "package_name": "builtin.paraview",
                }
            }
        },
    )
    runtime_store = ServiceRuntimeStore(runtime_path)
    runtime_store.append(_report())
    runtime_store.append(_report(revision=2, lifecycle=ServiceLifecycle.READY))

    store.update(
        record.execution_id,
        state=execution_state,
        terminal=True,
        return_code=return_code,
        error="service failed" if return_code else None,
    )

    latest = runtime_store.latest()
    assert latest is not None
    assert latest.revision == 3
    assert latest.lifecycle is expected_lifecycle
    assert "JARVIS reconciled" in (latest.message or "")


def test_execution_snapshot_is_exact_identity_checked_wire_document(
    tmp_path: Path,
) -> None:
    """Execution handles query the stable collection expected by relay."""
    store = ExecutionStore(tmp_path / "executions", "visualization")
    record = store.create("exec-render", mode="direct")
    runtime_root = store.executions_dir / record.execution_id / "service-runtimes"
    runtime_path = runtime_root / "viewer.jsonl"
    store.update(
        record.execution_id,
        state="running",
        metadata={
            "service_runtime_files": {
                "package-viewer": {
                    "filename": runtime_path.name,
                    "package_id": "viewer",
                    "package_name": "builtin.paraview",
                }
            }
        },
    )
    report = _report()
    ServiceRuntimeStore(runtime_path).append(report)

    snapshot = store.get(record.execution_id).handle.service_runtimes()
    document = snapshot.to_dict()

    assert document == {
        "schema_version": SERVICE_RUNTIME_SNAPSHOT_SCHEMA,
        "execution_id": "exec-render",
        "pipeline_id": "visualization",
        "execution_state": "running",
        "terminal": False,
        "service_runtimes": [report.to_dict()],
    }


def test_package_binding_exports_only_authoritative_runtime_path(
    tmp_path: Path,
) -> None:
    """Core can bind a package reporter without application-selected identity."""
    package = cast(Any, object.__new__(Pkg))
    package.pkg_type = "builtin.paraview"
    package.pkg_id = "viewer"
    package.env = {}
    package.mod_env = {}
    path = (tmp_path / "execution" / "service-runtimes" / "viewer.jsonl").resolve()

    package.bind_execution_service_runtime("exec-render", path)

    assert package.mod_env == {
        "JARVIS_EXECUTION_ID": "exec-render",
        "JARVIS_SERVICE_RUNTIME_PATH": str(path),
        "JARVIS_PACKAGE_NAME": "builtin.paraview",
        "JARVIS_PACKAGE_ID": "viewer",
    }
