"""Durable JARVIS execution handle and record tests."""

from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import Mock, patch

import jarvis_cd.core.execution as execution_module
import jarvis_cd.core.pipeline as pipeline_module
import jarvis_cd.shell
import pytest

from jarvis_cd.artifacts import (
    ArtifactLocation,
    ArtifactOwnership,
    ArtifactReporter,
    ArtifactRole,
    ArtifactState,
    ArtifactStore,
    ArtifactStructure,
)
from jarvis_cd.core.execution import (
    ARTIFACT_SNAPSHOT_SCHEMA,
    DIRECT_LAUNCH_SCHEMA,
    HANDLE_SCHEMA,
    MAX_RECORD_BYTES,
    PROGRESS_SNAPSHOT_SCHEMA,
    RECORD_NAME,
    ExecutionHandle,
    ExecutionRecord,
    ExecutionStore,
    direct_execution_lease,
    finalize_execution,
    prepare_direct_execution_lease,
    validate_pipeline_id,
)
from jarvis_cd.core.pipeline import Pipeline, _validate_execution_cleanup_receipt
from jarvis_cd.core.scheduler import SlurmScheduler


@pytest.mark.parametrize("pipeline_id", ["visualization", "case.2026", "a-b_c"])
def test_pipeline_id_accepts_portable_path_components(pipeline_id: str) -> None:
    """Portable pipeline identities remain usable across supported systems."""
    assert validate_pipeline_id(pipeline_id) == pipeline_id


@pytest.mark.parametrize(
    "pipeline_id",
    ["", "../outside", "nested/name", r"nested\name", ".hidden", "CON", "bad."],
)
def test_pipeline_id_rejects_path_aliases(pipeline_id: str) -> None:
    """Pipeline identity can never become traversal or a reserved path alias."""
    with pytest.raises(ValueError, match="pipeline_id"):
        validate_pipeline_id(pipeline_id)


def test_pipeline_constructor_rejects_invalid_query_before_config_access() -> None:
    """A named query validates identity before reading any pipeline path."""
    with patch("jarvis_cd.core.pipeline.Jarvis.get_instance") as get_instance:
        with pytest.raises(ValueError, match="pipeline_id"):
            Pipeline("../outside")

    get_instance.assert_not_called()


def test_pipeline_create_and_destroy_reject_escape_before_path_access() -> None:
    """Mutation boundaries reject traversal before asking config for a path."""
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.name = None
    pipeline._execution_root = None
    pipeline.jarvis = SimpleNamespace(
        get_current_pipeline=Mock(return_value=None),
        get_pipeline_dir=Mock(),
        get_pipeline_shared_dir=Mock(),
        get_pipeline_private_dir=Mock(),
    )

    with pytest.raises(ValueError, match="pipeline_id"):
        pipeline.create("../outside")
    with pytest.raises(ValueError, match="pipeline_id"):
        pipeline.destroy("../outside")

    pipeline.jarvis.get_pipeline_dir.assert_not_called()
    pipeline.jarvis.get_pipeline_shared_dir.assert_not_called()
    pipeline.jarvis.get_pipeline_private_dir.assert_not_called()


def test_pipeline_yaml_rejects_unsafe_name_before_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A site YAML cannot choose an out-of-root pipeline storage identity."""
    pipeline_file = tmp_path / "pipeline.yaml"
    pipeline_file.write_text("name: ../outside\npkgs: []\n", encoding="utf-8")
    monkeypatch.delenv("JARVIS_PIPELINE_SNAPSHOT_DIR", raising=False)
    pipeline = Pipeline.__new__(Pipeline)

    with pytest.raises(ValueError, match="pipeline_id"):
        pipeline._load_from_file("yaml", str(pipeline_file))


def test_cleanup_receipt_rejects_duplicate_identity_keys() -> None:
    """Cleanup authorization never applies JSON last-key-wins semantics."""
    payload = (
        b'{"cleanup_nonce":"nonce","execution_id":"owned",'
        b'"execution_id":"other","pipeline_name":"example",'
        b'"schema_version":"jarvis.execution-cleanup.v1",'
        b'"state":"completed","submitted":false,"terminal":true,'
        b'"tombstone_device":1,"tombstone_inode":1}'
    )

    with pytest.raises(RuntimeError, match="invalid execution cleanup receipt"):
        _validate_execution_cleanup_receipt(
            payload,
            receipt_label="receipt.json",
            expected_nonce="nonce",
            expected_execution_id="owned",
        )


def test_handle_round_trip_uses_explicit_nullable_scheduler_fields() -> None:
    """Direct handles carry JARVIS identity and explicit null scheduler fields."""
    handle = ExecutionHandle(
        execution_id="direct-1",
        pipeline_id="example",
        mode="direct",
    )

    document = handle.to_dict()

    assert document == {
        "schema_version": HANDLE_SCHEMA,
        "execution_id": "direct-1",
        "pipeline_id": "example",
        "mode": "direct",
        "scheduler_provider": None,
        "scheduler_native_id": None,
        "cluster": None,
    }
    assert ExecutionHandle.from_dict(document) == handle


def test_handle_rejects_ambiguous_or_extended_documents() -> None:
    """Scheduler identity cannot leak into direct handles or omit its provider."""
    with pytest.raises(ValueError, match="direct execution"):
        ExecutionHandle(
            execution_id="direct-1",
            pipeline_id="example",
            mode="direct",
            scheduler_provider="slurm",
        )
    with pytest.raises(ValueError, match="require scheduler_provider"):
        ExecutionHandle(
            execution_id="scheduled-1",
            pipeline_id="example",
            mode="scheduler",
        )
    document = ExecutionHandle(
        execution_id="scheduled-1",
        pipeline_id="example",
        mode="scheduler",
        scheduler_provider="slurm",
    ).to_dict()
    document["unexpected"] = True
    with pytest.raises(ValueError, match="schema"):
        ExecutionHandle.from_dict(document)


def test_store_persists_independent_records_and_queryable_handles(
    tmp_path: Path,
) -> None:
    """Each execution remains queryable after later executions are created."""
    store = ExecutionStore(tmp_path / "executions", "example")
    first = store.create("first", mode="direct")
    first = store.update(
        "first",
        state="running",
        metadata={
            "progress_files": {
                "render": {
                    "filename": "render.jsonl",
                    "package_name": "builtin.paraview",
                }
            }
        },
    )
    first = store.update(
        "first",
        state="completed",
        terminal=True,
        return_code=0,
    )
    second = store.create(
        "second",
        mode="scheduler",
        scheduler_provider="slurm",
    )
    second = store.update(
        "second",
        state="submitting",
        scheduler_provider="slurm",
    )
    second = store.update(
        "second",
        state="submitted",
        submitted=True,
        native_id="9123",
        cluster="ares",
    )

    records = store.list()

    assert [record.execution_id for record in records] == ["first", "second"]
    assert first.handle.refresh().state == "completed"
    assert second.handle.refresh().scheduler_native_id == "9123"
    assert second.handle.to_dict()["cluster"] == "ares"


def test_execution_store_rejects_junction_ancestor_without_writing(
    tmp_path: Path,
) -> None:
    """An execution collection is never created through path redirection."""
    target = tmp_path / "outside"
    target.mkdir()
    redirected = tmp_path / "redirected"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(redirected))
    else:
        redirected.symlink_to(target, target_is_directory=True)

    store = ExecutionStore(redirected / "executions", "example")
    with pytest.raises(RuntimeError, match="symbolic link or reparse point"):
        store.create("blocked", mode="direct")
    assert not (target / "executions").exists()


def test_record_reader_does_not_block_atomic_replacement(
    tmp_path: Path,
) -> None:
    """A live reader remains safe while a writer atomically replaces the path."""
    store = ExecutionStore(tmp_path / "executions", "example")
    store.create("replaceable", mode="direct")
    record_path = store.executions_dir / "replaceable" / RECORD_NAME
    initial_state = store.get("replaceable").state
    descriptor = os.open(record_path, os.O_RDONLY)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(store.update, "replaceable", state="running")
            if os.name != "nt":
                # POSIX replacement is allowed to complete while the old inode
                # is open. Waiting for a temporary file is therefore racy: the
                # writer can create, fsync, replace, and unlink it before the
                # polling thread is scheduled. Verify the useful contract
                # directly instead.
                updated = future.result(timeout=5)
                os.lseek(descriptor, 0, os.SEEK_SET)
                pinned = json.loads(os.read(descriptor, MAX_RECORD_BYTES))
                assert pinned["state"] == initial_state
                assert store.get("replaceable").state == "running"
                assert updated.state == "running"
                return

            # Windows replacement can wait for a reader that did not grant
            # delete sharing. The durable writer retains its temporary file
            # while retrying, so release the reader only after that state is
            # observable.
            temporary_pattern = f".{RECORD_NAME}.*.tmp"
            for _ in range(1_000):
                if list(record_path.parent.glob(temporary_pattern)):
                    break
                Event().wait(0.001)
            else:
                raise AssertionError("record writer did not reach atomic replacement")
            os.close(descriptor)
            descriptor = -1
            updated = future.result(timeout=5)
        assert updated.state == "running"
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def test_record_reader_retries_its_own_atomic_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secure read retries when a JARVIS writer replaces its open inode."""
    store = ExecutionStore(tmp_path / "executions", "example")
    store.create("replaceable", mode="direct")
    real_validate = execution_module._validate_private_regular_file
    reader_attempts = 0
    replaced = False

    def replace_during_validation(
        descriptor: int,
        path: Path,
        *,
        maximum_size: int,
    ) -> os.stat_result:
        nonlocal reader_attempts, replaced
        reader_attempts += 1
        if not replaced:
            replaced = True
            if os.name == "nt":
                # MoveFileEx cannot replace a path held by Python's os.open.
                # Exercise the same typed kernel-identity signal directly;
                # the POSIX branch below performs the real atomic replacement.
                raise execution_module.PrivatePathIdentityChangedError(
                    f"private path changed during secure open: {path}"
                )
            store.update("replaceable", state="running")
        return real_validate(descriptor, path, maximum_size=maximum_size)

    monkeypatch.setattr(
        execution_module,
        "_validate_private_regular_file",
        replace_during_validation,
    )

    record = store.get("replaceable")

    assert record.state == ("preparing" if os.name == "nt" else "running")
    assert reader_attempts >= 2


def test_record_validator_classifies_unlinked_replaced_inode_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unlinked old inode is replacement churn, not a corrupt hardlink."""
    record_path = tmp_path / RECORD_NAME
    record_path.write_text("{}\n", encoding="utf-8")
    old_inode = os.stat_result((stat.S_IFREG | 0o600, 101, 7, 0, 1, 1, 3, 0, 0, 0))
    replacement_inode = os.stat_result(
        (stat.S_IFREG | 0o600, 102, 7, 1, 1, 1, 3, 0, 0, 0)
    )

    def descriptor_already_validated(
        _path: Path,
        _descriptor: int,
        *,
        directory: bool,
    ) -> None:
        assert directory is False

    def stat_open_inode(_descriptor: int) -> os.stat_result:
        return old_inode

    def stat_replacement_path(_path: Path) -> os.stat_result:
        return replacement_inode

    monkeypatch.setattr(
        execution_module,
        "ensure_private_descriptor",
        descriptor_already_validated,
    )
    monkeypatch.setattr(execution_module.os, "fstat", stat_open_inode)
    monkeypatch.setattr(Path, "lstat", stat_replacement_path)

    with pytest.raises(
        execution_module.PrivatePathIdentityChangedError,
        match="changed during secure open",
    ):
        execution_module._validate_private_regular_file(
            19,
            record_path,
            maximum_size=MAX_RECORD_BYTES,
        )


def test_record_reader_does_not_retry_non_identity_security_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symlink and type validation failures remain immediately fatal."""
    store = ExecutionStore(tmp_path / "executions", "example")
    store.create("blocked", mode="direct")
    validations = 0

    def reject_redirection(
        _descriptor: int,
        _path: Path,
        *,
        maximum_size: int,
    ) -> os.stat_result:
        nonlocal validations
        validations += 1
        assert maximum_size == MAX_RECORD_BYTES
        raise RuntimeError("private path cannot traverse a symbolic link")

    monkeypatch.setattr(
        execution_module,
        "_validate_private_regular_file",
        reject_redirection,
    )

    with pytest.raises(RuntimeError, match="symbolic link"):
        store.get("blocked")
    assert validations == 1


