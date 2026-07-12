"""
Pipeline management for Jarvis-CD.
Provides the consolidated Pipeline class that combines pipeline creation, loading, and execution.
"""

import os
import hashlib
import json
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import yaml
from contextlib import ExitStack
from pathlib import Path
from typing import Dict, Any, List, Optional
from uuid import uuid4
from jarvis_cd.core.config import load_class, Jarvis
from jarvis_cd.core.execution import (
    LEGACY_RECORD_SCHEMA,
    RECORD_NAME,
    RECORD_SCHEMA,
    ExecutionHandle,
    ExecutionProgressSnapshot,
    ExecutionRecord,
    ExecutionStore,
    is_execution_transaction_lock,
    execution_transaction_lock,
    prepare_direct_execution_lease,
    validate_execution_id,
    validate_pipeline_id,
)
from jarvis_cd.util.logger import logger
from jarvis_cd.util.hostfile import Hostfile


_SNAPSHOT_ENVIRONMENT = "JARVIS_PIPELINE_SNAPSHOT_DIR"
_EXECUTION_MARKER = RECORD_NAME
_EXECUTION_MARKER_SCHEMA = LEGACY_RECORD_SCHEMA
_EXECUTION_CLEANUP_SCHEMA = "jarvis.execution-cleanup.v1"
_MAX_EXPLICIT_EXECUTION_CLEANUP = 1024


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    """Build a JSON object while rejecting duplicate security fields."""
    document: Dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate JSON key: {key}")
        document[key] = value
    return document


def _bounded_scheduler_stderr(result: Any, limit: int = 4096) -> Optional[str]:
    """Return a bounded scheduler diagnostic captured by the executor."""
    stderr = getattr(result, "stderr", {})
    if isinstance(stderr, dict):
        value = stderr.get("localhost", "")
    else:
        value = stderr
    diagnostic = str(value or "").strip()
    if not diagnostic:
        return None
    if len(diagnostic) <= limit:
        return diagnostic
    return "[truncated]\n" + diagnostic[-limit:]


def _bounded_execution_error(error: BaseException, limit: int = 16_384) -> str:
    """Return a bounded UTF-8 diagnostic without masking the original error."""
    diagnostic = str(error) or type(error).__name__
    encoded = diagnostic.encode("utf-8")
    if len(encoded) <= limit:
        return diagnostic
    prefix = b"[truncated]\n"
    suffix = encoded[-(limit - len(prefix)) :].decode("utf-8", errors="ignore")
    return f"[truncated]\n{suffix}"


def _atomic_yaml_dump(path: Path, value: Any) -> None:
    """Durably replace one YAML document without exposing a truncated target."""
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            yaml.dump(value, temporary, default_flow_style=False)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        if os.name != "nt":
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory = os.open(path.parent, flags)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _atomic_json_dump(path: Path, value: Dict[str, Any]) -> None:
    """Durably replace one small JSON ownership document."""
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(value, temporary, separators=(",", ":"), sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        if os.name != "nt":
            temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
        temporary_path = None
        _fsync_directory(path.parent)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _fsync_directory(path: Path) -> None:
    """Persist directory-entry changes on platforms that expose directory fsync."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _posix_effective_uid() -> int:
    """Return the effective POSIX uid or fail closed on unsupported platforms."""
    getter = getattr(os, "geteuid", None)
    if not callable(getter):
        raise RuntimeError("POSIX ownership checks are unavailable")
    return int(getter())


def _posix_fchmod(descriptor: int, mode: int) -> None:
    """Change a POSIX descriptor mode or fail closed when unavailable."""
    changer = getattr(os, "fchmod", None)
    if not callable(changer):
        raise RuntimeError("POSIX descriptor permissions are unavailable")
    changer(descriptor, mode)


def _validated_execution_id(value: Optional[str]) -> str:
    """Return a bounded path-safe execution identifier."""
    return validate_execution_id(value)


def _read_execution_marker(
    execution_root: Path,
    *,
    expected_execution_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Read and validate an owned execution marker without following links."""
    root_status = execution_root.lstat()
    if not stat.S_ISDIR(root_status.st_mode) or stat.S_ISLNK(root_status.st_mode):
        raise RuntimeError(f"execution root is not a real directory: {execution_root}")
    marker_path = execution_root / _EXECUTION_MARKER
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(marker_path, flags)
    try:
        status = os.fstat(descriptor)
        marker_status = marker_path.lstat()
        if (
            not stat.S_ISREG(status.st_mode)
            or stat.S_ISLNK(marker_status.st_mode)
            or (status.st_dev, status.st_ino)
            != (marker_status.st_dev, marker_status.st_ino)
            or status.st_nlink != 1
            or status.st_size > 65_536
        ):
            raise RuntimeError(f"invalid execution ownership marker: {marker_path}")
        payload = os.read(descriptor, 65_537)
        if len(payload) > 65_536:
            raise RuntimeError(
                f"execution ownership marker is too large: {marker_path}"
            )
    finally:
        os.close(descriptor)
    return _validate_execution_marker_payload(
        payload,
        marker_label=str(marker_path),
        expected_name=expected_execution_id or execution_root.name,
    )


def _validate_execution_marker_payload(
    payload: bytes,
    *,
    marker_label: str,
    expected_name: str,
) -> Dict[str, Any]:
    """Decode and validate a bounded execution marker payload."""
    try:
        document = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"invalid execution ownership marker: {marker_label}"
        ) from exc
    if not isinstance(document, dict) or document.get("schema_version") not in {
        _EXECUTION_MARKER_SCHEMA,
        RECORD_SCHEMA,
    }:
        raise RuntimeError(f"invalid execution ownership marker: {marker_label}")
    if document.get("schema_version") == RECORD_SCHEMA:
        try:
            record = ExecutionRecord.from_dict(document)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"invalid execution ownership marker: {marker_label}"
            ) from exc
        document = record.to_dict()
    execution_id = document.get("execution_id")
    if (
        not isinstance(execution_id, str)
        or _validated_execution_id(execution_id) != execution_id
    ):
        raise RuntimeError(f"invalid execution ownership marker: {marker_label}")
    if expected_name != execution_id:
        raise RuntimeError(f"execution marker identity mismatch: {marker_label}")
    pipeline_name = document.get("pipeline_name")
    if not isinstance(pipeline_name, str):
        raise RuntimeError(f"invalid execution ownership marker: {marker_label}")
    try:
        validate_pipeline_id(pipeline_name)
    except ValueError as exc:
        raise RuntimeError(
            f"invalid execution ownership marker: {marker_label}"
        ) from exc
    if not isinstance(document.get("state"), str):
        raise RuntimeError(f"invalid execution ownership marker: {marker_label}")
    if not isinstance(document.get("submitted"), bool) or not isinstance(
        document.get("terminal"), bool
    ):
        raise RuntimeError(f"invalid execution ownership marker: {marker_label}")
    return document


def _open_directory_at(parent_descriptor: int, name: str, *, label: Path) -> int:
    """Open one real child directory relative to an already trusted directory."""
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if no_follow == 0:
        raise RuntimeError("secure directory-relative cleanup is unavailable")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | no_follow
    )
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    status = os.fstat(descriptor)
    if not stat.S_ISDIR(status.st_mode):
        os.close(descriptor)
        raise RuntimeError(f"execution path is not a real directory: {label}")
    return descriptor


def _read_execution_marker_at(
    execution_descriptor: int,
    *,
    execution_label: Path,
    expected_execution_id: str,
) -> Dict[str, Any]:
    """Read an execution marker relative to a held, no-follow root handle."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(_EXECUTION_MARKER, flags, dir_fd=execution_descriptor)
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or status.st_size > 65_536
        ):
            raise RuntimeError(f"invalid execution ownership marker: {execution_label}")
        payload = os.read(descriptor, 65_537)
        if len(payload) > 65_536:
            raise RuntimeError(
                f"execution ownership marker is too large: {execution_label}"
            )
    finally:
        os.close(descriptor)
    return _validate_execution_marker_payload(
        payload,
        marker_label=str(execution_label / _EXECUTION_MARKER),
        expected_name=expected_execution_id,
    )


def _cleanup_receipt_document(
    marker: Dict[str, Any],
    *,
    tombstone_identity: tuple[int, int],
    nonce: str,
) -> Dict[str, Any]:
    """Return the durable sibling receipt that authorizes tombstone retries."""
    return {
        "schema_version": _EXECUTION_CLEANUP_SCHEMA,
        "pipeline_name": marker["pipeline_name"],
        "execution_id": marker["execution_id"],
        "state": marker["state"],
        "submitted": marker["submitted"],
        "terminal": marker["terminal"],
        "cleanup_nonce": nonce,
        "tombstone_device": tombstone_identity[0],
        "tombstone_inode": tombstone_identity[1],
    }


def _validate_execution_cleanup_receipt(
    payload: bytes,
    *,
    receipt_label: str,
    expected_nonce: str,
    expected_execution_id: str,
) -> Dict[str, Any]:
    """Decode one cleanup receipt and validate identity-bound fields."""
    try:
        document = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"invalid execution cleanup receipt: {receipt_label}"
        ) from exc
    pipeline_name = (
        document.get("pipeline_name") if isinstance(document, dict) else None
    )
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != _EXECUTION_CLEANUP_SCHEMA
        or document.get("execution_id") != expected_execution_id
        or document.get("cleanup_nonce") != expected_nonce
        or not isinstance(pipeline_name, str)
        or not isinstance(document.get("state"), str)
        or not isinstance(document.get("submitted"), bool)
        or not isinstance(document.get("terminal"), bool)
        or not isinstance(document.get("tombstone_device"), int)
        or document["tombstone_device"] < 0
        or not isinstance(document.get("tombstone_inode"), int)
        or document["tombstone_inode"] <= 0
    ):
        raise RuntimeError(f"invalid execution cleanup receipt: {receipt_label}")
    try:
        validate_pipeline_id(pipeline_name)
    except ValueError as exc:
        raise RuntimeError(
            f"invalid execution cleanup receipt: {receipt_label}"
        ) from exc
    return document


def _read_execution_cleanup_receipt(
    path: Path,
    *,
    expected_nonce: str,
    expected_execution_id: str,
) -> Dict[str, Any]:
    """Read one bounded no-follow cleanup receipt by path on Windows."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        status = os.fstat(descriptor)
        path_status = path.lstat()
        if (
            not stat.S_ISREG(status.st_mode)
            or stat.S_ISLNK(path_status.st_mode)
            or (status.st_dev, status.st_ino)
            != (path_status.st_dev, path_status.st_ino)
            or status.st_nlink != 1
            or status.st_size > 65_536
        ):
            raise RuntimeError(f"invalid execution cleanup receipt: {path}")
        if os.name != "nt" and (
            stat.S_IMODE(status.st_mode) & 0o077
            or status.st_uid != _posix_effective_uid()
        ):
            raise RuntimeError(f"execution cleanup receipt is not private: {path}")
        payload = os.read(descriptor, 65_537)
        if len(payload) > 65_536:
            raise RuntimeError(f"execution cleanup receipt is too large: {path}")
    finally:
        os.close(descriptor)
    return _validate_execution_cleanup_receipt(
        payload,
        receipt_label=str(path),
        expected_nonce=expected_nonce,
        expected_execution_id=expected_execution_id,
    )


def _read_execution_cleanup_receipt_at(
    executions_descriptor: int,
    receipt_name: str,
    *,
    expected_nonce: str,
    expected_execution_id: str,
) -> Dict[str, Any]:
    """Read one cleanup receipt relative to a held executions directory."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(receipt_name, flags, dir_fd=executions_descriptor)
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or status.st_size > 65_536
        ):
            raise RuntimeError(f"invalid execution cleanup receipt: {receipt_name}")
        if (
            stat.S_IMODE(status.st_mode) & 0o077
            or status.st_uid != _posix_effective_uid()
        ):
            raise RuntimeError(
                f"execution cleanup receipt is not private: {receipt_name}"
            )
        payload = os.read(descriptor, 65_537)
        if len(payload) > 65_536:
            raise RuntimeError(
                f"execution cleanup receipt is too large: {receipt_name}"
            )
    finally:
        os.close(descriptor)
    return _validate_execution_cleanup_receipt(
        payload,
        receipt_label=receipt_name,
        expected_nonce=expected_nonce,
        expected_execution_id=expected_execution_id,
    )


def _write_execution_cleanup_receipt_at(
    executions_descriptor: int,
    receipt_name: str,
    document: Dict[str, Any],
) -> None:
    """Create one durable nonce-named receipt relative to a held root."""
    payload = (
        json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary_name = f".{receipt_name}.{uuid4().hex}.tmp"
    descriptor = os.open(
        temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
        dir_fd=executions_descriptor,
    )
    published = False
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("could not persist execution cleanup receipt")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            receipt_name,
            src_dir_fd=executions_descriptor,
            dst_dir_fd=executions_descriptor,
        )
        published = True
        os.fsync(executions_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not published:
            try:
                os.unlink(temporary_name, dir_fd=executions_descriptor)
            except FileNotFoundError:
                pass


def _cleanup_receipt_nonce(execution_id: str, receipt_name: str) -> Optional[str]:
    """Return the nonce encoded by one exact cleanup receipt name."""
    prefix = f".remove-{execution_id}-"
    suffix = ".json"
    if not receipt_name.startswith(prefix) or not receipt_name.endswith(suffix):
        return None
    nonce = receipt_name[len(prefix) : -len(suffix)]
    if len(nonce) != 32 or any(
        character not in "0123456789abcdef" for character in nonce
    ):
        return None
    return nonce


def _find_cleanup_receipts(
    executions_dir: Path,
    execution_id: str,
    *,
    executions_descriptor: Optional[int],
) -> list[tuple[Path, str]]:
    """Find the single identity-bearing receipt allowed for an execution."""
    receipts: list[tuple[Path, str]] = []
    if executions_descriptor is None:
        entries_context = os.scandir(executions_dir)
    else:
        entries_context = os.scandir(executions_descriptor)
    with entries_context as entries:
        for entry in entries:
            nonce = _cleanup_receipt_nonce(execution_id, entry.name)
            if nonce is not None:
                receipts.append((executions_dir / entry.name, nonce))
                if len(receipts) > 1:
                    raise RuntimeError(
                        f"multiple cleanup receipts exist: {execution_id}"
                    )
    return receipts


def _require_exact_cleanup_receipt(
    executions_dir: Path,
    execution_id: str,
    *,
    executions_descriptor: Optional[int],
    expected_path: Path,
    expected_nonce: str,
    expected_identity: tuple[int, int],
) -> None:
    """Reopen and revalidate the sole identity-bound transaction receipt."""
    receipts = _find_cleanup_receipts(
        executions_dir,
        execution_id,
        executions_descriptor=executions_descriptor,
    )
    if receipts != [(expected_path, expected_nonce)]:
        raise RuntimeError(f"execution cleanup receipt set changed: {execution_id}")
    receipt = _read_cleanup_receipt_bound(
        expected_path,
        expected_nonce,
        executions_descriptor=executions_descriptor,
        expected_execution_id=execution_id,
    )
    if _receipt_tombstone_identity(receipt) != expected_identity:
        raise RuntimeError(
            f"execution cleanup receipt identity changed: {execution_id}"
        )


def _entry_exists(
    path: Path,
    *,
    parent_descriptor: Optional[int],
) -> bool:
    """Check one child without following it when a trusted parent is available."""
    if parent_descriptor is None:
        return os.path.lexists(path)
    try:
        os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _inspect_execution_root(
    path: Path,
    *,
    executions_descriptor: Optional[int],
    expected_execution_id: str,
) -> tuple[Dict[str, Any], tuple[int, int]]:
    """Read a root marker and return the exact directory identity it belongs to."""
    if executions_descriptor is None:
        status = path.lstat()
        if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
            raise RuntimeError(f"execution root is not a real directory: {path}")
        marker = _read_execution_marker(
            path,
            expected_execution_id=expected_execution_id,
        )
        return marker, (status.st_dev, status.st_ino)
    root_descriptor = _open_directory_at(
        executions_descriptor,
        path.name,
        label=path,
    )
    try:
        status = os.fstat(root_descriptor)
        marker = _read_execution_marker_at(
            root_descriptor,
            execution_label=path,
            expected_execution_id=expected_execution_id,
        )
        return marker, (status.st_dev, status.st_ino)
    finally:
        os.close(root_descriptor)


def _execution_root_identity(
    path: Path,
    *,
    executions_descriptor: Optional[int],
) -> tuple[int, int]:
    """Return one no-follow execution directory identity."""
    if executions_descriptor is None:
        status = path.lstat()
        if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
            raise RuntimeError(f"execution root is not a real directory: {path}")
        return status.st_dev, status.st_ino
    root_descriptor = _open_directory_at(
        executions_descriptor,
        path.name,
        label=path,
    )
    try:
        status = os.fstat(root_descriptor)
        return status.st_dev, status.st_ino
    finally:
        os.close(root_descriptor)


def _read_cleanup_receipt_bound(
    path: Path,
    nonce: str,
    *,
    executions_descriptor: Optional[int],
    expected_execution_id: str,
) -> Dict[str, Any]:
    """Read a receipt through the trusted executions descriptor when possible."""
    if executions_descriptor is None:
        return _read_execution_cleanup_receipt(
            path,
            expected_nonce=nonce,
            expected_execution_id=expected_execution_id,
        )
    return _read_execution_cleanup_receipt_at(
        executions_descriptor,
        path.name,
        expected_nonce=nonce,
        expected_execution_id=expected_execution_id,
    )


def _receipt_tombstone_identity(receipt: Dict[str, Any]) -> tuple[int, int]:
    """Return the validated tombstone identity carried by a receipt."""
    return (
        int(receipt["tombstone_device"]),
        int(receipt["tombstone_inode"]),
    )


def _unlock_execution_input_for_cleanup(
    execution_root: Path,
    *,
    root_descriptor: Optional[int] = None,
) -> None:
    """Open the sealed input directory without following links and unlock it.

    Execution inputs are intentionally sealed read-only on POSIX.  Exact
    cleanup temporarily adds owner write/execute permission so the directory
    entries can be unlinked.  Once cleanup starts, the execution remains under
    its deterministic tombstone and is never returned to the live identity.
    """
    if os.name == "nt":
        return
    owns_root_descriptor = root_descriptor is None
    if root_descriptor is None:
        parent = execution_root.parent
        parent_descriptor = os.open(
            parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            root_descriptor = _open_directory_at(
                parent_descriptor,
                execution_root.name,
                label=execution_root,
            )
        finally:
            os.close(parent_descriptor)
    assert root_descriptor is not None
    descriptor = _open_directory_at(
        root_descriptor,
        "input",
        label=execution_root / "input",
    )
    try:
        status = os.fstat(descriptor)
        previous = stat.S_IMODE(status.st_mode)
        target = previous | stat.S_IWUSR | stat.S_IXUSR
        _posix_fchmod(descriptor, target)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
        if owns_root_descriptor:
            os.close(root_descriptor)


def _remove_directory_contents_at(
    directory_descriptor: int,
    *,
    directory_label: Path,
) -> None:
    """Recursively remove children relative to a held no-follow directory."""
    with os.scandir(directory_descriptor) as entries:
        snapshot = list(entries)
    for entry in snapshot:
        name = entry.name
        try:
            observed = entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(observed.st_mode) and not stat.S_ISLNK(observed.st_mode):
            child_label = directory_label / name
            child_descriptor = _open_directory_at(
                directory_descriptor,
                name,
                label=child_label,
            )
            try:
                child_identity = os.fstat(child_descriptor)
                if (child_identity.st_dev, child_identity.st_ino) != (
                    observed.st_dev,
                    observed.st_ino,
                ):
                    raise RuntimeError(
                        f"execution directory changed during cleanup: {child_label}"
                    )
                _remove_directory_contents_at(
                    child_descriptor,
                    directory_label=child_label,
                )
                current = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if (current.st_dev, current.st_ino) != (
                    child_identity.st_dev,
                    child_identity.st_ino,
                ):
                    raise RuntimeError(
                        f"execution directory changed during cleanup: {child_label}"
                    )
                os.rmdir(name, dir_fd=directory_descriptor)
            finally:
                os.close(child_descriptor)
        else:
            current = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (current.st_dev, current.st_ino) != (observed.st_dev, observed.st_ino):
                raise RuntimeError(
                    f"execution entry changed during cleanup: {directory_label / name}"
                )
            os.unlink(name, dir_fd=directory_descriptor)
    os.fsync(directory_descriptor)


_WINDOWS_DELETE = 0x00010000
_WINDOWS_FILE_LIST_DIRECTORY = 0x00000001
_WINDOWS_FILE_READ_ATTRIBUTES = 0x00000080
_WINDOWS_FILE_SHARE_READ = 0x00000001
_WINDOWS_FILE_SHARE_WRITE = 0x00000002
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WINDOWS_FILE_DISPOSITION_INFO = 4


def _open_windows_cleanup_entry(
    path: Path,
    *,
    expected_inode: int,
) -> tuple[int, int]:
    """Open and validate one Windows entry without delete sharing."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    raw_handle = kernel32.CreateFileW(
        str(path),
        _WINDOWS_DELETE | _WINDOWS_FILE_LIST_DIRECTORY | _WINDOWS_FILE_READ_ATTRIBUTES,
        _WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_WRITE,
        None,
        _WINDOWS_OPEN_EXISTING,
        _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if raw_handle == invalid_handle:
        error = ctypes.get_last_error()
        raise OSError(error, os.strerror(error), path)
    handle = int(raw_handle)
    try:
        attributes, inode = _windows_cleanup_handle_information(handle, path)
        if expected_inode <= 0 or inode != expected_inode:
            raise RuntimeError(f"Windows cleanup entry changed while opening: {path}")
        return handle, attributes
    except BaseException:
        _close_windows_cleanup_handle(handle)
        raise


