"""Focused execution-record replacement coherence tests."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import jarvis_cd.core.execution as execution_module
import jarvis_cd.util.private_path as private_path_module
import pytest

from jarvis_cd.core.execution import MAX_RECORD_BYTES, RECORD_NAME, ExecutionStore


def _record_status(*, links: int) -> os.stat_result:
    """Return a regular-file status for one stable synthetic inode."""
    return cast(
        os.stat_result,
        SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_ino=101,
            st_dev=7,
            st_nlink=links,
            st_uid=1,
            st_gid=1,
            st_size=3,
            st_file_attributes=0,
        ),
    )


def test_record_validator_retries_zero_link_with_stale_same_inode_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared-filesystem stale inode view remains replacement churn."""
    record_path = tmp_path / RECORD_NAME
    record_path.write_text("{}\n", encoding="utf-8")
    unlinked_descriptor = _record_status(links=0)
    stale_path_view = _record_status(links=1)

    def descriptor_already_validated(
        _path: Path,
        _descriptor: int,
        *,
        directory: bool,
    ) -> None:
        assert directory is False

    monkeypatch.setattr(
        execution_module,
        "ensure_private_descriptor",
        descriptor_already_validated,
    )
    monkeypatch.setattr(
        execution_module.os,
        "fstat",
        lambda _descriptor: unlinked_descriptor,
    )
    monkeypatch.setattr(Path, "lstat", lambda _path: stale_path_view)

    with pytest.raises(
        execution_module.PrivatePathIdentityChangedError,
        match="changed during secure open",
    ):
        execution_module._validate_private_regular_file(
            19,
            record_path,
            maximum_size=MAX_RECORD_BYTES,
        )


def test_private_descriptor_classifies_zero_link_as_replacement_churn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The descriptor guard exposes the same typed retry signal."""
    record_path = tmp_path / RECORD_NAME
    record_path.write_text("{}\n", encoding="utf-8")
    unlinked_descriptor = _record_status(links=0)
    stale_path_view = _record_status(links=1)

    monkeypatch.setattr(
        private_path_module,
        "reject_private_path_redirection",
        lambda _path: None,
    )
    monkeypatch.setattr(
        private_path_module.os,
        "fstat",
        lambda _descriptor: unlinked_descriptor,
    )
    monkeypatch.setattr(Path, "lstat", lambda _path: stale_path_view)

    with pytest.raises(
        private_path_module.PrivatePathIdentityChangedError,
        match="changed during secure open",
    ):
        private_path_module.ensure_private_descriptor(
            record_path,
            19,
            directory=False,
        )


def test_post_submit_record_read_retries_after_metadata_settles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An accepted scheduler identity survives transient replacement churn."""
    store = ExecutionStore(tmp_path / "executions", "example")
    store.create("accepted", mode="scheduler", scheduler_provider="slurm")
    store.update("accepted", state="submitting")
    store.update(
        "accepted",
        state="submitted",
        submitted=True,
        native_id="21948",
    )
    real_validate = execution_module._validate_private_regular_file
    validation_attempts = 0
    retry_delays: list[float] = []

    def transient_replacement(
        descriptor: int,
        path: Path,
        *,
        maximum_size: int,
    ) -> os.stat_result:
        nonlocal validation_attempts
        validation_attempts += 1
        if validation_attempts == 1:
            raise execution_module.PrivatePathIdentityChangedError(
                f"private path changed during secure open: {path}"
            )
        return real_validate(descriptor, path, maximum_size=maximum_size)

    monkeypatch.setattr(
        execution_module,
        "_validate_private_regular_file",
        transient_replacement,
    )
    monkeypatch.setattr(execution_module, "sleep", retry_delays.append)

    record = store.get("accepted")

    assert record.state == "submitted"
    assert record.submitted is True
    assert record.scheduler_native_id == "21948"
    assert validation_attempts == 2
    assert retry_delays == [execution_module._SECURE_RECORD_READ_RETRY_SECONDS]