def test_record_reader_bounds_identity_retries_and_closes_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continuous pathname churn fails closed after a bounded descriptor loop."""
    store = ExecutionStore(tmp_path / "executions", "example")
    store.create("churning", mode="direct")
    descriptors: list[int] = []

    def reject_changed_identity(
        descriptor: int,
        path: Path,
        *,
        maximum_size: int,
    ) -> os.stat_result:
        descriptors.append(descriptor)
        assert maximum_size == MAX_RECORD_BYTES
        raise execution_module.PrivatePathIdentityChangedError(
            f"private path changed during secure open: {path}"
        )

    monkeypatch.setattr(
        execution_module,
        "_validate_private_regular_file",
        reject_changed_identity,
    )

    with pytest.raises(
        execution_module.PrivatePathIdentityChangedError,
        match="changed during secure open",
    ):
        store.get("churning")

    assert len(descriptors) == execution_module._SECURE_RECORD_READ_ATTEMPTS
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_store_rejects_non_json_metadata_and_terminal_regression(
    tmp_path: Path,
) -> None:
    """Durable records accept only bounded JSON and never reopen terminal work."""
    store = ExecutionStore(tmp_path / "executions", "example")
    with pytest.raises(ValueError, match="JSON"):
        store.create("bad-json", mode="direct", metadata={"path": tmp_path})
    store.create("done", mode="direct")
    store.update("done", state="running")
    store.update("done", state="completed", terminal=True, return_code=0)
    with pytest.raises(ValueError, match="transition|nonterminal"):
        store.update("done", state="running", terminal=False)


def test_store_rejects_boolean_or_incoherent_terminal_return_codes(
    tmp_path: Path,
) -> None:
    """A successful write can never create a record its reader rejects."""
    store = ExecutionStore(tmp_path / "executions", "example")
    for execution_id in ("boolean", "completed-bad", "failed-bad"):
        store.create(execution_id, mode="direct")
        store.update(execution_id, state="running")

    with pytest.raises(ValueError, match="integer"):
        store.update(
            "boolean",
            state="completed",
            terminal=True,
            return_code=True,
        )
    with pytest.raises(ValueError, match="return_code=0"):
        store.update(
            "completed-bad",
            state="completed",
            terminal=True,
            return_code=7,
        )
    with pytest.raises(ValueError, match="nonzero"):
        store.update(
            "failed-bad",
            state="failed",
            terminal=True,
            return_code=0,
        )
    assert store.get("boolean").state == "running"


def test_scheduler_activation_is_identity_bound_and_scripted_only(
    tmp_path: Path,
) -> None:
    """Only the scheduler helper can safely activate a generated script."""
    store = ExecutionStore(tmp_path / "executions", "example")
    store.create("manual", mode="scheduler", scheduler_provider="slurm")
    store.update("manual", state="scripted", terminal=True)

    activated = store.activate_scheduler(
        "manual",
        provider="slurm",
        native_id="41",
        cluster="ares",
    )

    assert activated.state == "running"
    assert activated.submitted is True
    assert activated.terminal is False
    assert activated.scheduler_native_id == "41"
    assert activated.cluster == "ares"
    with pytest.raises(ValueError, match="cannot change"):
        store.activate_scheduler(
            "manual",
            provider="slurm",
            native_id="42",
            cluster="ares",
        )
    with pytest.raises(ValueError, match="numeric"):
        store.activate_scheduler(
            "manual",
            provider="slurm",
            native_id="not-a-job",
        )


def test_scheduler_activation_backfills_submitted_cluster_projection(
    tmp_path: Path,
) -> None:
    """Allocation identity keeps the durable submission projection coherent."""

    store = ExecutionStore(tmp_path / "executions", "example")
    store.create("submitted", mode="scheduler", scheduler_provider="slurm")
    store.update("submitted", state="submitting")
    store.update(
        "submitted",
        state="submitted",
        submitted=True,
        native_id="42",
        metadata={
            "submission": {
                "schema_version": "jarvis.scheduler.submission.v1",
                "execution_id": "submitted",
                "provider": "slurm",
                "scheduler_job_id": "42",
                "scheduler_cluster": None,
                "identity_source": "scheduler_submit_api",
                "submitted": True,
            }
        },
    )

    activated = store.activate_scheduler(
        "submitted",
        provider="slurm",
        native_id="42",
        cluster="linux",
    )

    assert activated.cluster == "linux"
    submission = activated.metadata["submission"]
    assert submission["scheduler_cluster"] == "linux"
    assert submission["cluster_identity_source"] == "scheduler_runtime_environment"
    assert submission["identity_source"] == "scheduler_submit_api"


def test_scheduler_activation_rejects_conflicting_submission_cluster(
    tmp_path: Path,
) -> None:
    """An allocation cannot rewrite an already-bound submission cluster."""

    store = ExecutionStore(tmp_path / "executions", "example")
    store.create("conflict", mode="scheduler", scheduler_provider="slurm")
    store.update("conflict", state="submitting")
    store.update(
        "conflict",
        state="submitted",
        submitted=True,
        native_id="42",
        metadata={
            "submission": {
                "provider": "slurm",
                "scheduler_job_id": "42",
                "scheduler_cluster": "other",
            }
        },
    )

    with pytest.raises(RuntimeError, match="cluster conflicts"):
        store.activate_scheduler(
            "conflict",
            provider="slurm",
            native_id="42",
            cluster="ares",
        )


def test_scheduler_activation_does_not_promote_incomplete_submission_receipt(
    tmp_path: Path,
) -> None:
    """Runtime identity cannot make a manual/script-only projection look submitted."""

    store = ExecutionStore(tmp_path / "executions", "example")
    store.create(
        "manual-projection",
        mode="scheduler",
        scheduler_provider="slurm",
        metadata={
            "submission": {
                "schema_version": "jarvis.scheduler.submission.v1",
                "execution_id": "manual-projection",
                "provider": "slurm",
                "scheduler_job_id": None,
                "scheduler_cluster": None,
                "identity_source": None,
                "submitted": False,
            }
        },
    )
    store.update("manual-projection", state="scripted", terminal=True)

    activated = store.activate_scheduler(
        "manual-projection",
        provider="slurm",
        native_id="42",
        cluster="linux",
    )

    submission = activated.metadata["submission"]
    assert submission["scheduler_cluster"] is None
    assert "cluster_identity_source" not in submission
    assert submission["scheduler_job_id"] is None
    assert submission["submitted"] is False


def test_scheduler_query_repairs_and_persists_legacy_cluster_projection(
    tmp_path: Path,
) -> None:
    """A query repairs only identity-proven scheduler metadata from old runs."""

    store = ExecutionStore(tmp_path / "executions", "example")
    store.create("legacy", mode="scheduler", scheduler_provider="slurm")
    store.update("legacy", state="submitting")
    store.update(
        "legacy",
        state="submitted",
        submitted=True,
        native_id="42",
        metadata={
            "submission": {
                "schema_version": "jarvis.scheduler.submission.v1",
                "execution_id": "legacy",
                "provider": "slurm",
                "scheduler_job_id": "42",
                "scheduler_cluster": None,
                "identity_source": "scheduler_submit_api",
                "submitted": True,
            }
        },
    )
    store.update(
        "legacy",
        state="running",
        cluster="linux",
        metadata={
            "scheduler_activation": {
                "provider": "slurm",
                "native_id": "42",
                "cluster": "linux",
                "identity_source": "scheduler_runtime_environment",
            }
        },
    )

    repaired = store.get("legacy")

    submission = repaired.metadata["submission"]
    assert submission["scheduler_cluster"] == "linux"
    assert submission["cluster_identity_source"] == ("scheduler_runtime_environment")
    persisted = execution_module.read_execution_record(
        store.executions_dir / "legacy",
        expected_execution_id="legacy",
    )
    assert persisted.metadata["submission"] == submission


def test_scheduler_query_keeps_matching_submission_cluster_unchanged(
    tmp_path: Path,
) -> None:
    """A complete matching scheduler cluster is an explicit query no-op."""

    store = ExecutionStore(tmp_path / "executions", "example")
    store.create("cluster-match", mode="scheduler", scheduler_provider="slurm")
    store.update("cluster-match", state="submitting")
    store.update(
        "cluster-match",
        state="submitted",
        submitted=True,
        native_id="42",
        metadata={
            "submission": {
                "schema_version": "jarvis.scheduler.submission.v1",
                "execution_id": "cluster-match",
                "provider": "slurm",
                "scheduler_job_id": "42",
                "scheduler_cluster": "linux",
                "identity_source": "scheduler_submit_api",
                "submitted": True,
            }
        },
    )
    store.update(
        "cluster-match",
        state="running",
        cluster="linux",
        metadata={
            "scheduler_activation": {
                "provider": "slurm",
                "native_id": "42",
                "cluster": "linux",
                "identity_source": "scheduler_runtime_environment",
            }
        },
    )
    record_path = store.executions_dir / "cluster-match" / RECORD_NAME
    before = record_path.read_bytes()

    observed = store.get("cluster-match")

    assert observed.metadata["submission"]["scheduler_cluster"] == "linux"
    assert record_path.read_bytes() == before


def test_scheduler_query_rejects_conflicting_submission_cluster_without_write(
    tmp_path: Path,
) -> None:
    """A populated submission cluster cannot disagree with the durable record."""

    store = ExecutionStore(tmp_path / "executions", "example")
    store.create("cluster-conflict", mode="scheduler", scheduler_provider="slurm")
    store.update("cluster-conflict", state="submitting")
    store.update(
        "cluster-conflict",
        state="submitted",
        submitted=True,
        native_id="42",
        metadata={
            "submission": {
                "schema_version": "jarvis.scheduler.submission.v1",
                "execution_id": "cluster-conflict",
                "provider": "slurm",
                "scheduler_job_id": "42",
                "scheduler_cluster": "other",
                "identity_source": "scheduler_submit_api",
                "submitted": True,
            }
        },
    )
    store.update(
        "cluster-conflict",
        state="running",
        cluster="linux",
        metadata={
            "scheduler_activation": {
                "provider": "slurm",
                "native_id": "42",
                "cluster": "linux",
                "identity_source": "scheduler_runtime_environment",
            }
        },
    )
    record_path = store.executions_dir / "cluster-conflict" / RECORD_NAME
    before = record_path.read_bytes()

    with pytest.raises(RuntimeError, match="submission cluster conflicts"):
        store.get("cluster-conflict")

    assert record_path.read_bytes() == before


@pytest.mark.parametrize(
    ("metadata_key", "field_name", "conflicting_value", "diagnostic"),
    [
        ("submission", "execution_id", "other", "submission identity"),
        ("submission", "provider", "pbs", "submission identity"),
        ("submission", "scheduler_job_id", "99", "submission identity"),
        ("scheduler_activation", "provider", "pbs", "activation identity"),
        ("scheduler_activation", "native_id", "99", "activation identity"),
        ("scheduler_activation", "cluster", "other", "activation identity"),
    ],
)
def test_scheduler_query_rejects_conflicting_cluster_repair_identity(
    tmp_path: Path,
    metadata_key: str,
    field_name: str,
    conflicting_value: str,
    diagnostic: str,
) -> None:
    """A query never rewrites a projection whose durable identities conflict."""

    submission = {
        "schema_version": "jarvis.scheduler.submission.v1",
        "execution_id": "legacy-conflict",
        "provider": "slurm",
        "scheduler_job_id": "42",
        "scheduler_cluster": None,
        "identity_source": "scheduler_submit_api",
        "submitted": True,
    }
    activation = {
        "provider": "slurm",
        "native_id": "42",
        "cluster": "linux",
        "identity_source": "scheduler_runtime_environment",
    }
    target = submission if metadata_key == "submission" else activation
    target[field_name] = conflicting_value
    store = ExecutionStore(tmp_path / "executions", "example")
    store.create("legacy-conflict", mode="scheduler", scheduler_provider="slurm")
    store.update("legacy-conflict", state="submitting")
    store.update(
        "legacy-conflict",
        state="submitted",
        submitted=True,
        native_id="42",
        metadata={"submission": submission},
    )
    store.update(
        "legacy-conflict",
        state="running",
        cluster="linux",
        metadata={"scheduler_activation": activation},
    )
    record_path = store.executions_dir / "legacy-conflict" / RECORD_NAME
    before = record_path.read_bytes()

    with pytest.raises(RuntimeError, match=diagnostic):
        store.get("legacy-conflict")

    assert record_path.read_bytes() == before


def test_scheduler_query_does_not_promote_manual_submission_projection(
    tmp_path: Path,
) -> None:
    """Query reconciliation leaves an incomplete manual projection untouched."""

    store = ExecutionStore(tmp_path / "executions", "example")
    store.create(
        "manual-query",
        mode="scheduler",
        scheduler_provider="slurm",
        metadata={
            "submission": {
                "schema_version": "jarvis.scheduler.submission.v1",
                "execution_id": "manual-query",
                "provider": "slurm",
                "scheduler_job_id": None,
                "scheduler_cluster": None,
                "identity_source": None,
                "submitted": False,
            }
        },
    )
    store.update("manual-query", state="scripted", terminal=True)
    store.activate_scheduler(
        "manual-query",
        provider="slurm",
        native_id="42",
        cluster="linux",
    )
    record_path = store.executions_dir / "manual-query" / RECORD_NAME
    before = record_path.read_bytes()

    observed = store.get("manual-query")

    submission = observed.metadata["submission"]
    assert submission["scheduler_cluster"] is None
    assert "cluster_identity_source" not in submission
    assert record_path.read_bytes() == before


def test_record_reader_rejects_unknown_fields_and_symlink_roots(
    tmp_path: Path,
) -> None:
    """Record reads fail closed for schema expansion and path replacement."""
    store = ExecutionStore(tmp_path / "executions", "example")
    store.create("owned", mode="direct")
    record_path = store.executions_dir / "owned" / RECORD_NAME
    document = json.loads(record_path.read_text(encoding="utf-8"))
    document["unexpected"] = "field"
    record_path.write_text(json.dumps(document), encoding="utf-8")
    if record_path.stat().st_mode & 0o077:
        record_path.chmod(0o600)
    with pytest.raises(RuntimeError, match="invalid execution record"):
        store.get("owned")

    if not hasattr(Path, "symlink_to"):
        return
    outside = tmp_path / "outside"
    outside.mkdir()
    link = store.executions_dir / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        return
    with pytest.raises(
        RuntimeError,
        match="real directory|symbolic link or reparse point",
    ):
        store.get("linked")


def test_record_reader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    """Duplicate ownership fields never receive last-key-wins semantics."""
    store = ExecutionStore(tmp_path / "executions", "example")
    store.create("owned", mode="direct")
    record_path = store.executions_dir / "owned" / RECORD_NAME
    payload = record_path.read_text(encoding="utf-8")
    payload = payload.replace(
        '"execution_id":"owned"',
        '"execution_id":"owned","execution_id":"other"',
        1,
    )
    record_path.write_text(payload, encoding="utf-8")
    if record_path.stat().st_mode & 0o077:
        record_path.chmod(0o600)

    with pytest.raises(RuntimeError, match="invalid execution record"):
        store.get("owned")


def _pipeline_double(tmp_path: Path) -> Pipeline:
    """Return a minimal Pipeline whose lifecycle is safe for direct-run tests."""
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.name = "example"
    pipeline.jarvis = SimpleNamespace(
        get_pipeline_shared_dir=lambda _name: tmp_path / "shared" / "example"
    )
    pipeline.env = {"KEEP": "value"}
    pipeline.container_image = ""
    pipeline._execution_root = None
    pipeline._execution_id = None
    pipeline.configure_all_packages = Mock()
    pipeline.start = Mock()
    pipeline.stop = Mock()
    return pipeline


def _terminal_execution_for_cleanup(
    tmp_path: Path,
    execution_id: str,
) -> tuple[Pipeline, ExecutionStore]:
    """Create a minimal terminal execution accepted by exact cleanup."""
    shared_dir = tmp_path / "shared" / "example"
    store = ExecutionStore(shared_dir / "executions", "example")
    store.create(execution_id, mode="direct")
    store.update(execution_id, state="running")
    store.update(
        execution_id,
        state="completed",
        terminal=True,
        return_code=0,
    )
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.name = "example"
    pipeline._execution_root = None
    pipeline.jarvis = SimpleNamespace(
        get_pipeline_shared_dir=lambda _name: shared_dir,
    )
    return pipeline, store


def test_cleanup_waits_for_inflight_record_writer_before_detach(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A writer already inside its transaction commits before cleanup inspects."""
    pipeline, store = _terminal_execution_for_cleanup(tmp_path, "writer-first")
    writer_at_commit = Event()
    release_writer = Event()
    cleanup_attempted_lock = Event()
    ordering: list[str] = []
    real_atomic_write = execution_module._atomic_write_record
    real_cleanup_lock = pipeline_module.execution_transaction_lock
    real_inspect = pipeline_module._inspect_execution_root

    def blocked_atomic_write(path: Path, record: ExecutionRecord) -> None:
        if record.metadata.get("writer_revision") == 1:
            writer_at_commit.set()
            if not release_writer.wait(timeout=5):
                raise AssertionError("writer interleaving was not released")
            real_atomic_write(path, record)
            ordering.append("writer-committed")
            return
        real_atomic_write(path, record)

    @contextmanager
    def observed_cleanup_lock(
        executions_dir: Path,
        execution_id: str,
        *,
        timeout: float = 30.0,
    ) -> Iterator[None]:
        cleanup_attempted_lock.set()
        with real_cleanup_lock(
            executions_dir,
            execution_id,
            timeout=timeout,
        ):
            yield

    def observed_inspect(
        path: Path,
        *,
        executions_descriptor: int | None,
        expected_execution_id: str,
    ) -> tuple[dict[str, Any], tuple[int, int]]:
        ordering.append("cleanup-inspected")
        return real_inspect(
            path,
            executions_descriptor=executions_descriptor,
            expected_execution_id=expected_execution_id,
        )

    monkeypatch.setattr(execution_module, "_atomic_write_record", blocked_atomic_write)
    monkeypatch.setattr(
        pipeline_module,
        "execution_transaction_lock",
        observed_cleanup_lock,
    )
    monkeypatch.setattr(pipeline_module, "_inspect_execution_root", observed_inspect)

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(
            store.update,
            "writer-first",
            metadata={"writer_revision": 1},
        )
        assert writer_at_commit.wait(timeout=5)
        cleanup = executor.submit(pipeline.cleanup_executions, ["writer-first"])
        assert cleanup_attempted_lock.wait(timeout=5)
        release_writer.set()

        updated = writer.result(timeout=5)
        removed = cleanup.result(timeout=5)

    assert updated.metadata["writer_revision"] == 1
    assert removed == ["writer-first"]
    assert ordering == ["writer-committed", "cleanup-inspected"]
    assert not (store.executions_dir / "writer-first").exists()