def _windows_cleanup_handle_information(handle: int, path: Path) -> tuple[int, int]:
    """Return attributes and stable identity for one Windows cleanup handle."""
    import ctypes
    from ctypes import wintypes

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    information = _ByHandleFileInformation()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
        error = ctypes.get_last_error()
        raise OSError(error, os.strerror(error), path)
    inode = (int(information.file_index_high) << 32) | int(information.file_index_low)
    return int(information.file_attributes), inode


def _mark_windows_cleanup_handle_for_delete(handle: int, path: Path) -> None:
    """Mark one exact, already-open Windows entry for deletion on close."""
    import ctypes
    from ctypes import wintypes

    class _FileDispositionInformation(ctypes.Structure):
        _fields_ = [("delete_file", wintypes.BOOL)]

    disposition = _FileDispositionInformation(delete_file=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    if not kernel32.SetFileInformationByHandle(
        handle,
        _WINDOWS_FILE_DISPOSITION_INFO,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        error = ctypes.get_last_error()
        raise OSError(error, os.strerror(error), path)


def _close_windows_cleanup_handle(handle: int) -> None:
    """Close one Windows cleanup handle."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(handle)


def _remove_windows_directory_contents(path: Path) -> None:
    """Delete children by exact Windows handles without following reparse points."""
    with os.scandir(path) as entries:
        snapshot = list(entries)
    for entry in snapshot:
        child_path = path / entry.name
        observed = os.stat(child_path, follow_symlinks=False)
        if observed.st_ino <= 0:
            raise RuntimeError(
                f"Windows cleanup entry has no stable identity: {child_path}"
            )
        child_handle, attributes = _open_windows_cleanup_entry(
            child_path,
            expected_inode=observed.st_ino,
        )
        try:
            is_directory = bool(attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY)
            is_reparse = bool(attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT)
            if is_directory and not is_reparse:
                _remove_windows_directory_contents(child_path)
            _mark_windows_cleanup_handle_for_delete(child_handle, child_path)
        finally:
            _close_windows_cleanup_handle(child_handle)


def _remove_execution_tree(
    quarantine: Path,
    *,
    executions_descriptor: Optional[int],
    root_descriptor: Optional[int],
    windows_root_handle: Optional[int],
    root_identity: tuple[int, int],
) -> None:
    """Remove only the execution root whose held identity was validated."""
    if windows_root_handle is not None:
        attributes, inode = _windows_cleanup_handle_information(
            windows_root_handle,
            quarantine,
        )
        if (
            not attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY
            or attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
            or inode != root_identity[1]
        ):
            raise RuntimeError(
                f"cleanup tombstone changed before deletion: {quarantine}"
            )
        _remove_windows_directory_contents(quarantine)
        _mark_windows_cleanup_handle_for_delete(windows_root_handle, quarantine)
        return
    if executions_descriptor is None or root_descriptor is None:
        current = quarantine.lstat()
        if (
            not stat.S_ISDIR(current.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or (current.st_dev, current.st_ino) != root_identity
        ):
            raise RuntimeError(
                f"cleanup tombstone changed before deletion: {quarantine}"
            )
        shutil.rmtree(quarantine)
        _fsync_directory(quarantine.parent)
        return

    _remove_directory_contents_at(
        root_descriptor,
        directory_label=quarantine,
    )
    current = os.stat(
        quarantine.name,
        dir_fd=executions_descriptor,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != root_identity
    ):
        raise RuntimeError(f"cleanup tombstone changed before deletion: {quarantine}")
    os.rmdir(quarantine.name, dir_fd=executions_descriptor)
    os.fsync(executions_descriptor)


class Pipeline:
    """
    Consolidated pipeline management class.
    Handles pipeline creation, loading, running, and lifecycle management.
    """

    def __init__(self, name: str = None):
        """
        Initialize pipeline instance.

        :param name: Pipeline name (optional for new pipelines)
        """
        validated_name = validate_pipeline_id(name) if name is not None else None
        self.jarvis = Jarvis.get_instance()
        self.name = validated_name
        self.packages = []
        self.interceptors = {}  # Store pipeline-level interceptors by name
        self.env = {}
        self.created_at = None
        self.last_loaded_file = None
        # Structured metadata from the most recent scheduler submission.
        # This is populated only by the scheduler provider boundary; it is
        # never inferred from package or application stdout.
        self.last_submission = None
        # A scheduler execution may load a private working copy of the pipeline.
        # Saves from that process must never mutate the operator's named pipeline.
        self._execution_snapshot_dir = None
        self._execution_root = None
        self._execution_id = None

        # Container parameters
        self.container_image = ""  # Pre-built image to use
        self.container_uri = (
            ""  # Pre-built deploy image URI (skips build+deploy when set)
        )
        self.container_engine = "podman"  # Default container engine
        self.container_base = "iowarp/iowarp-build:latest"  # Base image
        self.container_ssh_port = 2222  # Default SSH port for containers
        self.container_extensions = {}  # Custom extensions to Docker compose file
        self.container_env = {}  # Environment variables injected into containers
        self.container_host_path = ""  # Docker host path prefix for DinD remapping
        self.container_workspace = ""  # Container workspace root for DinD remapping
        self.container_caps = []  # Apptainer --add-caps (e.g. SYS_ADMIN)
        self.container_binds = []  # Pipeline-level bind mounts (host:container)
        self.container_gpu = False  # --nv / GPU passthrough
        self.tmp_bind_root = None  # Per-host /tmp redirect root (apptainer)

        # Default deploy_mode propagated to packages that don't set
        # their own. None means no default ('default' deploy_mode).
        # Per-package install_method (not this field) selects the Installer.
        self.base_deploy_mode = None

        # Launcher overrides (set from YAML top-level keys ``ssh_cmd``,
        # ``pssh_cmd``, ``mpi_cmd``). None = built-in defaults.
        self.ssh_cmd = None
        self.pssh_cmd = None
        self.mpi_cmd = None

        # Hostfile parameter (None means use global jarvis hostfile)
        self.hostfile = None

        # Scheduler spec from pipeline YAML (None means no batch submission).
        # When set, ``submit()`` writes a job script under shared_dir that
        # builds the hostfile from the allocation and runs the pipeline.
        self.scheduler = None

        # Load existing pipeline if name is provided
        if validated_name:
            self.load()

    def get_hostfile(self) -> Hostfile:
        """
        Get the effective hostfile for this pipeline.
        Falls back to global jarvis hostfile if pipeline hostfile is not set.

        :return: Hostfile object
        """
        if self.hostfile:
            return self.hostfile
        return self.jarvis.hostfile

    def _scheduler_hostfile_path(self) -> str:
        """Resolve the hostfile path used by the scheduler block.

        Defaults to ``<pipeline shared_dir>/hostfile.txt`` when the
        scheduler block does not name one explicitly. Environment
        variables in the user-supplied value are expanded.
        """
        shared_dir = self.get_pipeline_shared_dir()
        spec_path = (self.scheduler or {}).get("hostfile")
        if spec_path:
            return os.path.expandvars(str(spec_path))
        return str(Path(shared_dir) / "hostfile.txt")

    def _apply_scheduler_hostfile(self):
        """Bind ``self.hostfile`` to the scheduler-owned hostfile path.

        The job script writes the hostfile at submit-time, so it may not
        exist yet — load with ``load_path=False`` and only resolve hosts
        on demand. This guarantees every package that consults
        ``self.hostfile`` (or ``get_hostfile()``) sees the same path the
        scheduler will populate inside the allocation.
        """
        path = self._scheduler_hostfile_path()
        if not self.scheduler:
            return
        self.scheduler["hostfile"] = path
        try:
            self.hostfile = Hostfile(
                path=path, load_path=Path(path).exists(), find_ips=False
            )
        except FileNotFoundError:
            self.hostfile = Hostfile(path=path, load_path=False, find_ips=False)

    def submit(
        self,
        submit: bool = True,
        wait: bool = False,
        execution_id: Optional[str] = None,
    ) -> ExecutionHandle:
        """Write the scheduler job script and (optionally) submit it.

        :param submit: when True, exec the scheduler's submit command
            (e.g. ``sbatch``); when False, only write the script and
            return its path so the caller can inspect it.
        :param wait: when True (and submit is True), ask the scheduler
            to block until the job finishes. SLURM maps this to
            ``sbatch --wait``; backends without an equivalent ignore it.
        :param execution_id: bounded caller-owned identity used to isolate the
            submitted pipeline, environment, script, and hostfile.
        :return: Queryable JARVIS execution handle. The generated script remains
            available as ``handle.script_path`` and in ``last_submission``.
        """
        if not self.scheduler:
            raise ValueError(
                "Pipeline has no scheduler block. Add `scheduler:` to the "
                "pipeline YAML before calling submit()."
            )
        if not self.name:
            raise ValueError("Pipeline name not set; cannot submit job.")

        from jarvis_cd.core.scheduler import make_scheduler

        shared_dir = self.get_pipeline_shared_dir()
        shared_dir.mkdir(parents=True, exist_ok=True)
        resolved_execution_id = _validated_execution_id(execution_id)
        scheduler_provider = self.scheduler.get("name")
        if not isinstance(scheduler_provider, str) or not scheduler_provider.strip():
            raise ValueError("scheduler.name is required (e.g. 'slurm')")
        store = self._execution_store()
        store.create(
            resolved_execution_id,
            mode="scheduler",
            scheduler_provider=scheduler_provider.strip().lower(),
        )
        execution_root = store.executions_dir / resolved_execution_id

        def persist_stage_exception(
            error: BaseException,
            stage: str,
            *,
            ambiguous: bool,
            accepted: bool = False,
            return_code: int = 1,
        ) -> None:
            """Best-effort durable state for one failed submission stage."""
            diagnostic = _bounded_execution_error(error)
            try:
                current = store.get(resolved_execution_id)
                metadata = {
                    "failure_stage": stage,
                    "submission_diagnostic": diagnostic,
                }
                if current.terminal and current.state != "scripted":
                    store.update(resolved_execution_id, metadata=metadata)
                elif ambiguous:
                    uncertain_state = (
                        "unknown"
                        if current.state in {"preparing", "submitting", "submitted"}
                        else None
                    )
                    store.update(
                        resolved_execution_id,
                        state=uncertain_state,
                        submitted=current.submitted or accepted,
                        terminal=None,
                        metadata=metadata,
                    )
                else:
                    store.update(
                        resolved_execution_id,
                        state="failed",
                        submitted=current.submitted or accepted,
                        terminal=True,
                        return_code=return_code if return_code != 0 else 1,
                        error=diagnostic,
                        metadata=metadata,
                    )
            except Exception as record_error:
                error.add_note(
                    f"could not persist scheduler stage {stage!r}: {record_error}"
                )
            try:
                self.save()
            except Exception as save_error:
                error.add_note(
                    f"could not save scheduler stage {stage!r}: {save_error}"
                )

        try:
            # Save current in-memory state before sealing the submission input.
            # The caller holds the per-pipeline MCP lock, so this cannot
            # overwrite a cooperating writer with stale state.
            self.save()
            scheduler_spec = dict(self.scheduler)
            scheduler_spec["hostfile"] = str(execution_root / "hostfile.txt")
            snapshot_dir, input_dir, snapshot_sha256 = self._write_execution_snapshot(
                execution_root,
                scheduler_spec,
            )
            sched = make_scheduler(
                scheduler_spec,
                execution_root,
                pipeline_name=self.name,
                pipeline_snapshot_dir=snapshot_dir,
            )
            script_path = sched.write_script()
        except BaseException as exc:
            try:
                store.update(
                    resolved_execution_id,
                    state="failed",
                    terminal=True,
                    return_code=1,
                    error=_bounded_execution_error(exc),
                    metadata={"failure_stage": "scheduler_preparation"},
                )
            except Exception as record_error:
                exc.add_note(
                    "could not persist failed scheduler preparation state: "
                    f"{record_error}"
                )
            raise
        logger.pipeline(f"Wrote scheduler script: {script_path}")
        logger.pipeline(f"Hostfile (built at job start): {sched.hostfile}")

        self.last_submission = {
            "schema_version": "jarvis.scheduler.submission.v1",
            "execution_id": resolved_execution_id,
            "provider": sched.NAME,
            "script_path": str(script_path),
            "hostfile_path": str(sched.hostfile),
            "pipeline_snapshot_path": str(snapshot_dir),
            "pipeline_input_path": str(input_dir),
            "execution_root_path": str(execution_root),
            "pipeline_snapshot_sha256": snapshot_sha256,
            "scheduler_job_id": None,
            "scheduler_cluster": None,
            "identity_source": None,
            "state": "scripted",
            "submitted": False,
            "wait": bool(wait),
            "terminal": not submit,
            "scheduler_stderr": None,
            # ``sbatch --wait`` reports the completed workload status through
            # the sbatch process.  Keep that raw value while separately
            # recording whether it is an observed terminal return code.
            "submission_returncode": None,
            "terminal_returncode": None,
            # A relay or site launcher may assign a provider-native job name
            # before submit. Persist it before invoking the scheduler so an
            # interrupted submit can be reconciled by an exact provider query.
            "reconciliation_marker": (
                self.scheduler.get("job_name")
                if isinstance(self.scheduler.get("job_name"), str)
                else None
            ),
        }
        try:
            self._update_execution_marker(execution_root)
            # Save local JARVIS state before the external side effect. The
            # relay's authenticated intent remains the reconciliation authority;
            # this record makes standalone recovery and diagnosis possible.
            self.save()
        except BaseException as exc:
            self.last_submission["state"] = "pre_submit_failed"
            self.last_submission["terminal"] = True
            persist_stage_exception(
                exc,
                "scheduler_pre_submit_persistence",
                ambiguous=False,
            )
            raise

        if not submit:
            return store.get(resolved_execution_id).handle

        from jarvis_cd.shell import Exec, LocalExecInfo

        argv = sched.submit_command(wait=wait)
        cmd = " ".join(shlex.quote(part) for part in argv)
        logger.pipeline(f"Submitting: {cmd}")
        try:
            result = Exec(cmd, LocalExecInfo(hide_output=True)).run()
        except BaseException as exc:
            self.last_submission["state"] = "submit_ambiguous"
            self.last_submission["terminal"] = False
            persist_stage_exception(
                exc,
                "scheduler_submit_side_effect",
                ambiguous=True,
            )
            raise
        exit_code = result.exit_code.get("localhost", 1)
        self.last_submission["submission_returncode"] = exit_code
        scheduler_stderr = _bounded_scheduler_stderr(result)
        self.last_submission["scheduler_stderr"] = scheduler_stderr
        diagnostic = (
            f"; scheduler stderr: {scheduler_stderr}" if scheduler_stderr else ""
        )
        stdout = result.stdout.get("localhost", "")
        try:
            provider_metadata = sched.parse_submission_output(stdout)
        except ValueError as parse_error:
            self.last_submission["state"] = (
                "submission_failed" if exit_code != 0 else "identity_failed"
            )
            self.last_submission["submitted"] = exit_code == 0
            self.last_submission["terminal"] = exit_code != 0
            if exit_code != 0:
                outcome_error = RuntimeError(
                    f"Scheduler submission failed (exit {exit_code}): {cmd}{diagnostic}"
                )
            else:
                outcome_error = RuntimeError(
                    "Scheduler accepted the submission but did not return a "
                    f"structured job identity{diagnostic}"
                )
            try:
                self._update_execution_marker(execution_root)
                self.save()
            except BaseException as persist_error:
                outcome_error.add_note(
                    f"could not persist scheduler parse outcome: {persist_error}"
                )
                persist_stage_exception(
                    outcome_error,
                    "scheduler_submission_identity_parse",
                    ambiguous=exit_code == 0,
                    accepted=exit_code == 0,
                    return_code=exit_code,
                )
            raise outcome_error from parse_error

        self.last_submission.update(provider_metadata)
        self.last_submission.update(
            {
                "submitted": True,
                "terminal": bool(wait),
                "terminal_returncode": exit_code if wait else None,
            }
        )
        try:
            self._update_execution_marker(execution_root)
            # Persist the provider-owned identity immediately after parsing;
            # later logging or state decoration must not reopen the orphan gap.
            self.save()
        except BaseException as exc:
            self.last_submission["state"] = "identity_persistence_failed"
            self.last_submission["terminal"] = False
            persist_stage_exception(
                exc,
                "scheduler_identity_persistence",
                ambiguous=True,
                accepted=True,
            )
            raise RuntimeError(
                "Scheduler accepted job "
                f"{self.last_submission['scheduler_job_id']}, but JARVIS could not "
                "persist its complete submission identity"
            ) from exc
        logger.pipeline(
            f"Scheduler job identity: {self.last_submission['scheduler_job_id']}"
        )
        if exit_code != 0:
            self.last_submission["state"] = (
                "workload_failed" if wait else "accepted_with_error"
            )
            if wait:
                outcome_error = RuntimeError(
                    "Scheduler job "
                    f"{self.last_submission['scheduler_job_id']} was accepted, "
                    f"but the workload failed (exit {exit_code}): "
                    f"{cmd}{diagnostic}"
                )
            else:
                outcome_error = RuntimeError(
                    "Scheduler job "
                    f"{self.last_submission['scheduler_job_id']} was accepted, "
                    f"but the submission command returned exit {exit_code}: "
                    f"{cmd}{diagnostic}"
                )
            try:
                self._update_execution_marker(execution_root)
                self.save()
            except BaseException as persist_error:
                outcome_error.add_note(
                    f"could not persist scheduler terminal outcome: {persist_error}"
                )
                persist_stage_exception(
                    outcome_error,
                    "scheduler_terminal_outcome_persistence",
                    ambiguous=not wait,
                    accepted=True,
                    return_code=exit_code,
                )
            raise outcome_error

        self.last_submission["state"] = "completed" if wait else "submitted"
        try:
            self._update_execution_marker(execution_root)
            self.save()
        except BaseException as exc:
            persist_stage_exception(
                exc,
                "scheduler_final_state_persistence",
                ambiguous=True,
                accepted=True,
            )
            raise RuntimeError(
                "Scheduler accepted job "
                f"{self.last_submission['scheduler_job_id']}, but JARVIS could not "
                "persist its final submission state"
            ) from exc
        return store.get(resolved_execution_id).handle

    def _execution_store(self) -> ExecutionStore:
        """Return the durable execution store for this pipeline identity."""
        if not self.name:
            raise ValueError("Pipeline name not set")
        execution_root = getattr(self, "_execution_root", None)
        if execution_root is not None:
            executions_dir = Path(execution_root).parent
        else:
            executions_dir = (
                self.jarvis.get_pipeline_shared_dir(self.name) / "executions"
            )
        return ExecutionStore(executions_dir, self.name)

    def get_execution(self, execution_id: str) -> ExecutionRecord:
        """Return the latest durable state for one exact execution identity."""
        return self._execution_store().get(execution_id)

    def list_executions(self) -> List[ExecutionRecord]:
        """Return all durable executions owned by this pipeline."""
        return self._execution_store().list()

    def get_execution_progress(
        self,
        execution_id: str,
    ) -> ExecutionProgressSnapshot:
        """Return validated package progress for one exact execution."""
        return self._execution_store().progress(execution_id)

    def _pipeline_storage_dir(self) -> Path:
        """Return the directory this Pipeline instance is allowed to mutate."""
        return self.get_pipeline_config_dir()

    def get_pipeline_config_dir(self) -> Path:
        """Return this invocation's pipeline configuration root."""
        snapshot_dir = getattr(self, "_execution_snapshot_dir", None)
        if snapshot_dir is not None:
            return Path(snapshot_dir)
        return self.jarvis.get_pipeline_dir(self.name)

    def get_pipeline_shared_dir(self) -> Path:
        """Return this invocation's isolated shared-data root."""
        execution_root = getattr(self, "_execution_root", None)
        if execution_root is not None:
            return Path(execution_root) / "shared"
        return self.jarvis.get_pipeline_shared_dir(self.name)

    def get_pipeline_private_dir(self) -> Path:
        """Return this invocation's isolated machine-private root."""
        execution_root = getattr(self, "_execution_root", None)
        if execution_root is not None:
            return Path(execution_root) / "private"
        return self.jarvis.get_pipeline_private_dir(self.name)

    @property
    def execution_container_name(self) -> str:
        """Return a bounded container/instance name unique to this execution."""
        execution_id = getattr(self, "_execution_id", None)
        if execution_id is None:
            return str(self.name)
        prefix = re.sub(r"[^A-Za-z0-9_.-]", "-", str(self.name))[:40] or "jarvis"
        digest = hashlib.sha256(str(execution_id).encode("ascii")).hexdigest()[:16]
        return f"{prefix}-{digest}"

    def _update_execution_marker(self, execution_root: Path) -> None:
        """Project scheduler compatibility metadata into its durable record."""
        submission = self.last_submission or {}
        execution_id = submission.get("execution_id")
        if not isinstance(execution_id, str) or execution_root.name != execution_id:
            raise RuntimeError("scheduler submission lost its execution identity")
        store = ExecutionStore(execution_root.parent, str(self.name))
        current = store.get(execution_id)
        scheduler_state = submission.get("state")
        if scheduler_state == "scripted":
            if submission.get("submitted") is True:
                if submission.get("wait") is True:
                    canonical_state = (
                        "completed"
                        if submission.get("terminal_returncode") == 0
                        else "failed"
                    )
                else:
                    canonical_state = "submitted"
            else:
                canonical_state = (
                    "scripted" if submission.get("terminal") else ("submitting")
                )
        else:
            canonical_state = {
                "submitted": "submitted",
                "completed": "completed",
                "submission_failed": "failed",
                "identity_failed": "unknown",
                "pre_submit_failed": "failed",
                "submit_ambiguous": "unknown",
                "identity_persistence_failed": "unknown",
                "workload_failed": "failed",
                "accepted_with_error": "submitted",
            }.get(scheduler_state)
        # A fast scheduler job may advance the record before the submitting
        # process has parsed the provider-native identity. Never regress that
        # independently persisted runtime state back to submitted. A terminal
        # runtime result is authoritative over sbatch's process status.
        runtime_terminal = current.terminal and current.state in {
            "completed",
            "failed",
            "canceled",
        }
        if runtime_terminal or (
            current.state == "running"
            and canonical_state in {None, "scripted", "submitting", "submitted"}
        ):
            canonical_state = None
        terminal = submission.get("terminal") is True
        if canonical_state is None:
            terminal_value = None
        else:
            terminal_value = terminal
        return_code = submission.get("terminal_returncode")
        if canonical_state == "failed" and not isinstance(return_code, int):
            submission_return_code = submission.get("submission_returncode")
            return_code = (
                submission_return_code
                if isinstance(submission_return_code, int)
                and not isinstance(submission_return_code, bool)
                and submission_return_code != 0
                else 1
            )
        if runtime_terminal or not isinstance(return_code, int):
            return_code = current.return_code
        scheduler_error = submission.get("scheduler_stderr")
        if (
            runtime_terminal
            or not isinstance(scheduler_error, str)
            or not scheduler_error
        ):
            scheduler_error = current.error
        provider = submission.get("provider")
        native_id = submission.get("scheduler_job_id")
        cluster = submission.get("scheduler_cluster")
        store.update(
            execution_id,
            state=canonical_state,
            submitted=current.submitted or submission.get("submitted") is True,
            terminal=terminal_value,
            scheduler_provider=(
                provider if provider is not None else current.scheduler_provider
            ),
            native_id=(
                native_id if native_id is not None else current.scheduler_native_id
            ),
            cluster=cluster if cluster is not None else current.cluster,
            return_code=return_code,
            error=scheduler_error,
            metadata={
                "submission": dict(submission),
                "script_path": submission.get("script_path"),
            },
        )

    def cleanup_executions(
        self,
        execution_ids: List[str],
        *,
        terminal_verified: Optional[List[str]] = None,
        force: bool = False,
    ) -> List[str]:
        """Remove explicitly named owned execution roots.

        Nonterminal submissions are refused unless their exact IDs appear in
        ``terminal_verified`` (after an operator checks scheduler state), or
        ``force`` is explicitly selected. There is deliberately no implicit
        age/count retention because an older execution may still be queued.
        """
        if getattr(self, "_execution_root", None) is not None:
            raise RuntimeError("cannot clean execution roots from a runtime snapshot")
        if not self.name:
            raise ValueError("Pipeline name not set")
        if (
            not isinstance(execution_ids, list)
            or not execution_ids
            or len(execution_ids) > _MAX_EXPLICIT_EXECUTION_CLEANUP
        ):
            raise ValueError(
                "execution_ids must contain 1-1024 explicit execution identities"
            )
        normalized = [_validated_execution_id(value) for value in execution_ids]
        if len(set(normalized)) != len(normalized):
            raise ValueError("execution_ids cannot contain duplicates")
        verified = {
            _validated_execution_id(value) for value in (terminal_verified or [])
        }
        if not verified.issubset(normalized):
            raise ValueError("terminal_verified must be a subset of execution_ids")

        executions_dir = self.jarvis.get_pipeline_shared_dir(self.name) / "executions"
        if not executions_dir.exists():
            return []
        executions_status = executions_dir.lstat()
        if not stat.S_ISDIR(executions_status.st_mode) or stat.S_ISLNK(
            executions_status.st_mode
        ):
            raise RuntimeError("pipeline executions path is not a real directory")

        executions_descriptor: Optional[int] = None
        if os.name != "nt":
            no_follow = getattr(os, "O_NOFOLLOW", 0)
            if no_follow == 0:
                raise RuntimeError("secure directory-relative cleanup is unavailable")
            executions_descriptor = os.open(
                executions_dir,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | no_follow,
            )
            descriptor_status = os.fstat(executions_descriptor)
            current_status = executions_dir.lstat()
            if (
                not stat.S_ISDIR(descriptor_status.st_mode)
                or stat.S_ISLNK(current_status.st_mode)
                or (descriptor_status.st_dev, descriptor_status.st_ino)
                != (current_status.st_dev, current_status.st_ino)
            ):
                os.close(executions_descriptor)
                raise RuntimeError("pipeline executions path changed during cleanup")
            if descriptor_status.st_uid != _posix_effective_uid():
                os.close(executions_descriptor)
                raise RuntimeError("pipeline executions path is not owner-controlled")
            _posix_fchmod(executions_descriptor, 0o700)
            os.fsync(executions_descriptor)

        candidates: List[
            tuple[
                str,
                Path,
                Path,
                Optional[Path],
                Optional[str],
                str,
                Dict[str, Any],
                Optional[tuple[int, int]],
            ]
        ] = []
        removed: List[str] = []
        execution_locks = ExitStack()
        try:
            # Acquire locks in stable order so overlapping multi-ID cleanups do
            # not deadlock. Hold them through ownership validation, tombstone
            # deletion, and cleanup-receipt removal.
            for execution_id in sorted(normalized):
                execution_locks.enter_context(
                    execution_transaction_lock(executions_dir, execution_id)
                )
            for execution_id in normalized:
                candidate = executions_dir / execution_id
                quarantine = executions_dir / f".remove-{execution_id}"
                receipts = _find_cleanup_receipts(
                    executions_dir,
                    execution_id,
                    executions_descriptor=executions_descriptor,
                )
                candidate_exists = _entry_exists(
                    candidate,
                    parent_descriptor=executions_descriptor,
                )
                quarantine_exists = _entry_exists(
                    quarantine,
                    parent_descriptor=executions_descriptor,
                )
                if candidate_exists:
                    if quarantine_exists or receipts:
                        raise RuntimeError(
                            f"execution and cleanup state both exist: {execution_id}"
                        )
                    cleanup_state = "live"
                    marker, expected_identity = _inspect_execution_root(
                        candidate,
                        executions_descriptor=executions_descriptor,
                        expected_execution_id=execution_id,
                    )
                    receipt = None
                    receipt_nonce = None
                elif quarantine_exists:
                    cleanup_state = "tombstone"
                    if receipts:
                        receipt, receipt_nonce = receipts[0]
                        marker = _read_cleanup_receipt_bound(
                            receipt,
                            receipt_nonce,
                            executions_descriptor=executions_descriptor,
                            expected_execution_id=execution_id,
                        )
                        expected_identity = _receipt_tombstone_identity(marker)
                        current_identity = _execution_root_identity(
                            quarantine,
                            executions_descriptor=executions_descriptor,
                        )
                        if current_identity != expected_identity:
                            raise RuntimeError(
                                f"cleanup receipt tombstone mismatch: {execution_id}"
                            )
                    else:
                        receipt = None
                        receipt_nonce = None
                        marker, expected_identity = _inspect_execution_root(
                            quarantine,
                            executions_descriptor=executions_descriptor,
                            expected_execution_id=execution_id,
                        )
                elif receipts:
                    cleanup_state = "receipt-only"
                    receipt, receipt_nonce = receipts[0]
                    marker = _read_cleanup_receipt_bound(
                        receipt,
                        receipt_nonce,
                        executions_descriptor=executions_descriptor,
                        expected_execution_id=execution_id,
                    )
                    expected_identity = None
                else:
                    raise RuntimeError(f"execution does not exist: {execution_id}")
                if marker["pipeline_name"] != self.name:
                    raise RuntimeError(
                        f"execution marker pipeline mismatch: {execution_id}"
                    )
                if (
                    not marker["terminal"]
                    and execution_id not in verified
                    and not force
                ):
                    raise RuntimeError(
                        f"execution is not terminal: {execution_id}; verify scheduler "
                        "state and pass terminal_verified explicitly"
                    )
                candidates.append(
                    (
                        execution_id,
                        candidate,
                        quarantine,
                        receipt,
                        receipt_nonce,
                        cleanup_state,
                        marker,
                        expected_identity,
                    )
                )

            for (
                execution_id,
                candidate,
                quarantine,
                receipt,
                receipt_nonce,
                cleanup_state,
                marker,
                expected_identity,
            ) in candidates:
                if cleanup_state == "live":
                    if receipt is not None or receipt_nonce is not None:
                        raise RuntimeError(
                            f"live execution has a cleanup receipt: {execution_id}"
                        )
                    if executions_descriptor is None:
                        os.replace(candidate, quarantine)
                        _fsync_directory(executions_dir)
                    else:
                        os.replace(
                            candidate.name,
                            quarantine.name,
                            src_dir_fd=executions_descriptor,
                            dst_dir_fd=executions_descriptor,
                        )
                        os.fsync(executions_descriptor)
                    cleanup_state = "tombstone"

                if cleanup_state == "tombstone":
                    root_descriptor: Optional[int] = None
                    windows_root_handle: Optional[int] = None
                    try:
                        if executions_descriptor is None:
                            quarantine_status = quarantine.lstat()
                            if not stat.S_ISDIR(
                                quarantine_status.st_mode
                            ) or stat.S_ISLNK(quarantine_status.st_mode):
                                raise RuntimeError(
                                    f"cleanup tombstone is not a real directory: "
                                    f"{execution_id}"
                                )
                            current_identity = (
                                quarantine_status.st_dev,
                                quarantine_status.st_ino,
                            )
                            if expected_identity is not None and (
                                current_identity != expected_identity
                            ):
                                raise RuntimeError(
                                    f"execution changed before cleanup: {execution_id}"
                                )
                            windows_root_handle, attributes = (
                                _open_windows_cleanup_entry(
                                    quarantine,
                                    expected_inode=current_identity[1],
                                )
                            )
                            if (
                                not attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY
                                or attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
                            ):
                                raise RuntimeError(
                                    f"cleanup tombstone is not a real directory: "
                                    f"{execution_id}"
                                )
                            if receipt is None:
                                marker = _read_execution_marker(
                                    quarantine,
                                    expected_execution_id=execution_id,
                                )
                        else:
                            root_descriptor = _open_directory_at(
                                executions_descriptor,
                                quarantine.name,
                                label=quarantine,
                            )
                            root_status = os.fstat(root_descriptor)
                            current_identity = (
                                root_status.st_dev,
                                root_status.st_ino,
                            )
                            if expected_identity is not None and (
                                current_identity != expected_identity
                            ):
                                raise RuntimeError(
                                    f"execution changed before cleanup: {execution_id}"
                                )
                            if receipt is None:
                                marker = _read_execution_marker_at(
                                    root_descriptor,
                                    execution_label=quarantine,
                                    expected_execution_id=execution_id,
                                )
                        if marker["pipeline_name"] != self.name or (
                            not marker["terminal"]
                            and execution_id not in verified
                            and not force
                        ):
                            raise RuntimeError(
                                f"execution changed before cleanup: {execution_id}"
                            )
                        if receipt is None:
                            receipt_nonce = uuid4().hex
                            receipt = executions_dir / (
                                f".remove-{execution_id}-{receipt_nonce}.json"
                            )
                            receipt_document = _cleanup_receipt_document(
                                marker,
                                tombstone_identity=current_identity,
                                nonce=receipt_nonce,
                            )
                            if executions_descriptor is None:
                                _atomic_json_dump(receipt, receipt_document)
                            else:
                                _write_execution_cleanup_receipt_at(
                                    executions_descriptor,
                                    receipt.name,
                                    receipt_document,
                                )
                        assert receipt is not None
                        assert receipt_nonce is not None
                        marker = _read_cleanup_receipt_bound(
                            receipt,
                            receipt_nonce,
                            executions_descriptor=executions_descriptor,
                            expected_execution_id=execution_id,
                        )
                        if (
                            marker["pipeline_name"] != self.name
                            or _receipt_tombstone_identity(marker) != current_identity
                        ):
                            raise RuntimeError(
                                f"execution cleanup receipt changed: {execution_id}"
                            )
                        _require_exact_cleanup_receipt(
                            executions_dir,
                            execution_id,
                            executions_descriptor=executions_descriptor,
                            expected_path=receipt,
                            expected_nonce=receipt_nonce,
                            expected_identity=current_identity,
                        )
                        try:
                            _unlock_execution_input_for_cleanup(
                                quarantine,
                                root_descriptor=root_descriptor,
                            )
                        except FileNotFoundError:
                            pass
                        _remove_execution_tree(
                            quarantine,
                            executions_descriptor=executions_descriptor,
                            root_descriptor=root_descriptor,
                            windows_root_handle=windows_root_handle,
                            root_identity=current_identity,
                        )
                    finally:
                        if root_descriptor is not None:
                            os.close(root_descriptor)
                        if windows_root_handle is not None:
                            _close_windows_cleanup_handle(windows_root_handle)

                assert receipt is not None
                assert receipt_nonce is not None
                _require_exact_cleanup_receipt(
                    executions_dir,
                    execution_id,
                    executions_descriptor=executions_descriptor,
                    expected_path=receipt,
                    expected_nonce=receipt_nonce,
                    expected_identity=_receipt_tombstone_identity(marker),
                )
                final_receipt = _read_cleanup_receipt_bound(
                    receipt,
                    receipt_nonce,
                    executions_descriptor=executions_descriptor,
                    expected_execution_id=execution_id,
                )
                if final_receipt["pipeline_name"] != self.name:
                    raise RuntimeError(
                        f"execution cleanup receipt changed: {execution_id}"
                    )
                if executions_descriptor is None:
                    receipt.unlink()
                    _fsync_directory(executions_dir)
                else:
                    os.unlink(receipt.name, dir_fd=executions_descriptor)
                    os.fsync(executions_descriptor)
                removed.append(execution_id)
            return removed
        finally:
            execution_locks.close()
            if executions_descriptor is not None:
                os.close(executions_descriptor)

    def _write_execution_snapshot(
        self,
        execution_root: Path,
        scheduler_spec: Dict[str, Any],
    ) -> tuple[Path, Path, str]:
        """Seal immutable input and create an isolated scheduler working copy."""
        source_dir = self._pipeline_storage_dir()
        if getattr(self, "_execution_snapshot_dir", None) is not None:
            raise RuntimeError(
                "cannot submit a scheduler job from an execution snapshot"
            )
        source_config = source_dir / "pipeline.yaml"
        try:
            with source_config.open("r", encoding="utf-8") as stream:
                pipeline_config = yaml.safe_load(stream)
        except (OSError, yaml.YAMLError) as exc:
            raise RuntimeError(
                f"could not read saved pipeline for snapshot: {exc}"
            ) from exc
        if not isinstance(pipeline_config, dict):
            raise RuntimeError("saved pipeline snapshot source is not a mapping")
        pipeline_config = dict(pipeline_config)
        pipeline_config["scheduler"] = dict(scheduler_spec)
        pipeline_config.pop("last_loaded_file", None)
        pipeline_config.pop("last_submission", None)

        input_dir = execution_root / "input"
        runtime_dir = execution_root / "runtime"
        shared_dir = execution_root / "shared"
        private_dir = execution_root / "private"
        input_dir.mkdir(mode=0o700)
        runtime_dir.mkdir(mode=0o700)
        shared_dir.mkdir(mode=0o700)
        private_dir.mkdir(mode=0o700)
        for directory in (input_dir, runtime_dir):
            _atomic_yaml_dump(directory / "pipeline.yaml", pipeline_config)
            _atomic_yaml_dump(directory / "environment.yaml", dict(self.env))

        digest = hashlib.sha256()
        for path in (input_dir / "pipeline.yaml", input_dir / "environment.yaml"):
            data = path.read_bytes()
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
            if os.name != "nt":
                path.chmod(0o400)
        if os.name != "nt":
            input_dir.chmod(0o500)
            runtime_dir.chmod(0o700)
            shared_dir.chmod(0o700)
            private_dir.chmod(0o700)
        _fsync_directory(input_dir)
        _fsync_directory(runtime_dir)
        _fsync_directory(shared_dir)
        _fsync_directory(private_dir)
        _fsync_directory(execution_root)
        return runtime_dir, input_dir, digest.hexdigest()

    def _hostfile_is_local_only(self, hf) -> bool:
        """True when every host in the hostfile is this machine (or hostfile
        is empty). Used to pick LocalExecInfo over PsshExecInfo for
        single-node/local runs without requiring sshd on :22. Real cluster
        hostfiles contain remote hostnames and return False."""
        if not hf or len(hf) == 0:
            return True
        local_names = {"localhost", "127.0.0.1", socket.gethostname()}
        return all(h in local_names for h in hf.hosts)

    def is_containerized(self) -> bool:
        """
        Check if this pipeline uses containers.

        :return: True if pipeline uses a container image
        """
        return bool(self.container_image)

    def _has_containerized_packages(self) -> bool:
        """
        Check if any package in the pipeline has deploy_mode: container.

        :return: True if at least one package uses container deployment
        """
        for pkg_def in self.packages:
            if pkg_def.get("config", {}).get("deploy_mode") == "container":
                return True
        return False

    def _apply_launcher_overrides(self):
        """
        Push pipeline-YAML-level ``ssh_cmd``/``pssh_cmd``/``mpi_cmd``
        into ExecInfo's class-level defaults so every subsequent
        SshExecInfo / PsshExecInfo / MpiExecInfo created by any package
        inherits the override automatically.

        Used to swap launchers without modifying packages — e.g.
        ``ssh_cmd: "env -u LD_LIBRARY_PATH ssh"`` keeps a conda env's
        libcrypto from being loaded into the host openssh, which
        otherwise aborts with an OpenSSL version mismatch.
        """
        from ..shell.exec_info import ExecInfo

        ExecInfo.set_launcher_defaults(
            ssh_cmd=self.ssh_cmd,
            pssh_cmd=self.pssh_cmd,
            mpi_cmd=self.mpi_cmd,
        )

    def _propagate_deploy_mode(self):
        """
        Propagate ``base_deploy_mode`` to every package/interceptor that
        doesn't set ``deploy_mode`` explicitly. YAML-level overrides win
        (e.g. a host-side FUSE/runtime alongside containerized workload
        pkgs).
        """
        if self.base_deploy_mode == "container":
            deploy_mode = "container"
        else:
            deploy_mode = "default"

        for pkg_def in self.packages:
            cfg = pkg_def.setdefault("config", {})
            cfg.setdefault("deploy_mode", deploy_mode)
        for idef in self.interceptors.values():
            cfg = idef.setdefault("config", {})
            cfg.setdefault("deploy_mode", deploy_mode)

    def create(self, pipeline_name: str):
        """
        Create a new pipeline.

        :param pipeline_name: Name of the pipeline to create
        """
        pipeline_name = validate_pipeline_id(pipeline_name)
        self.name = pipeline_name

        # Create all three directories for the pipeline
        pipeline_config_dir = self.jarvis.get_pipeline_dir(pipeline_name)
        pipeline_shared_dir = self.jarvis.get_pipeline_shared_dir(pipeline_name)
        pipeline_private_dir = self.jarvis.get_pipeline_private_dir(pipeline_name)

        pipeline_config_dir.mkdir(parents=True, exist_ok=True)
        pipeline_shared_dir.mkdir(parents=True, exist_ok=True)
        pipeline_private_dir.mkdir(parents=True, exist_ok=True)

        # Initialize pipeline state
        self.packages = []
        self.interceptors = {}
        self.env = {}
        self.created_at = str(Path().cwd())
        self.last_loaded_file = None
        self.last_submission = None

        # Save pipeline configuration and environment
        self.save()

        # Set as current pipeline
        self.jarvis.set_current_pipeline(pipeline_name)

        print(f"Created pipeline: {pipeline_name}")
        print(f"Config directory: {pipeline_config_dir}")
        print(f"Shared directory: {pipeline_shared_dir}")
        print(f"Private directory: {pipeline_private_dir}")

    def load(self, load_type: str = None, pipeline_file: str = None):
        """
        Load pipeline from file or current configuration.

        :param load_type: Type of pipeline file (e.g., 'yaml')
        :param pipeline_file: Path to pipeline file
        """
        if load_type and pipeline_file:
            self._load_from_file(load_type, pipeline_file)
        elif self.name:
            self._load_from_config()
        else:
            raise ValueError("No pipeline name or file specified")

    def save(self):
        """
        Save pipeline configuration and environment to separate files.

        Creates two YAML files:
        - pipeline.yaml: Contains package/interceptor configuration in script format
        - environment.yaml: Contains environment variables only
        """
        if not self.name:
            raise ValueError("Pipeline name not set")

        pipeline_dir = self._pipeline_storage_dir()
        pipeline_dir.mkdir(parents=True, exist_ok=True)

        # Create pipeline configuration in the SAME format as pipeline scripts
        # This allows code reuse by using the same parsing logic
        pipeline_config = {"name": self.name, "pkgs": [], "interceptors": []}

        # Add metadata fields
        if self.created_at:
            pipeline_config["created_at"] = self.created_at
        if self.last_loaded_file:
            pipeline_config["last_loaded_file"] = self.last_loaded_file
        if self.last_submission:
            pipeline_config["last_submission"] = self.last_submission
        # Add container parameters (always save, even if empty/default)
        pipeline_config["container_image"] = self.container_image
        pipeline_config["container_uri"] = self.container_uri
        pipeline_config["container_engine"] = self.container_engine
        pipeline_config["container_base"] = self.container_base
        pipeline_config["container_ssh_port"] = self.container_ssh_port
        if self.container_extensions:
            pipeline_config["container_extensions"] = self.container_extensions
        if self.container_env:
            pipeline_config["container_env"] = self.container_env
        if self.container_host_path:
            pipeline_config["container_host_path"] = self.container_host_path
        if self.container_workspace:
            pipeline_config["container_workspace"] = self.container_workspace
        if self.container_caps:
            pipeline_config["container_caps"] = self.container_caps
        if self.container_binds:
            pipeline_config["container_binds"] = self.container_binds
        pipeline_config["container_gpu"] = bool(self.container_gpu)
        if self.tmp_bind_root:
            pipeline_config["tmp_bind_root"] = self.tmp_bind_root

        # Add base_deploy_mode (default deploy_mode propagated to pkgs).
        if self.base_deploy_mode:
            pipeline_config["base_deploy_mode"] = self.base_deploy_mode

        # Persist launcher overrides so reloads round-trip and
        # downstream `ppl run` invocations see the same launcher even
        # without re-reading the original YAML.
        if self.ssh_cmd:
            pipeline_config["ssh_cmd"] = self.ssh_cmd
        if self.pssh_cmd:
            pipeline_config["pssh_cmd"] = self.pssh_cmd
        if self.mpi_cmd:
            pipeline_config["mpi_cmd"] = self.mpi_cmd

        # Persist scheduler block so reloads (and ``ppl print``) round-trip
        if self.scheduler:
            pipeline_config["scheduler"] = self.scheduler

        # Add hostfile parameter. The effective hostfile (pipeline override
        # if set, else the global jarvis hostfile) is always persisted to
        # <shared_dir>/hostfile so container mode can read it at the same
        # path the bind-mount exposes — no /tmp passthrough or external
        # bind required.
        # When a scheduler block is configured, the job script writes the
        # hostfile from the allocation at run time; we must not preempt it
        # with a stub here. The scheduler block carries the path itself.
        if self.scheduler:
            pipeline_config["hostfile"] = None
        else:
            effective_hostfile = self.hostfile or self.jarvis.hostfile
            if effective_hostfile and effective_hostfile.path:
                shared_dir = self.get_pipeline_shared_dir()
                shared_dir.mkdir(parents=True, exist_ok=True)
                hostfile_shared_path = str(shared_dir / "hostfile")
                effective_hostfile.save(hostfile_shared_path)
                pipeline_config["hostfile"] = hostfile_shared_path
            else:
                pipeline_config["hostfile"] = None

        # Convert packages to script format (pkg_type + config parameters)
        for pkg in self.packages:
            pkg_entry = {"pkg_type": pkg["pkg_type"]}
            # Add pkg_name if different from pkg_type
            if pkg["pkg_id"] != pkg["pkg_name"]:
                pkg_entry["pkg_name"] = pkg["pkg_id"]

            # Add all config parameters except internal ones
            config = pkg.get("config", {})
            for key, value in config.items():
                pkg_entry[key] = value

            pipeline_config["pkgs"].append(pkg_entry)

        # Convert interceptors to script format
        for interceptor_id, interceptor_def in self.interceptors.items():
            interceptor_entry = {"pkg_type": interceptor_def["pkg_type"]}
            # Add pkg_name if different from pkg_type
            if interceptor_id != interceptor_def["pkg_name"]:
                interceptor_entry["pkg_name"] = interceptor_id

            # Add all config parameters
            config = interceptor_def.get("config", {})
            for key, value in config.items():
                interceptor_entry[key] = value

            pipeline_config["interceptors"].append(interceptor_entry)

        # Save pipeline configuration (same format as pipeline scripts)
        config_file = pipeline_dir / "pipeline.yaml"
        _atomic_yaml_dump(config_file, pipeline_config)

        # Save environment to separate file
        env_file = pipeline_dir / "environment.yaml"
        _atomic_yaml_dump(env_file, self.env)

    def destroy(self, pipeline_name: str = None):
        """
        Destroy a pipeline by removing its directory and configuration.
        If no pipeline name is provided, destroy the current pipeline.

        :param pipeline_name: Name of pipeline to destroy (optional)
        """
        if getattr(self, "_execution_root", None) is not None:
            raise RuntimeError(
                "cannot destroy a named pipeline from a runtime snapshot"
            )

        # Determine which pipeline to destroy
        if pipeline_name is None:
            if not self.name:
                current_pipeline = self.jarvis.get_current_pipeline()
                if not current_pipeline:
                    print(
                        "No current pipeline to destroy. Specify a pipeline name or create/switch to one first."
                    )
                    return
                pipeline_name = current_pipeline
            else:
                pipeline_name = self.name

        pipeline_name = validate_pipeline_id(pipeline_name)
        target_pipeline_dir = self.jarvis.get_pipeline_dir(pipeline_name)
        executions_dir = (
            self.jarvis.get_pipeline_shared_dir(pipeline_name) / "executions"
        )
        if executions_dir.exists():
            status = executions_dir.lstat()
            if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
                raise RuntimeError("pipeline executions path is not a real directory")
            if any(
                not is_execution_transaction_lock(entry)
                for entry in executions_dir.iterdir()
            ):
                raise RuntimeError(
                    f"Pipeline '{pipeline_name}' still owns execution snapshots; "
                    "clean exact terminal execution IDs explicitly before destroy"
                )
        current_pipeline = self.jarvis.get_current_pipeline()
        is_current = pipeline_name == current_pipeline

        # Check if pipeline exists
        if not target_pipeline_dir.exists():
            print(f"Pipeline '{pipeline_name}' not found.")
            return

        # Try to clean packages first if pipeline is loadable
        config_file = target_pipeline_dir / "pipeline.yaml"
        if config_file.exists():
            try:
                # Load and clean pipeline
                temp_pipeline = Pipeline(pipeline_name)
                print("Attempting to clean package data before destruction...")
                temp_pipeline.clean()
            except Exception as e:
                print(f"Warning: Could not clean packages before destruction: {e}")

        # Remove pipeline directory
        import shutil

        try:
            shutil.rmtree(target_pipeline_dir)
            print(f"Destroyed pipeline: {pipeline_name}")

            # Clear current pipeline if we destroyed it
            if is_current:
                config = self.jarvis.config.copy()
                config["current_pipeline"] = None
                self.jarvis.save_config(config)
                print("Cleared current pipeline (destroyed pipeline was active)")

        except Exception as e:
            print(f"Error destroying pipeline directory: {e}")

    def start(self):
        """Start all packages in the pipeline"""
        from jarvis_cd.util.logger import logger

        logger.pipeline(f"Starting pipeline: {self.name}")

        # Store started instances for later access (e.g., _get_stat)
        self._started_instances = []

        # Check if pipeline is configured for containerized deployment
        if self.is_containerized():
            # Container deployment mode - deploy to all nodes in hostfile
            self._start_containerized_pipeline()
        else:
            # Standard deployment mode - start each package individually
            for pkg_def in self.packages:
                try:
                    # Print BEGIN message
                    logger.success(f"[{pkg_def['pkg_type']}] [START] BEGIN")

                    self._bind_package_execution_environment(pkg_def)
                    pkg_instance = self._load_package_instance(pkg_def, self.env)

                    # Apply interceptors to this package before starting
                    self._apply_interceptors_to_package(pkg_instance, pkg_def)

                    if hasattr(pkg_instance, "start"):
                        pkg_instance.start()
                    else:
                        logger.warning(
                            f"Package {pkg_def['pkg_id']} has no start method"
                        )

                    # Propagate environment changes to next packages
                    self.env.update(pkg_instance.env)

                    # Keep reference so _get_stat can access exec output
                    self._started_instances.append(pkg_instance)

                    # Print END message
                    logger.success(f"[{pkg_def['pkg_type']}] [START] END")

                except Exception as e:
                    logger.error(f"Error starting package {pkg_def['pkg_id']}: {e}")
                    raise RuntimeError(
                        f"Pipeline startup failed at package '{pkg_def['pkg_id']}': {e}"
                    ) from e

    def _bind_package_execution_environment(self, pkg_def: Dict[str, Any]) -> None:
        """Bind authoritative per-package progress paths to the active execution."""
        execution_root = getattr(self, "_execution_root", None)
        execution_id = getattr(self, "_execution_id", None)
        if execution_root is None or not isinstance(execution_id, str):
            return
        package_id = str(pkg_def.get("pkg_id") or pkg_def.get("pkg_type") or "package")
        package_name = str(pkg_def.get("pkg_type") or package_id)
        prefix = re.sub(r"[^A-Za-z0-9_-]", "-", package_id).strip("-")[:48]
        if not prefix:
            prefix = "package"
        digest = hashlib.sha256(package_id.encode("utf-8")).hexdigest()[:12]
        progress_dir = Path(execution_root) / "progress"
        progress_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            progress_dir.chmod(0o700)
        progress_path = progress_dir / f"{prefix}-{digest}.jsonl"
        package_config = pkg_def.get("config", {})
        package_containerized = isinstance(package_config, dict) and (
            package_config.get("deploy_mode") == "container"
        )
        progress_transport = (
            "stdout" if self.is_containerized() or package_containerized else "sidecar"
        )
        self.env.update(
            {
                "JARVIS_EXECUTION_ID": execution_id,
                "JARVIS_PACKAGE_ID": package_id,
                "JARVIS_PACKAGE_NAME": package_name,
                "JARVIS_PROGRESS_PATH": str(progress_path),
                "JARVIS_PROGRESS_TRANSPORT": progress_transport,
            }
        )
        record = self._execution_store().get(execution_id)
        progress_files = dict(record.metadata.get("progress_files", {}))
        progress_files[package_id] = {
            "filename": progress_path.name,
            "package_name": package_name,
        }
        self._execution_store().update(
            execution_id,
            metadata={"progress_files": progress_files},
        )

    def stop(self):
        """Stop all packages in the pipeline"""
        from jarvis_cd.util.logger import logger

        logger.pipeline(f"Stopping pipeline: {self.name}")

        # Check if pipeline is configured for containerized deployment
        if self.is_containerized():
            # Container deployment mode - stop containers on all nodes
            self._stop_containerized_pipeline()
        else:
            # Standard deployment mode - stop each package individually
            for pkg_def in reversed(self.packages):
                try:
                    # Print BEGIN message
                    logger.success(f"[{pkg_def['pkg_type']}] [STOP] BEGIN")

                    self._bind_package_execution_environment(pkg_def)
                    pkg_instance = self._load_package_instance(pkg_def, self.env)

                    if hasattr(pkg_instance, "stop"):
                        pkg_instance.stop()
                    else:
                        logger.warning(
                            f"Package {pkg_def['pkg_id']} has no stop method"
                        )

                    # Print END message
                    logger.success(f"[{pkg_def['pkg_type']}] [STOP] END")

                except Exception as e:
                    logger.error(f"Error stopping package {pkg_def['pkg_id']}: {e}")

    def kill(self):
        """Force kill all packages in the pipeline"""
        from jarvis_cd.util.logger import logger

        logger.pipeline(f"Killing pipeline: {self.name}")

        # Check if pipeline is configured for containerized deployment
        if self.is_containerized():
            # Container deployment mode - kill containers on all nodes
            self._kill_containerized_pipeline()
        else:
            # Standard deployment mode - kill each package individually
            for pkg_def in self.packages:
                try:
                    # Print BEGIN message
                    logger.success(f"[{pkg_def['pkg_type']}] [KILL] BEGIN")

                    self._bind_package_execution_environment(pkg_def)
                    pkg_instance = self._load_package_instance(pkg_def, self.env)

                    if hasattr(pkg_instance, "kill"):
                        pkg_instance.kill()
                    else:
                        logger.warning(
                            f"Package {pkg_def['pkg_id']} has no kill method"
                        )

                    # Print END message
                    logger.success(f"[{pkg_def['pkg_type']}] [KILL] END")

                except Exception as e:
                    logger.error(f"Error killing package {pkg_def['pkg_id']}: {e}")

    def status(self) -> str:
        """Get status of the pipeline and its packages"""
        from jarvis_cd.util.logger import logger

        if not self.name:
            return "No pipeline loaded"

        status_info = [f"Pipeline: {self.name}"]
        status_info.append("Packages:")

        # Show status for all packages
        for pkg_def in self.packages:
            try:
                # Print BEGIN message
                logger.success(f"[{pkg_def['pkg_type']}] [STATUS] BEGIN")

                pkg_instance = self._load_package_instance(pkg_def, self.env)

                if pkg_instance and hasattr(pkg_instance, "status"):
                    pkg_status = pkg_instance.status()
                    status_info.append(f"  {pkg_def['pkg_id']}: {pkg_status}")
                else:
                    status_info.append(f"  {pkg_def['pkg_id']}: no status method")

                # Print END message
                logger.success(f"[{pkg_def['pkg_type']}] [STATUS] END")

            except Exception as e:
                status_info.append(f"  {pkg_def['pkg_id']}: error ({e})")

        return "\n".join(status_info)

    def run(
        self,
        load_type: Optional[str] = None,
        pipeline_file: Optional[str] = None,
        execution_id: Optional[str] = None,
        wait: bool = True,
    ) -> ExecutionHandle:
        """
        Run the pipeline (start all packages, then stop them).
        Optionally load a pipeline file first.

        :param load_type: Type of pipeline file to load (e.g., 'yaml')
        :param pipeline_file: Path to pipeline file to load and run
        :param execution_id: Optional caller-selected JARVIS execution identity.
        :param wait: When false, launch an owned local execution process and
            return its queryable handle immediately.
        :return: Queryable handle for the live or completed execution.
        """
        if load_type and pipeline_file:
            self.load(load_type, pipeline_file)
        if not wait:
            return self._launch_direct_execution(execution_id)
        store = self._execution_store()
        original_execution_root = getattr(self, "_execution_root", None)
        original_execution_id = getattr(self, "_execution_id", None)
        original_execution_env = {
            name: self.env.get(name)
            for name in (
                "JARVIS_EXECUTION_ID",
                "JARVIS_PACKAGE_ID",
                "JARVIS_PACKAGE_NAME",
                "JARVIS_PROGRESS_PATH",
                "JARVIS_PROGRESS_TRANSPORT",
            )
        }
        snapshot_execution_id = getattr(self, "_execution_id", None)
        if getattr(self, "_execution_root", None) is not None:
            if execution_id is not None and execution_id != snapshot_execution_id:
                raise ValueError("scheduler snapshot execution_id cannot be overridden")
            if not isinstance(snapshot_execution_id, str):
                raise RuntimeError("scheduler snapshot has no execution identity")
            resolved_execution_id = snapshot_execution_id
            record = store.get(resolved_execution_id)
            if record.mode == "scheduler":
                store.update(
                    resolved_execution_id,
                    state="running",
                    submitted=True,
                    terminal=False,
                )
            elif record.mode == "direct":
                store.update(
                    resolved_execution_id,
                    state="running",
                    submitted=False,
                    terminal=False,
                )
            else:
                raise RuntimeError("execution snapshot record has the wrong mode")
        else:
            resolved_execution_id = _validated_execution_id(execution_id)
            record = store.create(resolved_execution_id, mode="direct")
            self._execution_root = store.executions_dir / resolved_execution_id
            self._execution_id = resolved_execution_id
            try:
                store.update(
                    resolved_execution_id,
                    state="running",
                    submitted=False,
                    terminal=False,
                )
            except BaseException:
                self._execution_root = original_execution_root
                self._execution_id = original_execution_id
                raise
        try:
            # Configure all packages before starting.
            # This runs _configure() on each package, which sets up
            # environment variables (e.g., CHI_SERVER_CONF) needed by start().
            self.configure_all_packages()

            self.start()
            logger.pipeline("Pipeline started successfully. Stopping packages...")
            self.stop()
            if record.mode == "scheduler":
                # The generated scheduler script owns the final transition so
                # failures in post-run hooks are included in the execution.
                return store.get(resolved_execution_id).handle
            return store.update(
                resolved_execution_id,
                state="completed",
                terminal=True,
                return_code=0,
                error=None,
            ).handle
        except BaseException as error:
            logger.error(f"Error during pipeline run: {error}")
            logger.info("Attempting to stop packages...")
            try:
                self.stop()
            except BaseException as stop_error:
                logger.error(f"Error during cleanup: {stop_error}")
                error.add_note(f"pipeline cleanup also failed: {stop_error}")
            try:
                store.update(
                    resolved_execution_id,
                    state="failed",
                    terminal=True,
                    return_code=1,
                    error=_bounded_execution_error(error),
                )
            except Exception as record_error:
                error.add_note(
                    f"could not persist failed execution state: {record_error}"
                )
            # Re-raise the original error after cleanup
            raise
        finally:
            if record.mode == "direct":
                self._execution_root = original_execution_root
                self._execution_id = original_execution_id
                for name, value in original_execution_env.items():
                    if value is None:
                        self.env.pop(name, None)
                    else:
                        self.env[name] = value

    def _launch_direct_execution(
        self,
        execution_id: Optional[str],
    ) -> ExecutionHandle:
        """Launch an isolated local execution and return before it completes."""
        if getattr(self, "_execution_root", None) is not None:
            raise RuntimeError("cannot background an execution snapshot")
        if not self.name:
            raise ValueError("Pipeline name not set")
        self.name = validate_pipeline_id(self.name)
        resolved_execution_id = _validated_execution_id(execution_id)
        self.save()
        store = self._execution_store()
        direct_launch = {
            "schema_version": "jarvis.direct-launch.v1",
            "phase": "launching",
            "launcher_pid": os.getpid(),
            "child_pid": None,
        }
        store.create(
            resolved_execution_id,
            mode="direct",
            metadata={"direct_launch": direct_launch},
        )
        execution_root = store.executions_dir / resolved_execution_id
        process: Optional[subprocess.Popen[Any]] = None
        try:
            runtime_dir, input_dir, snapshot_sha256 = self._write_execution_snapshot(
                execution_root,
                {},
            )
            prepare_direct_execution_lease(execution_root)
            stdout_path = execution_root / "stdout.log"
            stderr_path = execution_root / "stderr.log"
            direct_launch = {**direct_launch, "phase": "prepared"}
            store.update(
                resolved_execution_id,
                metadata={
                    "direct_launch": direct_launch,
                    "pipeline_snapshot_path": str(runtime_dir),
                    "pipeline_input_path": str(input_dir),
                    "pipeline_snapshot_sha256": snapshot_sha256,
                    "stdout_file": stdout_path.name,
                    "stderr_file": stderr_path.name,
                },
            )
            stdout_stream = stdout_path.open("ab", buffering=0)
            try:
                stderr_stream = stderr_path.open("ab", buffering=0)
            except BaseException:
                stdout_stream.close()
                raise
            try:
                if os.name != "nt":
                    stdout_path.chmod(0o600)
                    stderr_path.chmod(0o600)
                command = [
                    sys.executable,
                    "-m",
                    "jarvis_cd.core.execution",
                    "run-snapshot",
                    "--execution-root",
                    str(execution_root),
                    "--execution-id",
                    resolved_execution_id,
                    "--snapshot-dir",
                    str(runtime_dir),
                ]
                options: Dict[str, Any] = {
                    "stdin": subprocess.DEVNULL,
                    "stdout": stdout_stream,
                    "stderr": stderr_stream,
                    "close_fds": True,
                }
                if os.name == "nt":
                    options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    options["start_new_session"] = True
                launched_process = subprocess.Popen(command, **options)
                process = launched_process
            finally:
                stdout_stream.close()
                stderr_stream.close()
            direct_launch = {
                **direct_launch,
                "phase": "spawned",
                "child_pid": launched_process.pid,
            }
            try:
                store.update(
                    resolved_execution_id,
                    metadata={
                        "direct_launch": direct_launch,
                        "direct_process_id": launched_process.pid,
                    },
                )
            except BaseException:
                if launched_process.poll() is None:
                    launched_process.terminate()
                    try:
                        launched_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        launched_process.kill()
                        launched_process.wait(timeout=5)
                raise
        except BaseException as error:
            failed_launch = dict(direct_launch)
            failed_launch["phase"] = "failed"
            try:
                store.update(
                    resolved_execution_id,
                    state="failed",
                    terminal=True,
                    return_code=1,
                    error=_bounded_execution_error(error),
                    metadata={
                        "direct_launch": failed_launch,
                        "failure_stage": "direct_launch",
                    },
                )
            except Exception as record_error:
                error.add_note(
                    f"could not persist failed direct launch state: {record_error}"
                )
            raise
        assert process is not None
        return store.get(resolved_execution_id).handle

    def configure_all_packages(self):
        """
        Configure all packages and interceptors in the pipeline.
        This method loads each package/interceptor instance and calls its configure() method,
        then updates the pipeline environment and saves the configuration.
        """
        print("Configuring interceptors and packages...")

        # Configure interceptors
        for interceptor_id, interceptor_def in self.interceptors.items():
            self._configure_package_instance(interceptor_def, "interceptor")

        # Configure packages
        for pkg_def in self.packages:
            self._configure_package_instance(pkg_def, "package")

        # Save pipeline after configuration
        self.save()
        print("Pipeline configuration saved")

    def update(self):
        """
        Update pipeline by reconfiguring all packages with their existing configurations.
        This is useful when parts of the pipeline get corrupted or the environment changes.
        """
        # Reconfigure all packages with their existing configurations
        print("Reconfiguring pipeline packages with existing configurations...")
        self.configure_all_packages()

    def _configure_package_instance(self, pkg_def: Dict[str, Any], pkg_type_label: str):
        """
        Configure a single package or interceptor instance.

        :param pkg_def: Package definition dictionary
        :param pkg_type_label: Label for logging ("package" or "interceptor")
        """
        from jarvis_cd.util.logger import logger

        try:
            # Print BEGIN message
            logger.success(f"[{pkg_def['pkg_type']}] [CONFIGURE] BEGIN")

            pkg_instance = self._load_package_instance(pkg_def, self.env)
            if hasattr(pkg_instance, "configure"):
                # Configure the package with its config
                updated_config = pkg_instance.configure(**pkg_instance.config)

                # Update pkg_def with the final config from the package
                if updated_config:
                    pkg_def["config"] = updated_config
                else:
                    pkg_def["config"] = pkg_instance.config.copy()

                # Update the package environment in the pipeline's env
                self.env.update(pkg_instance.env)

            # Print END message
            logger.success(f"[{pkg_def['pkg_type']}] [CONFIGURE] END")

        except Exception as e:
            import traceback

            logger.error(f"Error configuring {pkg_type_label} {pkg_def['pkg_id']}: {e}")
            logger.error("Full traceback:")
            traceback.print_exc()
            raise

    def append(
        self,
        package_spec: str,
        package_alias: Optional[str] = None,
        config_args: Optional[List[str]] = None,
    ):
        """
        Append a package to the pipeline.

        :param package_spec: Package specification (repo.pkg or just pkg)
        :param package_alias: Optional alias for the package
        :param config_args: Optional configuration arguments as command-line args (e.g., ['--out_file=/path', 'nprocs=4'])
        """
        if not self.name:
            raise ValueError("No pipeline loaded. Create one with create() first")

        # Parse package specification
        if "." in package_spec:
            repo_name, pkg_name = package_spec.split(".", 1)
        else:
            # Try to find package in available repos
            pkg_name = package_spec
            full_spec = self.jarvis.find_package(pkg_name)
            if not full_spec:
                raise ValueError(f"Package not found: {pkg_name}")
            package_spec = full_spec

        # Determine package ID
        if package_alias:
            pkg_id = package_alias
        else:
            pkg_id = pkg_name

        # Check for duplicate package IDs
        existing_ids = [pkg["pkg_id"] for pkg in self.packages]
        if pkg_id in existing_ids:
            raise ValueError(f"Package ID already exists in pipeline: {pkg_id}")

        # Get default configuration from package
        default_config = self._get_package_default_config(package_spec)

        # Build the package entry (needed for loading package instance)
        package_entry = {
            "pkg_type": package_spec,
            "pkg_id": pkg_id,
            "pkg_name": pkg_name,
            "global_id": f"{self.name}.{pkg_id}",
            "config": default_config,
        }

        # Apply configuration arguments if provided (before validation)
        if config_args:
            # Load package instance
            pkg_instance = self._load_package_instance(package_entry, self.env)

            try:
                # Use PkgArgParse to parse and convert arguments
                argparse = pkg_instance.get_argparse()
                # Parse arguments - prepend 'configure' command
                argparse.parse(["configure"] + config_args)
                converted_args = argparse.kwargs

                # Update package configuration with converted values
                package_entry["config"].update(converted_args)
            except Exception as e:
                print(f"Warning: Error parsing configuration arguments: {e}")
                # Show available configuration options
                argparse = pkg_instance.get_argparse()
                argparse.print_help("configure")

        # Validate that all required parameters have values (after applying config_args)
        self._validate_required_config(package_spec, package_entry["config"])

        # Add package to pipeline
        self.packages.append(package_entry)

        # Save updated configuration
        self.save()

        print(f"Added package {package_spec} as {pkg_id} to pipeline")

    def rm(self, package_spec: str):
        """
        Remove a package from the pipeline.

        :param package_spec: Package specification to remove (pkg_id)
        """
        # Find and remove the package
        package_found = False

        for i, pkg_def in enumerate(self.packages):
            if pkg_def["pkg_id"] == package_spec:
                removed_package = self.packages.pop(i)
                package_found = True
                break

        if not package_found:
            # List available packages to help the user
            available_ids = [pkg["pkg_id"] for pkg in self.packages]
            if available_ids:
                print(f"Package '{package_spec}' not found in pipeline.")
                print(f"Available packages: {', '.join(available_ids)}")
            else:
                print("No packages in pipeline.")
            return

        # Save updated configuration
        self.save()

        print(
            f"Removed package '{removed_package['pkg_id']}' ({removed_package['pkg_type']}) from pipeline '{self.name}'"
        )

    def clean(self):
        """Clean all data for packages in the pipeline"""
        from jarvis_cd.util.logger import logger

        logger.pipeline(f"Cleaning pipeline: {self.name}")

        # Clean each package
        for pkg_def in self.packages:
            try:
                # Print BEGIN message
                logger.success(f"[{pkg_def['pkg_type']}] [CLEAN] BEGIN")

                pkg_instance = self._load_package_instance(pkg_def, self.env)

                if hasattr(pkg_instance, "clean"):
                    pkg_instance.clean()
                else:
                    logger.warning(f"Package {pkg_def['pkg_id']} has no clean method")

                # Print END message
                logger.success(f"[{pkg_def['pkg_type']}] [CLEAN] END")

            except Exception as e:
                logger.error(f"Error cleaning package {pkg_def['pkg_id']}: {e}")

    def configure_package(self, pkg_id: str, config_args: List[str]):
        """
        Configure a specific package in the pipeline.

        :param pkg_id: Package ID to configure
        :param config_args: Configuration arguments as command-line args (e.g., ['--nprocs', '4', 'block=32m'])
        """
        # Find package in pipeline
        pkg_def = None
        for pkg in self.packages:
            if pkg["pkg_id"] == pkg_id:
                pkg_def = pkg
                break

        if not pkg_def:
            raise ValueError(f"Package not found: {pkg_id}")

        # Load package instance
        pkg_instance = self._load_package_instance(pkg_def, self.env)

        try:
            # Use PkgArgParse to parse and convert arguments
            argparse = pkg_instance.get_argparse()

            # Get defaults by parsing with no user args
            argparse.parse(["configure"])
            defaults = argparse.kwargs.copy()

            # Parse with user args
            argparse.parse(["configure"] + config_args)
            converted_args = argparse.kwargs

            # Only keep args the user explicitly provided (differ from defaults)
            explicit_args = {
                k: v
                for k, v in converted_args.items()
                if k not in defaults or defaults[k] != v
            }

            # Update package configuration with only explicit values
            pkg_def["config"].update(explicit_args)

            # Configure the package instance
            if hasattr(pkg_instance, "configure"):
                pkg_instance.configure(**explicit_args)
                print(f"Configured package {pkg_id} successfully")
            else:
                print(f"Package {pkg_id} has no configure method")

            # Save updated pipeline
            self.save()
            print(f"Saved configuration for {pkg_id}")

        except Exception as e:
            print(f"Error configuring package {pkg_id}: {e}")
            # Show available configuration options
            argparse = pkg_instance.get_argparse()
            argparse.print_help("configure")

    def show_package_readme(self, pkg_id: str):
        """
        Show README for a specific package in the pipeline.

        :param pkg_id: Package ID to show README for
        """
        # Find package in pipeline
        pkg_def = None
        for pkg in self.packages:
            if pkg["pkg_id"] == pkg_id:
                pkg_def = pkg
                break

        if not pkg_def:
            raise ValueError(f"Package not found: {pkg_id}")

        # Load package instance and delegate to it
        try:
            pkg_instance = self._load_package_instance(pkg_def, self.env)
            pkg_instance.show_readme()
        except Exception as e:
            print(f"Error showing README for package {pkg_id}: {e}")

    def show_package_build_script(self, pkg_id: str):
        """Print the build.sh script for a package within this pipeline."""
        pkg_def = next((p for p in self.packages if p["pkg_id"] == pkg_id), None)
        if not pkg_def:
            raise ValueError(f"Package not found: {pkg_id}")
        try:
            pkg_instance = self._load_package_instance(pkg_def, self.env)
            pkg_instance.show_build_script()
        except Exception as e:
            print(f"Error showing build script for package {pkg_id}: {e}")

    def show_package_deploy_dockerfile(self, pkg_id: str):
        """Print Dockerfile.deploy for a package within this pipeline."""
        pkg_def = next((p for p in self.packages if p["pkg_id"] == pkg_id), None)
        if not pkg_def:
            raise ValueError(f"Package not found: {pkg_id}")
        try:
            pkg_instance = self._load_package_instance(pkg_def, self.env)
            pkg_instance.show_deploy_dockerfile()
        except Exception as e:
            print(f"Error showing deploy Dockerfile for package {pkg_id}: {e}")

    def show_package_paths(self, pkg_id: str, path_flags: Dict[str, bool]):
        """
        Show directory paths for a specific package in the pipeline.

        :param pkg_id: Package ID to show paths for
        :param path_flags: Dictionary of path flags to show
        """
        # Find package in pipeline
        pkg_def = None
        for pkg in self.packages:
            if pkg["pkg_id"] == pkg_id:
                pkg_def = pkg
                break

        if not pkg_def:
            raise ValueError(f"Package not found: {pkg_id}")

        # Load package instance and delegate to it
        try:
            pkg_instance = self._load_package_instance(pkg_def, self.env)
            pkg_instance.show_paths(path_flags)
        except Exception as e:
            print(f"Error showing paths for package {pkg_id}: {e}")

    def _load_from_config(self):
        """
        Load pipeline from its configuration files.

        Loads from two separate files:
        - pipeline.yaml: Contains package/interceptor configuration in script format
        - environment.yaml: Contains environment variables only
        """
        pipeline_dir = self.jarvis.get_pipeline_dir(self.name)
        config_file = pipeline_dir / "pipeline.yaml"

        if not config_file.exists():
            raise FileNotFoundError(f"Pipeline configuration not found: {config_file}")

        # Load pipeline configuration (in script format)
        with open(config_file, "r") as f:
            pipeline_config = yaml.safe_load(f)
        if not isinstance(pipeline_config, dict):
            raise ValueError("pipeline configuration must be a mapping")
        stored_name = validate_pipeline_id(pipeline_config.get("name"))
        if stored_name != self.name:
            raise ValueError("pipeline configuration identity mismatch")

        # Extract metadata
        self.created_at = pipeline_config.get("created_at")
        self.last_loaded_file = pipeline_config.get("last_loaded_file")
        self.last_submission = pipeline_config.get("last_submission")

        # Load container parameters
        self.container_image = pipeline_config.get("container_image", "")
        self.container_uri = pipeline_config.get("container_uri", "")
        self.container_engine = pipeline_config.get("container_engine", "podman")
        self.container_base = pipeline_config.get(
            "container_base", "iowarp/iowarp-build:latest"
        )
        self.container_ssh_port = pipeline_config.get("container_ssh_port", 2222)
        self.container_extensions = pipeline_config.get("container_extensions", {})
        self.container_env = pipeline_config.get("container_env", {})
        self.container_host_path = pipeline_config.get("container_host_path", "")
        self.container_workspace = pipeline_config.get("container_workspace", "")
        self.container_caps = pipeline_config.get("container_caps", [])
        self.container_binds = Pipeline._expand_env_in_config(
            pipeline_config.get("container_binds", []) or []
        )
        self.container_gpu = bool(pipeline_config.get("container_gpu", False))
        tmp_bind_root_raw = pipeline_config.get("tmp_bind_root")
        self.tmp_bind_root = (
            os.path.expandvars(tmp_bind_root_raw)
            if isinstance(tmp_bind_root_raw, str) and tmp_bind_root_raw
            else None
        )

        # Launcher overrides (top-level YAML keys). None = use built-in
        # defaults (ssh / pssh / mpiexec). Typical override pattern:
        #   ssh_cmd: "env -u LD_LIBRARY_PATH ssh"
        # Wrapping ssh in env -u keeps a conda env's libcrypto out of
        # the host openssh, which otherwise dies on an ABI mismatch.
        self.ssh_cmd = pipeline_config.get("ssh_cmd", None)
        self.pssh_cmd = pipeline_config.get("pssh_cmd", None)
        self.mpi_cmd = pipeline_config.get("mpi_cmd", None)
        self._apply_launcher_overrides()

        # Load base_deploy_mode. The legacy ``install_manager`` key is
        # still read once with a deprecation warning so saved pipelines
        # from before the rename keep loading.
        self.base_deploy_mode = pipeline_config.get("base_deploy_mode", None)
        if self.base_deploy_mode is None and "install_manager" in pipeline_config:
            self.base_deploy_mode = pipeline_config["install_manager"]
            print(
                "Warning: 'install_manager' is deprecated; "
                "rename to 'base_deploy_mode' (per-package "
                "'install_method' now selects the installer)."
            )

        # Load hostfile parameter (None means use global jarvis hostfile)
        hostfile_path = pipeline_config.get("hostfile")
        if hostfile_path:
            self.hostfile = Hostfile(path=os.path.expandvars(hostfile_path))
        else:
            self.hostfile = None

        # Load scheduler spec (mirrors _load_from_file). The hostfile
        # override is re-applied so reloading a saved pipeline keeps the
        # same scheduler-aware hostfile path.
        scheduler_spec = pipeline_config.get("scheduler")
        if scheduler_spec:
            self.scheduler = self._expand_env_in_config(dict(scheduler_spec))
            self._apply_scheduler_hostfile()
        else:
            self.scheduler = None

        # Initialize packages and interceptors
        self.packages = []
        self.interceptors = {}

        # Process interceptors from script format
        interceptors_list = pipeline_config.get("interceptors", [])
        for interceptor_def in interceptors_list:
            interceptor_id = interceptor_def.get(
                "pkg_name", interceptor_def["pkg_type"].split(".")[-1]
            )
            interceptor_entry = self._process_package_definition(
                interceptor_def, interceptor_id
            )
            self.interceptors[interceptor_id] = interceptor_entry

        # Process packages from script format
        for pkg_def in pipeline_config.get("pkgs", []):
            pkg_id = pkg_def.get("pkg_name", pkg_def["pkg_type"].split(".")[-1])
            package_entry = self._process_package_definition(pkg_def, pkg_id)
            self.packages.append(package_entry)

        # Load environment from separate file
        env_file = pipeline_dir / "environment.yaml"
        if env_file.exists():
            with open(env_file, "r") as f:
                env_config = yaml.safe_load(f)
                if env_config:
                    self.env = env_config
                else:
                    self.env = {}
        else:
            self.env = {}

    def _load_from_file(self, load_type: str, pipeline_file: str):
        """Load pipeline from a file"""
        if load_type != "yaml":
            raise ValueError(f"Unsupported pipeline file type: {load_type}")

        pipeline_file = Path(pipeline_file)
        if not pipeline_file.exists():
            raise FileNotFoundError(f"Pipeline file not found: {pipeline_file}")

        snapshot_environment = os.getenv(_SNAPSHOT_ENVIRONMENT)
        snapshot_env_file = None
        snapshot_marker = None
        if snapshot_environment:
            try:
                snapshot_dir = Path(snapshot_environment).resolve(strict=True)
                resolved_pipeline_file = pipeline_file.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise ValueError(f"invalid scheduler pipeline snapshot: {exc}") from exc
            expected_pipeline_file = snapshot_dir / "pipeline.yaml"
            if snapshot_dir.name != "runtime" or resolved_pipeline_file != (
                expected_pipeline_file
            ):
                raise ValueError(
                    "scheduler pipeline snapshot must contain the requested pipeline.yaml"
                )
            snapshot_env_file = snapshot_dir / "environment.yaml"
            try:
                resolved_env_file = snapshot_env_file.resolve(strict=True)
            except OSError as exc:
                raise ValueError(
                    f"scheduler pipeline snapshot omitted environment.yaml: {exc}"
                ) from exc
            if (
                resolved_env_file != snapshot_env_file
                or not snapshot_env_file.is_file()
            ):
                raise ValueError("scheduler pipeline snapshot omitted environment.yaml")
            try:
                snapshot_marker = _read_execution_marker(snapshot_dir.parent)
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError(f"invalid scheduler execution root: {exc}") from exc
            self._execution_snapshot_dir = snapshot_dir
            self._execution_root = snapshot_dir.parent
            self._execution_id = snapshot_marker["execution_id"]

        # Load pipeline definition
        with open(pipeline_file, "r") as f:
            pipeline_def = yaml.safe_load(f)
        if not isinstance(pipeline_def, dict):
            raise ValueError("pipeline YAML must be a mapping")

        self.name = validate_pipeline_id(pipeline_def.get("name", pipeline_file.stem))
        if (
            snapshot_marker is not None
            and snapshot_marker["pipeline_name"] != self.name
        ):
            raise ValueError("scheduler execution marker did not match pipeline name")

        # Handle environment - can be a string (named env) or missing (auto-build)
        # Inline dictionaries are NOT supported
        env_field = pipeline_def.get("env")

        if snapshot_env_file is not None:
            try:
                with snapshot_env_file.open("r", encoding="utf-8") as stream:
                    snapshot_env = yaml.safe_load(stream)
            except (OSError, yaml.YAMLError) as exc:
                raise ValueError(
                    f"could not read scheduler environment snapshot: {exc}"
                ) from exc
            if not isinstance(snapshot_env, dict) or not all(
                isinstance(name, str) and isinstance(value, str)
                for name, value in snapshot_env.items()
            ):
                raise ValueError(
                    "scheduler environment snapshot is not a string mapping"
                )
            self.env = dict(snapshot_env)
        elif env_field is None:
            # No env field defined - automatically build environment
            try:
                from jarvis_cd.core.environment import EnvironmentManager

                env_manager = EnvironmentManager(self.jarvis)
                self.env = env_manager._capture_current_environment()
                print(
                    f"Auto-built environment with {len(self.env)} variables (no 'env' field in pipeline)"
                )
            except Exception as e:
                print(f"Warning: Could not auto-build environment: {e}")
                self.env = {}
        elif isinstance(env_field, str):
            # Reference to named environment (deprecated)
            print(
                "Warning: String env references are deprecated. Use inline dict overrides instead."
            )
            env_name = env_field
            try:
                from jarvis_cd.core.environment import EnvironmentManager

                env_manager = EnvironmentManager(self.jarvis)
                self.env = env_manager.load_named_environment(env_name)
            except Exception:
                # Named environment doesn't exist - build it automatically
                print(
                    f"Named environment '{env_name}' does not exist. Building it now..."
                )
                try:
                    from jarvis_cd.core.environment import EnvironmentManager

                    env_manager = EnvironmentManager(self.jarvis)
                    # Build the named environment from current environment (no additional args)
                    env_manager.build_named_environment(env_name, [])
                    # Now load the newly created environment
                    self.env = env_manager.load_named_environment(env_name)
                    print(
                        f"Built named environment '{env_name}' with {len(self.env)} variables"
                    )
                except Exception as build_error:
                    print(
                        f"Warning: Could not build named environment '{env_name}': {build_error}"
                    )
                    self.env = {}
        elif isinstance(env_field, dict):
            # Inline dict: auto-capture environment, then overlay user overrides
            try:
                from jarvis_cd.core.environment import EnvironmentManager

                env_manager = EnvironmentManager(self.jarvis)
                self.env = env_manager._capture_current_environment()
            except Exception as e:
                print(f"Warning: Could not auto-build environment: {e}")
                self.env = {}
            # Apply user overrides from YAML
            self.env.update(env_field)
            print(
                f"Built environment with {len(self.env)} variables ({len(env_field)} overrides from pipeline YAML)"
            )
        else:
            raise ValueError(
                f"Invalid 'env' field type: {type(env_field).__name__}. "
                "The 'env' field must be either a string (named environment) or omitted (auto-build)."
            )

        # Initialize other attributes
        self.created_at = str(Path().cwd())
        self.last_loaded_file = str(pipeline_file.absolute())
        self.packages = []
        self.interceptors = {}  # Store pipeline-level interceptors by name

        # Load container parameters
        self.container_image = pipeline_def.get("container_image", "")
        self.container_uri = pipeline_def.get("container_uri", "")
        self.container_engine = pipeline_def.get("container_engine", "podman")
        self.container_base = pipeline_def.get(
            "container_base", "iowarp/iowarp-build:latest"
        )
        self.container_ssh_port = pipeline_def.get("container_ssh_port", 2222)
        self.container_extensions = pipeline_def.get("container_extensions", {})
        self.container_env = pipeline_def.get("container_env", {})
        self.container_host_path = pipeline_def.get("container_host_path", "")
        self.container_workspace = pipeline_def.get("container_workspace", "")
        self.container_gpu = pipeline_def.get("container_gpu", False)
        # Apptainer-only: capabilities and extra bind mounts that must be
        # baked into `apptainer instance start`. Per-exec --cap-add and
        # --bind are silently ignored on a running instance, so anything
        # the workload needs (e.g., SYS_ADMIN + /dev/fuse for FUSE mounts)
        # has to be declared at the pipeline level here.
        self.container_caps = pipeline_def.get("container_caps", [])
        self.container_binds = Pipeline._expand_env_in_config(
            pipeline_def.get("container_binds", []) or []
        )

        # Optional per-host /tmp redirect for the apptainer instance.
        # When a pipeline workload hardcodes scratch dirs under /tmp
        # (snakemake, biobb's /tmp/biobb-scratch, etc.), the default
        # `--no-mount tmp` + shared NFS overlay layout means every host's
        # apptainer instance shares the same /tmp on NFS — parallel reps
        # across hosts collide on mkdir / rm / rename of the same paths.
        # Setting `tmp_bind_root: /mnt/nvme/$USER` (or any per-host path)
        # binds <root>/<pipeline_name>/tmp into the container at /tmp,
        # giving each host its own /tmp on node-local NVMe/tmpfs. The
        # caller must mkdir <root>/<pipeline_name>/tmp on every host
        # (use a pre_cmd, since the path is per-host and may not exist
        # yet on first run).
        tmp_bind_root_raw = pipeline_def.get("tmp_bind_root", None)
        self.tmp_bind_root = (
            os.path.expandvars(tmp_bind_root_raw) if tmp_bind_root_raw else None
        )

        # Launcher overrides (top-level YAML keys). None = use built-in
        # defaults (ssh / pssh / mpiexec). See _apply_launcher_overrides.
        self.ssh_cmd = pipeline_def.get("ssh_cmd", None)
        self.pssh_cmd = pipeline_def.get("pssh_cmd", None)
        self.mpi_cmd = pipeline_def.get("mpi_cmd", None)
        self._apply_launcher_overrides()

        # Load base_deploy_mode. The legacy ``install_manager`` key is
        # still read once with a deprecation warning so existing YAMLs
        # keep loading after the rename.
        self.base_deploy_mode = pipeline_def.get("base_deploy_mode", None)
        if self.base_deploy_mode is None and "install_manager" in pipeline_def:
            self.base_deploy_mode = pipeline_def["install_manager"]
            print(
                "Warning: 'install_manager' is deprecated; "
                "rename to 'base_deploy_mode' (per-package "
                "'install_method' now selects the installer)."
            )

        # Load hostfile parameter (None means use global jarvis hostfile)
        hostfile_path = pipeline_def.get("hostfile")
        if hostfile_path:
            self.hostfile = Hostfile(path=os.path.expandvars(hostfile_path))
        else:
            self.hostfile = None

        # Parse the scheduler block (if present). The scheduler owns the
        # hostfile path — point self.hostfile at the file the generated
        # job script will write so every package in the pipeline sees
        # the same allocation-derived hosts.
        scheduler_spec = pipeline_def.get("scheduler")
        if scheduler_spec:
            self.scheduler = self._expand_env_in_config(dict(scheduler_spec))
            self._apply_scheduler_hostfile()
        else:
            self.scheduler = None

        # Process interceptors
        interceptors_list = pipeline_def.get("interceptors", [])
        for interceptor_def in interceptors_list:
            interceptor_id = interceptor_def.get(
                "pkg_name", interceptor_def["pkg_type"].split(".")[-1]
            )
            interceptor_entry = self._process_package_definition(
                interceptor_def, interceptor_id
            )
            self.interceptors[interceptor_id] = interceptor_entry

        # Process packages
        for pkg_def in pipeline_def.get("pkgs", []):
            pkg_id = pkg_def.get("pkg_name", pkg_def["pkg_type"].split(".")[-1])
            package_entry = self._process_package_definition(pkg_def, pkg_id)
            self.packages.append(package_entry)

        # Validate that interceptor and package IDs are unique
        self._validate_unique_ids()

        # Propagate base_deploy_mode -> per-package deploy_mode
        self._propagate_deploy_mode()

        # Install phase: dispatch to per-installer plans based on each
        # package's install_method (falling back to base_deploy_mode for
        # YAMLs that don't set install_method explicitly).
        from jarvis_cd.core.installer import Installer

        Installer.install_all(self)

        # Save pipeline configuration and environment
        self.save()

        # A scheduler snapshot is an isolated execution copy, not an operator
        # navigation event. Never let concurrent queued jobs race on the
        # process-global current-pipeline record.
        if snapshot_marker is None:
            self.jarvis.set_current_pipeline(self.name)

        print(f"Loaded pipeline: {self.name}")
        print(f"Packages: {[pkg['pkg_id'] for pkg in self.packages]}")

    def _load_package_instance(
        self, pkg_def: Dict[str, Any], pipeline_env: Optional[Dict[str, str]] = None
    ):
        """
        Load a package instance from package definition.

        :param pkg_def: Package definition dictionary
        :param pipeline_env: Pipeline environment variables
        :return: Package instance
        """
        pkg_type = pkg_def["pkg_type"]

        # Find package class
        if "." in pkg_type:
            # Full specification like "builtin.ior"
            import_parts = pkg_type.split(".")
            repo_name = import_parts[0]
            pkg_name = import_parts[1]
        else:
            # Just package name, search in repos
            full_spec = self.jarvis.find_package(pkg_type)
            if not full_spec:
                raise ValueError(f"Package not found: {pkg_type}")
            import_parts = full_spec.split(".")
            repo_name = import_parts[0]
            pkg_name = import_parts[1]

        # Determine class name (convert snake_case and kebab-case to PascalCase)
        import re

        class_name = "".join(word.capitalize() for word in re.split(r"[_-]", pkg_name))

        # Load class
        if repo_name == "builtin":
            repo_path = str(self.jarvis.get_builtin_repo_path())
        else:
            # Find repo path in registered repos
            repo_path = None
            for registered_repo in self.jarvis.repos["repos"]:
                if Path(registered_repo).name == repo_name:
                    repo_path = registered_repo
                    break

            if not repo_path:
                raise ValueError(f"Repository not found: {repo_name}")

        import_str = f"{repo_name}.{pkg_name}.pkg"
        try:
            pkg_class = load_class(import_str, repo_path, class_name)
        except Exception as e:
            import traceback

            error_details = traceback.format_exc()
            raise ValueError(
                f"Failed to load package '{pkg_type}':\n"
                f"  Repository: {repo_name}\n"
                f"  Package: {pkg_name}\n"
                f"  Repo path: {repo_path}\n"
                f"  Import string: {import_str}\n"
                f"  Class name: {class_name}\n"
                f"  Error: {e}\n"
                f"  Traceback:\n{error_details}"
            )

        if not pkg_class:
            raise ValueError(f"Package class not found: {class_name} in {import_str}")

        # Create instance with pipeline context
        try:
            pkg_instance = pkg_class(pipeline=self)
        except Exception as e:
            import traceback

            error_details = traceback.format_exc()
            raise ValueError(
                f"Failed to instantiate package '{pkg_type}':\n"
                f"  Class: {class_name}\n"
                f"  Error during __init__: {e}\n"
                f"  Traceback:\n{error_details}"
            )

        # Set basic attributes
        pkg_instance.pkg_type = pkg_def["pkg_type"]
        pkg_instance.pkg_id = pkg_def["pkg_id"]
        pkg_instance.global_id = pkg_def["global_id"]

        # Initialize directories now that pkg_id is set
        pkg_instance._ensure_directories()

        # Set configuration
        base_config = pkg_def.get("config", {})
        base_config.setdefault("do_dbg", False)
        base_config.setdefault("dbg_port", 50000)
        pkg_instance.config = base_config

        # Set up environment variables - mod_env is exact replica of env plus LD_PRELOAD
        if pipeline_env is None:
            pipeline_env = {}

        # env contains everything except LD_PRELOAD
        pkg_instance.env = {k: v for k, v in pipeline_env.items() if k != "LD_PRELOAD"}

        # mod_env is exact replica of env plus LD_PRELOAD (if it exists)
        pkg_instance.mod_env = pkg_instance.env.copy()
        if "LD_PRELOAD" in pipeline_env:
            pkg_instance.mod_env["LD_PRELOAD"] = pipeline_env["LD_PRELOAD"]

        return pkg_instance

    def _process_package_definition(
        self, pkg_def: Dict[str, Any], pkg_id: str
    ) -> Dict[str, Any]:
        """
        Process a package definition from YAML, merging YAML config with defaults.

        :param pkg_def: Package definition from YAML
        :param pkg_id: Package ID to use
        :return: Complete package entry with merged configuration
        """
        pkg_type = pkg_def["pkg_type"]

        # Resolve pkg_type to full specification (repo.package) if not already specified
        if "." not in pkg_type:
            resolved_type = self.jarvis.find_package(pkg_type)
            if resolved_type:
                pkg_type = resolved_type
            # If not found, keep original (will fail later during loading)

        # Get default configuration from package
        default_config = self._get_package_default_config(pkg_type)

        # Extract config from YAML
        yaml_config = {
            k: v for k, v in pkg_def.items() if k not in ["pkg_type", "pkg_name"]
        }

        # Merge YAML config on top of defaults
        merged_config = default_config.copy()
        merged_config.update(yaml_config)

        # Expand environment variables ($VAR / ${VAR}) in any string
        # config values so YAMLs can use $HOME / $USER / $JARVIS_RUN_DIR
        # instead of hardcoding paths.
        merged_config = self._expand_env_in_config(merged_config)

        return {
            "pkg_type": pkg_type,
            "pkg_id": pkg_id,
            "pkg_name": pkg_type.split(".")[-1],
            "global_id": f"{self.name}.{pkg_id}",
            "config": merged_config,
        }

    @staticmethod
    def _expand_env_in_config(value):
        """Recursively apply ``os.path.expandvars`` to every string in a
        config tree (dict / list / str). Leaves non-strings untouched.

        Lets pipeline YAMLs reference environment variables like
        ``$HOME/jarvis-runs/foo`` without each package having to expand
        them itself.
        """
        if isinstance(value, str):
            return os.path.expandvars(value)
        if isinstance(value, dict):
            return {k: Pipeline._expand_env_in_config(v) for k, v in value.items()}
        if isinstance(value, list):
            return [Pipeline._expand_env_in_config(v) for v in value]
        return value

    def _get_package_default_config(self, package_spec: str) -> Dict[str, Any]:
        """
        Get default configuration values for a package by parsing with PkgArgParse.
        Equivalent to calling 'configure' with no parameters.
        """
        try:
            # Create a temporary package definition to load the package
            temp_pkg_def = {
                "pkg_type": package_spec,
                "pkg_id": "temp",
                "pkg_name": package_spec.split(".")[-1],
                "global_id": "temp.temp",
                "config": {},
            }

            # Load package instance
            pkg_instance = self._load_package_instance(temp_pkg_def)

            # Use PkgArgParse to get defaults by parsing 'configure' with no args
            argparse = pkg_instance.get_argparse()
            argparse.parse(["configure"])
            default_config = argparse.kwargs

            return default_config

        except Exception as e:
            # Package loading failure should be fatal - cannot add package to pipeline
            raise ValueError(f"Failed to load package '{package_spec}': {e}")

        return {}

    def _validate_unique_ids(self):
        """
        Validate that interceptor IDs and package IDs are unique within the pipeline.
        """
        # Get all package IDs
        package_ids = {pkg["pkg_id"] for pkg in self.packages}

        # Get all interceptor IDs
        interceptor_ids = set(self.interceptors.keys())

        # Check for conflicts
        conflicts = package_ids & interceptor_ids
        if conflicts:
            conflict_list = ", ".join(conflicts)
            raise ValueError(
                f"ID conflicts between packages and interceptors: {conflict_list}. "
                f"Package and interceptor IDs must be unique within the pipeline."
            )

    def _validate_required_config(self, package_spec: str, config: Dict[str, Any]):
        """
        Validate that all required configuration parameters have values.

        :param package_spec: Package specification
        :param config: Configuration dictionary
        :raises ValueError: If required parameters are missing or None
        """
        try:
            # Create a temporary package definition to load the package
            temp_pkg_def = {
                "pkg_type": package_spec,
                "pkg_id": "temp",
                "pkg_name": package_spec.split(".")[-1],
                "global_id": "temp.temp",
                "config": {},
            }

            # Load package instance
            pkg_instance = self._load_package_instance(temp_pkg_def)

            # Get configuration menu to check for required parameters
            if hasattr(pkg_instance, "configure_menu"):
                config_menu = pkg_instance.configure_menu()
                if config_menu:
                    missing_required = []
                    for menu_item in config_menu:
                        name = menu_item.get("name")
                        default_value = menu_item.get("default")

                        # If parameter has no default and is not provided in config, it's required
                        if (
                            name
                            and default_value is None
                            and (name not in config or config[name] is None)
                        ):
                            missing_required.append(name)

                    if missing_required:
                        raise ValueError(
                            f"Missing required configuration parameters for {package_spec}: {', '.join(missing_required)}"
                        )

        except Exception as e:
            if "Missing required configuration parameters" in str(e):
                raise  # Re-raise our validation error
            # Other errors during validation are not fatal
            print(f"Warning: Could not validate configuration for {package_spec}: {e}")

    def _apply_interceptors_to_package(self, pkg_instance, pkg_def):
        """
        Apply interceptors to a package instance during pipeline start.

        :param pkg_instance: The package instance to apply interceptors to
        :param pkg_def: The package definition from pipeline configuration
        """
        from jarvis_cd.util.logger import logger

        # Get interceptors list from package configuration
        interceptors_list = pkg_def.get("config", {}).get("interceptors", [])

        if not interceptors_list:
            return

        logger.warning(
            f"Applying {len(interceptors_list)} interceptors to {pkg_def['pkg_id']}"
        )

        for interceptor_name in interceptors_list:
            try:
                # Find interceptor in pipeline-level interceptors
                if interceptor_name not in self.interceptors:
                    logger.error(
                        f"Warning: Interceptor '{interceptor_name}' not found in pipeline interceptors"
                    )
                    continue

                interceptor_def = self.interceptors[interceptor_name]

                # Print BEGIN message with full interceptor type
                logger.success(f"[{interceptor_def['pkg_type']}] [MODIFY_ENV] BEGIN")

                # Load interceptor instance
                interceptor_instance = self._load_package_instance(
                    interceptor_def, self.env
                )

                # Verify it's an interceptor and has modify_env method
                if not hasattr(interceptor_instance, "modify_env"):
                    logger.error(
                        f"Warning: Package '{interceptor_name}' does not have modify_env() method"
                    )
                    continue

                # Share the same mod_env reference between interceptor and package
                interceptor_instance.mod_env = pkg_instance.mod_env
                interceptor_instance.env = pkg_instance.env

                # Call modify_env on the interceptor to modify the shared environment
                interceptor_instance.modify_env()

                # The mod_env is shared, so changes are automatically applied to the package

                # Print END message
                logger.success(f"[{interceptor_def['pkg_type']}] [MODIFY_ENV] END")

            except Exception as e:
                logger.error(f"Error applying interceptor '{interceptor_name}': {e}")

    def _generate_pipeline_container_yaml(self):
        """
        Generate pipeline-wide YAML configuration file for use inside containers.
        This includes all packages and interceptors in the pipeline.

        :return: Path to generated YAML file
        """
        import yaml

        # Create pipeline configuration with all packages
        pipeline_config = {
            "name": f"{self.execution_container_name}_container",
            "pkgs": [],
        }

        # Add all packages (excluding 'deploy' config)
        for pkg_def in self.packages:
            pkg_entry = {"pkg_type": pkg_def["pkg_type"]}
            for key, value in pkg_def["config"].items():
                if key not in ["deploy", "deploy_mode", "deploy_ssh_port"]:
                    pkg_entry[key] = value
            pipeline_config["pkgs"].append(pkg_entry)

        # Add interceptors if any
        if self.interceptors:
            pipeline_config["interceptors"] = []
            for interceptor_name, interceptor_def in self.interceptors.items():
                interceptor_entry = {"pkg_type": interceptor_def["pkg_type"]}
                for key, value in interceptor_def.get("config", {}).items():
                    if key not in ["deploy", "deploy_mode", "deploy_ssh_port"]:
                        interceptor_entry[key] = value
                pipeline_config["interceptors"].append(interceptor_entry)

        # Write to shared directory
        shared_dir = self.get_pipeline_shared_dir()
        yaml_path = shared_dir / "pipeline.yaml"
        with open(yaml_path, "w") as f:
            yaml.dump(pipeline_config, f, default_flow_style=False)

        print(f"Generated pipeline YAML: {yaml_path}")
        return yaml_path

    def _generate_pipeline_compose_file(self):
        """
        Generate pipeline-specific docker-compose file that uses the global container image.
        This compose file is stored in the pipeline's shared directory.

        :return: Path to generated compose file
        """
        import yaml
        import os

        shared_dir = self.get_pipeline_shared_dir()
        compose_path = shared_dir / "docker-compose.yaml"

        container_name = f"{self.execution_container_name}_container"

        # Use pipeline-level SSH port configuration
        ssh_port = self.container_ssh_port

        # Path remapping for Docker-in-Docker / devcontainer environments.
        # When container_host_path and container_workspace are set, the
        # workspace is bind-mounted from a different path on the Docker host.
        # Volume mounts in docker-compose must use host paths.
        docker_host_prefix = self.container_host_path
        workspace_root = self.container_workspace
        if docker_host_prefix and workspace_root and os.path.isdir(workspace_root):

            def to_host_path(path_str):
                """Remap a workspace path to the Docker host path."""
                if path_str.startswith(workspace_root):
                    return docker_host_prefix + path_str[len(workspace_root) :]
                return path_str
        else:

            def to_host_path(path_str):
                return path_str

        # Container entrypoint: start SSH and sleep forever.
        # The host-side jarvis orchestrates packages by SSH/MPI-ing into
        # the containers — no jarvis installation needed inside them.
        # In DinD environments, the home directory is on the overlay
        # filesystem — copy SSH keys to the workspace so sibling
        # containers can access them.
        ssh_dir = os.path.expanduser("~/.ssh")
        if (
            docker_host_prefix
            and workspace_root
            and os.path.isdir(workspace_root)
            and not ssh_dir.startswith(workspace_root)
        ):
            docker_ssh_dir = os.path.join(workspace_root, ".ssh-host")
            if os.path.exists(ssh_dir):
                import shutil

                if os.path.exists(docker_ssh_dir):
                    shutil.rmtree(docker_ssh_dir)
                shutil.copytree(ssh_dir, docker_ssh_dir)
            ssh_dir = docker_ssh_dir
        container_cmd = (
            f"set -e && "
            f"cp -r /root/.ssh_host /root/.ssh && "
            f"chmod 700 /root/.ssh && "
            f"chmod 600 /root/.ssh/* 2>/dev/null || true && "
            f"cat /root/.ssh/*.pub > /root/.ssh/authorized_keys 2>/dev/null && "
            f"chmod 600 /root/.ssh/authorized_keys 2>/dev/null || true && "
            f'echo "Host *" > /root/.ssh/config && '
            f'echo "    Port {ssh_port}" >> /root/.ssh/config && '
            f'echo "    StrictHostKeyChecking no" >> /root/.ssh/config && '
            f"chmod 600 /root/.ssh/config && "
            f'sed -i "s/^#*Port .*/Port {ssh_port}/" /etc/ssh/sshd_config && '
            f"/usr/sbin/sshd && "
            f"sleep infinity"
        )

        # Create compose configuration using the global container image.
        # Shared and private directories are mounted at the SAME path
        # inside the container so that all configuration paths (hostfile,
        # pipeline YAML, package data) are identical on host and container.
        private_dir = self.get_pipeline_private_dir()

        # Prepare volume mounts — identical host:container paths
        # (use to_host_path for Docker-in-Docker remapping)
        volumes = [
            f"{to_host_path(str(private_dir))}:{private_dir}",
            f"{to_host_path(str(shared_dir))}:{shared_dir}",
            f"{to_host_path(ssh_dir)}:/root/.ssh_host:ro",
        ]

        # Workload-specific bind mounts from `container_binds` in the
        # pipeline YAML (e.g., /lus/flare/... for IOR output paths).
        # The apptainer path applies these in _start_containerized_pipeline;
        # for docker/podman compose we have to bake them into the
        # service's volume list.
        for bind in self.container_binds or []:
            if ":" in bind:
                host, _, rest = bind.partition(":")
                volumes.append(f"{to_host_path(host)}:{rest}")
            else:
                volumes.append(f"{to_host_path(bind)}:{bind}")

        service_config = {
            "container_name": container_name,
            "image": self.container_image,
            "entrypoint": ["/bin/bash", "-c"],
            "command": [container_cmd],
            "network_mode": "host",
            "ipc": "host",  # Share IPC namespace with host (removes shm limits)
            "volumes": volumes,
        }

        # Inject container environment variables into the compose service
        if self.container_env:
            service_config["environment"] = {
                str(k): os.path.expandvars(str(v))
                for k, v in self.container_env.items()
            }

        # Note: GPU configuration is not included by default
        # If GPU access is needed, users should add it to their pipeline configuration
        # or use host network mode which provides direct device access

        # Apply container extensions from pipeline configuration
        if self.container_extensions:
            # Deep merge container_extensions into service_config
            self._merge_dict(service_config, self.container_extensions)

        compose_config = {"services": {self.name: service_config}}

        # Write compose file
        with open(compose_path, "w") as f:
            yaml.dump(compose_config, f, default_flow_style=False)

        print(f"Generated docker-compose file: {compose_path}")
        return compose_path

    def _merge_dict(self, target: dict, source: dict):
        """
        Deep merge source dictionary into target dictionary.
        Lists are extended, dictionaries are recursively merged.

        :param target: Target dictionary to merge into
        :param source: Source dictionary to merge from
        """
        for key, value in source.items():
            if key in target:
                if isinstance(target[key], dict) and isinstance(value, dict):
                    # Recursively merge nested dictionaries
                    self._merge_dict(target[key], value)
                elif isinstance(target[key], list) and isinstance(value, list):
                    # Extend lists
                    target[key].extend(value)
                else:
                    # Override value
                    target[key] = value
            else:
                # Add new key
                target[key] = value

    def _start_containerized_pipeline(self):
        """
        Start containerized pipeline:
        1. Launch containers in detached mode (SSH daemons only)
        2. Run each package's start() — commands reach containers via SSH/MPI
        """
        from jarvis_cd.util.logger import logger
        from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo

        logger.info("Starting containerized pipeline deployment")

        engine = self.container_engine.lower()

        if engine == "apptainer":
            # Apptainer: start persistent instances with sshd on every node
            # (mirrors docker compose up -d).
            shared_dir = self.get_pipeline_shared_dir()
            private_dir = self.get_pipeline_private_dir()
            # Resolve the SIF: when YAML supplies `container_image`, treat
            # it as the SIF basename (or absolute path) so a pipeline can
            # reuse another pipeline's prebuilt SIF instead of building
            # its own. Default to <pipeline_name>.sif in the centralized
            # containers dir for normal "build my own" pipelines.
            sif_ref = self.container_image or self.name
            sif_candidate = Path(sif_ref)
            if sif_candidate.is_absolute() and sif_candidate.exists():
                sif_path = sif_candidate
            else:
                sif_path = self.jarvis.get_containers_dir() / f"{sif_ref}.sif"
            instance_name = self.execution_container_name
            ssh_port = self.container_ssh_port

            nv_flag = "--nv " if getattr(self, "container_gpu", False) else ""

            # Bake bind mounts into the instance: per-exec --bind is
            # silently ignored on a running instance, so anything packages
            # need access to has to come in here. The pipeline shared/
            # private dirs cover all per-package shared/private subdirs;
            # container_binds adds workload-specific extras (e.g.,
            # /dev/fuse for FUSE mounts).
            binds = [f"{shared_dir}:{shared_dir}", f"{private_dir}:{private_dir}"]
            binds.extend(self.container_binds or [])
            bind_flags = "".join(f"--bind {b} " for b in binds)

            cap_flag = ""
            if self.container_caps:
                cap_flag = f"--add-caps {','.join(self.container_caps)} "

            # Per-pipeline writable layer backed by NFS, not RAM.
            # `--writable-tmpfs` is RAM-only (capped at ~50% of system
            # RAM); workloads that apt-/conda-install at runtime
            # (snakemake conda envs, deferred pip installs, …) blow past
            # that even though the host has terabytes free. An overlay
            # directory under shared_dir lives on the same NFS mount as
            # the SIF, so the container has effectively unlimited
            # writable space and the data persists across runs.
            #
            # `--no-mount tmp` keeps the host's small /tmp out of the
            # picture; in-container /tmp lands in the overlay (NFS).
            overlay_dir = shared_dir / "overlay"
            overlay_dir.mkdir(parents=True, exist_ok=True)
            overlay_flag = f"--overlay {overlay_dir} "
            no_mount_flag = "--no-mount tmp "

            # When tmp_bind_root is set in the pipeline YAML, replace the
            # overlay-backed /tmp with a per-host bind mount so workloads
            # that hardcode /tmp scratch dirs don't collide across hosts.
            # See pipeline-load comment for rationale.
            tmp_bind_flag = ""
            if self.tmp_bind_root:
                tmp_bind_path = (
                    f"{self.tmp_bind_root}/{self.execution_container_name}/tmp"
                )
                tmp_bind_flag = f"--bind {tmp_bind_path}:/tmp "
                no_mount_flag = ""

            start_cmd = (
                f"apptainer instance start {nv_flag}{cap_flag}{bind_flags}"
                f"{tmp_bind_flag}{no_mount_flag}{overlay_flag}{sif_path} {instance_name}"
                f" && apptainer exec {nv_flag}instance://{instance_name}"
                f" /usr/sbin/sshd -p {ssh_port}"
                f" -o StrictModes=no -o UsePAM=no"
            )

            # Fan apptainer instance start across every host in the
            # hostfile via PsshExecInfo, matching the symmetric
            # stop/kill paths below. Required for any package that uses
            # PsshExecInfo with container=apptainer — that path wraps as
            # `apptainer exec instance://...` on each remote host, which
            # only works if an instance is actually running there. The
            # earlier "head-only" behavior silently restricted workload
            # parallelism to host[0] even on multi-node SLURM allocations.
            #
            # Within a SLURM allocation, ssh hops to allocated peers are
            # adopted by pam_slurm_adopt automatically; the pipeline's
            # own ssh_cmd override (e.g. env -u LD_LIBRARY_PATH ssh)
            # neutralizes the conda libcrypto/OpenSSL mismatch.
            hostfile = self.get_hostfile()
            if self._hostfile_is_local_only(hostfile):
                logger.info("Hostfile is local-only, deploying to localhost directly")
                exec_info = LocalExecInfo()
            else:
                logger.info(
                    f"Hostfile has {len(hostfile)} hosts; starting "
                    "apptainer instance on every host via PsshExecInfo"
                )
                exec_info = PsshExecInfo(hostfile=hostfile)

            # Per-host bind source for tmp_bind_root must exist on every
            # node before the apptainer instance start tries to mount it,
            # otherwise apptainer aborts with "mount source X doesn't
            # exist". Fan a mkdir to all hosts via the same exec_info.
            if self.tmp_bind_root:
                tmp_mkdir = (
                    f"mkdir -p {self.tmp_bind_root}/{self.execution_container_name}/tmp"
                )
                logger.info(
                    f"tmp_bind_root set; ensuring {self.tmp_bind_root}/"
                    f"{self.execution_container_name}/tmp exists on every host"
                )
                Exec(tmp_mkdir, exec_info).run()

            Exec(start_cmd, exec_info).run()
            logger.success("Apptainer instances started (SSH ready)")
        else:
            # Docker/Podman: start containers via compose
            shared_dir = self.get_pipeline_shared_dir()
            compose_path = shared_dir / "docker-compose.yaml"

            if not compose_path.exists():
                raise FileNotFoundError(
                    f"Compose file not found: {compose_path}. Did you load the pipeline?"
                )

            if engine == "podman":
                up_cmd = f"podman-compose -f {compose_path} up -d"
            else:
                up_cmd = f"docker compose -f {compose_path} up -d"

            hostfile = self.get_hostfile()
            if self._hostfile_is_local_only(hostfile):
                logger.info("Hostfile is local-only, deploying to localhost directly")
                exec_info = LocalExecInfo()
            else:
                logger.info("Deploying containers to all nodes in hostfile")
                self._distribute_image_to_hosts(hostfile)
                exec_info = PsshExecInfo(hostfile=hostfile)

            Exec(up_cmd, exec_info).run()
            logger.success("Containers started (SSH ready)")

        # Now run each package via the normal per-package flow.
        # Packages use PsshExecInfo / MpiExecInfo with the hostfile,
        # which reaches the containers through SSH on the configured port.
        for pkg_def in self.packages:
            try:
                logger.success(f"[{pkg_def['pkg_type']}] [START] BEGIN")
                self._bind_package_execution_environment(pkg_def)
                pkg_instance = self._load_package_instance(pkg_def, self.env)
                self._apply_interceptors_to_package(pkg_instance, pkg_def)

                if hasattr(pkg_instance, "start"):
                    pkg_instance.start()
                else:
                    logger.warning(f"Package {pkg_def['pkg_id']} has no start method")

                self.env.update(pkg_instance.env)
                self._started_instances.append(pkg_instance)
                logger.success(f"[{pkg_def['pkg_type']}] [START] END")
            except Exception as e:
                logger.error(f"Error starting package {pkg_def['pkg_id']}: {e}")
                raise RuntimeError(
                    f"Pipeline startup failed at package '{pkg_def['pkg_id']}': {e}"
                ) from e

    def _distribute_image_to_hosts(self, hostfile):
        """
        Ship the locally-built deploy image to every remote host's docker/podman
        daemon so `docker compose up -d` finds it without a registry pull.
        Mirrors the apptainer flow: save the image once to the pipeline's
        shared dir, then PSSH a `docker load -i ...` to every host that
        doesn't already have the image. Apptainer itself is skipped — its
        SIF is already on the shared FS.
        """
        from jarvis_cd.util.logger import logger
        from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo
        from jarvis_cd.util.hostfile import Hostfile

        engine = self.container_engine.lower()
        if engine == "apptainer":
            return

        image = self.name
        local_host = socket.gethostname()
        skip = ("localhost", "127.0.0.1", local_host)

        # Probe each remote host; only transfer to those missing the image
        needs_transfer = []
        for host in hostfile.hosts:
            if host in skip:
                continue
            check_cmd = (
                f"ssh -o BatchMode=yes -o StrictHostKeyChecking=no "
                f"{host} '{engine} image inspect {image} >/dev/null 2>&1'"
            )
            if (
                Exec(check_cmd, LocalExecInfo(hide_output=True))
                .run()
                .exit_code.get("localhost", 1)
                == 0
            ):
                logger.info(f"Image '{image}' already on {host}, skipping transfer")
            else:
                needs_transfer.append(host)

        if not needs_transfer:
            return

        # Save image once to the pipeline's shared dir (visible from every node)
        shared_dir = self.get_pipeline_shared_dir()
        tar_path = shared_dir / f"{image}.tar"
        logger.info(f"Saving image '{image}' to {tar_path}...")
        save = Exec(f"{engine} save -o {tar_path} {image}", LocalExecInfo()).run()
        if save.exit_code.get("localhost", 1) != 0:
            raise RuntimeError(f"Failed to save image '{image}' to {tar_path}")

        # PSSH-load on every host that needs it (parallel)
        logger.info(f"Loading image '{image}' on {needs_transfer} in parallel...")
        load = Exec(
            f"{engine} load -i {tar_path}",
            PsshExecInfo(hostfile=Hostfile(hosts=needs_transfer)),
        ).run()
        for host, code in load.exit_code.items():
            if code != 0:
                raise RuntimeError(f"Failed to load image '{image}' on {host}")

        # Tar served its purpose; reclaim the disk
        try:
            tar_path.unlink()
        except OSError:
            pass

    def _stop_containerized_pipeline(self):
        """
        Stop containerized pipeline:
        1. Stop each package via its stop() method
        2. Bring down the containers
        """
        from jarvis_cd.util.logger import logger
        from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo

        logger.info("Stopping containerized pipeline")

        # Stop packages in reverse order (same as non-containerized)
        for pkg_def in reversed(self.packages):
            try:
                logger.success(f"[{pkg_def['pkg_type']}] [STOP] BEGIN")
                self._bind_package_execution_environment(pkg_def)
                pkg_instance = self._load_package_instance(pkg_def, self.env)
                if hasattr(pkg_instance, "stop"):
                    pkg_instance.stop()
                logger.success(f"[{pkg_def['pkg_type']}] [STOP] END")
            except Exception as e:
                logger.error(f"Error stopping package {pkg_def['pkg_id']}: {e}")

        # Bring down the containers
        engine = self.container_engine.lower()
        if engine == "apptainer":
            stop_cmd = f"apptainer instance stop {self.execution_container_name}"
        elif engine == "podman":
            compose_path = self.get_pipeline_shared_dir() / "docker-compose.yaml"
            stop_cmd = f"podman-compose -f {compose_path} down"
        else:
            compose_path = self.get_pipeline_shared_dir() / "docker-compose.yaml"
            stop_cmd = f"docker compose -f {compose_path} down"

        hostfile = self.get_hostfile()
        if self._hostfile_is_local_only(hostfile):
            exec_info = LocalExecInfo()
        else:
            exec_info = PsshExecInfo(hostfile=hostfile)

        Exec(stop_cmd, exec_info).run()
        logger.success("Containers stopped")

    def _kill_containerized_pipeline(self):
        """
        Force-kill containerized pipeline:
        1. Kill each package
        2. Force-remove the containers
        """
        from jarvis_cd.util.logger import logger
        from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo

        logger.info("Force-killing containerized pipeline")

        # Kill packages
        for pkg_def in self.packages:
            try:
                logger.success(f"[{pkg_def['pkg_type']}] [KILL] BEGIN")
                self._bind_package_execution_environment(pkg_def)
                pkg_instance = self._load_package_instance(pkg_def, self.env)
                if hasattr(pkg_instance, "kill"):
                    pkg_instance.kill()
                logger.success(f"[{pkg_def['pkg_type']}] [KILL] END")
            except Exception as e:
                logger.error(f"Error killing package {pkg_def['pkg_id']}: {e}")

        # Force-remove containers
        engine = self.container_engine.lower()
        if engine == "apptainer":
            kill_cmd = f"apptainer instance stop {self.execution_container_name}"
        elif engine == "podman":
            compose_path = self.get_pipeline_shared_dir() / "docker-compose.yaml"
            kill_cmd = f"podman-compose -f {compose_path} kill && podman-compose -f {compose_path} down"
        else:
            compose_path = self.get_pipeline_shared_dir() / "docker-compose.yaml"
            kill_cmd = f"docker compose -f {compose_path} kill && docker compose -f {compose_path} down"

        hostfile = self.get_hostfile()
        if self._hostfile_is_local_only(hostfile):
            exec_info = LocalExecInfo()
        else:
            exec_info = PsshExecInfo(hostfile=hostfile)

        Exec(kill_cmd, exec_info).run()
        logger.success("Containers force-killed")
