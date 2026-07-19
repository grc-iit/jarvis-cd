"""Durable append-only storage for execution-owned service runtimes."""

from __future__ import annotations

import json
import os
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Final, Iterator, cast

from jarvis_cd.util.private_path import (
    ensure_private_descriptor,
    ensure_private_path,
    reject_private_path_redirection,
)

from .schema import (
    MAX_SERVICE_RUNTIME_BYTES,
    SERVICE_RUNTIME_SCHEMA_VERSION,
    SERVICE_RUNTIME_SCHEMA_VERSION_V1,
    ServiceLifecycle,
    ServiceRuntimeAuthority,
    ServiceRuntimeReport,
    validate_service_runtime_history,
)

MAX_SERVICE_RUNTIME_STORE_BYTES: Final = 64 * 1024 * 1024
PRIVATE_SERVICE_RUNTIME_SCHEMA_VERSION: Final = "jarvis.service-runtime.private.v1"


@dataclass(frozen=True, slots=True)
class _PrivateServiceRuntimeRecord:
    """One public runtime plus its optional owner-private authority."""

    runtime: ServiceRuntimeReport
    authority: ServiceRuntimeAuthority | None

    def __post_init__(self) -> None:
        """Bind the raw capability to the public digest and schema exactly."""
        if self.runtime.schema_version == SERVICE_RUNTIME_SCHEMA_VERSION_V1:
            if self.authority is not None or self.runtime.authorization is not None:
                raise ValueError("service runtime v1 cannot contain private authority")
            return
        if self.runtime.schema_version != SERVICE_RUNTIME_SCHEMA_VERSION:
            raise ValueError("unsupported private service runtime schema")
        if self.authority is None:
            raise ValueError("service runtime v2 requires owner-private authority")
        if self.runtime.authorization != self.authority.authorization:
            raise ValueError(
                "service runtime authority does not match its public token digest"
            )
        if self.authority.token in self.runtime.to_json():
            raise ValueError(
                "owner-private service runtime authority appeared in the public report"
            )

    def to_private_json(self) -> str:
        """Serialize one bounded private envelope without changing public v2."""
        if self.runtime.schema_version == SERVICE_RUNTIME_SCHEMA_VERSION_V1:
            return self.runtime.to_json()
        assert self.authority is not None
        payload = json.dumps(
            {
                "schema_version": PRIVATE_SERVICE_RUNTIME_SCHEMA_VERSION,
                "runtime": self.runtime.to_dict(),
                "authority": self.authority.to_private_dict(),
            },
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        if len(payload.encode("utf-8")) > MAX_SERVICE_RUNTIME_BYTES:
            raise ValueError("private service runtime record exceeds maximum size")
        return payload

    @classmethod
    def from_private_json(cls, payload: str) -> "_PrivateServiceRuntimeRecord":
        """Parse either a legacy public v1 line or an exact private envelope."""
        try:
            value = json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(
                "private service runtime record is not valid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise ValueError("private service runtime record must be an object")
        document = cast(dict[str, Any], value)
        if document.get("schema_version") == SERVICE_RUNTIME_SCHEMA_VERSION_V1:
            return cls(
                runtime=ServiceRuntimeReport.from_dict(document),
                authority=None,
            )
        if set(document) != {"schema_version", "runtime", "authority"}:
            raise ValueError("private service runtime envelope fields are invalid")
        if document.get("schema_version") != PRIVATE_SERVICE_RUNTIME_SCHEMA_VERSION:
            raise ValueError("unsupported private service runtime envelope schema")
        runtime = document.get("runtime")
        authority = document.get("authority")
        if not isinstance(runtime, dict) or not isinstance(authority, dict):
            raise ValueError("private service runtime envelope values are invalid")
        return cls(
            runtime=ServiceRuntimeReport.from_dict(cast(dict[str, Any], runtime)),
            authority=ServiceRuntimeAuthority.from_private_dict(
                cast(dict[str, Any], authority)
            ),
        )


class ServiceRuntimeStore:
    """Append and query a bounded, owner-private JSONL runtime store."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        """Bind the store to an exact JARVIS-owned sidecar path."""
        self.path = Path(path)

    def append(
        self,
        report: ServiceRuntimeReport,
        *,
        authority: ServiceRuntimeAuthority | None = None,
    ) -> None:
        """Append one validated public report and its private authority."""
        self._prepare_parent()
        with _exclusive_store_lock(self.path):
            descriptor = _open_store(self.path, write=True)
            try:
                information = _validate_descriptor(self.path, descriptor)
                existing = _read_records(descriptor, information.st_size)
                _commit_records(
                    descriptor,
                    information.st_size,
                    existing,
                    [_PrivateServiceRuntimeRecord(report, authority)],
                )
            finally:
                os.close(descriptor)

    def append_next(
        self,
        builder: Callable[
            [tuple[ServiceRuntimeReport, ...]],
            ServiceRuntimeReport,
        ],
        *,
        authority: ServiceRuntimeAuthority | None = None,
    ) -> ServiceRuntimeReport:
        """Build and append a monotonic report with private authority."""
        self._prepare_parent()
        with _exclusive_store_lock(self.path):
            descriptor = _open_store(self.path, write=True)
            try:
                information = _validate_descriptor(self.path, descriptor)
                existing = _read_records(descriptor, information.st_size)
                report = builder(tuple(record.runtime for record in existing))
                if not isinstance(report, ServiceRuntimeReport):
                    raise TypeError("service runtime builder returned an invalid value")
                _commit_records(
                    descriptor,
                    information.st_size,
                    existing,
                    [_PrivateServiceRuntimeRecord(report, authority)],
                )
                return report
            finally:
                os.close(descriptor)

    def read_all(self) -> list[ServiceRuntimeReport]:
        """Read every complete report and validate the whole history."""
        _reject_symlink_tree(self.path)
        if not self.path.exists():
            return []
        _reject_symlink_tree(self.path.parent)
        with _exclusive_store_lock(self.path):
            descriptor = _open_store(self.path, write=False)
            try:
                information = _validate_descriptor(self.path, descriptor)
                return [
                    record.runtime
                    for record in _read_records(descriptor, information.st_size)
                ]
            finally:
                os.close(descriptor)

    def current(self) -> dict[str, ServiceRuntimeReport]:
        """Return the latest report for every service instance."""
        current: dict[str, ServiceRuntimeReport] = {}
        for report in self.read_all():
            current[report.service_instance_id] = report
        return current

    def latest(
        self, service_instance_id: str | None = None
    ) -> ServiceRuntimeReport | None:
        """Return the last report globally or for one exact instance."""
        reports = self.read_all()
        if service_instance_id is None:
            return reports[-1] if reports else None
        return next(
            (
                report
                for report in reversed(reports)
                if report.service_instance_id == service_instance_id
            ),
            None,
        )

    def resolve_authority(
        self,
        *,
        service_instance_id: str,
        revision: int,
        token_sha256: str,
    ) -> tuple[ServiceRuntimeReport, ServiceRuntimeAuthority]:
        """Resolve one current owner-private capability by exact public identity."""
        _reject_symlink_tree(self.path)
        if not self.path.exists():
            raise LookupError("service runtime authority store does not exist")
        _reject_symlink_tree(self.path.parent)
        with _exclusive_store_lock(self.path):
            descriptor = _open_store(self.path, write=False)
            try:
                information = _validate_descriptor(self.path, descriptor)
                records = _read_records(descriptor, information.st_size)
            finally:
                os.close(descriptor)
        current = next(
            (
                record
                for record in reversed(records)
                if record.runtime.service_instance_id == service_instance_id
            ),
            None,
        )
        if current is None:
            raise LookupError("service runtime authority identity was not found")
        report = current.runtime
        authorization = report.authorization
        if (
            report.revision != revision
            or report.schema_version != SERVICE_RUNTIME_SCHEMA_VERSION
            or authorization is None
            or authorization.scheme != "bearer"
            or authorization.token_sha256 != token_sha256
            or current.authority is None
            or current.authority.authorization != authorization
        ):
            raise LookupError("service runtime authority identity is stale or invalid")
        return report, current.authority

    def finalize_active(self, *, failed: bool) -> list[ServiceRuntimeReport]:
        """Close nonterminal services when their owning execution terminates."""
        _reject_symlink_tree(self.path)
        if not self.path.exists():
            return []
        self._prepare_parent()
        with _exclusive_store_lock(self.path):
            descriptor = _open_store(self.path, write=True)
            try:
                information = _validate_descriptor(self.path, descriptor)
                existing = _read_records(descriptor, information.st_size)
                current: dict[str, _PrivateServiceRuntimeRecord] = {}
                for private_record in existing:
                    current[private_record.runtime.service_instance_id] = private_record
                observed_at = time.time()
                additions: list[_PrivateServiceRuntimeRecord] = []
                for private_record in current.values():
                    report = private_record.runtime
                    if report.lifecycle in {
                        ServiceLifecycle.STOPPED,
                        ServiceLifecycle.FAILED,
                    }:
                        continue
                    additions.append(
                        _PrivateServiceRuntimeRecord(
                            runtime=replace(
                                report,
                                revision=report.revision + 1,
                                lifecycle=(
                                    ServiceLifecycle.FAILED
                                    if failed
                                    else ServiceLifecycle.STOPPED
                                ),
                                message=(
                                    "JARVIS reconciled the service after its owning "
                                    + (
                                        "execution failed"
                                        if failed
                                        else "execution ended"
                                    )
                                ),
                                observed_at_epoch=max(
                                    observed_at,
                                    report.observed_at_epoch,
                                ),
                            ),
                            authority=private_record.authority,
                        )
                    )
                if additions:
                    _commit_records(
                        descriptor,
                        information.st_size,
                        existing,
                        additions,
                    )
                return [addition.runtime for addition in additions]
            finally:
                os.close(descriptor)

    def _prepare_parent(self) -> None:
        """Create and protect the parent without following redirections."""
        _reject_symlink_tree(self.path.parent)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ensure_private_path(self.path.parent, directory=True)
        _reject_symlink_tree(self.path.parent)


def _commit_records(
    descriptor: int,
    original_size: int,
    existing: list[_PrivateServiceRuntimeRecord],
    additions: list[_PrivateServiceRuntimeRecord],
) -> None:
    """Validate and durably append private records, rolling back partial writes."""
    combined = [*existing, *additions]
    validate_service_runtime_history([record.runtime for record in combined])
    payload = b"".join(
        (record.to_private_json() + "\n").encode("utf-8") for record in additions
    )
    if original_size + len(payload) > MAX_SERVICE_RUNTIME_STORE_BYTES:
        raise ValueError("service runtime store would exceed maximum size")
    os.lseek(descriptor, 0, os.SEEK_END)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short write while appending service runtime report")
            offset += written
        os.fsync(descriptor)
    except BaseException:
        os.ftruncate(descriptor, original_size)
        os.fsync(descriptor)
        raise


def _read_records(
    descriptor: int,
    size: int,
) -> list[_PrivateServiceRuntimeRecord]:
    """Read private records from one locked owner-verified descriptor."""
    if size > MAX_SERVICE_RUNTIME_STORE_BYTES:
        raise ValueError("service runtime store exceeds maximum size")
    if size:
        os.lseek(descriptor, size - 1, os.SEEK_SET)
        if os.read(descriptor, 1) != b"\n":
            raise ValueError("service runtime store has an incomplete JSONL record")
    os.lseek(descriptor, 0, os.SEEK_SET)
    records: list[_PrivateServiceRuntimeRecord] = []
    with os.fdopen(os.dup(descriptor), "r", encoding="utf-8", newline="") as stream:
        for line_number, line in enumerate(stream, start=1):
            if len(line.encode("utf-8")) > MAX_SERVICE_RUNTIME_BYTES + 1:
                raise ValueError(
                    f"service runtime line {line_number} exceeds maximum size"
                )
            payload = line.rstrip("\r\n")
            if not payload:
                raise ValueError(f"service runtime line {line_number} cannot be empty")
            records.append(_PrivateServiceRuntimeRecord.from_private_json(payload))
    validate_service_runtime_history([record.runtime for record in records])
    return records


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


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
            "service runtime path cannot be redirected by a symlink or reparse "
            f"point: {path}"
        ) from exc


def _open_store(path: Path, *, write: bool) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if write:
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
    else:
        descriptor = os.open(path, os.O_RDONLY | nofollow)
    try:
        ensure_private_descriptor(path, descriptor, directory=False)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _validate_descriptor(path: Path, descriptor: int) -> os.stat_result:
    information = os.fstat(descriptor)
    if not stat.S_ISREG(information.st_mode):
        raise ValueError(f"service runtime store must be a regular file: {path}")
    path_information = path.lstat()
    if stat.S_ISLNK(path_information.st_mode) or (
        path_information.st_dev,
        path_information.st_ino,
    ) != (information.st_dev, information.st_ino):
        raise ValueError(f"service runtime store changed during secure open: {path}")
    if os.name != "nt":
        getuid = getattr(os, "getuid", None)
        if getuid is not None and information.st_uid != getuid():
            raise PermissionError(
                f"service runtime store is not owned by current user: {path}"
            )
        if stat.S_IMODE(information.st_mode) & 0o077:
            raise PermissionError(
                f"service runtime store permissions are not private: {path}"
            )
    return information


@contextmanager
def _exclusive_store_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".lock")
    _reject_symlink(lock_path, label="service runtime lock")
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
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX)


def _unlock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


__all__ = ["MAX_SERVICE_RUNTIME_STORE_BYTES", "ServiceRuntimeStore"]