def test_record_writer_waits_for_cleanup_and_cannot_resurrect_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cleanup holding the transaction lock wins before a later writer reads."""
    pipeline, store = _terminal_execution_for_cleanup(tmp_path, "cleanup-first")
    cleanup_inside_lock = Event()
    release_cleanup = Event()
    writer_attempted_lock = Event()
    real_inspect = pipeline_module._inspect_execution_root
    real_writer_lock = execution_module.execution_transaction_lock

    def blocked_inspect(
        path: Path,
        *,
        executions_descriptor: int | None,
        expected_execution_id: str,
    ) -> tuple[dict[str, Any], tuple[int, int]]:
        cleanup_inside_lock.set()
        if not release_cleanup.wait(timeout=5):
            raise AssertionError("cleanup interleaving was not released")
        return real_inspect(
            path,
            executions_descriptor=executions_descriptor,
            expected_execution_id=expected_execution_id,
        )

    @contextmanager
    def observed_writer_lock(
        executions_dir: Path,
        execution_id: str,
        *,
        timeout: float = 30.0,
    ) -> Iterator[None]:
        writer_attempted_lock.set()
        with real_writer_lock(
            executions_dir,
            execution_id,
            timeout=timeout,
        ):
            yield

    monkeypatch.setattr(pipeline_module, "_inspect_execution_root", blocked_inspect)

    with ThreadPoolExecutor(max_workers=2) as executor:
        cleanup = executor.submit(pipeline.cleanup_executions, ["cleanup-first"])
        assert cleanup_inside_lock.wait(timeout=5)
        monkeypatch.setattr(
            execution_module,
            "execution_transaction_lock",
            observed_writer_lock,
        )
        writer = executor.submit(
            store.update,
            "cleanup-first",
            metadata={"too_late": True},
        )
        assert writer_attempted_lock.wait(timeout=5)
        release_cleanup.set()

        assert cleanup.result(timeout=5) == ["cleanup-first"]
        with pytest.raises((FileNotFoundError, RuntimeError)):
            writer.result(timeout=5)

    assert not (store.executions_dir / "cleanup-first").exists()
    assert not (store.executions_dir / ".remove-cleanup-first").exists()


def test_direct_run_returns_handle_and_restores_named_context(tmp_path: Path) -> None:
    """Blocking direct execution is durable while runtime paths remain isolated."""
    pipeline = _pipeline_double(tmp_path)
    observed: dict[str, object] = {}

    def inspect_running_record() -> None:
        observed["root"] = pipeline._execution_root
        observed["record"] = pipeline.get_execution("direct-run")
        pipeline.env["JARVIS_SERVICE_RUNTIME_PATH"] = str(
            tmp_path / "execution" / "service-runtimes" / "viewer.jsonl"
        )

    pipeline.start.side_effect = inspect_running_record

    handle = pipeline.run(execution_id="direct-run")

    running = observed["record"]
    assert isinstance(running, ExecutionRecord)
    assert running.state == "running"
    assert Path(observed["root"]) == (
        tmp_path / "shared" / "example" / "executions" / "direct-run"
    )
    assert handle.mode == "direct"
    assert handle.scheduler_native_id is None
    assert handle.refresh().state == "completed"
    assert pipeline._execution_root is None
    assert pipeline._execution_id is None
    assert pipeline.env == {"KEEP": "value"}


def test_direct_run_failure_is_durable_and_original_error_survives(
    tmp_path: Path,
) -> None:
    """A failed blocking run records failure before re-raising its cause."""
    pipeline = _pipeline_double(tmp_path)
    pipeline.start.side_effect = RuntimeError("package exploded\nwith detail")

    with pytest.raises(RuntimeError, match="package exploded"):
        pipeline.run(execution_id="failed-run")

    record = pipeline.get_execution("failed-run")
    assert record.state == "failed"
    assert record.terminal is True
    assert record.return_code == 1
    assert record.error == "package exploded\nwith detail"


class _FatalExecutionSignal(BaseException):
    """Test-only non-Exception interruption."""


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(KeyboardInterrupt(), id="keyboard-interrupt"),
        pytest.param(SystemExit(9), id="system-exit"),
        pytest.param(_FatalExecutionSignal("fatal signal"), id="base-exception"),
    ],
)
def test_direct_run_base_exception_is_terminal_and_re_raised(
    tmp_path: Path,
    failure: BaseException,
) -> None:
    """Interruptions cannot leave a direct execution indefinitely running."""
    pipeline = _pipeline_double(tmp_path)
    pipeline.start.side_effect = failure

    with pytest.raises(type(failure)):
        pipeline.run(execution_id="interrupted-run")

    record = pipeline.get_execution("interrupted-run")
    assert record.state == "failed"
    assert record.terminal is True
    assert record.return_code == 1
    assert record.error == (str(failure) or type(failure).__name__)
    assert pipeline._execution_root is None
    assert pipeline._execution_id is None
    assert pipeline.env == {"KEEP": "value"}


def test_direct_run_terminalizes_when_cleanup_is_also_interrupted(
    tmp_path: Path,
) -> None:
    """A cleanup interruption is noted without replacing the original failure."""
    pipeline = _pipeline_double(tmp_path)
    original = RuntimeError("package failed")
    pipeline.start.side_effect = original
    pipeline.stop.side_effect = KeyboardInterrupt()

    with pytest.raises(RuntimeError, match="package failed") as raised:
        pipeline.run(execution_id="cleanup-interrupted")

    assert raised.value is original
    assert any("cleanup also failed" in note for note in original.__notes__)
    record = pipeline.get_execution("cleanup-interrupted")
    assert record.state == "failed"
    assert record.terminal is True


def test_nonblocking_direct_run_returns_live_queryable_handle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Local work can return its generated ID before the workload completes."""
    pipeline = _pipeline_double(tmp_path)
    pipeline.save = Mock()

    def write_snapshot(
        execution_root: Path,
        scheduler_spec: dict[str, object],
    ) -> tuple[Path, Path, str]:
        assert scheduler_spec == {}
        runtime = execution_root / "runtime"
        inputs = execution_root / "input"
        runtime.mkdir()
        inputs.mkdir()
        return runtime, inputs, "abc123"

    pipeline._write_execution_snapshot = Mock(side_effect=write_snapshot)

    class Process:
        pid = 4242

    monkeypatch.setattr(
        "jarvis_cd.core.pipeline.subprocess.Popen", lambda *a, **k: Process()
    )
    monkeypatch.setattr(
        "jarvis_cd.core.execution._process_is_running", lambda _pid: True
    )

    handle = pipeline.run(execution_id="background", wait=False)

    assert handle.execution_id == "background"
    assert handle.mode == "direct"
    assert handle.scheduler_native_id is None
    record = handle.refresh()
    assert record.state == "preparing"
    assert record.terminal is False
    assert record.metadata["direct_process_id"] == 4242
    assert handle.progress().execution_id == "background"


