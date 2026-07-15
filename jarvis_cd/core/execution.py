"""Durable execution identities and lifecycle records for JARVIS pipelines."""

from __future__ import annotations

import json
import errno
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Iterator, List, Literal, Mapping, Optional, cast
from uuid import uuid4

from jarvis_cd.artifacts import (
    ArtifactEvent,
    ArtifactLocation,
    ArtifactReporter,
    ArtifactState,
    ArtifactStore,
)
from jarvis_cd.progress import ProgressEvent, ProgressStore
from jarvis_cd.service_runtime import (
    SERVICE_RUNTIME_SNAPSHOT_SCHEMA_VERSION,
    ServiceRuntimeReport,
    ServiceRuntimeStore,
)
from jarvis_cd.util.private_path import (
    ensure_private_descriptor,
    ensure_private_path,
    reject_private_path_redirection,
)


HANDLE_SCHEMA = "jarvis.execution.handle.v1"
RECORD_SCHEMA = "jarvis.execution.record.v1"
LEGACY_RECORD_SCHEMA = "jarvis.execution.v1"
RECORD_NAME = ".jarvis-execution.json"
MAX_RECORD_BYTES = 65_536
PROGRESS_SNAPSHOT_SCHEMA = "jarvis.execution.progress.v1"
ARTIFACT_SNAPSHOT_SCHEMA = "jarvis.execution.artifacts.v1"
SERVICE_RUNTIME_SNAPSHOT_SCHEMA = SERVICE_RUNTIME_SNAPSHOT_SCHEMA_VERSION
DIRECT_LAUNCH_SCHEMA = "jarvis.direct-launch.v1"
DIRECT_LEASE_NAME = ".jarvis-direct-execution.lease"
SCHEDULER_ARTIFACT_PATH_SCHEMA = "jarvis.scheduler.artifact-path.v1"
SCHEDULER_ARTIFACT_PATH_METADATA_KEY = "jarvis_scheduler_path"
SCHEDULER_ARTIFACT_PATH_UNRESOLVED_CODE = "scheduler_artifact_path_unresolved"
SCHEDULER_ARTIFACT_PATH_TERMINAL_DIAGNOSTIC = (
    "JARVIS reached execution terminalization before the scheduler artifact "
    "path was resolved"
)

ExecutionMode = Literal["direct", "scheduler"]

_EXECUTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SLURM_NATIVE_ID_PATTERN = re.compile(r"^[0-9]{1,64}$")
_SLURM_CLUSTER_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,255}$")
_WINDOWS_RESERVED_COMPONENTS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_STATES = {
    "preparing",
    "scripted",
    "submitting",
    "submitted",
    "running",
    "completed",
    "failed",
    "canceled",
    "unknown",
}
_TERMINAL_STATES = {"scripted", "completed", "failed", "canceled"}
_TRANSITIONS = {
    "preparing": {"scripted", "submitting", "running", "failed"},
    "scripted": {"failed"},
    "submitting": {"submitted", "running", "completed", "failed", "unknown"},
    "submitted": {"running", "completed", "failed", "canceled", "unknown"},
    "running": {"completed", "failed", "canceled", "unknown"},
    "completed": set(),
    "failed": set(),
    "canceled": set(),
    "unknown": {"submitted", "running", "completed", "failed", "canceled"},
}
_UNSET = object()
_TRANSACTION_LOCK_PREFIX = ".jarvis-execution-lock-"
_DIRECT_STARTUP_GRACE_SECONDS = 60.0


@dataclass(frozen=True)
class PackageProgressSnapshot:
    """Latest validated progress for one package alias in an execution."""

    package_id: str
    package_name: str
    event_count: int
    latest: Optional[ProgressEvent]

    def to_dict(self) -> dict[str, Any]:
        """Serialize this package snapshot without exposing storage paths."""
        return {
            "package_id": self.package_id,
            "package_name": self.package_name,
            "event_count": self.event_count,
            "latest": self.latest.as_dict() if self.latest is not None else None,
        }


@dataclass(frozen=True)
class ExecutionProgressSnapshot:
    """Queryable aggregate of package progress for one durable execution."""

    execution_id: str
    pipeline_id: str
    execution_state: str
    terminal: bool
    packages: tuple[PackageProgressSnapshot, ...]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the stable agent-facing progress snapshot schema."""
        return {
            "schema_version": PROGRESS_SNAPSHOT_SCHEMA,
            "execution_id": self.execution_id,
            "pipeline_id": self.pipeline_id,
            "execution_state": self.execution_state,
            "terminal": self.terminal,
            "packages": [package.to_dict() for package in self.packages],
        }


@dataclass(frozen=True)
class ExecutionArtifactSnapshot:
    """Queryable current artifact manifest for one durable execution."""

    execution_id: str
    pipeline_id: str
    execution_state: str
    terminal: bool
    artifacts: tuple[ArtifactEvent, ...]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the stable agent-facing artifact snapshot schema."""
        return {
            "schema_version": ARTIFACT_SNAPSHOT_SCHEMA,
            "execution_id": self.execution_id,
            "pipeline_id": self.pipeline_id,
            "execution_state": self.execution_state,
            "terminal": self.terminal,
            "artifacts": [artifact.as_dict() for artifact in self.artifacts],
        }


@dataclass(frozen=True)
class ExecutionServiceRuntimeSnapshot:
    """Queryable current service runtimes for one durable execution."""

    execution_id: str
    pipeline_id: str
    execution_state: str
    terminal: bool
    service_runtimes: tuple[ServiceRuntimeReport, ...]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the stable agent-facing service-runtime snapshot."""
        return {
            "schema_version": SERVICE_RUNTIME_SNAPSHOT_SCHEMA,
            "execution_id": self.execution_id,
            "pipeline_id": self.pipeline_id,
            "execution_state": self.execution_state,
            "terminal": self.terminal,
            "service_runtimes": [
                runtime.to_dict() for runtime in self.service_runtimes
            ],
        }


def validate_execution_id(value: Optional[str] = None) -> str:
    """Return a path-safe execution identity, generating one when omitted."""
    execution_id = value or f"jarvis_{uuid4().hex}"
    reserved_stem = execution_id.split(".", 1)[0].upper()
    if (
        _EXECUTION_ID_PATTERN.fullmatch(execution_id) is None
        or execution_id.endswith(".")
        or reserved_stem in _WINDOWS_RESERVED_COMPONENTS
    ):
        raise ValueError(
            "execution_id must be 1-128 ASCII letters, digits, dots, underscores, "
            "or hyphens, cannot begin with punctuation or end with a dot, and "
            "cannot be a reserved Windows path alias"
        )
    return execution_id


def validate_pipeline_id(value: str) -> str:
    """Return a bounded, portable pipeline path component."""
    if not isinstance(value, str) or _EXECUTION_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(
            "pipeline_id must be 1-128 ASCII letters, digits, dots, underscores, "
            "or hyphens and cannot begin with punctuation"
        )
    reserved_stem = value.split(".", 1)[0].upper()
    if value.endswith(".") or reserved_stem in _WINDOWS_RESERVED_COMPONENTS:
        raise ValueError("pipeline_id is not a portable path component")
    return value


def is_execution_transaction_lock(path: Path) -> bool:
    """Return whether a directory entry is one valid persistent ID lock."""
    if not path.name.startswith(_TRANSACTION_LOCK_PREFIX):
        return False
    execution_id = path.name[len(_TRANSACTION_LOCK_PREFIX) :]
    validate_execution_id(execution_id)
    ensure_private_path(path, directory=False)
    status = path.lstat()
    if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1 or status.st_size > 1:
        raise RuntimeError(f"invalid execution transaction lock: {path}")
    return True


def _validated_text(
    value: Any,
    *,
    field_name: str,
    maximum: int = 4096,
    nullable: bool = False,
) -> Optional[str]:
    """Validate one bounded printable record field."""
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{field_name} must be a non-empty bounded string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field_name} cannot contain control characters")
    return value


def _utc_now() -> str:
    """Return a stable UTC timestamp suitable for persisted records."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_timestamp(value: Any, *, field_name: str) -> str:
    """Validate one timezone-aware ISO-8601 timestamp."""
    rendered = _validated_text(value, field_name=field_name, maximum=64)
    assert rendered is not None
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return rendered


