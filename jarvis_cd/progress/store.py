"""Durable append-only storage for structured JARVIS progress events."""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Final, Iterator

from .schema import MAX_PROGRESS_EVENT_BYTES, ProgressEvent
from jarvis_cd.util.private_path import (
    ensure_private_descriptor,
    ensure_private_path,
    reject_private_path_redirection,
)

MAX_PROGRESS_STORE_BYTES: Final = 64 * 1024 * 1024


class ProgressStore:
    """Append and query a bounded JSONL progress sidecar."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def append(self, event: ProgressEvent) -> None:
        """Durably append one event without following a sidecar symlink."""
        payload = (event.to_json() + "\n").encode("utf-8")
        _reject_symlink_tree(self.path.parent)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ensure_private_path(self.path.parent, directory=True)
        _reject_symlink_tree(self.path.parent)
        with _exclusive_store_lock(self.path):
            descriptor = _open_store(self.path, write=True)
            try:
                info = _validate_descriptor(self.path, descriptor)
                projected_size = info.st_size + len(payload)
                if projected_size > MAX_PROGRESS_STORE_BYTES:
                    raise ValueError("progress store would exceed maximum size")
                previous_event = _latest_event(descriptor, info.st_size)
                if previous_event is not None and (
                    _stream_identity(event) != _stream_identity(previous_event)
                ):
                    raise ValueError(
                        "progress event identity must remain stable within a store"
                    )
                previous_sequence = (
                    previous_event.sequence if previous_event is not None else -1
                )
                if event.sequence <= previous_sequence:
                    raise ValueError("progress event sequences must increase strictly")
                offset = 0
                while offset < len(payload):
                    written = os.write(descriptor, payload[offset:])
                    if written <= 0:
                        raise OSError("short write while appending progress event")
                    offset += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def read_all(self) -> list[ProgressEvent]:
        """Read and validate every complete event in sequence order."""
        _reject_symlink_tree(self.path)
        if not self.path.exists():
            return []
        _reject_symlink_tree(self.path.parent)
        with _exclusive_store_lock(self.path):
            descriptor = _open_store(self.path, write=False)
            try:
                info = _validate_descriptor(self.path, descriptor)
                if info.st_size > MAX_PROGRESS_STORE_BYTES:
                    raise ValueError("progress store exceeds maximum size")
                _require_complete_framing(descriptor, info.st_size)
                os.lseek(descriptor, 0, os.SEEK_SET)
            except Exception:
                os.close(descriptor)
                raise
            events: list[ProgressEvent] = []
            previous_sequence = -1
            stream_identity: tuple[str, str, str] | None = None
            with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as stream:
                for line_number, line in enumerate(stream, start=1):
                    if len(line.encode("utf-8")) > MAX_PROGRESS_EVENT_BYTES + 1:
                        raise ValueError(
                            f"progress line {line_number} exceeds maximum size"
                        )
                    payload = line.rstrip("\r\n")
                    if not payload:
                        continue
                    event = ProgressEvent.from_json(payload)
                    event_identity = _stream_identity(event)
                    if stream_identity is None:
                        stream_identity = event_identity
                    elif event_identity != stream_identity:
                        raise ValueError(
                            "progress event identity must remain stable within a store"
                        )
                    if event.sequence <= previous_sequence:
                        raise ValueError(
                            "progress event sequences must increase strictly"
                        )
                    previous_sequence = event.sequence
                    events.append(event)
            return events

    def latest(self) -> ProgressEvent | None:
        """Return the most recent valid event, if one exists."""
        events = self.read_all()
        return events[-1] if events else None


def _reject_symlink(path: Path, *, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(mode) or attributes & reparse_flag:
        raise ValueError(f"{label} cannot be a symlink or reparse point: {path}")


def _reject_symlink_tree(path: Path) -> None:
    try:
        reject_private_path_redirection(path)
    except RuntimeError as exc:
        raise ValueError(
            f"progress path cannot be redirected by a symlink or reparse point: {path}"
        ) from exc
    absolute = Path(os.path.abspath(path))
    existing: list[Path] = []
    current = absolute
    while True:
        if current.exists() or current.is_symlink():
            existing.append(current)
        if current == current.parent:
            break
        current = current.parent
    for component in reversed(existing):
        _reject_symlink(component, label="progress path component")


def _open_store(path: Path, *, write: bool) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not write:
        descriptor = os.open(path, os.O_RDONLY | nofollow)
        try:
            ensure_private_descriptor(path, descriptor, directory=False)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor
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


def _latest_event(descriptor: int, size: int) -> ProgressEvent | None:
    if size == 0:
        return None
    _require_complete_framing(descriptor, size)
    tail_size = min(size, (MAX_PROGRESS_EVENT_BYTES + 1) * 2)
    os.lseek(descriptor, size - tail_size, os.SEEK_SET)
    remaining = tail_size
    chunks: list[bytes] = []
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    lines = b"".join(chunks).splitlines()
    if size > tail_size and lines:
        lines.pop(0)
    for line in reversed(lines):
        if line.strip():
            try:
                payload = line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("progress store contains invalid UTF-8") from exc
            return ProgressEvent.from_json(payload)
    return None


def _require_complete_framing(descriptor: int, size: int) -> None:
    """Reject a non-empty JSONL store whose final record is unterminated."""
    if size == 0:
        return
    os.lseek(descriptor, size - 1, os.SEEK_SET)
    if os.read(descriptor, 1) != b"\n":
        raise ValueError("progress store has an incomplete final JSONL record")


def _stream_identity(event: ProgressEvent) -> tuple[str, str, str]:
    """Return the immutable identity shared by every event in one sidecar."""
    return (event.execution_id, event.package_name, event.package_id)


def _validate_descriptor(path: Path, descriptor: int) -> os.stat_result:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"progress store must be a regular file: {path}")
    path_info = path.lstat()
    if stat.S_ISLNK(path_info.st_mode) or (
        path_info.st_dev,
        path_info.st_ino,
    ) != (info.st_dev, info.st_ino):
        raise ValueError(f"progress store changed during secure open: {path}")
    if os.name != "nt":
        getuid = getattr(os, "getuid", None)
        if getuid is not None and info.st_uid != getuid():
            raise PermissionError(
                f"progress store is not owned by current user: {path}"
            )
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError(f"progress store permissions are not private: {path}")
    return info


@contextmanager
def _exclusive_store_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".lock")
    _reject_symlink(lock_path, label="progress lock")
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