def test_usable_systemd_user_runtime_dir_prefers_a_real_env_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ambient XDG_RUNTIME_DIR wins when it actually exists on disk."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setattr(
        pipeline_module.os.path, "isdir", lambda path: path == "/run/user/1000"
    )
    assert pipeline_module._usable_systemd_user_runtime_dir() == "/run/user/1000"


def test_usable_systemd_user_runtime_dir_falls_back_to_the_conventional_uid_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stripped env var does not mean the session is gone.

    Regression case for clio-relay#222's local reproduction: the real relay
    broker -> uv -> clio-kit -> jarvis-mcp launch chain does NOT forward
    XDG_RUNTIME_DIR down to where a direct-mode pipeline actually launches
    (confirmed live on a WSL harness reproduction), even though the user's
    systemd session and its conventional /run/user/<uid> directory are still
    live underneath, and systemd-run itself falls back to that same path.
    """
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(pipeline_module.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(
        pipeline_module.os.path, "isdir", lambda path: path == "/run/user/1000"
    )
    assert pipeline_module._usable_systemd_user_runtime_dir() == "/run/user/1000"


def test_usable_systemd_user_runtime_dir_none_when_nothing_is_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No env var, no conventional directory: correctly report unusable."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(pipeline_module.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(pipeline_module.os.path, "isdir", lambda _path: False)
    assert pipeline_module._usable_systemd_user_runtime_dir() is None


def test_escaped_direct_launch_command_wraps_with_systemd_run_scope_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A usable systemd user session gets its own transient scope, not a plain child.

    ``start_new_session=True`` (setsid) detaches session/process-group but not
    cgroup membership: a merely-detached child stays inside whatever cgroup the
    launching process is tracked in. On a host with a working systemd --user
    session (the same precondition clio-relay's own process containment
    requires), wrapping the launch in its own ``systemd-run --user --scope``
    unit gives it an independent, sibling cgroup instead — see clio-relay#222.
    """
    monkeypatch.setattr(pipeline_module, "_is_windows_platform", lambda: False)
    monkeypatch.setattr(
        pipeline_module.shutil, "which", lambda _name: "/usr/bin/systemd-run"
    )
    monkeypatch.setattr(
        pipeline_module, "_usable_systemd_user_runtime_dir", lambda: "/run/user/1000"
    )
    monkeypatch.setattr(
        pipeline_module,
        "_systemd_user_scope_is_usable",
        lambda _systemd_run, _environment: (True, None),
    )

    original = ["python3", "-m", "jarvis_cd.core.execution", "run-snapshot"]
    wrapped, reason, detail, environment = pipeline_module._escaped_direct_launch_command(
        original
    )

    assert reason == "systemd_scope"
    assert detail is None
    assert environment is not None
    assert environment["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert wrapped[0] == "/usr/bin/systemd-run"
    assert wrapped[1:4] == ["--user", "--scope", "--quiet"]
    assert wrapped[4].startswith("--unit=jarvis-cd-direct-")
    assert wrapped[5] == "--"
    assert wrapped[6:] == original
    # Two independent calls must not collide on the same transient unit name.
    other, _other_reason, _other_detail, _other_env = (
        pipeline_module._escaped_direct_launch_command(original)
    )
    assert other[4] != wrapped[4]


def test_escaped_direct_launch_command_falls_back_without_systemd_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosts without systemd-run keep today's setsid-only launch, unchanged."""
    monkeypatch.setattr(pipeline_module, "_is_windows_platform", lambda: False)
    monkeypatch.setattr(pipeline_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        pipeline_module, "_usable_systemd_user_runtime_dir", lambda: "/run/user/1000"
    )
    monkeypatch.setattr(
        pipeline_module,
        "_systemd_user_scope_is_usable",
        lambda _systemd_run, _environment: (True, None),
    )

    original = ["python3", "-m", "jarvis_cd.core.execution", "run-snapshot"]
    command, reason, detail, environment = pipeline_module._escaped_direct_launch_command(
        original
    )
    assert command == original
    assert reason == "skipped_no_systemd_run"
    assert detail is None
    assert environment is None


def test_escaped_direct_launch_command_falls_back_without_runtime_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No live systemd user runtime directory at all is not trusted to scope."""
    monkeypatch.setattr(pipeline_module, "_is_windows_platform", lambda: False)
    monkeypatch.setattr(
        pipeline_module.shutil, "which", lambda _name: "/usr/bin/systemd-run"
    )
    monkeypatch.setattr(
        pipeline_module, "_usable_systemd_user_runtime_dir", lambda: None
    )

    original = ["python3", "-m", "jarvis_cd.core.execution", "run-snapshot"]
    command, reason, detail, environment = pipeline_module._escaped_direct_launch_command(
        original
    )
    assert command == original
    assert reason == "skipped_no_runtime_dir"
    assert detail is None
    assert environment is None


def test_escaped_direct_launch_command_falls_back_when_the_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A binary and a runtime dir are not enough; the probe must also succeed.

    Regression case for a real failure observed while validating clio-relay#222:
    a deeply nested launch chain (relay broker -> uv -> clio-kit -> jarvis-mcp)
    had a live systemd-run binary and a live /run/user/<uid>, yet
    ``systemd-run --user --scope`` still failed its D-Bus connection
    ("Failed to connect to bus: No medium found") — jarvis-cd's own
    orphan-reconciliation then wrongly failed the execution a few hundred
    milliseconds after a successful spawn, because the wrap silently swapped
    a leaked-descendant failure for a broken-launch failure. Falling back to
    the unwrapped (pre-#222-fix) launch when the probe fails avoids trading
    one failure mode for a worse one. The probe's own diagnostic text (R1)
    must survive into the returned reason's detail, not just a boolean.
    """
    monkeypatch.setattr(pipeline_module, "_is_windows_platform", lambda: False)
    monkeypatch.setattr(
        pipeline_module.shutil, "which", lambda _name: "/usr/bin/systemd-run"
    )
    monkeypatch.setattr(
        pipeline_module, "_usable_systemd_user_runtime_dir", lambda: "/run/user/1000"
    )
    monkeypatch.setattr(
        pipeline_module,
        "_systemd_user_scope_is_usable",
        lambda _systemd_run, _environment: (
            False,
            "Failed to connect to bus: No medium found",
        ),
    )

    original = ["python3", "-m", "jarvis_cd.core.execution", "run-snapshot"]
    command, reason, detail, environment = pipeline_module._escaped_direct_launch_command(
        original
    )
    assert command == original
    assert reason == "degraded_probe_failed"
    assert detail == "Failed to connect to bus: No medium found"
    assert environment is None


_PROBE_TEST_ENVIRONMENT = {"XDG_RUNTIME_DIR": "/run/user/1000"}


def test_systemd_user_scope_is_usable_true_on_a_successful_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean probe scope (exit 0) is trusted, with no diagnostic to report."""

    class _Completed:
        returncode = 0

    monkeypatch.setattr(
        pipeline_module.subprocess,
        "run",
        lambda *_a, **_k: _Completed(),
    )
    assert pipeline_module._systemd_user_scope_is_usable(
        "/usr/bin/systemd-run", _PROBE_TEST_ENVIRONMENT
    ) == (True, None)


def test_systemd_user_scope_is_usable_false_on_a_nonzero_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe scope that exits nonzero (e.g. a failed D-Bus connection) is not
    trusted, and its own stderr text is surfaced as the diagnostic (R1)."""

    class _Completed:
        returncode = 1
        stderr = b"Failed to connect to bus: No medium found\n"

    monkeypatch.setattr(
        pipeline_module.subprocess,
        "run",
        lambda *_a, **_k: _Completed(),
    )
    assert pipeline_module._systemd_user_scope_is_usable(
        "/usr/bin/systemd-run", _PROBE_TEST_ENVIRONMENT
    ) == (
        False,
        "Failed to connect to bus: No medium found",
    )


def test_systemd_user_scope_is_usable_false_on_a_nonzero_probe_with_no_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nonzero exit with no captured stderr still yields a non-empty diagnostic."""

    class _Completed:
        returncode = 17
        stderr = b""

    monkeypatch.setattr(
        pipeline_module.subprocess,
        "run",
        lambda *_a, **_k: _Completed(),
    )
    usable, diagnostic = pipeline_module._systemd_user_scope_is_usable(
        "/usr/bin/systemd-run", _PROBE_TEST_ENVIRONMENT
    )
    assert usable is False
    assert diagnostic == "systemd-run --user --scope probe exited 17"


def test_systemd_user_scope_is_usable_false_when_the_probe_hangs_or_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that times out or cannot even launch is treated as unusable,
    and each failure mode gets its own non-empty diagnostic (R1)."""

    def _raise_timeout(*_a: Any, **_k: Any) -> Any:
        raise pipeline_module.subprocess.TimeoutExpired(cmd="systemd-run", timeout=2.0)

    monkeypatch.setattr(pipeline_module.subprocess, "run", _raise_timeout)
    usable, diagnostic = pipeline_module._systemd_user_scope_is_usable(
        "/usr/bin/systemd-run", _PROBE_TEST_ENVIRONMENT
    )
    assert usable is False
    assert diagnostic is not None and "2.0" in diagnostic

    def _raise_oserror(*_a: Any, **_k: Any) -> Any:
        raise OSError("no such executable")

    monkeypatch.setattr(pipeline_module.subprocess, "run", _raise_oserror)
    assert pipeline_module._systemd_user_scope_is_usable(
        "/usr/bin/systemd-run", _PROBE_TEST_ENVIRONMENT
    ) == (
        False,
        "no such executable",
    )


def test_systemd_user_scope_is_usable_probes_with_the_exact_argv_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3: pin the probe's own argv, not just its boolean outcome.

    A sabotage flip of ``--user`` -> ``--system`` in the real probe would
    make it permanently unusable for a non-root user (silent, universal
    fallback -- clio-relay#222 everywhere), yet every other test in this
    file stubs this function away entirely and never inspects the command
    it runs. Assert the composed argv and the bounded timeout directly so
    that exact regression is caught here.
    """
    captured: dict[str, Any] = {}

    class _Completed:
        returncode = 0

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return _Completed()

    monkeypatch.setattr(pipeline_module.subprocess, "run", fake_run)
    pipeline_module._systemd_user_scope_is_usable(
        "/usr/bin/systemd-run", _PROBE_TEST_ENVIRONMENT
    )

    assert captured["command"] == [
        "/usr/bin/systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--",
        "true",
    ]
    assert captured["kwargs"]["timeout"] == pipeline_module._SYSTEMD_SCOPE_PROBE_TIMEOUT_SECONDS
    assert captured["kwargs"]["stdin"] == pipeline_module.subprocess.DEVNULL
    assert captured["kwargs"]["stdout"] == pipeline_module.subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] == pipeline_module.subprocess.PIPE
    # R2 root-cause: the probe MUST run with the explicit environment the
    # caller resolved, not bare ambient inheritance -- this is the exact
    # fix for the deep relay chain where XDG_RUNTIME_DIR/
    # DBUS_SESSION_BUS_ADDRESS are both stripped before jarvis-cd ever
    # sees them.
    assert captured["kwargs"]["env"] == _PROBE_TEST_ENVIRONMENT


def test_systemd_user_runtime_environment_derives_the_dbus_address_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2 root cause: a deep relay chain can strip BOTH XDG_RUNTIME_DIR and
    DBUS_SESSION_BUS_ADDRESS before jarvis-cd's own process ever sees them.

    Confirmed live on the WSL harness: with neither var set, a real
    ``systemd-run --user --scope`` subprocess fails
    ("Failed to connect to bus: No medium found") even though the
    directory demonstrably exists (``_usable_systemd_user_runtime_dir``
    returns non-None via its getuid()-derived fallback) -- systemd-run
    does not reliably rediscover the bus on its own from the directory
    alone. Explicitly exporting XDG_RUNTIME_DIR (and, when absent,
    deriving DBUS_SESSION_BUS_ADDRESS from it) into the subprocess's own
    environment is what actually closes the gap; verified empirically
    (env-stripped subprocess: fails without, succeeds with either var set).
    """
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setenv("SOME_OTHER_VAR", "kept")
    environment = pipeline_module._systemd_user_runtime_environment("/run/user/1000")
    assert environment["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert environment["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"
    assert environment["SOME_OTHER_VAR"] == "kept"


def test_systemd_user_runtime_environment_trusts_an_existing_bus_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-correct ambient DBUS_SESSION_BUS_ADDRESS is never guessed
    over -- some hosts genuinely use a non-default bus address."""
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/custom/bus/path")
    environment = pipeline_module._systemd_user_runtime_environment("/run/user/1000")
    assert environment["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert environment["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/custom/bus/path"


def test_cgroup_membership_is_none_without_a_readable_proc_entry() -> None:
    """A PID with no ``/proc/<pid>/cgroup`` (gone, or no /proc at all) is None."""
    assert pipeline_module._cgroup_membership("999999999") is None


def test_cgroup_escape_confirmed_true_once_membership_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single differing read is enough — no need to keep polling."""
    monkeypatch.setattr(
        pipeline_module,
        "_cgroup_membership",
        lambda pid: "caller\n" if pid == "self" else "escaped\n",
    )
    assert pipeline_module._cgroup_escape_confirmed(4242) is True


def test_cgroup_escape_confirmed_false_when_membership_never_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Still in the caller's own cgroup at the deadline: not confirmed."""
    monkeypatch.setattr(pipeline_module, "_cgroup_membership", lambda _pid: "same\n")
    monkeypatch.setattr(
        pipeline_module, "_CGROUP_ESCAPE_CONFIRM_TIMEOUT_SECONDS", 0.05
    )
    monkeypatch.setattr(
        pipeline_module, "_CGROUP_ESCAPE_POLL_INTERVAL_SECONDS", 0.01
    )
    assert pipeline_module._cgroup_escape_confirmed(4242) is False


def test_cgroup_escape_confirmed_false_when_the_process_is_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable target cgroup (process already exited) is not confirmed."""

    def fake_membership(pid: str) -> str | None:
        return "caller\n" if pid == "self" else None

    monkeypatch.setattr(pipeline_module, "_cgroup_membership", fake_membership)
    assert pipeline_module._cgroup_escape_confirmed(4242) is False


def test_cgroup_escape_confirmed_false_without_a_readable_caller_cgroup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No /proc support at all (e.g. non-Linux) is not confirmed, not raised."""
    monkeypatch.setattr(pipeline_module, "_cgroup_membership", lambda _pid: None)
    assert pipeline_module._cgroup_escape_confirmed(4242) is False


def test_cgroup_escape_confirmed_false_for_a_cgroup_nested_inside_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S2: a nested descendant cgroup proves "moved", not "moved *out*".

    Plain string inequality (the original check) would wrongly confirm a
    cgroup nested *inside* the caller's own delegated subtree as escaped,
    even though it remains fully visible to a recursive containment scan.
    Not reachable via ``systemd-run --user --scope`` today (scopes land as
    a sibling under ``app.slice``), but the strict path-segment check must
    still reject it rather than merely differ-by-string.
    """
    caller = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/outer.scope\n"
    nested = (
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
        "outer.scope/nested-child\n"
    )
    monkeypatch.setattr(
        pipeline_module,
        "_cgroup_membership",
        lambda pid: caller if pid == "self" else nested,
    )
    monkeypatch.setattr(
        pipeline_module, "_CGROUP_ESCAPE_CONFIRM_TIMEOUT_SECONDS", 0.05
    )
    monkeypatch.setattr(
        pipeline_module, "_CGROUP_ESCAPE_POLL_INTERVAL_SECONDS", 0.01
    )
    assert pipeline_module._cgroup_escape_confirmed(4242) is False


def test_cgroup_escape_confirmed_true_for_a_sibling_sharing_a_name_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S2: a sibling whose unit name merely starts with the same characters
    as the caller's must NOT be mistaken for a (raw-string-prefix) descendant.
    """
    caller = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/outer.scope\n"
    sibling = (
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
        "outer.scope-2\n"
    )
    monkeypatch.setattr(
        pipeline_module,
        "_cgroup_membership",
        lambda pid: caller if pid == "self" else sibling,
    )
    assert pipeline_module._cgroup_escape_confirmed(4242) is True


def test_terminate_launched_process_best_effort_skips_an_already_exited_process() -> None:
    """No termination is attempted once the process has already exited."""

    class Process:
        terminate_calls = 0

        def poll(self) -> int:
            return 0

        def terminate(self) -> None:
            Process.terminate_calls += 1

    pipeline_module._terminate_launched_process_best_effort(Process())
    assert Process.terminate_calls == 0


def test_terminate_launched_process_best_effort_kills_after_a_stuck_terminate() -> None:
    """A terminate() that never lands falls back to kill()."""

    class Process:
        def __init__(self) -> None:
            self.killed = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            if not self.killed:
                raise pipeline_module.subprocess.TimeoutExpired(cmd="x", timeout=timeout)
            return -9

        def kill(self) -> None:
            self.killed = True

    process = Process()
    pipeline_module._terminate_launched_process_best_effort(process)
    assert process.killed is True


def test_escaped_direct_launch_command_falls_back_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This launch is left untouched on Windows -- not because there is no
    equivalent race, but because fixing it needs a relay-side change first.

    (S1) clio-relay's ``ensure_owned_process_tree_empty`` has a Windows Job
    Object containment branch with the structurally same bug: Job
    membership is inherited across ``CreateProcess`` and
    ``CREATE_NEW_PROCESS_GROUP`` does not break out of a job -- only
    ``CREATE_BREAKAWAY_FROM_JOB`` does, which requires the parent job to
    carry ``JOB_OBJECT_LIMIT_BREAKAWAY_OK`` (clio-relay grants none today).
    So the Windows race is real and unfixed; it cannot be closed from
    jarvis-cd alone. jarvis-cd already has its own Job Object layer
    (``jarvis_cd/shell/windows_job.py``), which is the natural home for
    the eventual fix once relay grants the breakaway limit.
    """
    monkeypatch.setattr(pipeline_module, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(
        pipeline_module.shutil, "which", lambda _name: "/usr/bin/systemd-run"
    )
    monkeypatch.setattr(
        pipeline_module, "_usable_systemd_user_runtime_dir", lambda: "/run/user/1000"
    )
    monkeypatch.setattr(
        pipeline_module,
        "_systemd_user_scope_is_usable",
        lambda _systemd_run, _environment: (True, None),
    )

    original = ["python3", "-m", "jarvis_cd.core.execution", "run-snapshot"]
    command, reason, detail, environment = pipeline_module._escaped_direct_launch_command(
        original
    )
    assert command == original
    assert reason == "skipped_windows"
    assert detail is None
    assert environment is None


def test_nonblocking_direct_run_escapes_into_its_own_systemd_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The real launch path wraps the run-snapshot command through the escape.

    Regression test for clio-relay#222: without this wrap, the run-snapshot
    child (and the application it launches) stays inside the caller's tracked
    cgroup, so a caller-scoped containment check sees it as a leaked
    descendant and kills it mid-run even though ``jarvis_run`` documents
    direct mode as "start a pipeline without waiting".
    """
    pipeline = _pipeline_double(tmp_path)
    pipeline.save = Mock()

    def write_snapshot(
        execution_root: Path,
        scheduler_spec: dict[str, object],
    ) -> tuple[Path, Path, str]:
        assert scheduler_spec == {}
        runtime = execution_root / "runtime"
        inputs = execution_root / "input"
        runtime.mkdir()
        inputs.mkdir()
        return runtime, inputs, "abc123"

    pipeline._write_execution_snapshot = Mock(side_effect=write_snapshot)

    captured_commands: list[list[str]] = []
    captured_kwargs: list[dict[str, Any]] = []

    class Process:
        pid = 4242

    def fake_popen(command: list[str], **kwargs: Any) -> Process:
        captured_commands.append(command)
        captured_kwargs.append(kwargs)
        return Process()

    monkeypatch.setattr(pipeline_module, "_is_windows_platform", lambda: False)
    monkeypatch.setattr(
        pipeline_module.shutil, "which", lambda _name: "/usr/bin/systemd-run"
    )
    monkeypatch.setattr(
        pipeline_module, "_usable_systemd_user_runtime_dir", lambda: "/run/user/1000"
    )
    monkeypatch.setattr(
        pipeline_module,
        "_systemd_user_scope_is_usable",
        lambda _systemd_run, _environment: (True, None),
    )
    monkeypatch.setattr(
        pipeline_module, "_cgroup_escape_confirmed", lambda _pid: True
    )
    monkeypatch.setattr("jarvis_cd.core.pipeline.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "jarvis_cd.core.execution._process_is_running", lambda _pid: True
    )

    handle = pipeline.run(execution_id="background", wait=False)

    assert handle.execution_id == "background"
    assert len(captured_commands) == 1
    launched = captured_commands[0]
    assert launched[0] == "/usr/bin/systemd-run"
    assert launched[1:4] == ["--user", "--scope", "--quiet"]
    assert launched[4].startswith("--unit=jarvis-cd-direct-")
    assert launched[5] == "--"
    assert "run-snapshot" in launched
    assert launched[6] == pipeline_module.sys.executable

    # L1: the REAL wrapped launch (not just the live probe) must receive the
    # runtime-bus environment the escape resolved. Dropping `env=environment`
    # from this specific Popen call (while leaving it on the probe) silently
    # reinstates the #222 race on the exact deep-relay-chain host class this
    # fix exists to serve: systemd-run spawns fine (no interpreter error, a
    # live PID) but its own D-Bus connection fails for lack of
    # XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS, never execs the target, and
    # the confirm poll times out into a degraded retry -- with every other
    # test in this file still green, since none of them inspect Popen's
    # kwargs.
    assert len(captured_kwargs) == 1
    launched_env = captured_kwargs[0].get("env")
    assert launched_env is not None
    assert launched_env["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert launched_env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"

    # R1: the successful escape's reason lands on the durable metadata
    # record clio-relay reads, not just the interpreter's local state.
    record = handle.refresh()
    direct_launch = record.metadata["direct_launch"]
    assert direct_launch["schema_version"] == pipeline_module.DIRECT_LAUNCH_SCHEMA
    assert direct_launch["escape"] == "systemd_scope"
    assert direct_launch["escape_detail"] is None


def test_nonblocking_direct_run_retries_unwrapped_when_the_cgroup_escape_does_not_land(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A confirmed-failed migration falls back to a second, unwrapped launch.

    Regression test for a second, distinct race found while validating the
    clio-relay#222 fix on a real deployment: ``systemd-run --user --scope``
    can spawn successfully (no interpreter error, a live PID) yet still not
    have migrated its target out of the caller's cgroup by the time this
    code checks — an asynchronous D-Bus race, not a permanent incapability.
    Failing the whole launch in that case would regress a working (if
    race-prone) direct execution into a broken one; retrying unwrapped keeps
    it working exactly as it did before the escape was ever added.

    Also covers S3: the retry must stop the transient scope UNIT (via
    ``systemctl --user stop <unit>.scope``), not just terminate the
    leader PID — a leader-only terminate can leave grandchildren the
    leader already forked inside the escaped cgroup running and
    un-contained.
    """
    pipeline = _pipeline_double(tmp_path)
    pipeline.save = Mock()

    def write_snapshot(
        execution_root: Path,
        scheduler_spec: dict[str, object],
    ) -> tuple[Path, Path, str]:
        assert scheduler_spec == {}
        runtime = execution_root / "runtime"
        inputs = execution_root / "input"
        runtime.mkdir()
        inputs.mkdir()
        return runtime, inputs, "abc123"

    pipeline._write_execution_snapshot = Mock(side_effect=write_snapshot)

    captured_commands: list[list[str]] = []
    captured_kwargs: list[dict[str, Any]] = []
    terminated_pids: list[int] = []

    class Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self._polled = False

        def poll(self) -> int | None:
            # Alive on the first poll (still needs terminating), gone after.
            if self._polled:
                return 0
            self._polled = True
            return None

        def terminate(self) -> None:
            terminated_pids.append(self.pid)

        def wait(self, timeout: float | None = None) -> int:
            return 0

    pids = iter([9001, 9002])

    def fake_popen(command: list[str], **kwargs: Any) -> Process:
        captured_commands.append(command)
        captured_kwargs.append(kwargs)
        return Process(next(pids))

    systemctl_calls: list[list[str]] = []

    class _CompletedSystemctl:
        returncode = 0

    def fake_run(command: list[str], **_kwargs: Any) -> Any:
        systemctl_calls.append(command)
        return _CompletedSystemctl()

    def fake_which(name: str) -> str | None:
        return {
            "systemd-run": "/usr/bin/systemd-run",
            "systemctl": "/usr/bin/systemctl",
        }.get(name)

    monkeypatch.setattr(pipeline_module, "_is_windows_platform", lambda: False)
    monkeypatch.setattr(pipeline_module.shutil, "which", fake_which)
    monkeypatch.setattr(
        pipeline_module, "_usable_systemd_user_runtime_dir", lambda: "/run/user/1000"
    )
    monkeypatch.setattr(
        pipeline_module,
        "_systemd_user_scope_is_usable",
        lambda _systemd_run, _environment: (True, None),
    )
    monkeypatch.setattr(
        pipeline_module, "_cgroup_escape_confirmed", lambda _pid: False
    )
    monkeypatch.setattr("jarvis_cd.core.pipeline.subprocess.Popen", fake_popen)
    monkeypatch.setattr(pipeline_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        "jarvis_cd.core.execution._process_is_running", lambda _pid: True
    )

    handle = pipeline.run(execution_id="background", wait=False)

    assert handle.execution_id == "background"
    assert len(captured_commands) == 2
    first, second = captured_commands
    assert first[0] == "/usr/bin/systemd-run"
    assert second == first[6:]  # the same unwrapped run-snapshot argv
    assert terminated_pids == [9001]

    # S3: the scope UNIT was stopped (not just the leader PID terminated).
    assert len(systemctl_calls) == 1
    stop_call = systemctl_calls[0]
    assert stop_call[0] == "/usr/bin/systemctl"
    assert stop_call[1:3] == ["--user", "stop"]
    unit_token = first[4]
    assert unit_token.startswith("--unit=")
    expected_unit = unit_token[len("--unit=") :]
    assert stop_call[3] == f"{expected_unit}.scope"

    # L1: the wrapped attempt received the runtime-bus env; the unwrapped
    # retry must NOT -- it falls back to the plain, ambient-inheriting
    # options exactly as the pre-escape launch did, not a hybrid of the two.
    assert len(captured_kwargs) == 2
    wrapped_kwargs, retry_kwargs = captured_kwargs
    assert wrapped_kwargs.get("env", {}).get("XDG_RUNTIME_DIR") == "/run/user/1000"
    assert "env" not in retry_kwargs

    record = handle.refresh()
    assert record.metadata["direct_process_id"] == 9002

    # R1: the degraded reason and its diagnostic land on the durable
    # metadata record clio-relay reads, not just an interpreter-local retry.
    direct_launch = record.metadata["direct_launch"]
    assert direct_launch["escape"] == "degraded_migration_unconfirmed"
    assert direct_launch["escape_detail"] is not None
    assert "cgroup migration not confirmed" in direct_launch["escape_detail"]


def test_spawn_direct_execution_process_retries_unwrapped_on_a_spawn_oserror(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """S4: a wrapped spawn that raises OSError falls back unwrapped, typed.

    Before this fix, only an *unconfirmed migration* had a fallback; if the
    wrapped ``subprocess.Popen`` call itself raised (e.g. ``systemd-run``
    was removed between the live probe and this call), the exception
    propagated and the launch hard-failed — a state that could not occur
    pre-#222-fix. Closes that gap with the same "retry unwrapped, record
    why" shape as the other degraded paths (R1).
    """
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    stdout_stream = stdout_path.open("ab", buffering=0)
    stderr_stream = stderr_path.open("ab", buffering=0)

    monkeypatch.setattr(pipeline_module, "_is_windows_platform", lambda: False)
    monkeypatch.setattr(
        pipeline_module.shutil, "which", lambda _name: "/usr/bin/systemd-run"
    )
    monkeypatch.setattr(
        pipeline_module, "_usable_systemd_user_runtime_dir", lambda: "/run/user/1000"
    )
    monkeypatch.setattr(
        pipeline_module,
        "_systemd_user_scope_is_usable",
        lambda _systemd_run, _environment: (True, None),
    )

    captured_commands: list[list[str]] = []

    class Process:
        pid = 4321

    def fake_popen(command: list[str], **_kwargs: Any) -> Process:
        captured_commands.append(command)
        if len(captured_commands) == 1:
            raise OSError("systemd-run vanished between probe and spawn")
        return Process()

    monkeypatch.setattr(pipeline_module.subprocess, "Popen", fake_popen)

    options: dict[str, Any] = {
        "stdin": pipeline_module.subprocess.DEVNULL,
        "stdout": stdout_stream,
        "stderr": stderr_stream,
    }
    process, reason, detail = pipeline_module._spawn_direct_execution_process(
        ["python3", "-m", "jarvis_cd.core.execution", "run-snapshot"],
        options,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    options["stdout"].close()
    options["stderr"].close()

    assert reason == "degraded_spawn_error"
    assert detail == "systemd-run vanished between probe and spawn"
    assert len(captured_commands) == 2
    assert captured_commands[0][0] == "/usr/bin/systemd-run"
    assert captured_commands[1] == [
        "python3",
        "-m",
        "jarvis_cd.core.execution",
        "run-snapshot",
    ]
    assert process.pid == 4321


def _configure_skipped_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline_module, "_is_windows_platform", lambda: True)


def _configure_skipped_no_systemd_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline_module, "_is_windows_platform", lambda: False)
    monkeypatch.setattr(pipeline_module.shutil, "which", lambda _name: None)


def _configure_skipped_no_runtime_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline_module, "_is_windows_platform", lambda: False)
    monkeypatch.setattr(
        pipeline_module.shutil, "which", lambda _name: "/usr/bin/systemd-run"
    )
    monkeypatch.setattr(
        pipeline_module, "_usable_systemd_user_runtime_dir", lambda: None
    )


def _configure_degraded_probe_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline_module, "_is_windows_platform", lambda: False)
    monkeypatch.setattr(
        pipeline_module.shutil, "which", lambda _name: "/usr/bin/systemd-run"
    )
    monkeypatch.setattr(
        pipeline_module, "_usable_systemd_user_runtime_dir", lambda: "/run/user/1000"
    )
    monkeypatch.setattr(
        pipeline_module,
        "_systemd_user_scope_is_usable",
        lambda _systemd_run, _environment: (
            False,
            "Failed to connect to bus: No medium found",
        ),
    )


def _configure_degraded_spawn_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline_module, "_is_windows_platform", lambda: False)
    monkeypatch.setattr(
        pipeline_module.shutil, "which", lambda _name: "/usr/bin/systemd-run"
    )
    monkeypatch.setattr(
        pipeline_module, "_usable_systemd_user_runtime_dir", lambda: "/run/user/1000"
    )
    monkeypatch.setattr(
        pipeline_module,
        "_systemd_user_scope_is_usable",
        lambda _systemd_run, _environment: (True, None),
    )


@pytest.mark.parametrize(
    ("escape_reason", "configure", "raises_first_popen", "expect_detail"),
    [
        ("skipped_windows", _configure_skipped_windows, False, False),
        ("skipped_no_systemd_run", _configure_skipped_no_systemd_run, False, False),
        ("skipped_no_runtime_dir", _configure_skipped_no_runtime_dir, False, False),
        ("degraded_probe_failed", _configure_degraded_probe_failed, False, True),
        ("degraded_spawn_error", _configure_degraded_spawn_error, True, True),
    ],
    ids=[
        "skipped_windows",
        "skipped_no_systemd_run",
        "skipped_no_runtime_dir",
        "degraded_probe_failed",
        "degraded_spawn_error",
    ],
)
def test_nonblocking_direct_run_records_the_exact_escape_reason_for_every_skip_or_degrade_branch(
    escape_reason: str,
    configure: Any,
    raises_first_popen: bool,
    expect_detail: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """L2: the PERSISTED record must carry the exact escape reason.

    ``systemd_scope`` (the happy path, above) and
    ``degraded_migration_unconfirmed`` (the retry test, above) are the only
    two of the seven :data:`DirectLaunchEscapeReason` values pinned at the
    durable-record level. The other five are only asserted against
    ``_escaped_direct_launch_command``'s own return value in the standalone
    unit tests above -- nothing catches a reason that is computed correctly
    by that helper and then reported wrongly by the time it reaches
    ``store.update``. A one-line hardcode of the reported reason to
    ``"systemd_scope"`` on the skip/degrade return in
    ``_spawn_direct_execution_process`` (e.g. the
    ``if escaped_command is command: ... return ..., reason, detail`` line)
    passes the whole suite today precisely because of that gap -- an
    operator reading the ``direct_launch`` record could no longer tell
    "escape worked" from "escape skipped", which is exactly the state R1
    exists to make impossible. Drives the real ``pipeline.run(wait=False)``
    path (not the helper directly) so the assertion is against what
    clio-relay actually reads off disk.
    """
    pipeline = _pipeline_double(tmp_path)
    pipeline.save = Mock()

    def write_snapshot(
        execution_root: Path,
        scheduler_spec: dict[str, object],
    ) -> tuple[Path, Path, str]:
        assert scheduler_spec == {}
        runtime = execution_root / "runtime"
        inputs = execution_root / "input"
        runtime.mkdir()
        inputs.mkdir()
        return runtime, inputs, "abc123"

    pipeline._write_execution_snapshot = Mock(side_effect=write_snapshot)

    configure(monkeypatch)

    popen_calls = {"count": 0}

    class Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    def fake_popen(command: list[str], **_kwargs: Any) -> Process:
        popen_calls["count"] += 1
        if raises_first_popen and popen_calls["count"] == 1:
            raise OSError("systemd-run vanished between probe and spawn")
        return Process(4200 + popen_calls["count"])

    monkeypatch.setattr("jarvis_cd.core.pipeline.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "jarvis_cd.core.execution._process_is_running", lambda _pid: True
    )

    handle = pipeline.run(execution_id="background", wait=False)

    record = handle.refresh()
    direct_launch = record.metadata["direct_launch"]
    assert direct_launch["schema_version"] == pipeline_module.DIRECT_LAUNCH_SCHEMA
    assert direct_launch["escape"] == escape_reason
    if expect_detail:
        assert direct_launch["escape_detail"]
    else:
        assert direct_launch["escape_detail"] is None


def test_nonblocking_direct_record_reconciles_a_crashed_child(tmp_path: Path) -> None:
    """A lost detached process cannot leave a durable record running forever."""
    store = ExecutionStore(tmp_path / "executions", "example")
    record = store.create(
        "orphaned",
        mode="direct",
        metadata={
            "direct_launch": {
                "schema_version": DIRECT_LAUNCH_SCHEMA,
                "phase": "spawned",
                "launcher_pid": os.getpid(),
                "child_pid": os.getpid(),
                "escape": "systemd_scope",
                "escape_detail": None,
            }
        },
    )
    assert record._record_path is not None
    prepare_direct_execution_lease(record._record_path.parent)
    store.update("orphaned", state="running")

    reconciled = store.get("orphaned")

    assert reconciled.state == "failed"
    assert reconciled.terminal is True
    assert reconciled.return_code == 1
    assert reconciled.metadata["failure_stage"] == "direct_orphan_reconciliation"


def test_nonblocking_direct_record_stays_live_while_child_holds_lease(
    tmp_path: Path,
) -> None:
    """A held process lease is authoritative even during concurrent queries."""
    store = ExecutionStore(tmp_path / "executions", "example")
    record = store.create(
        "active",
        mode="direct",
        metadata={
            "direct_launch": {
                "schema_version": DIRECT_LAUNCH_SCHEMA,
                "phase": "spawned",
                "launcher_pid": os.getpid(),
                "child_pid": os.getpid(),
                "escape": "systemd_scope",
                "escape_detail": None,
            }
        },
    )
    assert record._record_path is not None
    execution_root = record._record_path.parent
    prepare_direct_execution_lease(execution_root)
    with direct_execution_lease(execution_root):
        store.update("active", state="running")
        assert store.get("active").state == "running"

    assert store.get("active").state == "failed"


def test_reconcile_direct_execution_rejects_a_stale_v1_schema_record(
    tmp_path: Path,
) -> None:
    """The schema bump (R1) is enforced, not decorative.

    A record still tagged with the pre-fix ``jarvis.direct-launch.v1``
    schema (no ``escape``/``escape_detail`` fields) must be rejected as
    invalid rather than silently treated as a valid, merely reason-less,
    launch.
    """
    store = ExecutionStore(tmp_path / "executions", "example")
    record = store.create(
        "stale-schema",
        mode="direct",
        metadata={
            "direct_launch": {
                "schema_version": "jarvis.direct-launch.v1",
                "phase": "spawned",
                "launcher_pid": os.getpid(),
                "child_pid": os.getpid(),
            }
        },
    )
    assert record._record_path is not None
    prepare_direct_execution_lease(record._record_path.parent)
    with pytest.raises(RuntimeError, match="direct execution launch metadata"):
        store.get("stale-schema")


def test_reconcile_direct_execution_rejects_an_unknown_escape_reason(
    tmp_path: Path,
) -> None:
    """An escape value outside the typed vocabulary is rejected, not passed
    through — the same rigor already applied to ``phase``."""
    store = ExecutionStore(tmp_path / "executions", "example")
    record = store.create(
        "bad-escape",
        mode="direct",
        metadata={
            "direct_launch": {
                "schema_version": DIRECT_LAUNCH_SCHEMA,
                "phase": "spawned",
                "launcher_pid": os.getpid(),
                "child_pid": os.getpid(),
                "escape": "not_a_real_reason",
                "escape_detail": None,
            }
        },
    )
    assert record._record_path is not None
    prepare_direct_execution_lease(record._record_path.parent)
    with pytest.raises(RuntimeError, match="direct execution launch metadata"):
        store.get("bad-escape")


def test_package_progress_environment_is_execution_owned(tmp_path: Path) -> None:
    """Aliases receive distinct authoritative progress sidecars under the run root."""
    pipeline = _pipeline_double(tmp_path)
    store = pipeline._execution_store()
    store.create("run", mode="direct")
    store.update("run", state="running")
    pipeline._execution_root = store.executions_dir / "run"
    pipeline._execution_id = "run"

    pipeline._bind_package_execution_environment(
        {"pkg_id": "render-left", "pkg_type": "builtin.paraview"}
    )

    progress_path = Path(pipeline.env["JARVIS_PROGRESS_PATH"])
    assert pipeline.env["JARVIS_EXECUTION_ID"] == "run"
    assert pipeline.env["JARVIS_PACKAGE_ID"] == "render-left"
    assert pipeline.env["JARVIS_PACKAGE_NAME"] == "builtin.paraview"
    assert pipeline.env["JARVIS_PROGRESS_TRANSPORT"] == "sidecar"
    assert progress_path.parent == store.executions_dir / "run" / "progress"
    assert store.get("run").metadata["progress_files"] == {
        "render-left": {
            "filename": progress_path.name,
            "package_name": "builtin.paraview",
        }
    }


def test_handle_progress_returns_identity_checked_path_free_snapshot(
    tmp_path: Path,
) -> None:
    """A handle exposes current progress without leaking its sidecar path."""
    from jarvis_cd.progress import ProgressEvent, ProgressStore

    store = ExecutionStore(tmp_path / "executions", "example")
    record = store.create("progress-run", mode="direct")
    filename = "render-a.jsonl"
    store.update(
        "progress-run",
        state="running",
        metadata={
            "progress_files": {
                "render-a": {
                    "filename": filename,
                    "package_name": "builtin.paraview",
                }
            }
        },
    )
    ProgressStore(store.executions_dir / "progress-run" / "progress" / filename).append(
        ProgressEvent(
            package_name="builtin.paraview",
            package_id="render-a",
            execution_id="progress-run",
            label="frame",
            current=3,
            total=8,
            unit="frame",
            sequence=1,
        )
    )

    snapshot = record.handle.progress()
    document = snapshot.to_dict()

    assert document["schema_version"] == PROGRESS_SNAPSHOT_SCHEMA
    assert document["execution_id"] == "progress-run"
    assert document["packages"][0]["event_count"] == 1
    assert document["packages"][0]["latest"]["current"] == 3.0
    assert "path" not in json.dumps(document)


def test_execution_progress_rejects_index_escape_and_event_mismatch(
    tmp_path: Path,
) -> None:
    """Queries cannot follow metadata outside the exact owned execution."""
    from jarvis_cd.progress import ProgressEvent, ProgressStore

    store = ExecutionStore(tmp_path / "executions", "example")
    store.create(
        "escape",
        mode="direct",
        metadata={
            "progress_files": {
                "render": {
                    "filename": "../outside.jsonl",
                    "package_name": "builtin.paraview",
                }
            }
        },
    )
    with pytest.raises(RuntimeError, match="invalid path"):
        store.progress("escape")

    store.create(
        "mismatch",
        mode="direct",
        metadata={
            "progress_files": {
                "render": {
                    "filename": "render.jsonl",
                    "package_name": "builtin.paraview",
                }
            }
        },
    )
    sidecar = store.executions_dir / "mismatch" / "progress" / "render.jsonl"
    ProgressStore(sidecar).append(
        ProgressEvent(
            package_name="builtin.paraview",
            package_id="other",
            execution_id="mismatch",
            label="frame",
            current=1,
            total=2,
            unit="frame",
            sequence=1,
        )
    )
    with pytest.raises(RuntimeError, match="identity mismatch"):
        store.progress("mismatch")


def test_container_start_forwards_execution_owned_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Container package launch receives the same owned progress contract."""
    pipeline = _pipeline_double(tmp_path)
    store = pipeline._execution_store()
    store.create("container-run", mode="direct")
    store.update("container-run", state="running")
    pipeline._execution_root = store.executions_dir / "container-run"
    pipeline._execution_id = "container-run"
    pipeline.container_engine = "docker"
    pipeline.container_image = "example:latest"
    pipeline.packages = [
        {
            "pkg_id": "render-container",
            "pkg_type": "builtin.paraview",
        }
    ]
    pipeline._started_instances = []
    pipeline._hostfile_is_local_only = Mock(return_value=True)
    pipeline.get_hostfile = Mock(return_value=SimpleNamespace(hosts=["localhost"]))
    pipeline._apply_interceptors_to_package = Mock()
    shared_dir = pipeline.get_pipeline_shared_dir()
    shared_dir.mkdir(parents=True)
    (shared_dir / "docker-compose.yaml").write_text(
        "services: {}\n",
        encoding="utf-8",
    )

    observed: dict[str, str] = {}

    class Package:
        env: dict[str, str] = {}

        def start(self) -> None:
            observed.update(self.env)

    def load_package(
        _definition: dict[str, object],
        environment: dict[str, str],
    ) -> Package:
        package = Package()
        package.env = dict(environment)
        return package

    class SuccessfulExec:
        def __init__(self, _command: str, _exec_info: object) -> None:
            self.exit_code = {"localhost": 0}

        def run(self) -> "SuccessfulExec":
            return self

    pipeline._load_package_instance = Mock(side_effect=load_package)
    monkeypatch.setattr(jarvis_cd.shell, "Exec", SuccessfulExec)

    pipeline._start_containerized_pipeline()

    progress_path = Path(observed["JARVIS_PROGRESS_PATH"])
    assert observed["JARVIS_EXECUTION_ID"] == "container-run"
    assert observed["JARVIS_PACKAGE_NAME"] == "builtin.paraview"
    assert observed["JARVIS_PACKAGE_ID"] == "render-container"
    assert observed["JARVIS_PROGRESS_TRANSPORT"] == "stdout"
    assert progress_path.parent == store.executions_dir / "container-run" / "progress"
    assert pipeline._started_instances


def test_scheduler_finalizer_covers_failures_before_pipeline_run(
    tmp_path: Path,
) -> None:
    """The scheduler EXIT helper makes pre-run script failures terminal."""
    store = ExecutionStore(tmp_path / "executions", "example")
    store.create(
        "scheduled",
        mode="scheduler",
        scheduler_provider="slurm",
    )
    store.update("scheduled", state="submitting")
    store.update("scheduled", state="submitted", submitted=True, native_id="55")

    finalized = finalize_execution(store.executions_dir / "scheduled", "scheduled", 7)

    assert finalized.state == "failed"
    assert finalized.return_code == 7
    assert finalized.error == "scheduler script exited with status 7"


def test_scheduler_snapshot_run_and_script_finalize_the_same_record(
    tmp_path: Path,
) -> None:
    """The runtime snapshot advances its submit-created record without a new ID."""
    store = ExecutionStore(tmp_path / "executions", "example")
    store.create("scheduled", mode="scheduler", scheduler_provider="slurm")
    store.update("scheduled", state="submitting")
    store.update("scheduled", state="submitted", submitted=True, native_id="55")
    pipeline = _pipeline_double(tmp_path)
    pipeline._execution_root = store.executions_dir / "scheduled"
    pipeline._execution_id = "scheduled"

    handle = pipeline.run()

    assert handle.execution_id == "scheduled"
    assert store.get("scheduled").state == "running"
    assert finalize_execution(
        store.executions_dir / "scheduled", "scheduled", 0
    ).state == ("completed")


def test_submit_projection_preserves_runtime_terminal_outcome(tmp_path: Path) -> None:
    """A submit-process status cannot overwrite the workload finalizer."""
    pipeline = _pipeline_double(tmp_path)
    store = pipeline._execution_store()
    store.create("runtime-wins", mode="scheduler", scheduler_provider="slurm")
    store.update("runtime-wins", state="submitting")
    store.update(
        "runtime-wins",
        state="running",
        submitted=True,
        native_id="41",
    )
    store.update(
        "runtime-wins",
        state="failed",
        terminal=True,
        return_code=7,
        error="runtime failed",
    )
    pipeline.last_submission = {
        "execution_id": "runtime-wins",
        "provider": "slurm",
        "scheduler_job_id": "41",
        "scheduler_cluster": None,
        "state": "completed",
        "submitted": True,
        "terminal": True,
        "terminal_returncode": 0,
        "scheduler_stderr": "submitter disagreed",
        "script_path": str(store.executions_dir / "runtime-wins" / "submit.slurm"),
    }

    pipeline._update_execution_marker(store.executions_dir / "runtime-wins")

    record = store.get("runtime-wins")
    assert record.state == "failed"
    assert record.return_code == 7
    assert record.error == "runtime failed"


def test_slurm_script_installs_durable_exit_finalizer(tmp_path: Path) -> None:
    """Hostfile and hook failures are recorded even before JARVIS starts."""
    execution_root = tmp_path / "executions" / "scheduled"
    scheduler = SlurmScheduler(
        {"name": "slurm"},
        execution_root,
        pipeline_snapshot_dir=execution_root / "runtime",
    )

    rendered = scheduler.render()

    assert "trap jarvis_finalize_execution EXIT" in rendered
    assert "-m jarvis_cd.core.execution finalize" in rendered
    assert "--execution-id scheduled" in rendered


def test_execution_artifacts_aggregate_package_manifests(
    tmp_path: Path,
) -> None:
    """A handle returns current typed outputs without storage sidecar paths."""
    store = ExecutionStore(tmp_path / "executions", "example")
    record = store.create("artifacts", mode="direct")
    artifact_root = store.executions_dir / record.execution_id / "artifacts"
    render_path = artifact_root / "render.jsonl"
    simulation_path = artifact_root / "simulation.jsonl"
    ArtifactReporter(
        package_name="builtin.paraview",
        package_id="render",
        execution_id=record.execution_id,
        path=render_path,
    ).emit(
        logical_name="frame-1",
        kind="image",
        role=ArtifactRole.OUTPUT,
        structure=ArtifactStructure.FILE,
        ownership=ArtifactOwnership.EXECUTION,
        state=ArtifactState.FINALIZED,
        location=ArtifactLocation.execution_relative("shared/frame-1.png"),
        media_type="image/png",
        format="png",
    )
    ArtifactReporter(
        package_name="builtin.gray_scott",
        package_id="simulation",
        execution_id=record.execution_id,
        path=simulation_path,
    ).emit(
        logical_name="timesteps",
        kind="scientific_dataset",
        role=ArtifactRole.INTERMEDIATE,
        structure=ArtifactStructure.COLLECTION,
        ownership=ArtifactOwnership.SHARED,
        state=ArtifactState.PRODUCING,
        location=ArtifactLocation.cluster_path("/scratch/example/gs.bp"),
        media_type="application/x-adios2-bp",
        format="adios2-bp5",
    )
    store.update(
        record.execution_id,
        metadata={
            "artifact_files": {
                "render": {
                    "filename": render_path.name,
                    "package_id": "render",
                    "package_name": "builtin.paraview",
                },
                "simulation": {
                    "filename": simulation_path.name,
                    "package_id": "simulation",
                    "package_name": "builtin.gray_scott",
                },
            }
        },
    )

    snapshot = record.handle.artifacts()

    assert snapshot.to_dict()["schema_version"] == ARTIFACT_SNAPSHOT_SCHEMA
    assert snapshot.execution_id == record.execution_id
    assert [artifact.package_id for artifact in snapshot.artifacts] == [
        "render",
        "simulation",
    ]
    assert "filename" not in json.dumps(snapshot.to_dict())


@pytest.mark.parametrize(
    ("failed", "expected_state"),
    [
        (False, ArtifactState.INCOMPLETE),
        (True, ArtifactState.FAILED),
    ],
)
def test_execution_terminalization_seals_producing_artifacts(
    tmp_path: Path,
    failed: bool,
    expected_state: ArtifactState,
) -> None:
    """A terminal execution cannot leave a manifest claiming active output."""
    store = ExecutionStore(tmp_path / "executions", "example")
    record = store.create("seal-artifacts", mode="direct")
    artifact_path = store.executions_dir / record.execution_id / "artifacts/pkg.jsonl"
    ArtifactReporter(
        package_name="builtin.gray_scott",
        package_id="simulation",
        execution_id=record.execution_id,
        path=artifact_path,
    ).emit(
        logical_name="timesteps",
        kind="scientific_dataset",
        role=ArtifactRole.OUTPUT,
        structure=ArtifactStructure.COLLECTION,
        ownership=ArtifactOwnership.SHARED,
        state=ArtifactState.PRODUCING,
        location=ArtifactLocation.cluster_path("/scratch/example/gs.bp"),
    )
    store.update(
        record.execution_id,
        metadata={
            "artifact_files": {
                "simulation": {
                    "filename": artifact_path.name,
                    "package_id": "simulation",
                    "package_name": "builtin.gray_scott",
                }
            }
        },
    )

    sealed = store.finalize_artifacts(record.execution_id, failed=failed)

    assert len(sealed) == 1
    assert sealed[0].state is expected_state
    assert store.artifacts(record.execution_id).artifacts[0].state is expected_state


def test_pipeline_stop_reuses_the_started_async_package_instance() -> None:
    """Stopping waits on the process-bearing instance rather than a reload."""
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.name = "example"
    pipeline.packages = [
        {
            "pkg_id": "simulation",
            "pkg_type": "builtin.adios2_gray_scott",
            "config": {},
        }
    ]
    pipeline.env = {}
    pipeline._execution_root = None
    pipeline._execution_id = None
    pipeline.is_containerized = Mock(return_value=False)
    started = SimpleNamespace(pkg_id="simulation", stop=Mock())
    pipeline._started_instances = [started]
    pipeline._load_package_instance = Mock()

    pipeline.stop()

    started.stop.assert_called_once_with()
    pipeline._load_package_instance.assert_not_called()


def test_pipeline_stop_attempts_every_package_before_reraising() -> None:
    """One failed async wait cannot prevent cleanup of remaining packages."""
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.name = "example"
    pipeline.packages = [
        {"pkg_id": package_id, "pkg_type": f"builtin.{package_id}", "config": {}}
        for package_id in ("first", "failed", "last")
    ]
    pipeline.env = {}
    pipeline._execution_root = None
    pipeline._execution_id = None
    pipeline.is_containerized = Mock(return_value=False)
    attempts: list[str] = []

    class StartedPackage:
        def __init__(self, package_id: str, *, fail: bool = False) -> None:
            self.pkg_id = package_id
            self._fail = fail

        def stop(self) -> None:
            attempts.append(self.pkg_id)
            if self._fail:
                raise RuntimeError(f"{self.pkg_id} wait failed")

    pipeline._started_instances = [
        StartedPackage("first"),
        StartedPackage("failed", fail=True),
        StartedPackage("last"),
    ]
    pipeline._load_package_instance = Mock()

    with pytest.raises(ExceptionGroup, match="pipeline package stop failed") as error:
        pipeline.stop()

    assert attempts == ["last", "failed", "first"]
    assert len(error.value.exceptions) == 1
    assert str(error.value.exceptions[0]) == "failed wait failed"
    pipeline._load_package_instance.assert_not_called()


def test_terminal_record_update_seals_and_finalizes_core_logs(tmp_path: Path) -> None:
    """Every terminal transition seals manifests while closing owned logs."""
    store = ExecutionStore(tmp_path / "executions", "example")
    record = store.create("core-log", mode="direct")
    artifact_path = store.executions_dir / record.execution_id / "artifacts/core.jsonl"
    ArtifactReporter(
        package_name="jarvis.core",
        package_id="jarvis-core",
        execution_id=record.execution_id,
        path=artifact_path,
    ).emit(
        logical_name="stdout",
        kind="log",
        role=ArtifactRole.LOG,
        structure=ArtifactStructure.STREAM,
        ownership=ArtifactOwnership.EXECUTION,
        state=ArtifactState.PRODUCING,
        location=ArtifactLocation.execution_relative("stdout.log"),
    )
    store.update(
        record.execution_id,
        state="running",
        metadata={
            "artifact_files": {
                "jarvis-core": {
                    "filename": artifact_path.name,
                    "package_id": "jarvis-core",
                    "package_name": "jarvis.core",
                }
            }
        },
    )

    store.update(
        record.execution_id,
        state="completed",
        terminal=True,
        return_code=0,
    )

    artifact = store.artifacts(record.execution_id).artifacts[0]
    assert artifact.state is ArtifactState.FINALIZED
    assert artifact.terminal is True
    assert ArtifactStore(artifact_path).is_sealed() is True


def test_scheduler_preparation_failure_seals_package_manifest(tmp_path: Path) -> None:
    """Pre-submit terminal failures cannot leave application output writable."""
    store = ExecutionStore(tmp_path / "executions", "example")
    record = store.create(
        "scheduler-failure",
        mode="scheduler",
        scheduler_provider="slurm",
    )
    artifact_path = store.executions_dir / record.execution_id / "artifacts/app.jsonl"
    ArtifactReporter(
        package_name="site.application",
        package_id="app",
        execution_id=record.execution_id,
        path=artifact_path,
    ).emit(
        logical_name="partial-output",
        kind="scientific_dataset",
        role=ArtifactRole.OUTPUT,
        structure=ArtifactStructure.COLLECTION,
        ownership=ArtifactOwnership.SHARED,
        state=ArtifactState.PRODUCING,
        location=ArtifactLocation.cluster_path("/scratch/partial.bp"),
    )
    store.update(
        record.execution_id,
        metadata={
            "artifact_files": {
                "app": {
                    "filename": artifact_path.name,
                    "package_id": "app",
                    "package_name": "site.application",
                }
            }
        },
    )

    store.update(
        record.execution_id,
        state="failed",
        terminal=True,
        return_code=1,
        error="scheduler preparation failed",
    )

    artifact = store.artifacts(record.execution_id).artifacts[0]
    assert artifact.state is ArtifactState.FAILED
    assert ArtifactStore(artifact_path).is_sealed() is True


def test_package_alias_cannot_overwrite_core_artifact_index(tmp_path: Path) -> None:
    """Artifact index keys remain separate from operator-selected aliases."""
    store = ExecutionStore(tmp_path / "executions", "example")
    record = store.create("alias-collision", mode="direct")
    artifact_root = store.executions_dir / record.execution_id / "artifacts"
    core_path = artifact_root / "core.jsonl"
    package_path = artifact_root / "package.jsonl"
    for package_name, path, logical_name in (
        ("jarvis.core", core_path, "stdout"),
        ("site.application", package_path, "result"),
    ):
        ArtifactReporter(
            package_name=package_name,
            package_id="jarvis-core",
            execution_id=record.execution_id,
            path=path,
        ).emit(
            logical_name=logical_name,
            kind="log" if logical_name == "stdout" else "result",
            role=(
                ArtifactRole.LOG if logical_name == "stdout" else ArtifactRole.OUTPUT
            ),
            structure=ArtifactStructure.FILE,
            ownership=ArtifactOwnership.EXECUTION,
            state=ArtifactState.FINALIZED,
            location=ArtifactLocation.execution_relative(f"{logical_name}.dat"),
        )
    store.update(
        record.execution_id,
        metadata={
            "artifact_files": {
                "jarvis-core": {
                    "filename": core_path.name,
                    "package_id": "jarvis-core",
                    "package_name": "jarvis.core",
                },
                "package-operator-alias": {
                    "filename": package_path.name,
                    "package_id": "jarvis-core",
                    "package_name": "site.application",
                },
            }
        },
    )

    artifacts = store.artifacts(record.execution_id).artifacts

    assert {artifact.package_name for artifact in artifacts} == {
        "jarvis.core",
        "site.application",
    }