@dataclass(frozen=True)
class ExecutionHandle:
    """Stable identity returned by every top-level pipeline execution."""

    execution_id: str
    pipeline_id: str
    mode: ExecutionMode
    scheduler_provider: Optional[str] = None
    scheduler_native_id: Optional[str] = None
    cluster: Optional[str] = None
    _record_path: Optional[Path] = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Reject malformed handles before they can become public references."""
        validate_execution_id(self.execution_id)
        validate_pipeline_id(self.pipeline_id)
        if self.mode not in {"direct", "scheduler"}:
            raise ValueError("mode must be 'direct' or 'scheduler'")
        _validated_text(
            self.scheduler_provider,
            field_name="scheduler_provider",
            nullable=True,
        )
        _validated_text(
            self.scheduler_native_id,
            field_name="scheduler_native_id",
            nullable=True,
        )
        _validated_text(self.cluster, field_name="cluster", nullable=True)
        if self.mode == "direct" and any(
            value is not None
            for value in (
                self.scheduler_provider,
                self.scheduler_native_id,
                self.cluster,
            )
        ):
            raise ValueError(
                "direct execution handles cannot contain scheduler identity"
            )
        if self.mode == "scheduler" and self.scheduler_provider is None:
            raise ValueError("scheduler execution handles require scheduler_provider")

    @property
    def native_id(self) -> Optional[str]:
        """Return the provider-native identity using the deprecated short name."""
        return self.scheduler_native_id

    @property
    def script_path(self) -> Optional[Path]:
        """Return the generated scheduler script path for compatibility."""
        record = self.refresh()
        value = record.metadata.get("script_path")
        return Path(value) if isinstance(value, str) and value else None

    def to_dict(self) -> dict[str, Any]:
        """Serialize this handle using the stable agent-facing schema."""
        return {
            "schema_version": HANDLE_SCHEMA,
            "execution_id": self.execution_id,
            "pipeline_id": self.pipeline_id,
            "mode": self.mode,
            "scheduler_provider": self.scheduler_provider,
            "scheduler_native_id": self.scheduler_native_id,
            "cluster": self.cluster,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "ExecutionHandle":
        """Decode an exact serialized handle from an API client."""
        expected_fields = {
            "schema_version",
            "execution_id",
            "pipeline_id",
            "mode",
            "scheduler_provider",
            "scheduler_native_id",
            "cluster",
        }
        if set(document) != expected_fields or document.get("schema_version") != (
            HANDLE_SCHEMA
        ):
            raise ValueError("invalid execution handle schema")
        mode = document.get("mode")
        if mode not in {"direct", "scheduler"}:
            raise ValueError("invalid execution handle mode")
        execution_id = _validated_text(
            document.get("execution_id"), field_name="execution_id"
        )
        pipeline_id = _validated_text(
            document.get("pipeline_id"), field_name="pipeline_id"
        )
        assert execution_id is not None and pipeline_id is not None
        return cls(
            execution_id=execution_id,
            pipeline_id=pipeline_id,
            mode=cast(ExecutionMode, mode),
            scheduler_provider=document.get("scheduler_provider"),
            scheduler_native_id=document.get("scheduler_native_id"),
            cluster=document.get("cluster"),
        )

    def refresh(self) -> "ExecutionRecord":
        """Read the current durable record associated with this handle."""
        if self._record_path is None:
            raise RuntimeError("execution handle is not bound to a durable record")
        record = ExecutionStore(
            self._record_path.parent.parent,
            self.pipeline_id,
        ).get(self.execution_id)
        if record.pipeline_id != self.pipeline_id or record.mode != self.mode:
            raise RuntimeError("execution record identity changed")
        return record

    def progress(self) -> ExecutionProgressSnapshot:
        """Return validated package progress for this exact execution."""
        record = self.refresh()
        assert record._record_path is not None
        store = ExecutionStore(record._record_path.parent.parent, self.pipeline_id)
        return store.progress(self.execution_id)

    def artifacts(self) -> ExecutionArtifactSnapshot:
        """Return the current generated artifacts for this exact execution."""
        record = self.refresh()
        assert record._record_path is not None
        store = ExecutionStore(record._record_path.parent.parent, self.pipeline_id)
        return store.artifacts(self.execution_id)

    def service_runtimes(self) -> ExecutionServiceRuntimeSnapshot:
        """Return current services owned by this exact execution."""
        record = self.refresh()
        assert record._record_path is not None
        store = ExecutionStore(record._record_path.parent.parent, self.pipeline_id)
        return store.service_runtimes(self.execution_id)


@dataclass(frozen=True)
class ExecutionRecord:
    """Durable, changing lifecycle state associated with an execution handle."""

    execution_id: str
    pipeline_id: str
    mode: ExecutionMode
    scheduler_provider: Optional[str]
    scheduler_native_id: Optional[str]
    cluster: Optional[str]
    state: str
    submitted: bool
    terminal: bool
    created_at: str
    updated_at: str
    return_code: Optional[int] = None
    error: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    _record_path: Optional[Path] = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Validate the complete record as one coherent state document."""
        ExecutionHandle(
            execution_id=self.execution_id,
            pipeline_id=self.pipeline_id,
            mode=self.mode,
            scheduler_provider=self.scheduler_provider,
            scheduler_native_id=self.scheduler_native_id,
            cluster=self.cluster,
        )
        if self.state not in _STATES:
            raise ValueError(f"unsupported execution state: {self.state}")
        if not isinstance(self.submitted, bool) or not isinstance(self.terminal, bool):
            raise ValueError("submitted and terminal must be booleans")
        if self.terminal and self.state not in _TERMINAL_STATES:
            raise ValueError("terminal execution record has a nonterminal state")
        if self.state in {"completed", "failed", "canceled"} and not self.terminal:
            raise ValueError("terminal execution state must set terminal=true")
        _validate_timestamp(self.created_at, field_name="created_at")
        _validate_timestamp(self.updated_at, field_name="updated_at")
        if self.return_code is not None and (
            isinstance(self.return_code, bool) or not isinstance(self.return_code, int)
        ):
            raise ValueError("return_code must be an integer or null")
        if self.state == "completed" and self.return_code != 0:
            raise ValueError("completed execution records require return_code=0")
        if self.state == "failed" and (
            self.return_code is None or self.return_code == 0
        ):
            raise ValueError("failed execution records require a nonzero return_code")
        if self.error is not None:
            if (
                not isinstance(self.error, str)
                or not self.error
                or len(self.error.encode("utf-8")) > 16_384
                or any(
                    ord(character) < 32 and character not in {"\n", "\r", "\t"}
                    for character in self.error
                )
                or any(ord(character) == 127 for character in self.error)
            ):
                raise ValueError("error must be a bounded printable diagnostic")
        if not isinstance(self.metadata, Mapping) or not all(
            isinstance(key, str) for key in self.metadata
        ):
            raise ValueError("metadata must be a string-keyed mapping")
        try:
            encoded_metadata = json.dumps(
                self.metadata,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("metadata must contain bounded JSON values") from exc
        if len(encoded_metadata) > 48_000:
            raise ValueError("metadata exceeds the execution record limit")

    @classmethod
    def new(
        cls,
        *,
        execution_id: str,
        pipeline_id: str,
        mode: ExecutionMode,
        scheduler_provider: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> "ExecutionRecord":
        """Construct a new preparing execution record."""
        now = _utc_now()
        return cls(
            execution_id=execution_id,
            pipeline_id=pipeline_id,
            mode=mode,
            scheduler_provider=scheduler_provider,
            scheduler_native_id=None,
            cluster=None,
            state="preparing",
            submitted=False,
            terminal=False,
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
        )

    @property
    def handle(self) -> ExecutionHandle:
        """Return the stable, queryable handle projected from this record."""
        return ExecutionHandle(
            execution_id=self.execution_id,
            pipeline_id=self.pipeline_id,
            mode=self.mode,
            scheduler_provider=self.scheduler_provider,
            scheduler_native_id=self.scheduler_native_id,
            cluster=self.cluster,
            _record_path=self._record_path,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize this record for durable storage and JSON CLI output."""
        return {
            "schema_version": RECORD_SCHEMA,
            "execution_id": self.execution_id,
            "pipeline_id": self.pipeline_id,
            # Keep the historic key so existing cleanup receipts retain their
            # pipeline ownership check while the public handle says pipeline_id.
            "pipeline_name": self.pipeline_id,
            "mode": self.mode,
            "scheduler_provider": self.scheduler_provider,
            "scheduler_native_id": self.scheduler_native_id,
            "cluster": self.cluster,
            "state": self.state,
            "submitted": self.submitted,
            "terminal": self.terminal,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "return_code": self.return_code,
            "error": self.error,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(
        cls,
        document: Mapping[str, Any],
        *,
        record_path: Optional[Path] = None,
    ) -> "ExecutionRecord":
        """Validate and decode one execution record document."""
        expected_fields = {
            "schema_version",
            "execution_id",
            "pipeline_id",
            "pipeline_name",
            "mode",
            "scheduler_provider",
            "scheduler_native_id",
            "cluster",
            "state",
            "submitted",
            "terminal",
            "created_at",
            "updated_at",
            "return_code",
            "error",
            "metadata",
        }
        if (
            set(document) != expected_fields
            or document.get("schema_version") != RECORD_SCHEMA
        ):
            raise ValueError("unsupported execution record schema")
        pipeline_id = document.get("pipeline_id")
        if document.get("pipeline_name") != pipeline_id:
            raise ValueError("execution record pipeline identity mismatch")
        mode = document.get("mode")
        if mode not in {"direct", "scheduler"}:
            raise ValueError("invalid execution mode")
        metadata = document.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("execution record metadata must be a mapping")
        execution_id = _validated_text(
            document.get("execution_id"), field_name="execution_id"
        )
        validated_pipeline_id = _validated_text(pipeline_id, field_name="pipeline_id")
        state = document.get("state")
        submitted = document.get("submitted")
        terminal = document.get("terminal")
        created_at = document.get("created_at")
        updated_at = document.get("updated_at")
        return_code = document.get("return_code")
        if not isinstance(state, str):
            raise ValueError("execution record state must be a string")
        if not isinstance(submitted, bool) or not isinstance(terminal, bool):
            raise ValueError("execution record lifecycle flags must be booleans")
        if not isinstance(created_at, str) or not isinstance(updated_at, str):
            raise ValueError("execution record timestamps must be strings")
        if return_code is not None and (
            not isinstance(return_code, int) or isinstance(return_code, bool)
        ):
            raise ValueError("execution record return_code must be an integer or null")
        assert execution_id is not None and validated_pipeline_id is not None
        return cls(
            execution_id=execution_id,
            pipeline_id=validated_pipeline_id,
            mode=cast(ExecutionMode, mode),
            scheduler_provider=document.get("scheduler_provider"),
            scheduler_native_id=document.get("scheduler_native_id"),
            cluster=document.get("cluster"),
            state=state,
            submitted=submitted,
            terminal=terminal,
            created_at=created_at,
            updated_at=updated_at,
            return_code=return_code,
            error=document.get("error"),
            metadata=dict(metadata),
            _record_path=record_path,
        )


def _effective_uid() -> int:
    """Return the effective POSIX uid or fail closed."""
    getter = getattr(os, "geteuid", None)
    if not callable(getter):
        raise RuntimeError("POSIX ownership checks are unavailable")
    value: Any = getter()
    return int(value)


def _validate_private_regular_file(
    descriptor: int,
    path: Path,
    *,
    maximum_size: int,
) -> os.stat_result:
    """Validate a no-follow descriptor against its path identity."""
    ensure_private_descriptor(path, descriptor, directory=False)
    descriptor_status = os.fstat(descriptor)
    path_status = path.lstat()
    if (
        not stat.S_ISREG(descriptor_status.st_mode)
        or stat.S_ISLNK(path_status.st_mode)
        or (descriptor_status.st_dev, descriptor_status.st_ino)
        != (path_status.st_dev, path_status.st_ino)
        or descriptor_status.st_nlink != 1
        or descriptor_status.st_size > maximum_size
    ):
        raise RuntimeError(f"invalid execution record: {path}")
    if os.name != "nt" and (
        descriptor_status.st_uid != _effective_uid()
        or stat.S_IMODE(descriptor_status.st_mode) & 0o077
    ):
        raise RuntimeError(f"execution record is not private: {path}")
    return descriptor_status


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous JSON objects at every record nesting level."""
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate execution record key: {key}")
        document[key] = value
    return document


def read_execution_record(
    execution_root: Path,
    *,
    expected_execution_id: Optional[str] = None,
) -> ExecutionRecord:
    """Read a bounded execution record without following path replacements."""
    execution_root = Path(execution_root)
    ensure_private_path(execution_root, directory=True)
    root_status = execution_root.lstat()
    if not stat.S_ISDIR(root_status.st_mode) or stat.S_ISLNK(root_status.st_mode):
        raise RuntimeError(f"execution root is not a real directory: {execution_root}")
    if os.name != "nt" and (
        root_status.st_uid != _effective_uid()
        or stat.S_IMODE(root_status.st_mode) & 0o077
    ):
        raise RuntimeError(f"execution root is not owner-controlled: {execution_root}")
    record_path = execution_root / RECORD_NAME
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(record_path, flags)
    try:
        _validate_private_regular_file(
            descriptor,
            record_path,
            maximum_size=MAX_RECORD_BYTES,
        )
        payload = os.read(descriptor, MAX_RECORD_BYTES + 1)
        if len(payload) > MAX_RECORD_BYTES:
            raise RuntimeError(f"execution record is too large: {record_path}")
    finally:
        os.close(descriptor)
    try:
        document = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"invalid execution record: {record_path}") from exc
    if not isinstance(document, dict):
        raise RuntimeError(f"invalid execution record: {record_path}")
    try:
        record = ExecutionRecord.from_dict(document, record_path=record_path)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid execution record: {record_path}") from exc
    expected = expected_execution_id or execution_root.name
    if validate_execution_id(expected) != expected or record.execution_id != expected:
        raise RuntimeError(f"execution record identity mismatch: {record_path}")
    return record


def _fsync_directory(path: Path) -> None:
    """Persist directory-entry changes on platforms supporting directory fsync."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_replace(source: Path, destination: Path) -> None:
    """Atomically replace a record and request write-through on Windows."""
    if os.name != "nt":
        os.replace(source, destination)
        return
    import win32con
    import win32file
    import pywintypes

    deadline = monotonic() + 30.0
    while True:
        try:
            win32file.MoveFileEx(
                str(source),
                str(destination),
                win32con.MOVEFILE_REPLACE_EXISTING | win32file.MOVEFILE_WRITE_THROUGH,
            )
            return
        except pywintypes.error as exc:
            if exc.winerror not in {5, 32} or monotonic() >= deadline:
                raise
            sleep(0.01)


def _atomic_write_record(path: Path, record: ExecutionRecord) -> None:
    """Durably replace one validated execution record."""
    payload = (
        json.dumps(record.to_dict(), separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_RECORD_BYTES:
        raise ValueError("execution record exceeds the storage limit")
    temporary_path: Optional[Path] = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
                ensure_private_descriptor(
                    temporary_path,
                    stream.fileno(),
                    directory=False,
                )
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        _durable_replace(temporary_path, path)
        temporary_path = None
        ensure_private_path(path, directory=False)
        _fsync_directory(path.parent)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@contextmanager
def execution_transaction_lock(
    executions_dir: Path,
    execution_id: str,
    *,
    timeout: float = 30.0,
) -> Iterator[None]:
    """Serialize record writers and cleanup for one durable execution.

    The lock is a persistent sibling of the execution root. Keeping it outside
    the root lets cleanup rename and delete the root on Windows, while ensuring
    that a writer waiting on POSIX cannot continue against the detached root.

    The lock file is intentionally retained after cleanup. Unlinking a locked
    file can create two independent lock objects when another process already
    has the old file open.
    """
    validated_id = validate_execution_id(execution_id)
    lock_path = Path(executions_dir) / f"{_TRANSACTION_LOCK_PREFIX}{validated_id}"
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        _validate_private_regular_file(descriptor, lock_path, maximum_size=1)
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
                os.fsync(descriptor)
            deadline = monotonic() + timeout
            while True:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if monotonic() >= deadline:
                        raise TimeoutError("timed out locking execution record")
                    sleep(0.01)
            try:
                yield
            finally:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _lock_direct_lease(descriptor: int, *, blocking: bool) -> bool:
    """Acquire a direct-run lease, returning false only when it is held."""
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        try:
            msvcrt.locking(descriptor, mode, 1)
        except OSError as exc:
            if not blocking and exc.errno in {
                errno.EACCES,
                errno.EAGAIN,
                errno.EDEADLK,
            }:
                return False
            raise
        return True

    import fcntl

    operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(descriptor, operation)
    except OSError as exc:
        if not blocking and exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise
    return True


def _unlock_direct_lease(descriptor: int) -> None:
    """Release one direct-run lease held by this descriptor."""
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def prepare_direct_execution_lease(execution_root: Path) -> Path:
    """Create the private lease used to prove an async direct child is alive."""
    root = Path(execution_root)
    ensure_private_path(root, directory=True)
    lease_path = root / DIRECT_LEASE_NAME
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(lease_path, flags, 0o600)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        os.write(descriptor, b"0")
        os.fsync(descriptor)
        _validate_private_regular_file(descriptor, lease_path, maximum_size=1)
    except BaseException:
        os.close(descriptor)
        lease_path.unlink(missing_ok=True)
        raise
    os.close(descriptor)
    ensure_private_path(lease_path, directory=False)
    _fsync_directory(root)
    return lease_path


@contextmanager
def direct_execution_lease(execution_root: Path) -> Iterator[None]:
    """Hold the process lease for exactly one async direct snapshot runner."""
    root = Path(execution_root)
    ensure_private_path(root, directory=True)
    lease_path = root / DIRECT_LEASE_NAME
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lease_path, flags)
    acquired = False
    try:
        status = _validate_private_regular_file(
            descriptor,
            lease_path,
            maximum_size=1,
        )
        if status.st_size != 1:
            raise RuntimeError("direct execution lease is not initialized")
        acquired = _lock_direct_lease(descriptor, blocking=False)
        if not acquired:
            raise RuntimeError("direct execution snapshot is already running")
        yield
    finally:
        if acquired:
            _unlock_direct_lease(descriptor)
        os.close(descriptor)


def _direct_execution_lease_is_held(execution_root: Path) -> bool:
    """Return whether another process currently owns the direct-run lease."""
    root = Path(execution_root)
    ensure_private_path(root, directory=True)
    lease_path = root / DIRECT_LEASE_NAME
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lease_path, flags)
    acquired = False
    try:
        status = _validate_private_regular_file(
            descriptor,
            lease_path,
            maximum_size=1,
        )
        if status.st_size != 1:
            raise RuntimeError("direct execution lease is not initialized")
        acquired = _lock_direct_lease(descriptor, blocking=False)
        return not acquired
    finally:
        if acquired:
            _unlock_direct_lease(descriptor)
        os.close(descriptor)


def _process_is_running(process_id: int) -> Optional[bool]:
    """Return local process liveness, or null when the OS cannot establish it."""
    if (
        isinstance(process_id, bool)
        or not isinstance(process_id, int)
        or process_id <= 0
    ):
        raise ValueError("process identity must be a positive integer")
    if os.name != "nt":
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None
        return True

    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        process_id,
    )
    if not handle:
        error = ctypes.get_last_error()
        if error == 5:  # Access denied proves that the process identity exists.
            return True
        if error == 87:  # Invalid parameter is returned for a missing PID.
            return False
        return None
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


class ExecutionStore:
    """Own and query execution roots for one named pipeline."""

    def __init__(self, executions_dir: Path, pipeline_id: str):
        self.executions_dir = Path(executions_dir)
        self.pipeline_id = validate_pipeline_id(pipeline_id)

    def _prepare_parent(self) -> None:
        """Create and validate the private execution collection directory."""
        reject_private_path_redirection(self.executions_dir)
        self.executions_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        ensure_private_path(self.executions_dir, directory=True)
        status = self.executions_dir.lstat()
        if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
            raise RuntimeError("pipeline executions path is not a real directory")
        if os.name != "nt":
            if status.st_uid != _effective_uid():
                raise RuntimeError("pipeline executions path is not owner-controlled")
            self.executions_dir.chmod(0o700)
        _fsync_directory(self.executions_dir.parent)

    def create(
        self,
        execution_id: str,
        *,
        mode: ExecutionMode,
        scheduler_provider: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ExecutionRecord:
        """Create one unique owned execution root and initial durable record."""
        validated_id = validate_execution_id(execution_id)
        self._prepare_parent()
        execution_root = self.executions_dir / validated_id
        reject_private_path_redirection(execution_root)
        with execution_transaction_lock(self.executions_dir, validated_id):
            execution_root.mkdir(mode=0o700)
            try:
                ensure_private_path(execution_root, directory=True)
                if os.name != "nt":
                    execution_root.chmod(0o700)
                record = ExecutionRecord.new(
                    execution_id=validated_id,
                    pipeline_id=self.pipeline_id,
                    mode=mode,
                    scheduler_provider=scheduler_provider,
                    metadata=metadata,
                )
                _atomic_write_record(execution_root / RECORD_NAME, record)
                _fsync_directory(self.executions_dir)
                return replace(record, _record_path=execution_root / RECORD_NAME)
            except BaseException:
                try:
                    for child in execution_root.iterdir():
                        child.unlink()
                    execution_root.rmdir()
                    _fsync_directory(self.executions_dir)
                except OSError:
                    pass
                raise

    def get(self, execution_id: str) -> ExecutionRecord:
        """Read and reconcile one exact durable execution record."""
        validated_id = validate_execution_id(execution_id)
        record = read_execution_record(
            self.executions_dir / validated_id,
            expected_execution_id=validated_id,
        )
        return self._reconcile_direct_execution(record)

    def _reconcile_direct_execution(
        self,
        record: ExecutionRecord,
    ) -> ExecutionRecord:
        """Terminalize an owned async child that died without finalizing."""
        if record.mode != "direct" or record.terminal:
            return record
        launch = record.metadata.get("direct_launch")
        if launch is None:
            # Blocking direct runs predate and do not use a detached lease.
            return record
        expected_fields = {
            "schema_version",
            "phase",
            "launcher_pid",
            "child_pid",
        }
        if not isinstance(launch, dict) or set(launch) != expected_fields:
            raise RuntimeError("direct execution launch metadata is invalid")
        if launch.get("schema_version") != DIRECT_LAUNCH_SCHEMA:
            raise RuntimeError("direct execution launch metadata is invalid")
        phase = launch.get("phase")
        if phase not in {"launching", "prepared", "spawned", "failed", "orphaned"}:
            raise RuntimeError("direct execution launch metadata is invalid")
        launcher_pid = launch.get("launcher_pid")
        child_pid = launch.get("child_pid")
        for field_name, value, nullable in (
            ("launcher_pid", launcher_pid, False),
            ("child_pid", child_pid, True),
        ):
            if value is None and nullable:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise RuntimeError(f"direct execution {field_name} metadata is invalid")

        execution_root = self.executions_dir / record.execution_id
        lease_path = execution_root / DIRECT_LEASE_NAME
        if lease_path.exists() or lease_path.is_symlink():
            if _direct_execution_lease_is_held(execution_root):
                return record
        elif phase in {"prepared", "spawned"}:
            raise RuntimeError("direct execution lease is missing")

        orphaned = False
        if record.state == "running" or phase in {"failed", "orphaned"}:
            orphaned = True
        elif phase in {"launching", "prepared"}:
            orphaned = _process_is_running(cast(int, launcher_pid)) is False
        elif phase == "spawned":
            assert isinstance(child_pid, int)
            child_running = _process_is_running(child_pid)
            if child_running is False:
                orphaned = True
            elif child_running is None:
                created = datetime.fromisoformat(
                    record.created_at.replace("Z", "+00:00")
                )
                age = (datetime.now(timezone.utc) - created).total_seconds()
                orphaned = age >= _DIRECT_STARTUP_GRACE_SECONDS
        if not orphaned:
            return record

        failed_launch = dict(launch)
        failed_launch["phase"] = "orphaned"
        try:
            return self.update(
                record.execution_id,
                state="failed",
                terminal=True,
                return_code=1,
                error="owned direct execution exited without finalizing its record",
                metadata={
                    "direct_launch": failed_launch,
                    "failure_stage": "direct_orphan_reconciliation",
                },
            )
        except ValueError:
            # A normal finalizer may win after the liveness probe.
            latest = read_execution_record(
                execution_root,
                expected_execution_id=record.execution_id,
            )
            if latest.terminal:
                return latest
            raise

    def list(self) -> list[ExecutionRecord]:
        """Return every owned execution record in stable creation order."""
        if not self.executions_dir.exists():
            return []
        status = self.executions_dir.lstat()
        if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
            raise RuntimeError("pipeline executions path is not a real directory")
        records: list[ExecutionRecord] = []
        for path in self.executions_dir.iterdir():
            if path.name.startswith("."):
                continue
            validate_execution_id(path.name)
            records.append(self.get(path.name))
        return sorted(
            records, key=lambda record: (record.created_at, record.execution_id)
        )

    def progress(self, execution_id: str) -> ExecutionProgressSnapshot:
        """Return the latest validated event for every bound package alias."""
        record = self.get(execution_id)
        progress_files = record.metadata.get("progress_files", {})
        if not isinstance(progress_files, dict):
            raise RuntimeError("execution progress index is invalid")
        execution_root = self.executions_dir / record.execution_id
        progress_root = execution_root / "progress"
        if progress_root.exists() or progress_root.is_symlink():
            status = progress_root.lstat()
            if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
                raise RuntimeError("execution progress path is not a real directory")
        snapshots: list[PackageProgressSnapshot] = []
        for package_id, entry in sorted(progress_files.items()):
            if (
                not isinstance(package_id, str)
                or not package_id
                or not isinstance(entry, dict)
                or set(entry) != {"filename", "package_name"}
                or not isinstance(entry.get("filename"), str)
                or not entry["filename"]
                or not isinstance(entry.get("package_name"), str)
                or not entry["package_name"]
            ):
                raise RuntimeError("execution progress index is invalid")
            filename = entry["filename"]
            package_name = entry["package_name"]
            relative = Path(filename)
            if (
                relative.name != filename
                or relative.is_absolute()
                or relative.suffix != ".jsonl"
            ):
                raise RuntimeError("execution progress index contains an invalid path")
            sidecar = progress_root / filename
            try:
                events = ProgressStore(sidecar).read_all()
            except (OSError, PermissionError, ValueError) as exc:
                raise RuntimeError(
                    f"invalid progress sidecar for package {package_id!r}"
                ) from exc
            for event in events:
                if (
                    event.execution_id != record.execution_id
                    or event.package_id != package_id
                    or event.package_name != package_name
                ):
                    raise RuntimeError(
                        f"progress identity mismatch for package {package_id!r}"
                    )
            snapshots.append(
                PackageProgressSnapshot(
                    package_id=package_id,
                    package_name=package_name,
                    event_count=len(events),
                    latest=events[-1] if events else None,
                )
            )
        return ExecutionProgressSnapshot(
            execution_id=record.execution_id,
            pipeline_id=record.pipeline_id,
            execution_state=record.state,
            terminal=record.terminal,
            packages=tuple(snapshots),
        )

    def artifacts(self, execution_id: str) -> ExecutionArtifactSnapshot:
        """Return the current identity-checked artifact manifest."""
        record = self.get(execution_id)
        artifact_files = record.metadata.get("artifact_files", {})
        if not isinstance(artifact_files, dict):
            raise RuntimeError("execution artifact index is invalid")
        execution_root = self.executions_dir / record.execution_id
        artifact_root = execution_root / "artifacts"
        if artifact_root.exists() or artifact_root.is_symlink():
            status = artifact_root.lstat()
            if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
                raise RuntimeError("execution artifact path is not a real directory")
        latest: list[ArtifactEvent] = []
        seen_ids: set[str] = set()
        for artifact_key, entry in sorted(artifact_files.items()):
            if (
                not isinstance(artifact_key, str)
                or not artifact_key
                or not isinstance(entry, dict)
                or set(entry) != {"filename", "package_id", "package_name"}
                or not isinstance(entry.get("filename"), str)
                or not entry["filename"]
                or not isinstance(entry.get("package_id"), str)
                or not entry["package_id"]
                or not isinstance(entry.get("package_name"), str)
                or not entry["package_name"]
            ):
                raise RuntimeError("execution artifact index is invalid")
            filename = entry["filename"]
            package_id = entry["package_id"]
            package_name = entry["package_name"]
            relative = Path(filename)
            if (
                relative.name != filename
                or relative.is_absolute()
                or relative.suffix != ".jsonl"
            ):
                raise RuntimeError("execution artifact index contains an invalid path")
            sidecar = artifact_root / filename
            try:
                current = ArtifactStore(sidecar).current()
            except (OSError, PermissionError, ValueError) as exc:
                raise RuntimeError(
                    f"invalid artifact sidecar for package {package_id!r}"
                ) from exc
            for event in current.values():
                if (
                    event.execution_id != record.execution_id
                    or event.package_id != package_id
                    or event.package_name != package_name
                ):
                    raise RuntimeError(
                        f"artifact identity mismatch for package {package_id!r}"
                    )
                if event.artifact_id in seen_ids:
                    raise RuntimeError(
                        "artifact ID is duplicated across package manifests"
                    )
                seen_ids.add(event.artifact_id)
                latest.append(event)
        return ExecutionArtifactSnapshot(
            execution_id=record.execution_id,
            pipeline_id=record.pipeline_id,
            execution_state=record.state,
            terminal=record.terminal,
            artifacts=tuple(
                sorted(
                    latest,
                    key=lambda event: (
                        event.package_id,
                        event.logical_name,
                        event.artifact_id,
                    ),
                )
            ),
        )

    def service_runtimes(
        self,
        execution_id: str,
    ) -> ExecutionServiceRuntimeSnapshot:
        """Return the current identity-checked service runtime set."""
        record = self.get(execution_id)
        runtime_files = record.metadata.get("service_runtime_files", {})
        if not isinstance(runtime_files, dict):
            raise RuntimeError("execution service runtime index is invalid")
        execution_root = self.executions_dir / record.execution_id
        runtime_root = execution_root / "service-runtimes"
        if runtime_root.exists() or runtime_root.is_symlink():
            status = runtime_root.lstat()
            if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
                raise RuntimeError(
                    "execution service runtime path is not a real directory"
                )
        current: list[ServiceRuntimeReport] = []
        seen_instances: set[str] = set()
        for runtime_key, entry in sorted(runtime_files.items()):
            if (
                not isinstance(runtime_key, str)
                or not runtime_key
                or not isinstance(entry, dict)
                or set(entry) != {"filename", "package_id", "package_name"}
                or not isinstance(entry.get("filename"), str)
                or not entry["filename"]
                or not isinstance(entry.get("package_id"), str)
                or not entry["package_id"]
                or not isinstance(entry.get("package_name"), str)
                or not entry["package_name"]
            ):
                raise RuntimeError("execution service runtime index is invalid")
            filename = entry["filename"]
            package_id = entry["package_id"]
            package_name = entry["package_name"]
            relative = Path(filename)
            if (
                relative.name != filename
                or relative.is_absolute()
                or relative.suffix != ".jsonl"
            ):
                raise RuntimeError(
                    "execution service runtime index contains an invalid path"
                )
            sidecar = runtime_root / filename
            try:
                reports = ServiceRuntimeStore(sidecar).current().values()
            except (OSError, PermissionError, ValueError) as exc:
                raise RuntimeError(
                    f"invalid service runtime sidecar for package {package_id!r}"
                ) from exc
            for report in reports:
                if (
                    report.execution_id != record.execution_id
                    or report.package_id != package_id
                    or report.package_name != package_name
                ):
                    raise RuntimeError(
                        f"service runtime identity mismatch for package {package_id!r}"
                    )
                if report.service_instance_id in seen_instances:
                    raise RuntimeError(
                        "service instance ID is duplicated across package runtimes"
                    )
                seen_instances.add(report.service_instance_id)
                current.append(report)
        return ExecutionServiceRuntimeSnapshot(
            execution_id=record.execution_id,
            pipeline_id=record.pipeline_id,
            execution_state=record.state,
            terminal=record.terminal,
            service_runtimes=tuple(
                sorted(
                    current,
                    key=lambda report: (
                        report.package_id,
                        report.service_instance_id,
                    ),
                )
            ),
        )

    def finalize_artifacts(
        self,
        execution_id: str,
        *,
        failed: bool = False,
    ) -> List[ArtifactEvent]:
        """Seal every producing artifact before execution terminalization."""
        validated_id = validate_execution_id(execution_id)
        record = read_execution_record(
            self.executions_dir / validated_id,
            expected_execution_id=validated_id,
        )
        artifact_files = record.metadata.get("artifact_files", {})
        if not isinstance(artifact_files, dict):
            raise RuntimeError("execution artifact index is invalid")
        artifact_root = self.executions_dir / record.execution_id / "artifacts"
        sealed: List[ArtifactEvent] = []
        for artifact_key, entry in artifact_files.items():
            if (
                not isinstance(artifact_key, str)
                or not artifact_key
                or not isinstance(entry, dict)
                or set(entry) != {"filename", "package_id", "package_name"}
                or not isinstance(entry.get("filename"), str)
                or not isinstance(entry.get("package_id"), str)
                or not entry["package_id"]
                or not isinstance(entry.get("package_name"), str)
                or not entry["package_name"]
            ):
                raise RuntimeError("execution artifact index is invalid")
            filename = entry["filename"]
            relative = Path(filename)
            if relative.name != filename or relative.suffix != ".jsonl":
                raise RuntimeError("execution artifact index contains an invalid path")
            artifact_path = artifact_root / filename
            core_artifacts = (
                artifact_key == "jarvis-core"
                and entry["package_id"] == "jarvis-core"
                and entry["package_name"] == "jarvis.core"
            )
            if core_artifacts:
                sealed.extend(
                    self._terminalize_pending_scheduler_artifact_paths(
                        artifact_path,
                        execution_id=validated_id,
                    )
                )
            sealed.extend(
                ArtifactStore(artifact_path).finalize_open(
                    ArtifactState.FINALIZED
                    if core_artifacts
                    else (ArtifactState.FAILED if failed else ArtifactState.INCOMPLETE),
                    state_without_location=(
                        ArtifactState.INCOMPLETE if core_artifacts else None
                    ),
                )
            )
        return sealed

    def finalize_service_runtimes(
        self,
        execution_id: str,
        *,
        failed: bool = False,
    ) -> list[ServiceRuntimeReport]:
        """Close every active service before execution terminalization."""
        validated_id = validate_execution_id(execution_id)
        record = read_execution_record(
            self.executions_dir / validated_id,
            expected_execution_id=validated_id,
        )
        runtime_files = record.metadata.get("service_runtime_files", {})
        if not isinstance(runtime_files, dict):
            raise RuntimeError("execution service runtime index is invalid")
        runtime_root = self.executions_dir / validated_id / "service-runtimes"
        finalized: list[ServiceRuntimeReport] = []
        for runtime_key, entry in runtime_files.items():
            if (
                not isinstance(runtime_key, str)
                or not runtime_key
                or not isinstance(entry, dict)
                or set(entry) != {"filename", "package_id", "package_name"}
                or not isinstance(entry.get("filename"), str)
                or not isinstance(entry.get("package_id"), str)
                or not entry["package_id"]
                or not isinstance(entry.get("package_name"), str)
                or not entry["package_name"]
            ):
                raise RuntimeError("execution service runtime index is invalid")
            filename = entry["filename"]
            relative = Path(filename)
            if relative.name != filename or relative.suffix != ".jsonl":
                raise RuntimeError(
                    "execution service runtime index contains an invalid path"
                )
            finalized.extend(
                ServiceRuntimeStore(runtime_root / filename).finalize_active(
                    failed=failed
                )
            )
        return finalized

    def _terminalize_pending_scheduler_artifact_paths(
        self,
        artifact_path: Path,
        *,
        execution_id: str,
    ) -> List[ArtifactEvent]:
        """Give unresolved scheduler logs coherent fail-closed terminal metadata."""
        current = ArtifactStore(artifact_path).current()
        reporter = ArtifactReporter(
            package_name="jarvis.core",
            package_id="jarvis-core",
            execution_id=execution_id,
            path=artifact_path,
        )
        revised: List[ArtifactEvent] = []
        for event in current.values():
            details_value = event.metadata.get(SCHEDULER_ARTIFACT_PATH_METADATA_KEY)
            if details_value is None:
                continue
            if not isinstance(details_value, dict):
                raise RuntimeError("scheduler artifact path metadata is invalid")
            if details_value.get("status") != "pending":
                continue
            if (
                set(details_value)
                != {
                    "schema_version",
                    "provider",
                    "path_pattern",
                    "scope",
                    "array_requested",
                    "status",
                }
                or details_value.get("schema_version") != SCHEDULER_ARTIFACT_PATH_SCHEMA
                or not isinstance(details_value.get("provider"), str)
                or not isinstance(details_value.get("path_pattern"), str)
                or details_value.get("scope") not in {"execution", "cluster"}
                or not isinstance(details_value.get("array_requested"), bool)
                or event.location is not None
                or event.state is not ArtifactState.PRODUCING
            ):
                raise RuntimeError("pending scheduler artifact path is invalid")
            details = dict(details_value)
            details.update(
                {
                    "status": "incomplete",
                    "diagnostic_code": SCHEDULER_ARTIFACT_PATH_UNRESOLVED_CODE,
                    "diagnostic": SCHEDULER_ARTIFACT_PATH_TERMINAL_DIAGNOSTIC,
                }
            )
            metadata = dict(event.metadata)
            metadata[SCHEDULER_ARTIFACT_PATH_METADATA_KEY] = details
            revised.append(
                reporter.emit(
                    artifact_id=event.artifact_id,
                    logical_name=event.logical_name,
                    kind=event.kind,
                    role=event.role,
                    structure=event.structure,
                    ownership=event.ownership,
                    state=ArtifactState.INCOMPLETE,
                    location=None,
                    media_type=event.media_type,
                    format=event.format,
                    size_bytes=event.size_bytes,
                    checksum=event.checksum,
                    message=SCHEDULER_ARTIFACT_PATH_TERMINAL_DIAGNOSTIC,
                    metadata=metadata,
                )
            )
        return revised

    def resolve_scheduler_artifact_paths(
        self,
        execution_id: str,
        *,
        provider: str,
        native_id: str,
    ) -> List[ArtifactEvent]:
        """Resolve pending core log paths through trusted scheduler identity.

        Resolution is serialized with execution terminalization. A provider
        that cannot prove one concrete path produces a terminal, location-less
        ``INCOMPLETE`` revision instead of an invented filesystem reference.
        Repeated submitter/runtime calls are idempotent.
        """
        from jarvis_cd.core.scheduler import resolve_scheduler_artifact_path

        validated_id = validate_execution_id(execution_id)
        validated_provider = _validated_text(
            provider,
            field_name="scheduler_provider",
            maximum=64,
        )
        validated_native_id = _validated_text(
            native_id,
            field_name="scheduler_native_id",
            maximum=256,
        )
        assert validated_provider is not None and validated_native_id is not None
        if validated_provider == "slurm" and (
            _SLURM_NATIVE_ID_PATTERN.fullmatch(validated_native_id) is None
        ):
            raise ValueError("SLURM native execution identity must be numeric")
        execution_root = self.executions_dir / validated_id
        with execution_transaction_lock(self.executions_dir, validated_id):
            record = read_execution_record(
                execution_root,
                expected_execution_id=validated_id,
            )
            if record.pipeline_id != self.pipeline_id or record.mode != "scheduler":
                raise RuntimeError(
                    "scheduler artifact resolution identity did not match"
                )
            if record.scheduler_provider not in {None, validated_provider}:
                raise RuntimeError("scheduler artifact provider identity did not match")
            if record.scheduler_native_id != validated_native_id:
                raise RuntimeError("scheduler artifact native identity did not match")
            artifact_files = record.metadata.get("artifact_files", {})
            if not isinstance(artifact_files, dict):
                raise RuntimeError("execution artifact index is invalid")
            entry = artifact_files.get("jarvis-core")
            if entry is None:
                return []
            if (
                not isinstance(entry, dict)
                or set(entry) != {"filename", "package_id", "package_name"}
                or entry.get("package_id") != "jarvis-core"
                or entry.get("package_name") != "jarvis.core"
                or not isinstance(entry.get("filename"), str)
            ):
                raise RuntimeError("JARVIS core artifact index is invalid")
            filename = entry["filename"]
            relative = Path(filename)
            if relative.name != filename or relative.suffix != ".jsonl":
                raise RuntimeError("JARVIS core artifact path is invalid")
            artifact_path = execution_root / "artifacts" / filename
            current = ArtifactStore(artifact_path).current()
            pending: List[ArtifactEvent] = []
            for event in current.values():
                details_value = event.metadata.get(SCHEDULER_ARTIFACT_PATH_METADATA_KEY)
                if details_value is None:
                    continue
                if not isinstance(details_value, dict):
                    raise RuntimeError("scheduler artifact path metadata is invalid")
                if details_value.get("provider") != validated_provider:
                    raise RuntimeError("scheduler artifact path provider did not match")
                status = details_value.get("status")
                if status in {"resolved", "incomplete"}:
                    continue
                if (
                    status == "pending"
                    and event.state is ArtifactState.INCOMPLETE
                    and record.terminal
                    and event.location is None
                ):
                    continue
                if (
                    status != "pending"
                    or set(details_value)
                    != {
                        "schema_version",
                        "provider",
                        "path_pattern",
                        "scope",
                        "array_requested",
                        "status",
                    }
                    or details_value.get("schema_version")
                    != SCHEDULER_ARTIFACT_PATH_SCHEMA
                    or details_value.get("scope") not in {"execution", "cluster"}
                    or not isinstance(details_value.get("path_pattern"), str)
                    or not isinstance(details_value.get("array_requested"), bool)
                    or event.location is not None
                    or event.state is not ArtifactState.PRODUCING
                ):
                    raise RuntimeError("pending scheduler artifact path is invalid")
                pending.append(event)
            if not pending:
                return []

            reporter = ArtifactReporter(
                package_name="jarvis.core",
                package_id="jarvis-core",
                execution_id=validated_id,
                path=artifact_path,
            )
            revised: List[ArtifactEvent] = []
            for event in pending:
                details_value = event.metadata[SCHEDULER_ARTIFACT_PATH_METADATA_KEY]
                assert isinstance(details_value, dict)
                details = dict(details_value)
                resolution = resolve_scheduler_artifact_path(
                    validated_provider,
                    str(details["path_pattern"]),
                    native_id=validated_native_id,
                    array_requested=bool(details["array_requested"]),
                )
                location = None
                state = ArtifactState.INCOMPLETE
                diagnostic_code = resolution.diagnostic_code
                diagnostic = resolution.diagnostic
                if resolution.path is not None:
                    try:
                        location = (
                            ArtifactLocation.execution_relative(resolution.path)
                            if details["scope"] == "execution"
                            else ArtifactLocation.cluster_path(resolution.path)
                        )
                    except ValueError:
                        diagnostic_code = "scheduler_artifact_path_invalid"
                        diagnostic = (
                            "JARVIS could not validate the resolved scheduler "
                            "artifact path"
                        )
                    else:
                        state = ArtifactState.PRODUCING
                metadata = dict(event.metadata)
                if location is None:
                    diagnostic_code = (
                        diagnostic_code or "scheduler_artifact_path_unresolved"
                    )
                    diagnostic = diagnostic or (
                        "JARVIS could not resolve the scheduler artifact path"
                    )
                    details.update(
                        {
                            "status": "incomplete",
                            "diagnostic_code": diagnostic_code,
                            "diagnostic": diagnostic,
                        }
                    )
                else:
                    details.update(
                        {
                            "status": "resolved",
                            "native_id": validated_native_id,
                            "resolved_path": resolution.path,
                        }
                    )
                metadata[SCHEDULER_ARTIFACT_PATH_METADATA_KEY] = details
                revised.append(
                    reporter.emit(
                        artifact_id=event.artifact_id,
                        logical_name=event.logical_name,
                        kind=event.kind,
                        role=event.role,
                        structure=event.structure,
                        ownership=event.ownership,
                        state=state,
                        location=location,
                        media_type=event.media_type,
                        format=event.format,
                        size_bytes=event.size_bytes,
                        checksum=event.checksum,
                        message=(event.message if location is not None else diagnostic),
                        metadata=metadata,
                    )
                )
            return revised

    def update(
        self,
        execution_id: str,
        *,
        state: Optional[str] = None,
        submitted: Optional[bool] = None,
        terminal: Optional[bool] = None,
        scheduler_provider: object = _UNSET,
        native_id: object = _UNSET,
        cluster: object = _UNSET,
        return_code: object = _UNSET,
        error: object = _UNSET,
        metadata: Optional[Mapping[str, Any]] = None,
        _script_activation: bool = False,
    ) -> ExecutionRecord:
        """Atomically merge one lifecycle transition into the durable record."""
        validated_id = validate_execution_id(execution_id)
        execution_root = self.executions_dir / validated_id
        with execution_transaction_lock(self.executions_dir, validated_id):
            current = read_execution_record(
                execution_root,
                expected_execution_id=validated_id,
            )
            if current.pipeline_id != self.pipeline_id:
                raise RuntimeError("execution record belongs to another pipeline")
            next_state = state or current.state
            if next_state not in _STATES:
                raise ValueError(f"unsupported execution state: {next_state}")
            next_submitted = current.submitted if submitted is None else submitted
            next_terminal = current.terminal if terminal is None else terminal
            next_provider = (
                current.scheduler_provider
                if scheduler_provider is _UNSET
                else scheduler_provider
            )
            next_native_id = (
                current.scheduler_native_id if native_id is _UNSET else native_id
            )
            next_cluster = current.cluster if cluster is _UNSET else cluster
            scripted_activation = (
                _script_activation
                and current.mode == "scheduler"
                and current.state == "scripted"
                and next_state == "running"
                and next_submitted is True
                and next_terminal is False
                and next_provider is not None
                and next_native_id is not None
            )
            if (
                next_state != current.state
                and next_state not in _TRANSITIONS[current.state]
                and not scripted_activation
            ):
                raise ValueError(
                    f"invalid execution state transition: {current.state} -> {next_state}"
                )
            if current.terminal and not next_terminal and not scripted_activation:
                raise ValueError("terminal execution records cannot become nonterminal")
            for field_name, old_value, new_value in (
                ("scheduler_provider", current.scheduler_provider, next_provider),
                ("scheduler_native_id", current.scheduler_native_id, next_native_id),
                ("cluster", current.cluster, next_cluster),
            ):
                if old_value is not None and new_value != old_value:
                    raise ValueError(f"{field_name} cannot change once assigned")
            merged_metadata = dict(current.metadata)
            if metadata:
                merged_metadata.update(metadata)
            next_return_code = cast(
                Optional[int],
                current.return_code if return_code is _UNSET else return_code,
            )
            next_error = cast(
                Optional[str],
                current.error if error is _UNSET else error,
            )
            updated = ExecutionRecord(
                execution_id=current.execution_id,
                pipeline_id=current.pipeline_id,
                mode=current.mode,
                scheduler_provider=next_provider,  # type: ignore[arg-type]
                scheduler_native_id=next_native_id,  # type: ignore[arg-type]
                cluster=next_cluster,  # type: ignore[arg-type]
                state=next_state,
                submitted=next_submitted,
                terminal=next_terminal,
                created_at=current.created_at,
                updated_at=_utc_now(),
                return_code=next_return_code,
                error=next_error,
                metadata=merged_metadata,
                _record_path=execution_root / RECORD_NAME,
            )
            if (
                not current.terminal
                and updated.terminal
                and updated.state != "scripted"
            ):
                self.finalize_service_runtimes(
                    validated_id,
                    failed=updated.state == "failed",
                )
                self.finalize_artifacts(
                    validated_id,
                    failed=updated.state == "failed",
                )
            _atomic_write_record(execution_root / RECORD_NAME, updated)
            return updated

    def activate_scheduler(
        self,
        execution_id: str,
        *,
        provider: str,
        native_id: str,
        cluster: Optional[str] = None,
    ) -> ExecutionRecord:
        """Bind scheduler-owned identity and mark an allocated script running.

        This is the only operation allowed to activate a terminal ``scripted``
        record produced by ``submit=False``. It is also idempotent for a normal
        submission whose submitter and allocation race to persist the same
        provider identity.
        """
        validated_provider = _validated_text(
            provider,
            field_name="scheduler_provider",
            maximum=64,
        )
        validated_native_id = _validated_text(
            native_id,
            field_name="scheduler_native_id",
            maximum=256,
        )
        validated_cluster = _validated_text(
            cluster,
            field_name="cluster",
            maximum=255,
            nullable=True,
        )
        assert validated_provider is not None and validated_native_id is not None
        if validated_provider == "slurm":
            if _SLURM_NATIVE_ID_PATTERN.fullmatch(validated_native_id) is None:
                raise ValueError("SLURM native execution identity must be numeric")
            if validated_cluster is not None and (
                _SLURM_CLUSTER_PATTERN.fullmatch(validated_cluster) is None
            ):
                raise ValueError("SLURM cluster identity is invalid")
        metadata = {
            "scheduler_activation": {
                "provider": validated_provider,
                "native_id": validated_native_id,
                "cluster": validated_cluster,
                "identity_source": "scheduler_runtime_environment",
            }
        }
        return self.update(
            execution_id,
            state="running",
            submitted=True,
            terminal=False,
            scheduler_provider=validated_provider,
            native_id=validated_native_id,
            cluster=validated_cluster if validated_cluster is not None else _UNSET,
            metadata=metadata,
            _script_activation=True,
        )


def finalize_execution(
    execution_root: Path,
    execution_id: str,
    return_code: int,
) -> ExecutionRecord:
    """Finalize one scheduler script using its exact durable execution record."""
    validated_id = validate_execution_id(execution_id)
    root = Path(execution_root)
    if root.name != validated_id:
        raise RuntimeError("scheduler execution root does not match execution_id")
    current = read_execution_record(root, expected_execution_id=validated_id)
    if current.mode != "scheduler":
        raise RuntimeError("scheduler finalizer cannot update a direct execution")
    if current.terminal:
        return current
    store = ExecutionStore(root.parent, current.pipeline_id)
    if return_code == 0:
        return store.update(
            validated_id,
            state="completed",
            terminal=True,
            return_code=0,
            error=None,
        )
    return store.update(
        validated_id,
        state="failed",
        terminal=True,
        return_code=return_code,
        error=f"scheduler script exited with status {return_code}",
    )


def run_execution_snapshot(
    execution_root: Path,
    execution_id: str,
    snapshot_dir: Path,
) -> ExecutionRecord:
    """Run one owned direct snapshot in a child process."""
    validated_id = validate_execution_id(execution_id)
    root = Path(execution_root).resolve(strict=True)
    runtime = Path(snapshot_dir).resolve(strict=True)
    if root.name != validated_id or runtime != root / "runtime":
        raise RuntimeError("direct execution snapshot identity mismatch")
    with direct_execution_lease(root):
        current = read_execution_record(root, expected_execution_id=validated_id)
        if current.mode != "direct" or current.terminal:
            raise RuntimeError("direct execution snapshot is not runnable")
        os.environ["JARVIS_PIPELINE_SNAPSHOT_DIR"] = str(runtime)
        try:
            from jarvis_cd.core.pipeline import Pipeline

            pipeline = Pipeline()
            pipeline.load("yaml", str(runtime / "pipeline.yaml"))
            handle = pipeline.run()
            return handle.refresh()
        except BaseException as error:
            try:
                current = read_execution_record(
                    root,
                    expected_execution_id=validated_id,
                )
                if not current.terminal:
                    diagnostic = str(error) or type(error).__name__
                    encoded = diagnostic.encode("utf-8")
                    if len(encoded) > 16_384:
                        diagnostic = "[truncated]\n" + encoded[-16_000:].decode(
                            "utf-8", errors="ignore"
                        )
                    ExecutionStore(root.parent, current.pipeline_id).update(
                        validated_id,
                        state="failed",
                        terminal=True,
                        return_code=1,
                        error=diagnostic,
                        metadata={"failure_stage": "direct_snapshot"},
                    )
            except Exception as record_error:
                error.add_note(
                    f"could not persist failed direct snapshot state: {record_error}"
                )
            raise


def activate_scheduler_execution(
    execution_root: Path,
    execution_id: str,
    provider: str,
    native_id: str,
    cluster: Optional[str] = None,
) -> ExecutionRecord:
    """Activate one scheduler script using provider-owned runtime identity."""
    validated_id = validate_execution_id(execution_id)
    root = Path(execution_root)
    if root.name != validated_id:
        raise RuntimeError("scheduler execution root does not match execution_id")
    current = read_execution_record(root, expected_execution_id=validated_id)
    if current.mode != "scheduler":
        raise RuntimeError("scheduler activation cannot update a direct execution")
    store = ExecutionStore(root.parent, current.pipeline_id)
    store.activate_scheduler(
        validated_id,
        provider=provider,
        native_id=native_id,
        cluster=cluster,
    )
    store.resolve_scheduler_artifact_paths(
        validated_id,
        provider=provider,
        native_id=native_id,
    )
    return store.get(validated_id)


def _main(argv: Optional[list[str]] = None) -> int:
    """Run the private scheduler-script lifecycle helper."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m jarvis_cd.core.execution")
    subparsers = parser.add_subparsers(dest="command", required=True)
    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--execution-root", required=True)
    finalize.add_argument("--execution-id", required=True)
    finalize.add_argument("--return-code", required=True, type=int)
    run_snapshot = subparsers.add_parser("run-snapshot")
    run_snapshot.add_argument("--execution-root", required=True)
    run_snapshot.add_argument("--execution-id", required=True)
    run_snapshot.add_argument("--snapshot-dir", required=True)
    activate = subparsers.add_parser("activate")
    activate.add_argument("--execution-root", required=True)
    activate.add_argument("--execution-id", required=True)
    activate.add_argument("--provider", required=True)
    activate.add_argument("--native-id", required=True)
    activate.add_argument("--cluster")
    arguments = parser.parse_args(argv)
    if arguments.command == "finalize":
        finalize_execution(
            Path(arguments.execution_root),
            arguments.execution_id,
            arguments.return_code,
        )
        return 0
    if arguments.command == "run-snapshot":
        run_execution_snapshot(
            Path(arguments.execution_root),
            arguments.execution_id,
            Path(arguments.snapshot_dir),
        )
        return 0
    if arguments.command == "activate":
        activate_scheduler_execution(
            Path(arguments.execution_root),
            arguments.execution_id,
            arguments.provider,
            arguments.native_id,
            arguments.cluster,
        )
        return 0
    raise RuntimeError("unsupported execution helper command")


if __name__ == "__main__":
    raise SystemExit(_main())
