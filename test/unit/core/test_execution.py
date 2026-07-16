"""Durable JARVIS execution handle and record tests."""

from __future__ import annotations

import json
import os
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


def test_nonblocking_direct_record_reconciles_a_crashed_child(tmp_path: Path) -> None:
    """A lost detached process cannot leave a durable record running forever."""
    store = ExecutionStore(tmp_path / "executions", "example")
    record = store.create(
        "orphaned",
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
                "schema_version": "jarvis.direct-launch.v1",
                "phase": "spawned",
                "launcher_pid": os.getpid(),
                "child_pid": os.getpid(),
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
