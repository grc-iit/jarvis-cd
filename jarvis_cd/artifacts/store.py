"""Durable append-only storage for structured JARVIS artifact manifests."""

from __future__ import annotations

import os
import stat
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Callable, Final, Iterator

from jarvis_cd.util.private_path import (
    ensure_private_descriptor,
    ensure_private_path,
    reject_private_path_redirection,
)

from .schema import (
    MAX_ARTIFACT_EVENT_BYTES,
    PROCESS_EXIT_RECONCILIATION_KEY,
    ArtifactEvent,
    ArtifactState,
    validate_artifact_history,
)

MAX_ARTIFACT_STORE_BYTES: Final = 128 * 1024 * 1024


class ArtifactStore:
    """Append and query a bounded, owner-private JSONL artifact manifest."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def append(self, event: ArtifactEvent) -> None:
        """Durably append one lifecycle event under an exclusive store lock."""
        self._prepare_parent()
        with _exclusive_store_lock(self.path):
            if _store_is_sealed(self.path):
                raise RuntimeError("artifact store is sealed")
            descriptor = _open_store(self.path, write=True)
            try:
                info = _validate_descriptor(self.path, descriptor)
                existing = _read_events_from_descriptor(descriptor, info.st_size)
                _commit_events(descriptor, info.st_size, existing, [event])
            finally:
                os.close(descriptor)

    def append_next(
        self,
        builder: Callable[[tuple[ArtifactEvent, ...]], ArtifactEvent],
    ) -> ArtifactEvent:
        """Build and append one event from current history under one lock."""
        self._prepare_parent()
        with _exclusive_store_lock(self.path):
            if _store_is_sealed(self.path):
                raise RuntimeError("artifact store is sealed")
            descriptor = _open_store(self.path, write=True)
            try:
                info = _validate_descriptor(self.path, descriptor)
                existing = _read_events_from_descriptor(descriptor, info.st_size)
                event = builder(tuple(existing))
                if not isinstance(event, ArtifactEvent):
                    raise TypeError("artifact event builder returned an invalid value")
                _commit_events(descriptor, info.st_size, existing, [event])
                return event
            finally:
                os.close(descriptor)

    def read_all(self) -> list[ArtifactEvent]:
        """Read and validate every complete manifest event in order."""
        _reject_symlink_tree(self.path)
        if not self.path.exists():
            return []
        _reject_symlink_tree(self.path.parent)
        with _exclusive_store_lock(self.path):
            descriptor = _open_store(self.path, write=False)
            try:
                info = _validate_descriptor(self.path, descriptor)
                return _read_events_from_descriptor(descriptor, info.st_size)
            finally:
                os.close(descriptor)

    def latest(self, artifact_id: str | None = None) -> ArtifactEvent | None:
        """Return the latest event globally or for one opaque artifact ID."""
        events = self.read_all()
        if artifact_id is None:
            return events[-1] if events else None
        return next(
            (event for event in reversed(events) if event.artifact_id == artifact_id),
            None,
        )

    def current(self) -> dict[str, ArtifactEvent]:
        """Return the current validated event for every registered artifact."""
        current: dict[str, ArtifactEvent] = {}
        for event in self.read_all():
            current[event.artifact_id] = event
        return current

    def reconcile_process_exit(self, return_code: int) -> list[ArtifactEvent]:
        """Correct false finalized claims after a nonzero package process exit.

        The correction is committed as one locked batch, retaining each opaque
        artifact ID and advancing both its revision and the manifest sequence.
        Available, producing, and already-incomplete/failed artifacts are left
        untouched because their lifecycle remains meaningful after failure.
        """
        if isinstance(return_code, bool) or not isinstance(return_code, int):
            raise TypeError("process return code must be an integer")
        if return_code == 0:
            return []
        self._prepare_parent()
        with _exclusive_store_lock(self.path):
            if _store_is_sealed(self.path):
                raise RuntimeError("artifact store is sealed")
            descriptor = _open_store(self.path, write=True)
            try:
                info = _validate_descriptor(self.path, descriptor)
                existing = _read_events_from_descriptor(descriptor, info.st_size)
                current: dict[str, ArtifactEvent] = {}
                for event in existing:
                    current[event.artifact_id] = event
                sequence = existing[-1].sequence if existing else 0
                observed_at = time.time()
                reconciled: list[ArtifactEvent] = []
                for event in current.values():
                    if event.state is not ArtifactState.FINALIZED:
                        continue
                    sequence += 1
                    metadata = dict(event.metadata)
                    metadata[PROCESS_EXIT_RECONCILIATION_KEY] = {
                        "reported_state": ArtifactState.FINALIZED.value,
                        "return_code": return_code,
                        "source": "jarvis_process_owner",
                    }
                    reconciled.append(
                        replace(
                            event,
                            state=ArtifactState.INCOMPLETE,
                            message=(
                                "JARVIS corrected a finalized artifact after the "
                                f"package process exited with code {return_code}"
                            ),
                            revision=event.revision + 1,
                            sequence=sequence,
                            observed_at_epoch=observed_at,
                            metadata=metadata,
                        )
                    )
                if reconciled:
                    _commit_events(descriptor, info.st_size, existing, reconciled)
                return reconciled
            finally:
                os.close(descriptor)

    def is_sealed(self) -> bool:
        """Return whether execution terminalization sealed this manifest."""
        _reject_symlink_tree(self.path.parent)
        if not self.path.parent.exists():
            return False
        with _exclusive_store_lock(self.path):
            return _store_is_sealed(self.path)

    def finalize_open(
        self,
        state_for_open: ArtifactState = ArtifactState.INCOMPLETE,
    ) -> list[ArtifactEvent]:
        """Seal every still-producing artifact during execution terminalization.

        ``AVAILABLE`` artifacts remain available. The execution layer seals
        application output as ``INCOMPLETE``/``FAILED`` unless its authoritative
        producer explicitly owns completion, such as JARVIS core log streams.
        """
        if state_for_open not in {
            ArtifactState.FINALIZED,
            ArtifactState.INCOMPLETE,
            ArtifactState.FAILED,
        }:
            raise ValueError("open artifacts require a terminal sealing state")
        self._prepare_parent()
        with _exclusive_store_lock(self.path):
            if _store_is_sealed(self.path):
                return []
            descriptor = _open_store(self.path, write=True)
            try:
                info = _validate_descriptor(self.path, descriptor)
                existing = _read_events_from_descriptor(descriptor, info.st_size)
                current: dict[str, ArtifactEvent] = {}
                for event in existing:
                    current[event.artifact_id] = event
                sequence = existing[-1].sequence if existing else 0
                sealed: list[ArtifactEvent] = []
                observed_at = time.time()
                for event in current.values():
                    if event.state is not ArtifactState.PRODUCING:
                        continue
                    sequence += 1
                    sealed.append(
                        replace(
                            event,
                            state=state_for_open,
                            revision=event.revision + 1,
                            sequence=sequence,
                            observed_at_epoch=observed_at,
                        )
                    )
                if sealed:
                    _commit_events(descriptor, info.st_size, existing, sealed)
                _seal_store(self.path)
                return sealed
            finally:
                os.close(descriptor)

    def _prepare_parent(self) -> None:
        """Create and protect the manifest parent without following redirects."""
        _reject_symlink_tree(self.path.parent)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ensure_private_path(self.path.parent, directory=True)
        _reject_symlink_tree(self.path.parent)


def _commit_events(
    descriptor: int,
    original_size: int,
    existing: list[ArtifactEvent],
    additions: list[ArtifactEvent],
) -> None:
    """Validate and commit a locked event batch, rolling back short writes."""
    if not additions:
        return
    combined = [*existing, *additions]
    validate_artifact_history(combined)
    payload = b"".join((event.to_json() + "\n").encode("utf-8") for event in additions)
    if original_size + len(payload) > MAX_ARTIFACT_STORE_BYTES:
        raise ValueError("artifact store would exceed maximum size")
    os.lseek(descriptor, 0, os.SEEK_END)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short write while appending artifact events")
            offset += written
        os.fsync(descriptor)
    except BaseException:
        os.ftruncate(descriptor, original_size)
        os.fsync(descriptor)
        raise


def _seal_store(path: Path) -> None:
    """Create and durably validate an owner-private terminal marker."""
    marker = _sealed_path(path)
    _reject_symlink(marker, label="artifact seal")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(
            marker,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | nofollow,
            0o600,
        )
    except FileExistsError:
        if not _store_is_sealed(path):
            raise RuntimeError("artifact seal could not be validated")
        return
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)  # type: ignore[attr-defined]
        ensure_private_descriptor(marker, descriptor, directory=False)
        payload = b"jarvis.artifact.sealed.v1"
        if os.write(descriptor, payload) != len(payload):
            raise OSError("short write while sealing artifact store")
        os.fsync(descriptor)
        _validate_descriptor(marker, descriptor)
    except BaseException:
        os.close(descriptor)
        try:
            marker.unlink()
        except OSError:
            pass
        raise
    else:
        os.close(descriptor)


def _store_is_sealed(path: Path) -> bool:
    marker = _sealed_path(path)
    _reject_symlink(marker, label="artifact seal")
    try:
        descriptor = _open_store(marker, write=False)
    except FileNotFoundError:
        return False
    try:
        information = _validate_descriptor(marker, descriptor)
        if information.st_size != len(b"jarvis.artifact.sealed.v1"):
            raise ValueError("artifact seal has invalid content")
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.read(descriptor, information.st_size) != b"jarvis.artifact.sealed.v1":
            raise ValueError("artifact seal has invalid content")
        return True
    finally:
        os.close(descriptor)


def _sealed_path(path: Path) -> Path:
    return path.with_name(path.name + ".sealed")


def _read_events_from_descriptor(descriptor: int, size: int) -> list[ArtifactEvent]:
    if size > MAX_ARTIFACT_STORE_BYTES:
        raise ValueError("artifact store exceeds maximum size")
    _require_complete_framing(descriptor, size)
    os.lseek(descriptor, 0, os.SEEK_SET)
    events: list[ArtifactEvent] = []
    with os.fdopen(os.dup(descriptor), "r", encoding="utf-8", newline="") as stream:
        for line_number, line in enumerate(stream, start=1):
            if len(line.encode("utf-8")) > MAX_ARTIFACT_EVENT_BYTES + 1:
                raise ValueError(f"artifact line {line_number} exceeds maximum size")
            payload = line.rstrip("\r\n")
            if not payload:
                raise ValueError(f"artifact line {line_number} cannot be empty")
            events.append(ArtifactEvent.from_json(payload))
    validate_artifact_history(events)
    return events


def _reject_symlink(path: Path, *, label: str) -> None:
    try:
        information = path.lstat()
    except FileNotFoundError:
        return
    attributes = getattr(information, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(information.st_mode) or attributes & reparse_flag:
        raise ValueError(f"{label} cannot be a symlink or reparse point: {path}")


def _reject_symlink_tree(path: Path) -> None:
    try:
        reject_private_path_redirection(path)
    except RuntimeError as exc:
        raise ValueError(
            f"artifact path cannot be redirected by a symlink or reparse point: {path}"
        ) from exc


def _open_store(path: Path, *, write: bool) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not write:
        descriptor = os.open(path, os.O_RDONLY | nofollow)
    else:
        try:
            descriptor = os.open(
                path,
                os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_RDWR | nofollow,
                0o600,
            )
        except FileExistsError:
            descriptor = os.open(path, os.O_APPEND | os.O_RDWR | nofollow)
        else:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)  # type: ignore[attr-defined]
    try:
        ensure_private_descriptor(path, descriptor, directory=False)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _require_complete_framing(descriptor: int, size: int) -> None:
    if size == 0:
        return
    os.lseek(descriptor, size - 1, os.SEEK_SET)
    if os.read(descriptor, 1) != b"\n":
        raise ValueError("artifact store has an incomplete final JSONL record")


def _validate_descriptor(path: Path, descriptor: int) -> os.stat_result:
    information = os.fstat(descriptor)
    if not stat.S_ISREG(information.st_mode):
        raise ValueError(f"artifact store must be a regular file: {path}")
    path_information = path.lstat()
    if stat.S_ISLNK(path_information.st_mode) or (
        path_information.st_dev,
        path_information.st_ino,
    ) != (information.st_dev, information.st_ino):
        raise ValueError(f"artifact store changed during secure open: {path}")
    if os.name != "nt":
        getuid = getattr(os, "getuid", None)
        if getuid is not None and information.st_uid != getuid():
            raise PermissionError(
                f"artifact store is not owned by current user: {path}"
            )
        if stat.S_IMODE(information.st_mode) & 0o077:
            raise PermissionError(f"artifact store permissions are not private: {path}")
    return information


@contextmanager
def _exclusive_store_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".lock")
    _reject_symlink(lock_path, label="artifact lock")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_EXCL | os.O_RDWR | nofollow,
            0o600,
        )
    except FileExistsError:
        descriptor = os.open(lock_path, os.O_RDWR | nofollow)
    try:
        ensure_private_descriptor(lock_path, descriptor, directory=False)
        _validate_descriptor(lock_path, descriptor)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
        _lock_descriptor(descriptor)
        try:
            yield
        finally:
            _unlock_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _lock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(  # type: ignore[attr-defined]
            descriptor,
            fcntl.LOCK_EX,  # type: ignore[attr-defined]
        )


def _unlock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(  # type: ignore[attr-defined]
            descriptor,
            fcntl.LOCK_UN,  # type: ignore[attr-defined]
        )
