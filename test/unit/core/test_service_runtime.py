"""Durable execution-owned service-runtime contract tests."""

from __future__ import annotations

import hashlib
import json
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
    SERVICE_RUNTIME_SCHEMA_VERSION_V1,
    ServiceAuthorization,
    ServiceLifecycle,
    ServiceProtocol,
    ServiceRuntimeAuthority,
    ServiceRuntimeReport,
    ServiceRuntimeReporter,
    ServiceRuntimeStore,
    calculate_dataset_fingerprint,
)

_RAW_TOKEN = "a" * 64
_TOKEN_SHA256 = hashlib.sha256(_RAW_TOKEN.encode("ascii")).hexdigest()
_AUTHORITY = ServiceRuntimeAuthority(scheme="bearer", token=_RAW_TOKEN)
_AUTHORIZATION = ServiceAuthorization(
    scheme="bearer",
    token_sha256=_TOKEN_SHA256,
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
        authorization=_AUTHORIZATION,
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
    assert decoded.authorization == _AUTHORIZATION
    with pytest.raises(ValueError, match="reachable host"):
        replace(report, host="0.0.0.0")


def test_service_runtime_v2_requires_capability_and_v1_remains_readable() -> None:
    report = _report()
    document = report.to_dict()
    assert document["schema_version"] == "jarvis.service-runtime.v2"
    assert document["authorization"] == {
        "scheme": "bearer",
        "token_sha256": _TOKEN_SHA256,
    }
    assert _RAW_TOKEN not in json.dumps(document)

    legacy = dict(document)
    legacy["schema_version"] = SERVICE_RUNTIME_SCHEMA_VERSION_V1
    del legacy["authorization"]
    parsed = ServiceRuntimeReport.from_dict(legacy)
    assert parsed.schema_version == SERVICE_RUNTIME_SCHEMA_VERSION_V1
    assert parsed.authorization is None
    assert "authorization" not in parsed.to_dict()

    missing = dict(document)
    del missing["authorization"]
    with pytest.raises(ValueError, match="authorization"):
        ServiceRuntimeReport.from_dict(missing)
    with pytest.raises(ValueError, match="64 lowercase hexadecimal"):
        ServiceAuthorization(scheme="bearer", token_sha256="A" * 64)


def test_store_enforces_revision_identity_and_terminal_lifecycle(
    tmp_path: Path,
) -> None:
    """Append-only history cannot fork identity or reopen a stopped service."""
    store = ServiceRuntimeStore(tmp_path / "service-runtimes" / "viewer.jsonl")
    starting = _report()
    ready = _report(revision=2, lifecycle=ServiceLifecycle.READY)
    stopped = _report(revision=3, lifecycle=ServiceLifecycle.STOPPED)

    store.append(starting, authority=_AUTHORITY)
    store.append(ready, authority=_AUTHORITY)
    store.append(stopped, authority=_AUTHORITY)

    assert store.latest() == stopped
    assert store.current() == {stopped.service_instance_id: stopped}
    with pytest.raises(ValueError, match="lifecycle transition"):
        store.append(
            _report(revision=4, lifecycle=ServiceLifecycle.READY),
            authority=_AUTHORITY,
        )


def test_private_store_never_returns_or_publicly_serializes_raw_authority(
    tmp_path: Path,
) -> None:
    path = tmp_path / "service-runtimes" / "viewer.jsonl"
    store = ServiceRuntimeStore(path)
    report = _report()

    store.append(report, authority=_AUTHORITY)

    private_payload = path.read_text(encoding="utf-8")
    private_document = json.loads(private_payload)
    assert private_document == {
        "schema_version": "jarvis.service-runtime.private.v1",
        "runtime": report.to_dict(),
        "authority": {"scheme": "bearer", "token": _RAW_TOKEN},
    }
    public = store.read_all()
    assert public == [report]
    assert _RAW_TOKEN not in json.dumps(public[0].to_dict())
    assert _RAW_TOKEN not in repr(public[0])
    with pytest.raises(ValueError, match="owner-private authority"):
        ServiceRuntimeStore(tmp_path / "missing-authority.jsonl").append(report)
    with pytest.raises(ValueError, match="does not match"):
        ServiceRuntimeStore(tmp_path / "wrong-authority.jsonl").append(
            report,
            authority=ServiceRuntimeAuthority(scheme="bearer", token="b" * 64),
        )
    with pytest.raises(ValueError, match="appeared in the public report"):
        ServiceRuntimeStore(tmp_path / "leaking-public-report.jsonl").append(
            replace(report, message="diagnostic=" + _RAW_TOKEN),
            authority=_AUTHORITY,
        )
    forged_public = report.to_dict()
    forged_public["authorization"] = {
        "scheme": "bearer",
        "token": _RAW_TOKEN,
    }
    with pytest.raises(ValueError, match="authorization"):
        ServiceRuntimeReport.from_dict(forged_public)


def test_private_store_preserves_v1_read_compatibility(tmp_path: Path) -> None:
    legacy = replace(
        _report(),
        schema_version=SERVICE_RUNTIME_SCHEMA_VERSION_V1,
        authorization=None,
    )
    store = ServiceRuntimeStore(tmp_path / "legacy.jsonl")

    store.append(legacy)

    assert store.read_all() == [legacy]
    assert "jarvis.service-runtime.private" not in store.path.read_text(
        encoding="utf-8"
    )


def test_reporter_uses_bound_identity_and_assigns_revisions(tmp_path: Path) -> None:
    """Packages cannot choose another execution identity through report fields."""
    path = tmp_path / "execution" / "service-runtimes" / "viewer.jsonl"
    reporter = ServiceRuntimeReporter.from_environment(
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
        host="compute-01.cluster.example",
        port=18080,
        dataset_descriptor=_descriptor(),
        authority=_AUTHORITY,
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
    runtime_store.append(_report(), authority=_AUTHORITY)
    runtime_store.append(
        _report(revision=2, lifecycle=ServiceLifecycle.READY),
        authority=_AUTHORITY,
    )

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
    ServiceRuntimeStore(runtime_path).append(report, authority=_AUTHORITY)

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
    assert _RAW_TOKEN not in json.dumps(document)
    assert snapshot.service_runtimes[0].authorization == _AUTHORIZATION


def test_execution_authority_resolver_requires_every_current_identity(
    tmp_path: Path,
) -> None:
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
    report = _report()
    ServiceRuntimeStore(runtime_path).append(report, authority=_AUTHORITY)

    resolved = store.resolve_service_runtime_authority(
        record.execution_id,
        package_id=report.package_id,
        service_instance_id=report.service_instance_id,
        revision=report.revision,
        token_sha256=_TOKEN_SHA256,
    )

    assert resolved.to_dict() == {
        "schema_version": "jarvis.execution.service-runtime-authority.v1",
        "execution_id": record.execution_id,
        "pipeline_id": "visualization",
        "package_id": report.package_id,
        "service_instance_id": report.service_instance_id,
        "revision": report.revision,
        "token_sha256": _TOKEN_SHA256,
        "authorization": {"scheme": "bearer", "token": _RAW_TOKEN},
    }
    assert _RAW_TOKEN not in repr(resolved)

    invalid = (
        {"revision": 2},
        {"token_sha256": "0" * 64},
        {"package_id": "other"},
        {"service_instance_id": "srv_other"},
    )
    baseline: dict[str, Any] = {
        "package_id": report.package_id,
        "service_instance_id": report.service_instance_id,
        "revision": report.revision,
        "token_sha256": _TOKEN_SHA256,
    }
    for changed in invalid:
        with pytest.raises((LookupError, RuntimeError)):
            store.resolve_service_runtime_authority(
                record.execution_id,
                **{**baseline, **changed},
            )
    wrong_pipeline = ExecutionStore(store.executions_dir, "wrong-pipeline")
    with pytest.raises(RuntimeError, match="belongs to another pipeline"):
        wrong_pipeline.resolve_service_runtime_authority(
            record.execution_id,
            **baseline,
        )

    duplicate_path = runtime_path.with_name("other.jsonl")
    duplicate_report = replace(
        report,
        package_id="other",
        package_name="builtin.other",
    )
    ServiceRuntimeStore(duplicate_path).append(
        duplicate_report,
        authority=_AUTHORITY,
    )
    store.update(
        record.execution_id,
        metadata={
            "service_runtime_files": {
                "package-viewer": {
                    "filename": runtime_path.name,
                    "package_id": report.package_id,
                    "package_name": report.package_name,
                },
                "package-other": {
                    "filename": duplicate_path.name,
                    "package_id": duplicate_report.package_id,
                    "package_name": duplicate_report.package_name,
                },
            }
        },
    )
    with pytest.raises(
        RuntimeError,
        match="service instance ID is duplicated across package runtimes",
    ):
        store.resolve_service_runtime_authority(
            record.execution_id,
            **baseline,
        )


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
