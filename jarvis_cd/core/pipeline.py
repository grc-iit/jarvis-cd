"""
Pipeline management for Jarvis-CD.
Provides the consolidated Pipeline class that combines pipeline creation, loading, and execution.
"""

import os
import hashlib
import json
import posixpath
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import yaml
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Mapping, Optional
from uuid import uuid4
from jarvis_cd.artifacts import (
    ArtifactLocation,
    ArtifactOwnership,
    ArtifactReporter,
    ArtifactRole,
    ArtifactState,
    ArtifactStructure,
)
from jarvis_cd.core.config import load_class, Jarvis
from jarvis_cd.core.execution import (
    LEGACY_RECORD_SCHEMA,
    RECORD_NAME,
    RECORD_SCHEMA,
    SCHEDULER_ARTIFACT_PATH_METADATA_KEY,
    SCHEDULER_ARTIFACT_PATH_SCHEMA,
    ExecutionHandle,
    ExecutionArtifactSnapshot,
    ExecutionProgressSnapshot,
    ExecutionServiceRuntimeAuthority,
    ExecutionServiceRuntimeSnapshot,
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
_SCHEDULER_MISSING_GRACE_SECONDS = 60.0


def _absolute_scheduler_log_path(value: object) -> str:
    """Return one lexical absolute path without expanding or resolving links."""
    if not isinstance(value, str) or not value:
        raise ValueError("scheduler output and error paths must be non-empty strings")
    if os.name == "nt" and value.startswith("/"):
        return posixpath.normpath(value)
    return os.path.normpath(os.path.abspath(value))


def _prepare_scheduler_log_parents(scheduler_spec: Mapping[str, Any]) -> None:
    """Create concrete scheduler log directories before external submission."""
    for key in ("output", "error"):
        configured_path = str(scheduler_spec[key])
        if os.name == "nt" and configured_path.startswith("/"):
            # A POSIX scheduler path cannot be materialized by a Windows
            # controller. This preserves cross-platform rendering/tests; an
            # actual Windows-local path is still prepared below.
            continue
        path = Path(configured_path)
        parent = path.parent
        if "%" in str(parent):
            raise ValueError(
                f"scheduler.{key} replacement tokens are supported only in filenames"
            )
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent.is_dir():
            raise RuntimeError(f"scheduler.{key} parent is not a directory: {parent}")


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


def _executor_status_error(
    result: Any,
    *,
    operation: str,
    limit: int = 4096,
) -> Optional[RuntimeError]:
    """Return an error when an executor omitted or failed a host status."""
    exit_codes = getattr(result, "exit_code", None)
    if not isinstance(exit_codes, Mapping) or not exit_codes:
        return RuntimeError(f"{operation} returned no per-host exit status")

    stderr = getattr(result, "stderr", {})
    stderr_by_host = stderr if isinstance(stderr, Mapping) else {}
    failures: list[str] = []
    for raw_host, code in exit_codes.items():
        host = str(raw_host)
        if isinstance(code, bool) or not isinstance(code, int):
            detail = f"{host}=invalid exit status {code!r}"
        elif code != 0:
            detail = f"{host}=exit {code}"
        else:
            continue
        diagnostic = str(stderr_by_host.get(raw_host, "") or "").strip()
        if diagnostic:
            detail += f" stderr={diagnostic!r}"
        failures.append(detail)

    if not failures:
        return None
    message = f"{operation} failed: " + "; ".join(failures)
    if len(message) > limit:
        message = "[truncated]\n" + message[-limit:]
    return RuntimeError(message)


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


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validated_execution_id(value: Optional[str]) -> str:
    """Return a bounded path-safe execution identifier."""
    return validate_execution_id(value)


def _couplings_from(value: Any) -> List[Dict[str, Any]]:
    """Normalize a pipeline's ``couplings:`` list (validated by ``ppl check``)."""
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("couplings must be a list of {from, to, kind, label} mappings")
    couplings = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValueError(f"couplings[{index}] must be a mapping")
        couplings.append({
            key: (str(item[key]) if item[key] is not None else None)
            for key in ("from", "to", "kind", "label")
            if key in item
        })
    return couplings


def _component_states_metadata() -> Dict[str, Any]:
    from jarvis_cd.core.component_states import (
        COMPONENT_STATES,
        COMPONENT_STATES_FILE,
        COMPONENT_STATES_SCHEMA,
    )

    return {"schema_version": COMPONENT_STATES_SCHEMA,
            "path": COMPONENT_STATES_FILE, "states": list(COMPONENT_STATES)}


def _require_bound_plan(
    rendered: Mapping[str, Any], expected: str, execution_id: str,
) -> None:
    """Refuse (``submission_plan_changed``, exit 5) a submit off its plan."""
    expected_hex = str(expected).strip().lower()
    if expected_hex.startswith("sha256:"):
        expected_hex = expected_hex[len("sha256:"):]
    if rendered.get("plan_sha256") == expected_hex:
        return
    from jarvis_cd.core.issues import JarvisIssueError, make_jarvis_issue

    raise JarvisIssueError([make_jarvis_issue(
        kind="submission_plan_changed",
        subject=f"execution {execution_id}",
        message=(
            "this submission no longer matches the plan that was approved: "
            "run `jarvis ppl submit ... +plan +json` again and ask again"),
        identity={"execution_id": execution_id, "expected": expected_hex,
                  "rendered": rendered.get("plan_sha256")},
        details={"expected_plan_sha256": expected_hex,
                 "plan_sha256": rendered.get("plan_sha256"),
                 "script_sha256": rendered.get("script_sha256"),
                 "input_sha256": rendered.get("input_sha256")},
        remedy="re-plan with the same --execution-id and submit with the new plan_sha256",
    )])


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

        # Declared data/control edges between packages (``couplings:``), for
        # plans and job cards; see docs/pipelines.md.
        self.couplings: List[Dict[str, Any]] = []

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

    def _submission_scheduler_spec(
        self, execution_root: Path, action: str, install_check: bool,
    ) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        """The scheduler directives one submission renders (no side effects)."""
        scheduler_spec = dict(self.scheduler)
        install_job_shape = None
        if action == "install":
            # An install job builds on one node: keep the site's account,
            # partition and time, drop the workload's task/GPU shape.
            for workload_key in (
                "ntasks",
                "ntasks_per_node",
                "gpus",
                "gpus_per_node",
                "gres",
                "array",
            ):
                scheduler_spec.pop(workload_key, None)
            scheduler_spec["nodes"] = 1
            scheduler_spec["ntasks"] = 1
            if not install_check:
                # A build uses every core of its node (spack -j defaults to
                # the node's cores); a check is a light single task.
                scheduler_spec.setdefault("exclusive", True)
            overrides = scheduler_spec.pop("install", None) or {}
            if not isinstance(overrides, dict):
                raise ValueError("scheduler.install must be a mapping of directives")
            if "nodes" in overrides and int(overrides["nodes"]) != 1:
                raise ValueError("scheduler.install.nodes must be 1")
            scheduler_spec.update(overrides)
            install_job_shape = {
                key: scheduler_spec.get(key)
                for key in ("nodes", "ntasks", "cpus_per_task", "mem",
                            "exclusive", "time", "partition", "account", "qos")
                if scheduler_spec.get(key) is not None
            }
            install_job_shape["overrides"] = sorted(overrides)
        else:
            scheduler_spec.pop("install", None)
        scheduler_spec["hostfile"] = str(execution_root / "hostfile.txt")
        scheduler_spec.setdefault("output", str(execution_root / "stdout.log"))
        scheduler_spec.setdefault("error", str(execution_root / "stderr.log"))
        for scheduler_key in ("output", "error"):
            scheduler_spec[scheduler_key] = _absolute_scheduler_log_path(
                scheduler_spec[scheduler_key]
            )
        # Directive order must not depend on how the pipeline was loaded (a
        # YAML keeps its order, the saved config is sorted): +plan binds to it.
        return dict(sorted(scheduler_spec.items())), install_job_shape

    @staticmethod
    def _submission_action_args(
        action: str, install_check: bool, has_answers: bool, input_dir: Path,
    ) -> List[str]:
        """Trusted arguments of the ``jarvis ppl`` command the job runs."""
        action_args: List[str] = []
        if action == "install":
            if install_check:
                action_args.append("+check")
            if has_answers:
                action_args += ["--answers", str(input_dir / "answers.json")]
            action_args.append("+json")
        return action_args

    @staticmethod
    def _sealed_install_policy(action: str) -> Optional[str]:
        if action != "run":
            return None
        configured = os.environ.get("JARVIS_INSTALL_ON_LOAD", "").strip().lower()
        return configured if configured in ("install", "check", "skip") else None

    def _render_submission(
        self,
        execution_id: str,
        *,
        action: str = "run",
        install_check: bool = False,
        answers_bytes: Optional[bytes] = None,
    ) -> Dict[str, Any]:
        """Render exactly what ``submit`` would write, without writing it.

        Returns the scheduler spec, the Scheduler, the script text and the
        digests that bind a plan to the later submission: ``script_sha256``
        (the batch script bytes) and ``input_sha256`` (the sealed snapshot
        input, the same digest the record keeps as
        ``pipeline_snapshot_sha256``).
        """
        from jarvis_cd.core.scheduler import make_scheduler
        from jarvis_cd.core.submit_plan import submission_plan_sha256

        execution_root = self._execution_store().executions_dir / execution_id
        scheduler_spec, install_job_shape = self._submission_scheduler_spec(
            execution_root, action, install_check)
        input_dir = execution_root / "input"
        sched = make_scheduler(
            scheduler_spec,
            execution_root,
            pipeline_name=self.name,
            pipeline_snapshot_dir=execution_root / "runtime",
            jarvis_root=getattr(self.jarvis, "jarvis_root", None),
            pipeline_action=action,
            pipeline_action_args=self._submission_action_args(
                action, install_check, answers_bytes is not None, input_dir),
            install_on_load=self._sealed_install_policy(action),
        )
        text = sched.render()
        script_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        input_sha256 = self._execution_snapshot_digest(
            scheduler_spec,
            {"answers.json": answers_bytes} if answers_bytes is not None else None,
        )
        return {
            "execution_root": execution_root,
            "scheduler_spec": scheduler_spec,
            "install_job_shape": install_job_shape,
            "scheduler": sched,
            "script_text": text,
            "script_sha256": script_sha256,
            "input_sha256": input_sha256,
            "plan_sha256": submission_plan_sha256(script_sha256, input_sha256),
        }

    def plan_submission(
        self,
        execution_id: Optional[str] = None,
        *,
        action: str = "run",
        install_check: bool = False,
        install_answers: Optional[str] = None,
        install_outcome: Optional[Dict[str, Any]] = None,
        pipeline_file: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resolve one submission without submitting it (``+plan``).

        Returns a ``jarvis.submit-plan.v1`` document: the scheduler, its
        directives, the total nodes, the components and couplings, the exact
        batch script with its sha256, and any issues that refuse the submit.
        Nothing is written to the execution store, no Spack and no scheduler
        command runs. ``jarvis ppl submit ... --execution-id ID --plan_sha256
        SHA`` then submits only if it renders the same script and input.
        """
        from jarvis_cd.core.submit_plan import build_submit_plan

        if action not in ("run", "install"):
            raise ValueError("submit action must be 'run' or 'install'")
        if not self.scheduler:
            raise ValueError(
                "Pipeline has no scheduler block. Add `scheduler:` to the "
                "pipeline YAML before planning a submission."
            )
        if not self.name:
            raise ValueError("Pipeline name not set; cannot plan a submission.")
        answers_bytes = None
        if install_answers:
            from jarvis_cd.core.spack_install import load_answers

            answers_file = Path(install_answers).expanduser()
            load_answers(answers_file)
            answers_bytes = answers_file.read_bytes()
        return build_submit_plan(
            self,
            _validated_execution_id(execution_id),
            action=action,
            install_check=install_check,
            answers_bytes=answers_bytes,
            install_outcome=install_outcome,
            pipeline_file=pipeline_file,
        )

    def submit(
        self,
        submit: bool = True,
        wait: bool = False,
        execution_id: Optional[str] = None,
        action: str = "run",
        install_check: bool = False,
        install_answers: Optional[str] = None,
        expect_plan_sha256: Optional[str] = None,
    ) -> ExecutionHandle:
        """Write the scheduler job script and (optionally) submit it.

        :param submit: when True, exec the scheduler's submit command
            (e.g. ``sbatch``); when False, only write the script without
            submitting it. The returned handle exposes it as ``script_path``.
        :param wait: when True (and submit is True), ask the scheduler
            to block until the job finishes. SLURM maps this to
            ``sbatch --wait``; backends without an equivalent ignore it.
        :param execution_id: bounded caller-owned identity used to isolate the
            submitted pipeline, environment, script, and hostfile.
        :param action: ``run`` (default) runs the pipeline; ``install`` runs
            ``jarvis ppl install`` on one node of the allocation instead.
        :param install_check: with ``action='install'``, only check.
        :param install_answers: with ``action='install'``, an answers.json
            sealed into the execution input and applied by the job.
        :return: Queryable JARVIS execution handle. The generated script remains
            available as ``handle.script_path`` and in ``last_submission``.
        """
        if action not in ("run", "install"):
            raise ValueError("submit action must be 'run' or 'install'")
        if action != "install" and (install_check or install_answers):
            raise ValueError("install options require action='install'")
        if not self.scheduler:
            raise ValueError(
                "Pipeline has no scheduler block. Add `scheduler:` to the "
                "pipeline YAML before calling submit()."
            )
        if not self.name:
            raise ValueError("Pipeline name not set; cannot submit job.")
        site_facts_report = None
        if action == "run":
            from jarvis_cd.core.pipeline_check import enforce_pipeline_check

            # Refuse a pipeline whose coupling or configuration rules are
            # broken before anything reaches the scheduler. A blocking
            # site-fact question refuses with exit 3; advisories are recorded.
            site_facts_report = enforce_pipeline_check(self).get("site_facts")
        install_on_load_meta = None
        sealed_install_policy = None
        if action == "run":
            configured_policy = os.environ.get("JARVIS_INSTALL_ON_LOAD", "").strip().lower()
            if configured_policy in ("install", "check", "skip"):
                sealed_install_policy = configured_policy
            activation = getattr(self, "_install_activation", None) or {}
            if activation.get("method") == "deferred":
                check = dict(activation.get("check") or {})
                install_on_load_meta = {
                    "policy": activation.get("policy"),
                    "runs_in": "allocation",
                    "submit_host": {
                        key: check.get(key)
                        for key in ("source", "outcome", "reason", "checked_at",
                                    "checked_where", "record", "missing")
                        if check.get(key) is not None
                    },
                    "advisory": (
                        "install-on-load runs inside the allocation: no Spack ran on "
                        "the submitting host"
                        + (f" ({check.get('reason')})" if check.get("reason") else "")
                        + "; the job reports install questions or failures in "
                          "metadata.install"
                    ),
                }
                print(f"Advisory: {install_on_load_meta['advisory']}", file=sys.stderr)

        from jarvis_cd.core.scheduler import make_scheduler, resolve_scheduler_name

        # ``name: auto`` resolves to the host's scheduler before anything is
        # recorded, so the record names the scheduler actually used.
        scheduler_provider = resolve_scheduler_name(self.scheduler)
        answers_bytes: Optional[bytes] = None
        if install_answers:
            from jarvis_cd.core.spack_install import load_answers

            answers_file = Path(install_answers).expanduser()
            load_answers(answers_file)  # reject malformed answers before submitting
            answers_bytes = answers_file.read_bytes()

        shared_dir = self.get_pipeline_shared_dir()
        shared_dir.mkdir(parents=True, exist_ok=True)
        resolved_execution_id = _validated_execution_id(execution_id)
        store = self._execution_store()
        bound_plan = None
        if expect_plan_sha256 is not None:
            # The person's yes binds to one plan: refuse before anything is
            # recorded or submitted when this submission renders differently.
            self.save()
            bound_plan = self._render_submission(
                resolved_execution_id, action=action, install_check=install_check,
                answers_bytes=answers_bytes)
            _require_bound_plan(bound_plan, expect_plan_sha256, resolved_execution_id)
        recorded_facts = None
        if site_facts_report and "issues" in site_facts_report:
            from jarvis_cd.core.site_facts import SITE_FACTS_FILE, record_site_facts

            recorded_facts = record_site_facts(
                site_facts_report,
                full_in=str(store.executions_dir / resolved_execution_id / SITE_FACTS_FILE))
        create_metadata: Dict[str, Any] = {}
        if recorded_facts:
            create_metadata["site_facts"] = recorded_facts
        if install_on_load_meta:
            create_metadata["install_on_load"] = install_on_load_meta
        store.create(
            resolved_execution_id,
            mode="scheduler",
            scheduler_provider=scheduler_provider,
            **({"metadata": create_metadata} if create_metadata else {}),
        )
        execution_root = store.executions_dir / resolved_execution_id
        if recorded_facts:
            from jarvis_cd.core.site_facts import write_site_facts_file

            write_site_facts_file(execution_root, site_facts_report)

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
            scheduler_spec, install_job_shape = self._submission_scheduler_spec(
                execution_root, action, install_check)
            _prepare_scheduler_log_parents(scheduler_spec)
            snapshot_dir, input_dir, snapshot_sha256 = self._write_execution_snapshot(
                execution_root,
                scheduler_spec,
                extra_inputs=(
                    {"answers.json": answers_bytes} if answers_bytes is not None else None
                ),
            )
            self._register_execution_snapshot_artifacts(
                store,
                resolved_execution_id,
            )
            action_args = self._submission_action_args(
                action, install_check, answers_bytes is not None, input_dir)
            if action == "install":
                install_dir = execution_root / "install"
                store.update(
                    resolved_execution_id,
                    metadata={
                        "action": "install",
                        "install_job": install_job_shape,
                        "install": {
                            "schema_version": "jarvis.install.progress.v1",
                            "mode": "check" if install_check else "install",
                            "phase": "queued",
                            "log": str(install_dir / "install.log"),
                            "issues": str(install_dir / "issues.jsonl"),
                            "record": str(install_dir / "install-record.json"),
                            "updated_at": _utc_timestamp(),
                        },
                    },
                )
            sched = make_scheduler(
                scheduler_spec,
                execution_root,
                pipeline_name=self.name,
                pipeline_snapshot_dir=snapshot_dir,
                jarvis_root=getattr(self.jarvis, "jarvis_root", None),
                pipeline_action=action,
                pipeline_action_args=action_args,
                install_on_load=sealed_install_policy,
            )
            script_path = sched.write_script()
            from jarvis_cd.core.submit_plan import submission_plan_sha256

            written_script_sha256 = hashlib.sha256(script_path.read_bytes()).hexdigest()
            submission_plan = {
                "schema_version": "jarvis.submit-plan.binding.v1",
                "script_sha256": written_script_sha256,
                "input_sha256": snapshot_sha256,
                "plan_sha256": submission_plan_sha256(
                    written_script_sha256, snapshot_sha256),
                "bound": expect_plan_sha256 is not None,
            }
            if expect_plan_sha256 is not None:
                submission_plan["expected_plan_sha256"] = expect_plan_sha256
            store.update(resolved_execution_id,
                         metadata={"submission_plan": submission_plan,
                                   "component_states": _component_states_metadata()})
            if expect_plan_sha256 is not None:
                _require_bound_plan(
                    {"plan_sha256": submission_plan["plan_sha256"],
                     "script_sha256": written_script_sha256,
                     "input_sha256": snapshot_sha256},
                    expect_plan_sha256, resolved_execution_id)
            self._components_waiting(execution_root, action=action, hosts_known=False)
            try:
                script_relative_path = script_path.relative_to(
                    execution_root
                ).as_posix()
            except ValueError:
                script_relative_path = None
            if script_relative_path is not None:
                self._register_execution_file(
                    store,
                    resolved_execution_id,
                    logical_name="scheduler-script",
                    relative_path=script_relative_path,
                    kind="script",
                    role=ArtifactRole.PROVENANCE,
                    media_type="text/x-shellscript",
                    format_name="slurm-shell",
                )
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

        try:
            for logical_name, scheduler_key in (
                ("stdout", "output"),
                ("stderr", "error"),
            ):
                self._register_scheduler_log_artifact(
                    store,
                    resolved_execution_id,
                    execution_root=execution_root,
                    provider=sched.NAME,
                    array_requested=sched.array_requested,
                    logical_name=logical_name,
                    configured_path=str(scheduler_spec[scheduler_key]),
                )
        except BaseException as exc:
            self.last_submission["state"] = "pre_submit_failed"
            self.last_submission["terminal"] = True
            persist_stage_exception(
                exc,
                "scheduler_artifact_registration",
                ambiguous=False,
            )
            raise

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
            provider_value = provider_metadata.get("provider")
            native_id_value = provider_metadata.get("scheduler_job_id")
            if not isinstance(provider_value, str) or not isinstance(
                native_id_value, str
            ):
                raise RuntimeError(
                    "scheduler provider returned incomplete artifact identity"
                )
            self._update_execution_marker(execution_root)
            # Persist the provider-owned identity immediately after parsing;
            # artifact resolution, logging, and state decoration must not
            # reopen the orphan gap for an accepted scheduler job.
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
        try:
            store.resolve_scheduler_artifact_paths(
                resolved_execution_id,
                provider=provider_value,
                native_id=native_id_value,
            )
        except BaseException as exc:
            self.last_submission["state"] = "artifact_resolution_failed"
            self.last_submission["terminal"] = False
            persist_stage_exception(
                exc,
                "scheduler_artifact_resolution",
                ambiguous=True,
                accepted=True,
            )
            raise RuntimeError(
                "Scheduler accepted job "
                f"{self.last_submission['scheduler_job_id']} and JARVIS persisted "
                "its identity, but scheduler artifact path resolution failed"
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
        store = self._execution_store()
        record = store.get(execution_id)
        if (
            record.mode != "scheduler"
            or record.terminal
            or not record.submitted
            or record.scheduler_provider is None
            or record.scheduler_native_id is None
        ):
            return record

        from jarvis_cd.core.scheduler import (
            query_scheduler_execution,
            query_scheduler_queue,
        )

        observation = query_scheduler_execution(
            record.scheduler_provider,
            record.scheduler_native_id,
        )
        if observation.state == "unknown":
            return record
        queue_facts = None
        reconciliation: Optional[Dict[str, Any]] = None
        if observation.state == "missing":
            created = datetime.fromisoformat(record.created_at.replace("Z", "+00:00"))
            age_seconds = (datetime.now(timezone.utc) - created).total_seconds()
            if age_seconds < _SCHEDULER_MISSING_GRACE_SECONDS:
                return record
            state, return_code, error, basis = self._reconcile_vanished_job(
                store, record, observation
            )
            terminal = True
            reconciliation = {
                "basis": basis,
                "accounting": observation.accounting,
                "record_state_before": record.state,
            }
        else:
            state = observation.state
            terminal = observation.terminal
            return_code = observation.return_code
            error = (
                f"scheduler {record.scheduler_provider} reported "
                f"{observation.provider_state} for job {record.scheduler_native_id}"
                if state in {"failed", "canceled"}
                else None
            )
            if state == "submitted":
                queue_facts = query_scheduler_queue(
                    record.scheduler_provider, record.scheduler_native_id
                )
        observation_document: Dict[str, Any] = {
            "schema_version": "jarvis.scheduler.observation.v1",
            "provider": record.scheduler_provider,
            "native_id": record.scheduler_native_id,
            "state": observation.state,
            "provider_state": observation.provider_state,
            "diagnostic": observation.diagnostic,
            "basis": observation.basis,
            "accounting": observation.accounting,
        }
        if reconciliation is not None:
            observation_document["reconciliation"] = reconciliation
        metadata: Dict[str, Any] = {"scheduler_observation": observation_document}
        if queue_facts is not None:
            metadata["queue"] = queue_facts
        elif (
            isinstance(record.metadata.get("queue"), dict)
            and record.metadata["queue"].get("state") == "pending"
            and state != "submitted"
        ):
            # the job left the queue: keep the last facts, marked as historical
            metadata["queue"] = dict(record.metadata["queue"], state="left_queue")
        if (
            state == record.state
            and terminal == record.terminal
            and return_code == record.return_code
            and error == record.error
            and "queue" not in metadata
        ):
            return record
        try:
            if state == record.state and not terminal:
                return store.update(execution_id, metadata=metadata)
            return store.update(
                execution_id,
                state=state,
                terminal=terminal,
                return_code=return_code,
                error=error,
                metadata=metadata,
            )
        except ValueError:
            latest = store.get(execution_id)
            if latest.terminal:
                return latest
            raise

    def _reconcile_vanished_job(
        self, store, record: ExecutionRecord, observation
    ) -> tuple[str, int, str, str]:
        """Terminal (state, rc, error, basis) for a job the scheduler forgot.

        Works without scheduler accounting (sites with no slurmdbd): a job
        that ended normally would have finalized its own record from the
        batch script, so a still-open record tells what happened.

        - a ``cancel_request`` or ``stop_request`` was recorded: ``canceled``;
        - the record never left ``submitted`` and the job wrote no batch
          output: it never started, so it was removed while pending
          (``canceled`` outside JARVIS);
        - otherwise the runtime started and was lost: ``failed``.
        """
        provider = record.scheduler_provider
        native_id = record.scheduler_native_id
        unaccounted = (
            "; scheduler accounting is unavailable"
            if observation.accounting == "unavailable"
            else ""
        )
        for key in ("cancel_request", "stop_request"):
            request = record.metadata.get(key)
            if isinstance(request, dict) and request.get("status") != "failed":
                return (
                    "canceled",
                    143,
                    f"scheduler {provider} job {native_id} ended after jarvis "
                    f"execution {key.split('_')[0]}{unaccounted}",
                    key,
                )
        execution_root = store.executions_dir / record.execution_id
        batch_output = [
            path
            for path in (execution_root / "stdout.log", execution_root / "stderr.log")
            if path.is_file() and path.stat().st_size > 0
        ]
        if record.state in {"submitted", "unknown"} and not batch_output:
            return (
                "canceled",
                143,
                f"scheduler {provider} job {native_id} left the queue before it "
                f"started (removed outside JARVIS){unaccounted}",
                "vanished_pending",
            )
        return (
            "failed",
            1,
            f"scheduler {provider} no longer reports job {native_id}, and its "
            f"JARVIS runtime never finalized the record (lost){unaccounted}",
            "vanished_running",
        )

    def move_execution(self, execution_id: str, queue: str) -> ExecutionRecord:
        """Move a pending scheduler execution to another queue (partition).

        Refuses with :class:`~jarvis_cd.core.issues.JarvisIssueError`
        (``execution_move_refused``) when the record is not a pending
        scheduler job, the provider has no move verb, or the scheduler
        rejects the move. The request and its outcome are recorded as
        ``move_request`` metadata either way.
        """
        from jarvis_cd.core.issues import JarvisIssueError, make_jarvis_issue
        from jarvis_cd.core.scheduler import (
            SchedulerControlUnsupported,
            move_scheduler_execution,
        )

        store = self._execution_store()
        record = self.get_execution(execution_id)

        def refuse(reason: str, message: str, **details: Any) -> JarvisIssueError:
            issue = make_jarvis_issue(
                kind="execution_move_refused",
                subject=f"{record.execution_id}:{queue}",
                message=message,
                identity={"reason": reason, "state": record.state},
                field="scheduler.queue",
                details={"reason": reason, "queue": queue, "state": record.state, **details},
            )
            return JarvisIssueError(
                [issue],
                document={"execution": record.to_dict()},
            )

        if record.mode != "scheduler":
            raise refuse("not_scheduler", "only scheduler executions can be moved")
        if record.terminal or record.state != "submitted":
            raise refuse(
                "not_pending",
                f"execution {execution_id} is {record.state}; only a pending job "
                "can move to another queue",
            )
        provider, native_id = self._scheduler_control_target(record, "move")
        control_cluster = self._scheduler_control_cluster(record)
        previous = None
        if isinstance(record.metadata.get("queue"), dict):
            previous = record.metadata["queue"].get("partition")
        request: Dict[str, Any] = {
            "schema_version": "jarvis.execution.control.v1",
            "action": "move",
            "status": "requested",
            "requested_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "provider": provider,
            "native_id": native_id,
            "from_queue": previous,
            "to_queue": queue,
        }
        try:
            result = move_scheduler_execution(
                provider, native_id, queue, cluster=control_cluster
            )
        except SchedulerControlUnsupported as exc:
            request["status"] = "unsupported"
            store.update(execution_id, metadata={"move_request": request})
            raise refuse("unsupported_provider", str(exc), provider=provider) from exc
        except ValueError as exc:
            raise refuse("invalid_queue", str(exc)) from exc
        request["result"] = result
        if not result["accepted"]:
            request["status"] = "refused"
            store.update(execution_id, metadata={"move_request": request})
            raise refuse(
                str(result["refusal"]),
                f"{result['command'][0]} refused to move job {native_id} to "
                f"{queue}: {result['diagnostic'] or 'exit ' + str(result['return_code'])}",
                diagnostic=result["diagnostic"],
            )
        request["status"] = "accepted"
        store.update(execution_id, metadata={"move_request": request})
        return self.get_execution(execution_id)

    def _scheduler_control_target(
        self,
        record: ExecutionRecord,
        action: str,
    ) -> tuple[str, str]:
        """Return (provider, native id) or refuse a record that has no job."""
        if record.mode != "scheduler":
            alternative = "jarvis ppl kill" if action == "cancel" else "jarvis ppl stop"
            raise ValueError(
                f"execution {record.execution_id} is a direct execution; "
                f"jarvis execution {action} only controls scheduler jobs "
                f"(use {alternative})"
            )
        if record.scheduler_provider is None or record.scheduler_native_id is None:
            raise RuntimeError(
                f"execution {record.execution_id} has no scheduler job yet "
                f"(state {record.state}); retry jarvis execution {action} after "
                "the submission is recorded"
            )
        return record.scheduler_provider, record.scheduler_native_id

    @staticmethod
    def _scheduler_control_cluster(record: ExecutionRecord) -> Optional[str]:
        """Return the cluster ``sbatch --parsable`` reported, else ``None``.

        ``record.cluster`` may come from ``SLURM_CLUSTER_NAME`` inside the
        job, which every Slurm job has. Passing that to ``scancel --clusters``
        needs slurmdbd and fails on sites without it, so control commands
        only name a cluster that the submit itself reported (``jobid;cluster``).
        """
        from collections.abc import Mapping

        submission = record.metadata.get("submission")
        if not isinstance(submission, Mapping):
            return None
        if submission.get("identity_source") != "scheduler_submit_api":
            return None
        if submission.get("cluster_identity_source") is not None:
            return None
        cluster = submission.get("scheduler_cluster")
        return cluster if isinstance(cluster, str) and cluster else None

    def cancel_execution(self, execution_id: str) -> ExecutionRecord:
        """Cancel one scheduler execution and commit ``canceled``.

        Idempotent: a terminal record is returned unchanged without touching
        the scheduler, and a job the scheduler already removed is reconciled
        instead of failing.
        """
        from jarvis_cd.core.scheduler import (
            control_scheduler_execution,
            scheduler_control_argv,
        )

        store = self._execution_store()
        record = self.get_execution(execution_id)
        if record.terminal:
            return record
        provider, native_id = self._scheduler_control_target(record, "cancel")
        control_cluster = self._scheduler_control_cluster(record)
        argv = scheduler_control_argv(
            provider, "cancel", native_id, cluster=control_cluster
        )
        request: Dict[str, Any] = {
            "schema_version": "jarvis.execution.control.v1",
            "action": "cancel",
            "status": "requested",
            "requested_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "provider": provider,
            "native_id": native_id,
            "cluster": control_cluster,
            "recorded_cluster": record.cluster,
            "command": argv,
        }
        # The request is durable before the scheduler acts, so the job's own
        # TERM/EXIT handling finalizes it as canceled even if it wins the race.
        store.update(execution_id, metadata={"cancel_request": request})
        result = control_scheduler_execution(
            provider, "cancel", native_id, cluster=control_cluster
        )
        request["result"] = result.to_dict()
        if not (result.accepted or result.job_ended):
            request["status"] = "failed"
            store.update(execution_id, metadata={"cancel_request": request})
            raise RuntimeError(
                f"{argv[0]} could not cancel {provider} job {native_id}: "
                f"{result.diagnostic or 'exit ' + str(result.return_code)}"
            )
        request["status"] = "accepted" if result.accepted else "job_ended"
        if result.job_ended:
            reconciled = self.get_execution(execution_id)
            if reconciled.terminal:
                return store.update(execution_id, metadata={"cancel_request": request})
        error = "canceled by jarvis execution cancel"
        if result.job_ended:
            error += "; the scheduler no longer had the job"
        try:
            return store.update(
                execution_id,
                state="canceled",
                terminal=True,
                error=error,
                metadata={"cancel_request": request},
            )
        except ValueError:
            latest = store.get(execution_id)
            if latest.terminal:
                return store.update(execution_id, metadata={"cancel_request": request})
            raise

    def stop_execution(
        self,
        execution_id: str,
        *,
        wait_seconds: Optional[float] = None,
    ) -> ExecutionRecord:
        """Ask a running scheduler job to stop its pipeline gracefully.

        Sends TERM to the batch shell only. The generated script traps it,
        runs ``jarvis ppl stop``, reaps the pipeline's process tree, and
        finalizes the record. A job that has not started is canceled instead.
        With ``wait_seconds`` the call polls until the record is terminal or
        the time runs out; otherwise it returns at once with ``stop_request``
        metadata and the caller polls ``jarvis execution get``.
        """
        import time

        from jarvis_cd.core.scheduler import (
            control_scheduler_execution,
            scheduler_control_argv,
        )

        store = self._execution_store()
        record = self.get_execution(execution_id)
        if record.terminal:
            return record
        provider, native_id = self._scheduler_control_target(record, "stop")
        control_cluster = self._scheduler_control_cluster(record)
        argv = scheduler_control_argv(
            provider, "stop", native_id, cluster=control_cluster
        )
        request: Dict[str, Any] = {
            "schema_version": "jarvis.execution.control.v1",
            "action": "stop",
            "status": "requested",
            "requested_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "provider": provider,
            "native_id": native_id,
            "cluster": control_cluster,
            "recorded_cluster": record.cluster,
            "command": argv,
        }
        if record.state == "submitted":
            request["status"] = "fallback_cancel"
            request["fallback"] = "cancel"
            request["command"] = None
            store.update(execution_id, metadata={"stop_request": request})
            return self.cancel_execution(execution_id)
        store.update(execution_id, metadata={"stop_request": request})
        result = control_scheduler_execution(
            provider, "stop", native_id, cluster=control_cluster
        )
        request["result"] = result.to_dict()
        if result.job_pending:
            request["status"] = "fallback_cancel"
            request["fallback"] = "cancel"
            store.update(execution_id, metadata={"stop_request": request})
            return self.cancel_execution(execution_id)
        if result.job_ended:
            request["status"] = "job_ended"
            store.update(execution_id, metadata={"stop_request": request})
            return self.get_execution(execution_id)
        if not result.accepted:
            request["status"] = "failed"
            store.update(execution_id, metadata={"stop_request": request})
            raise RuntimeError(
                f"{argv[0]} could not signal {provider} job {native_id}: "
                f"{result.diagnostic or 'exit ' + str(result.return_code)}"
            )
        request["status"] = "accepted"
        latest = store.update(execution_id, metadata={"stop_request": request})
        if wait_seconds is None or wait_seconds <= 0:
            return latest
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            latest = self.get_execution(execution_id)
            if latest.terminal:
                return latest
            time.sleep(1.0)
        return self.get_execution(execution_id)

    def install(
        self,
        check: bool = False,
        answers_path: Optional[str] = None,
        allow_local: bool = False,
    ) -> Dict[str, Any]:
        """Run (or check) the install phase with a streamable log.

        Writes ``install.log`` (append-only), ``issues.jsonl`` and
        ``install-record.json`` under ``<execution>/install`` inside a
        scheduler job, otherwise under ``<pipeline shared>/install``. Inside a
        job, progress is mirrored into the execution record's ``install``
        metadata. Raises ``InstallDecisionNeeded`` (CLI exit 3) for questions
        or, with ``check``, for packages that still need building.

        Building (``spack install``, env concretize, container/pip/conda
        builds) is refused outside a scheduler allocation with
        ``BuildOutsideAllocation`` (CLI exit 4) unless ``allow_local`` or
        ``JARVIS_ALLOW_LOCAL_BUILD=1``; a check never builds.
        """
        from jarvis_cd.core.installer import (
            Installer,
            install_context_for,
            write_last_install,
        )
        from jarvis_cd.core.spack_install import (
            PROGRESS_SCHEMA,
            BuildOutsideAllocation,
            InstallDecisionNeeded,
            InstallFailed,
            load_answers,
        )

        mode = "check" if check else "install"
        context = install_context_for(
            self, mode, require_allocation=not allow_local
        )
        if answers_path:
            context.answers = load_answers(Path(answers_path))
            context.answers_path = Path(answers_path)
        execution_id = getattr(self, "_execution_id", None)
        store = (
            self._execution_store()
            if getattr(self, "_execution_root", None) is not None and execution_id
            else None
        )
        progress: Dict[str, Any] = {
            "schema_version": PROGRESS_SCHEMA,
            "mode": mode,
            "phase": "starting",
            "packages_total": sum(
                len(pkgs) for pkgs in Installer.get_installers(self).values()
            ),
            "packages_done": 0,
            "current_package": None,
            "missing": [],
            "log": str(context.log_path),
            "issues": str(context.issues_path),
            "record": str(context.record_path),
        }

        def publish(**fields: Any) -> None:
            progress.update(fields)
            progress["updated_at"] = _utc_timestamp()
            if store is None:
                return
            try:
                store.update(execution_id, metadata={"install": dict(progress)})
            except Exception as exc:  # progress must never break the build
                context.log(f"WARNING: could not update execution progress: {exc}\n")

        context.progress = publish
        context.open()
        publish()
        if store is not None:
            from jarvis_cd.core.component_states import append_component_state

            self._record_scheduler_job(store, execution_id)
            append_component_state(self._execution_root, "install", "running",
                                   package="jarvis.install",
                                   hosts=[socket.gethostname()],
                                   reason=f"install {mode}")
        outcome = "failed"
        try:
            report = Installer.install_all(self, mode=mode, context=context)
            outcome = "done"
        except InstallDecisionNeeded as needed:
            outcome = "needs_decision"
            publish(
                phase="needs_decision",
                current_package=None,
                issue_count=len(needed.issues),
            )
            raise
        except BuildOutsideAllocation as refused:
            outcome = "refused"
            publish(phase="refused", current_package=None, refused=refused.to_dict())
            raise
        except InstallFailed as failed:
            outcome = "failed"
            context.log(f"ERROR: {failed}\n")
            publish(
                phase="failed",
                current_package=None,
                error=str(failed)[:2000],
                open_issues=[issue["issue_id"] for issue in failed.issues],
                issue_kinds=sorted({str(issue.get("kind")) for issue in failed.issues}),
            )
            raise
        except BaseException as exc:
            context.log(f"ERROR: {exc}\n")
            publish(phase="failed", current_package=None, error=str(exc)[:2000])
            raise
        finally:
            context.close()
            write_last_install(self, context, execution_id=execution_id, phase=outcome)
        self.save()
        publish(
            phase="done",
            current_package=None,
            packages_done=progress["packages_total"],
            satisfied=bool(report.get("satisfied")),
        )
        report["record_path"] = str(context.record_path)
        return report

    def list_executions(self) -> List[ExecutionRecord]:
        """Return all durable executions owned by this pipeline."""
        return self._execution_store().list()

    def get_execution_progress(
        self,
        execution_id: str,
    ) -> ExecutionProgressSnapshot:
        """Return validated package progress for one exact execution."""
        return self._execution_store().progress(execution_id)

    def get_execution_artifacts(
        self,
        execution_id: str,
    ) -> ExecutionArtifactSnapshot:
        """Return generated artifacts for one exact execution."""
        return self._execution_store().artifacts(execution_id)

    def get_execution_service_runtimes(
        self,
        execution_id: str,
    ) -> ExecutionServiceRuntimeSnapshot:
        """Return current service runtimes for one exact execution."""
        return self._execution_store().service_runtimes(execution_id)

    def resolve_execution_service_runtime_authority(
        self,
        execution_id: str,
        *,
        package_id: str,
        service_instance_id: str,
        revision: int,
        token_sha256: str,
    ) -> ExecutionServiceRuntimeAuthority:
        """Resolve one private service authority for a trusted connector."""
        return self._execution_store().resolve_service_runtime_authority(
            execution_id,
            package_id=package_id,
            service_instance_id=service_instance_id,
            revision=revision,
            token_sha256=token_sha256,
        )

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
        submission_projection = dict(submission)
        if cluster is None and current.cluster is not None:
            cluster = current.cluster
            submission["scheduler_cluster"] = cluster
            submission["cluster_identity_source"] = "scheduler_runtime_environment"
            submission_projection["scheduler_cluster"] = cluster
            submission_projection["cluster_identity_source"] = (
                "scheduler_runtime_environment"
            )
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
                "submission": submission_projection,
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

    def _execution_snapshot_payload(
        self, scheduler_spec: Dict[str, Any],
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """The ``pipeline.yaml`` and ``environment.yaml`` one snapshot seals."""
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
        return pipeline_config, dict(self.env)

    @staticmethod
    def _sealed_inputs_digest(paths: List[Path]) -> str:
        digest = hashlib.sha256()
        for path in paths:
            data = path.read_bytes()
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        return digest.hexdigest()

    def _execution_snapshot_digest(
        self,
        scheduler_spec: Dict[str, Any],
        extra_inputs: Optional[Dict[str, bytes]] = None,
    ) -> str:
        """The snapshot digest ``_write_execution_snapshot`` would record."""
        pipeline_config, environment = self._execution_snapshot_payload(scheduler_spec)
        with tempfile.TemporaryDirectory(prefix="jarvis-plan-") as scratch:
            scratch_dir = Path(scratch)
            _atomic_yaml_dump(scratch_dir / "pipeline.yaml", pipeline_config)
            _atomic_yaml_dump(scratch_dir / "environment.yaml", environment)
            sealed = [scratch_dir / "pipeline.yaml", scratch_dir / "environment.yaml"]
            for name, data in sorted((extra_inputs or {}).items()):
                (scratch_dir / name).write_bytes(data)
                sealed.append(scratch_dir / name)
            return self._sealed_inputs_digest(sealed)

    def _write_execution_snapshot(
        self,
        execution_root: Path,
        scheduler_spec: Dict[str, Any],
        extra_inputs: Optional[Dict[str, bytes]] = None,
    ) -> tuple[Path, Path, str]:
        """Seal immutable input and create an isolated scheduler working copy.

        ``extra_inputs`` are additional sealed files (e.g. install answers)
        written into ``input/`` and covered by the snapshot digest.
        """
        pipeline_config, environment = self._execution_snapshot_payload(scheduler_spec)

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
            _atomic_yaml_dump(directory / "environment.yaml", environment)
        sealed = [input_dir / "pipeline.yaml", input_dir / "environment.yaml"]
        for name, data in sorted((extra_inputs or {}).items()):
            if "/" in name or "\\" in name or name.startswith("."):
                raise ValueError(f"invalid sealed input name {name!r}")
            (input_dir / name).write_bytes(data)
            sealed.append(input_dir / name)

        snapshot_sha256 = self._sealed_inputs_digest(sealed)
        for path in sealed:
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
        return runtime_dir, input_dir, snapshot_sha256

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

        self._validate_interceptor_links()

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
        if getattr(self, "couplings", None):
            pipeline_config["couplings"] = [dict(item) for item in self.couplings]

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

        self._validate_interceptor_links()
        logger.pipeline(f"Starting pipeline: {self.name}")

        # Store started instances for later access (e.g., _get_stat)
        self._started_instances = []

        # Check if pipeline is configured for containerized deployment
        if self.is_containerized():
            # Container deployment mode - deploy to all nodes in hostfile
            self._start_containerized_pipeline()
        else:
            # Standard deployment mode - start each package individually.
            # Background packages (services) run start() in a thread; the next
            # package starts once they report ready, and start() returns only
            # after every background start() has returned (e.g. on ppl stop).
            background: List[Dict[str, Any]] = []
            last_step_index: Optional[int] = None
            self._stopped_after_steps: set = set()
            for index, pkg_def in enumerate(self.packages):
                try:
                    # Print BEGIN message
                    logger.success(f"[{pkg_def['pkg_type']}] [START] BEGIN")
                    self._component_state(pkg_def, "starting",
                                          hosts=self._component_hosts(pkg_def))

                    self._bind_package_execution_environment(pkg_def)
                    pkg_instance = self._load_package_instance(pkg_def, self.env)

                    # Apply interceptors to this package before starting
                    self._apply_interceptors_to_package(pkg_instance, pkg_def)

                    if not hasattr(pkg_instance, "start"):
                        logger.warning(
                            f"Package {pkg_def['pkg_id']} has no start method"
                        )
                        self._component_state(pkg_def, "done", reason="no start method")
                    elif self._package_starts_in_background(pkg_instance):
                        phase_started = time.monotonic()
                        self._time_package_phase(pkg_def, pkg_instance, "started",
                                                 phase_started, start_mode="background")
                        handle = self._start_package_in_background(pkg_instance, pkg_def)
                        handle.update(index=index, instance=pkg_instance, pkg_def=pkg_def)
                        background.append(handle)
                        self._time_package_phase(
                            pkg_def, pkg_instance,
                            handle.get("start_outcome") or "ready", phase_started)
                        self._component_state(
                            pkg_def, "running",
                            reason=f"service {handle.get('start_outcome') or 'ready'}")
                    else:
                        phase_started = time.monotonic()
                        self._time_package_phase(pkg_def, pkg_instance, "started",
                                                 phase_started, start_mode="foreground")
                        is_step = self._is_step_package(pkg_instance)
                        if is_step:
                            self._component_state(pkg_def, "running")
                        pkg_instance.start()
                        self._time_package_phase(pkg_def, pkg_instance, "returned",
                                                 phase_started)
                        if is_step:
                            last_step_index = index
                            self._component_state(pkg_def, "done", reason="returned")
                        else:
                            self._component_state(pkg_def, "running", reason="started")

                    # Propagate environment changes to next packages
                    self.env.update(pkg_instance.env)

                    # Keep reference so _get_stat can access exec output
                    self._started_instances.append(pkg_instance)

                    # Print END message
                    logger.success(f"[{pkg_def['pkg_type']}] [START] END")

                except Exception as e:
                    logger.error(f"Error starting package {pkg_def['pkg_id']}: {e}")
                    if self._stop_was_requested():
                        self._component_state(pkg_def, "cancelled",
                                              reason=f"stopped by request: {e}")
                    else:
                        self._component_state(pkg_def, "failed", reason=str(e))
                    raise RuntimeError(
                        f"Pipeline startup failed at package '{pkg_def['pkg_id']}': {e}"
                    ) from e
            self._stop_services_after_steps(background, last_step_index)
            self._wait_background_packages(background)

    @staticmethod
    def _is_step_package(pkg_instance: Any) -> bool:
        """A package whose foreground start() is the work itself (not a service)."""
        from jarvis_cd.core.pkg import Library, Service

        return not isinstance(pkg_instance, (Service, Library))

    @staticmethod
    def _after_steps_policy(handle: Dict[str, Any], last_step_index: Optional[int]) -> str:
        """``stop`` or ``keep`` for one background package once the steps returned.

        ``auto`` (the default) stops a package a step started after, since the
        steps it served are done; one listed after the last step (or in a
        pipeline without steps) is the pipeline's purpose and is kept.
        """
        config = getattr(handle.get("instance"), "config", {}) or {}
        policy = str(config.get("after_steps") or "auto").strip().lower()
        if policy not in ("auto", "stop", "keep"):
            raise ValueError(
                f"{handle['pkg_id']}: after_steps must be auto, stop or keep")
        if policy == "auto":
            if last_step_index is not None and handle.get("index", 0) < last_step_index:
                return "stop"
            return "keep"
        return policy

    def _stop_services_after_steps(
        self, background: List[Dict[str, Any]], last_step_index: Optional[int]
    ) -> None:
        """Stop background packages the finished steps no longer need.

        Runs after every foreground package returned OK. Each stopped
        package's stop() runs here (newest first) and its start() thread is
        joined, so the execution finalizes ``completed`` instead of holding
        the allocation until its wall time. ``Pipeline.stop`` then skips them.
        """
        from jarvis_cd.util.logger import logger

        decisions: List[Dict[str, Any]] = []
        for handle in reversed(background):
            policy = self._after_steps_policy(handle, last_step_index)
            entry: Dict[str, Any] = {"pkg_id": handle["pkg_id"], "after_steps": policy}
            if policy == "stop" and not handle["thread"].is_alive():
                entry["after_steps"] = "already_returned"
            elif policy == "stop":
                if handle["error"] is not None:
                    raise RuntimeError(
                        f"Background package '{handle['pkg_id']}' failed: {handle['error']}"
                    ) from handle["error"]
                logger.info(f"[{handle['pkg_def']['pkg_type']}] [STOP] the pipeline's "
                            "last step returned; stopping (after_steps)")
                stop_started = time.monotonic()
                self._bind_package_execution_environment(handle["pkg_def"])
                instance = handle["instance"]
                if not self._claim_package_stop(str(handle["pkg_id"])):
                    entry["stopped_by"] = "stop_request"
                    logger.info(f"[{handle['pkg_def']['pkg_type']}] [STOP] being "
                                "stopped by this execution's stop request")
                elif hasattr(instance, "stop"):
                    instance.stop()
                # stop() ends the service; give its start() a bounded join.
                timeout = 120.0
                handle["thread"].join(timeout)
                entry["stop_seconds"] = round(time.monotonic() - stop_started, 3)
                if handle["thread"].is_alive():
                    entry["start_thread"] = "still running after stop"
                    logger.warning(f"{handle['pkg_id']}: start() still running "
                                   f"{timeout:g}s after stop(); not waiting on it")
                elif handle["error"] is not None:
                    # start() failing because stop() ended it is the expected exit.
                    entry["start_error_after_stop"] = str(handle["error"])[:512]
                handle["stopped_after_steps"] = True
                self._stopped_after_steps.add(str(handle["pkg_id"]))
                self._component_state(handle["pkg_def"], "done",
                                      reason="stopped after the last step (after_steps)")
            decisions.append(entry)
        timings = getattr(self, "_phase_timings", None)
        if timings is not None and decisions:
            timings["services_after_steps"] = {
                "last_step": (self.packages[last_step_index]["pkg_id"]
                              if last_step_index is not None else None),
                "packages": list(reversed(decisions)),
            }
            for decision in decisions:
                entry = next((e for e in timings["packages"]
                              if e["pkg_id"] == decision["pkg_id"]), None)
                if entry is not None:
                    entry["after_steps"] = decision["after_steps"]
            self._publish_phase_timings()

    @staticmethod
    def _package_starts_in_background(pkg_instance: Any) -> bool:
        probe = getattr(pkg_instance, "starts_in_background", None)
        return bool(probe()) if callable(probe) else False

    PHASE_TIMINGS_SCHEMA = "jarvis.phase-timings.v1"

    def _begin_phase_timings(self, store: Any, execution_id: str) -> None:
        """Start recording where the run's time goes, in ``metadata.phase_timings``."""
        self._phase_clock = time.monotonic()
        self._phase_store = (store, execution_id)
        self._phase_timings: Optional[Dict[str, Any]] = {
            "schema_version": self.PHASE_TIMINGS_SCHEMA,
            "run_started_at": _utc_timestamp(),
            "env_activation": getattr(self, "_install_activation", None),
            "configure": None,
            "packages": [],
            "first_step": None,
            "before_first_step_seconds": None,
        }
        self._publish_phase_timings()

    def _phase_elapsed(self) -> float:
        return round(time.monotonic() - getattr(self, "_phase_clock", time.monotonic()), 3)

    def _publish_phase_timings(self) -> None:
        """Write the timings so far; a run killed at its wall time keeps them."""
        timings = getattr(self, "_phase_timings", None)
        target = getattr(self, "_phase_store", None)
        if not timings or not target:
            return
        try:
            target[0].update(target[1], metadata={"phase_timings": json.loads(
                json.dumps(timings, default=str))})
        except Exception as error:  # timings must never fail a run
            logger.warning(f"phase timings not recorded: {error}")

    def _end_phase_timings(self) -> None:
        self._publish_phase_timings()
        self._phase_timings = None
        self._phase_store = None

    def _time_package_phase(self, pkg_def: Dict[str, Any], pkg_instance: Any,
                            event: str, started: float, **fields: Any) -> None:
        """Record one package's start milestone (``started``, ``ready``, ``returned``)."""
        timings = getattr(self, "_phase_timings", None)
        if timings is None:
            return
        from jarvis_cd.core.pkg import Service

        pkg_id = pkg_def.get("pkg_id")
        entry = next((e for e in timings["packages"] if e["pkg_id"] == pkg_id), None)
        if entry is None:
            entry = {
                "pkg_id": pkg_id,
                "pkg_type": pkg_def.get("pkg_type"),
                "role": "service" if isinstance(pkg_instance, Service) else "step",
                "started_at": _utc_timestamp(),
                "started_after_seconds": self._phase_elapsed(),
            }
            timings["packages"].append(entry)
        elapsed = round(time.monotonic() - started, 3)
        if event == "started" and entry["role"] == "step" and timings["first_step"] is None:
            activation = (timings.get("env_activation") or {}).get("seconds") or 0
            timings["first_step"] = {
                "pkg_id": pkg_id, "started_at": entry["started_at"],
                "started_after_seconds": entry["started_after_seconds"],
            }
            timings["before_first_step_seconds"] = round(
                activation + entry["started_after_seconds"], 3)
        elif event == "ready":
            entry["ready_at"] = _utc_timestamp()
            entry["ready_seconds"] = elapsed
        elif event == "returned":
            entry["returned_seconds"] = elapsed
            if entry["role"] == "service" and "ready_seconds" not in entry:
                # A foreground service's start() returns once it is ready.
                entry["ready_at"] = _utc_timestamp()
                entry["ready_seconds"] = elapsed
        entry.update(fields)
        self._publish_phase_timings()

    def _component_hosts(self, pkg_def: Dict[str, Any]) -> Optional[List[str]]:
        """The hosts one package runs on (its hostfile, narrowed by host_slice)."""
        try:
            config = pkg_def.get("config") or {}
            path = config.get("hostfile")
            base = Hostfile(path=str(path)) if path else self.get_hostfile()
            hosts = [str(host) for host in base.hosts]
            host_slice = str(config.get("host_slice") or "").strip()
            match = re.fullmatch(r"(-?\d*):(-?\d*)", host_slice) if host_slice else None
            if match is not None:
                start = int(match.group(1)) if match.group(1) else None
                end = int(match.group(2)) if match.group(2) else None
                hosts = hosts[start:end]
            return hosts
        except Exception:
            return None

    def _component_state(self, pkg_def: Dict[str, Any], state: str, *,
                         hosts: Optional[List[str]] = None,
                         reason: Optional[str] = None) -> None:
        """Append one ``jarvis.job.v1`` line for a package of this execution."""
        root = getattr(self, "_execution_root", None)
        if root is None:
            return
        from jarvis_cd.core.component_states import append_component_state

        append_component_state(
            root, str(pkg_def.get("pkg_id")), state,
            package=str(pkg_def.get("pkg_type") or pkg_def.get("pkg_id")),
            hosts=hosts, reason=reason)

    def _component_stopped(self, pkg_def: Dict[str, Any], state: str,
                           reason: str) -> None:
        """Record a stop; a component that never started is ``cancelled``."""
        root = getattr(self, "_execution_root", None)
        if root is None:
            return
        from jarvis_cd.core.component_states import latest_component_states

        latest = latest_component_states(root).get(str(pkg_def.get("pkg_id")))
        if latest is None or latest.get("state") == "waiting":
            self._component_state(pkg_def, "cancelled", reason="not started")
            return
        self._component_state(pkg_def, state, reason=reason)

    def _record_scheduler_job(self, store: Any, execution_id: str) -> None:
        """Record the allocation's real start once (``metadata.scheduler_job``)."""
        try:
            from jarvis_cd.core.scheduler import scheduler_job_start

            current = store.get(execution_id).metadata.get("scheduler_job")
            job = scheduler_job_start()
            if job is None or (isinstance(current, dict)
                               and current.get("job_id") == job["job_id"]):
                return
            store.update(execution_id, metadata={"scheduler_job": job})
        except Exception as exc:  # a start time must never break the run
            logger.warning(f"could not record the scheduler job start: {exc}")

    def _components_waiting(self, execution_root: Any, *, action: str = "run",
                            hosts_known: bool = True) -> None:
        """Every component ``waiting``: at submit (hosts unknown) and at run start."""
        from jarvis_cd.core.component_states import append_component_state

        if action == "install":
            append_component_state(execution_root, "install", "waiting",
                                   package="jarvis.install",
                                   reason="queued" if not hosts_known else None)
            return
        for pkg_def in self.packages:
            append_component_state(
                execution_root, str(pkg_def.get("pkg_id")), "waiting",
                package=str(pkg_def.get("pkg_type") or pkg_def.get("pkg_id")),
                hosts=self._component_hosts(pkg_def) if hosts_known else None,
                reason="queued" if not hosts_known else None)

    def _stop_was_requested(self) -> bool:
        """A cancel/stop of this execution is under way (its stop claims or record)."""
        marker = self._stopped_marker_path()
        if marker is not None:
            claims = marker.parent / "stop-claims"
            if marker.is_file() or (claims.is_dir() and any(claims.iterdir())):
                return True
        execution_id = getattr(self, "_execution_id", None)
        if getattr(self, "_execution_root", None) is None or not execution_id:
            return False
        try:
            metadata = self._execution_store().get(execution_id).metadata
        except Exception:
            return False
        return any(isinstance(metadata.get(key), dict)
                   and metadata[key].get("status") != "failed"
                   for key in ("cancel_request", "stop_request"))

    def _start_package_in_background(
        self, pkg_instance: Any, pkg_def: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Run ``pkg_instance.start()`` in a thread and wait until it is ready.

        Ready means ``pkg_instance.ready()`` is true (by default: its service
        runtime reports ``ready``) or start() already returned. Raises if
        start() fails, the service reports failure, or ``start_timeout``
        seconds pass first.
        """
        import threading
        from jarvis_cd.util.logger import logger

        pkg_id = pkg_def["pkg_id"]
        handle: Dict[str, Any] = {"pkg_id": pkg_id, "error": None}

        def _run() -> None:
            try:
                pkg_instance.start()
            except BaseException as error:  # reported by the waiter
                handle["error"] = error

        thread = threading.Thread(
            target=_run, name=f"jarvis-start-{pkg_id}", daemon=True
        )
        handle["thread"] = thread
        config = getattr(pkg_instance, "config", {}) or {}
        raw_timeout = config.get("start_timeout")
        timeout = float(600 if raw_timeout is None else raw_timeout)
        logger.info(f"[{pkg_def['pkg_type']}] [START] background, waiting for ready")
        thread.start()
        deadline = time.monotonic() + timeout if timeout > 0 else None
        while True:
            if not thread.is_alive():
                if handle["error"] is not None:
                    raise RuntimeError(
                        f"background start failed: {handle['error']}"
                    ) from handle["error"]
                logger.info(f"[{pkg_def['pkg_type']}] [START] start() returned")
                handle["start_outcome"] = "returned"
                return handle
            state = pkg_instance.ready() if hasattr(pkg_instance, "ready") else None
            if state is True:
                logger.success(f"[{pkg_def['pkg_type']}] [START] ready")
                handle["start_outcome"] = "ready"
                return handle
            if state is False:
                raise RuntimeError("service reported failed/stopped before ready")
            if deadline is not None and time.monotonic() >= deadline:
                raise RuntimeError(
                    f"not ready after start_timeout={timeout:g}s"
                )
            thread.join(0.5)

    def _wait_background_packages(self, background: List[Dict[str, Any]]) -> None:
        """Block until every background start() returns; re-raise the first error."""
        from jarvis_cd.util.logger import logger

        background = [h for h in background if not h.get("stopped_after_steps")]
        if not background:
            return
        logger.info(
            "Pipeline started; holding for background package(s) (after_steps "
            "keep, or no step after them): "
            + ", ".join(handle["pkg_id"] for handle in background)
        )
        for handle in background:
            while handle["thread"].is_alive():
                handle["thread"].join(1.0)
        for handle in background:
            if handle["error"] is not None:
                state = "cancelled" if self._stop_was_requested() else "failed"
                self._component_state(handle["pkg_def"], state,
                                      reason=str(handle["error"]))
        for handle in background:
            if handle["error"] is not None:
                raise RuntimeError(
                    f"Background package '{handle['pkg_id']}' failed: {handle['error']}"
                ) from handle["error"]

    def _bind_package_execution_environment(self, pkg_def: Dict[str, Any]) -> None:
        """Bind authoritative package runtime sidecars to the active execution."""
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
        artifact_dir = Path(execution_root) / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            artifact_dir.chmod(0o700)
        artifact_path = artifact_dir / f"{prefix}-{digest}.jsonl"
        runtime_dir = Path(execution_root) / "service-runtimes"
        runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            runtime_dir.chmod(0o700)
        runtime_path = runtime_dir / f"{prefix}-{digest}.jsonl"
        provenance_dir = Path(execution_root) / "provenance"
        provenance_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
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
                "JARVIS_ARTIFACT_PATH": str(artifact_path),
                "JARVIS_ARTIFACT_TRANSPORT": progress_transport,
                "JARVIS_SERVICE_RUNTIME_PATH": str(runtime_path),
                "JARVIS_PROVENANCE_DIR": str(provenance_dir),
            }
        )
        record = self._execution_store().get(execution_id)
        progress_files = dict(record.metadata.get("progress_files", {}))
        progress_files[package_id] = {
            "filename": progress_path.name,
            "package_name": package_name,
        }
        artifact_files = dict(record.metadata.get("artifact_files", {}))
        artifact_files[f"package-{prefix}-{digest}"] = {
            "filename": artifact_path.name,
            "package_id": package_id,
            "package_name": package_name,
        }
        runtime_files = dict(record.metadata.get("service_runtime_files", {}))
        runtime_files[f"package-{prefix}-{digest}"] = {
            "filename": runtime_path.name,
            "package_id": package_id,
            "package_name": package_name,
        }
        self._execution_store().update(
            execution_id,
            metadata={
                "progress_files": progress_files,
                "artifact_files": artifact_files,
                "service_runtime_files": runtime_files,
            },
        )

    def _core_artifact_reporter(
        self,
        store: ExecutionStore,
        execution_id: str,
    ) -> ArtifactReporter:
        """Return the JARVIS-owned reporter for execution infrastructure files."""
        execution_root = store.executions_dir / execution_id
        artifact_dir = execution_root / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            artifact_dir.chmod(0o700)
        artifact_path = artifact_dir / "jarvis-core.jsonl"
        record = store.get(execution_id)
        artifact_files = dict(record.metadata.get("artifact_files", {}))
        artifact_files["jarvis-core"] = {
            "filename": artifact_path.name,
            "package_id": "jarvis-core",
            "package_name": "jarvis.core",
        }
        store.update(execution_id, metadata={"artifact_files": artifact_files})
        return ArtifactReporter(
            package_name="jarvis.core",
            package_id="jarvis-core",
            execution_id=execution_id,
            path=artifact_path,
        )

    def _register_execution_file(
        self,
        store: ExecutionStore,
        execution_id: str,
        *,
        logical_name: str,
        relative_path: str,
        kind: str,
        role: ArtifactRole,
        structure: ArtifactStructure = ArtifactStructure.FILE,
        state: ArtifactState = ArtifactState.FINALIZED,
        media_type: Optional[str] = None,
        format_name: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        """Register one core-owned path without exposing host filesystem authority."""
        self._core_artifact_reporter(store, execution_id).emit(
            logical_name=logical_name,
            kind=kind,
            role=role,
            structure=structure,
            ownership=ArtifactOwnership.EXECUTION,
            state=state,
            location=ArtifactLocation.execution_relative(relative_path),
            media_type=media_type,
            format=format_name,
            message=message,
        )

    def _register_scheduler_log_artifact(
        self,
        store: ExecutionStore,
        execution_id: str,
        *,
        execution_root: Path,
        provider: str,
        array_requested: bool,
        logical_name: str,
        configured_path: str,
    ) -> None:
        """Register a location-less log pending provider-owned path resolution."""
        path = Path(configured_path)
        try:
            path_pattern = path.relative_to(execution_root).as_posix()
        except ValueError:
            scope = "cluster"
            path_pattern = (
                posixpath.normpath(configured_path)
                if os.name == "nt" and configured_path.startswith("/")
                else path.as_posix()
            )
            ownership = ArtifactOwnership.SHARED
        else:
            scope = "execution"
            ownership = ArtifactOwnership.EXECUTION
        self._core_artifact_reporter(store, execution_id).emit(
            logical_name=logical_name,
            kind="log",
            role=ArtifactRole.LOG,
            structure=ArtifactStructure.STREAM,
            ownership=ownership,
            state=ArtifactState.PRODUCING,
            location=None,
            media_type="text/plain",
            format="log",
            message=("Scheduler output stream; content may grow until execution ends"),
            metadata={
                SCHEDULER_ARTIFACT_PATH_METADATA_KEY: {
                    "schema_version": SCHEDULER_ARTIFACT_PATH_SCHEMA,
                    "provider": provider,
                    "path_pattern": path_pattern,
                    "scope": scope,
                    "array_requested": array_requested,
                    "status": "pending",
                }
            },
        )

    def _register_execution_snapshot_artifacts(
        self,
        store: ExecutionStore,
        execution_id: str,
    ) -> None:
        """Publish the immutable input and runnable pipeline snapshots."""
        for logical_name, relative_path, role in (
            ("pipeline-input", "input/pipeline.yaml", ArtifactRole.PROVENANCE),
            ("environment-input", "input/environment.yaml", ArtifactRole.PROVENANCE),
            ("pipeline-runtime", "runtime/pipeline.yaml", ArtifactRole.PROVENANCE),
            (
                "environment-runtime",
                "runtime/environment.yaml",
                ArtifactRole.PROVENANCE,
            ),
        ):
            self._register_execution_file(
                store,
                execution_id,
                logical_name=logical_name,
                relative_path=relative_path,
                kind="configuration",
                role=role,
                media_type="application/yaml",
                format_name="yaml",
            )

    def _write_execution_provenance(
        self,
        store: ExecutionStore,
        execution_id: str,
    ) -> None:
        """Write and register ``provenance.json`` (``jarvis.provenance.v1``).

        Best effort: a failure is logged and never fails the run.
        """
        from jarvis_cd.core.provenance import provenance_enabled, write_provenance
        from jarvis_cd.util.logger import logger

        if not provenance_enabled(self.env):
            return
        try:
            execution_root = store.executions_dir / execution_id
            install_records = [execution_root / "install" / "install-record.json"]
            if self.name:
                from jarvis_cd.core.installer import read_last_install

                try:
                    last = read_last_install(self) or {}
                except Exception:
                    last = {}
                if last.get("record"):
                    install_records.append(Path(last["record"]))
                install_records.append(
                    Path(self.jarvis.get_pipeline_shared_dir(self.name))
                    / "install"
                    / "install-record.json"
                )
            try:
                phase_timings = store.get(execution_id).metadata.get("phase_timings")
            except Exception:
                phase_timings = None
            path = write_provenance(
                execution_root,
                execution_id=execution_id,
                pipeline_id=self.name,
                install_records=install_records,
                phase_timings=phase_timings,
            )
            self._register_execution_file(
                store,
                execution_id,
                logical_name="provenance",
                relative_path=path.relative_to(execution_root).as_posix(),
                kind="provenance",
                role=ArtifactRole.PROVENANCE,
                media_type="application/json",
                format_name="json",
                message="jarvis.provenance.v1: Spack hashes and lock digest, "
                "launcher/binary realpaths, ldd, nodes and ranks, one rank's "
                "/proc maps, job environment",
            )
            store.update(execution_id, metadata={"provenance_file": path.name})
        except Exception as error:
            logger.warning(f"provenance not written: {error}")

    def _record_site_facts(self, store: Any, execution_id: str) -> None:
        """Merge each launch's site facts into the record's ``site_facts``."""
        from jarvis_cd.core.site_facts import record_execution_site_facts

        record_execution_site_facts(store.executions_dir / execution_id, execution_id)

    @staticmethod
    def _launch_failure_issues(
        execution_root: Path, error: BaseException
    ) -> Optional[Dict[str, Any]]:
        """Collect the typed issues of a failed run for its execution record.

        A ``JarvisIssueError`` raised by a package (anywhere in the cause
        chain) contributes its issues as-is. Otherwise the error and the tail
        of the execution's logs are classified for an MPI network signature;
        that issue names ``resource_graph.net.override`` as the field to change.
        """
        from jarvis_cd.core.issues import JarvisIssueError

        raised: List[Dict[str, Any]] = []
        seen: set = set()
        current: Optional[BaseException] = error
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, JarvisIssueError):
                raised.extend(dict(issue) for issue in current.issues)
            current = current.__cause__ or current.__context__
        if raised:
            return {"issues": raised}
        try:
            from jarvis_cd.util.network import classify_mpi_network_failure

            texts = [str(error)]
            for name in ("stdout.log", "stderr.log"):
                path = execution_root / name
                if path.is_file():
                    with open(path, "rb") as stream:
                        stream.seek(max(0, path.stat().st_size - 256 * 1024))
                        texts.append(stream.read().decode("utf-8", "replace"))
            launch_network = None
            for launch in sorted((execution_root / "provenance").glob("launch-*")):
                try:
                    document = json.loads(
                        (launch / "launch.json").read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    continue
                launch_network = document.get("network") or launch_network
            issue = classify_mpi_network_failure(
                "\n".join(texts), launch_network=launch_network
            )
        except Exception:
            return None
        return {"issues": [issue]} if issue else None

    def _started_package_instance(self, package_id: str) -> Optional[Any]:
        """Return the exact instance started for a package alias, if retained."""
        for instance in reversed(getattr(self, "_started_instances", [])):
            if str(getattr(instance, "pkg_id", "")) == package_id:
                return instance
        return None

    def stop(self) -> None:
        """Stop all packages in the pipeline"""
        from jarvis_cd.util.logger import logger

        logger.pipeline(f"Stopping pipeline: {self.name}")

        # Check if pipeline is configured for containerized deployment
        if self.is_containerized():
            # Container deployment mode - stop containers on all nodes
            self._stop_containerized_pipeline()
        else:
            # Standard deployment mode - stop each package individually
            failures: list[Exception] = []
            already_stopped = set(getattr(self, "_stopped_after_steps", None) or ())
            by_request = self._read_stopped_by_request() - already_stopped
            for pkg_def in reversed(self.packages):
                if str(pkg_def["pkg_id"]) in already_stopped:
                    logger.info(f"[{pkg_def['pkg_type']}] [STOP] already stopped "
                                "after the last step")
                    continue
                if (str(pkg_def["pkg_id"]) in by_request
                        or str(pkg_def["pkg_id"]) in self._read_stopped_by_request()):
                    logger.info(f"[{pkg_def['pkg_type']}] [STOP] already stopped "
                                "by this execution's stop request")
                    continue
                if not self._claim_package_stop(str(pkg_def["pkg_id"])):
                    logger.info(f"[{pkg_def['pkg_type']}] [STOP] already being "
                                "stopped by another stop in this execution")
                    continue
                try:
                    # Print BEGIN message
                    logger.success(f"[{pkg_def['pkg_type']}] [STOP] BEGIN")

                    self._bind_package_execution_environment(pkg_def)
                    package_id = str(pkg_def["pkg_id"])
                    pkg_instance = self._started_package_instance(package_id)
                    if pkg_instance is None:
                        pkg_instance = self._load_package_instance(pkg_def, self.env)

                    if hasattr(pkg_instance, "stop"):
                        pkg_instance.stop()
                    else:
                        logger.warning(
                            f"Package {pkg_def['pkg_id']} has no stop method"
                        )

                    # Print END message
                    logger.success(f"[{pkg_def['pkg_type']}] [STOP] END")
                    self._record_stopped_by_request(package_id)
                    self._component_stopped(pkg_def, "done", "stopped")

                except Exception as e:
                    logger.error(f"Error stopping package {pkg_def['pkg_id']}: {e}")
                    self._component_stopped(pkg_def, "failed", f"stop failed: {e}")
                    failures.append(e)
            if failures:
                raise ExceptionGroup("pipeline package stop failed", failures)

    @staticmethod
    def _stopped_marker_path():
        """``runtime/stopped-packages.json`` of the scheduler execution, if any.

        The batch shell's TERM trap runs ``ppl stop`` against the same
        runtime snapshot as the waited ``ppl run`` child, so the child's own
        stop skips packages the trap already stopped.
        """
        snapshot = os.getenv(_SNAPSHOT_ENVIRONMENT)
        if not snapshot:
            return None
        directory = Path(snapshot)
        return directory / "stopped-packages.json" if directory.is_dir() else None

    def _read_stopped_by_request(self) -> set:
        path = self._stopped_marker_path()
        if path is None or not path.is_file():
            return set()
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return set()
        if not isinstance(data, dict) or data.get("pipeline") != self.name:
            return set()
        return {str(item) for item in data.get("packages") or []}

    def _claim_package_stop(self, package_id: str) -> bool:
        """Atomically claim one package's stop() within a scheduler execution.

        The batch shell's TERM-trap ``ppl stop``, the waited ``ppl run``
        child's own stop, and the after_steps stop can overlap (a service's
        start() returns as soon as the trap stops it, so the child tears
        down too). ``stopped-packages.json`` only records finished stops, so
        an O_EXCL claim file makes exactly one of them call stop(). Outside a
        scheduler execution there is no shared snapshot and every stop runs.
        """
        path = self._stopped_marker_path()
        if path is None:
            return True
        claims = path.parent / "stop-claims"
        name = re.sub(r"[^A-Za-z0-9._-]", "_", package_id) or "_"
        try:
            claims.mkdir(exist_ok=True)
            fd = os.open(claims / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        except OSError:
            return True
        with os.fdopen(fd, "w") as handle:
            handle.write(f"{socket.gethostname()} {os.getpid()}\n")
        return True

    def _record_stopped_by_request(self, package_id: str) -> None:
        path = self._stopped_marker_path()
        if path is None:
            return
        packages = sorted(self._read_stopped_by_request() | {package_id})
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps({"pipeline": self.name, "packages": packages}))
            os.replace(tmp, path)
        except OSError:
            pass

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
                    package_id = str(pkg_def["pkg_id"])
                    pkg_instance = self._started_package_instance(package_id)
                    if pkg_instance is None:
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
        if getattr(self, "_execution_root", None) is None:
            from jarvis_cd.core.pipeline_check import enforce_pipeline_check

            self._pending_site_facts = enforce_pipeline_check(self).get("site_facts")
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
                "JARVIS_ARTIFACT_PATH",
                "JARVIS_ARTIFACT_TRANSPORT",
                "JARVIS_SERVICE_RUNTIME_PATH",
                "JARVIS_PROVENANCE_DIR",
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
            pending = getattr(self, "_pending_site_facts", None)
            self._pending_site_facts = None
            recorded_facts = None
            if pending:
                from jarvis_cd.core.site_facts import SITE_FACTS_FILE, record_site_facts

                recorded_facts = record_site_facts(
                    pending,
                    full_in=str(store.executions_dir / resolved_execution_id / SITE_FACTS_FILE))
            record = store.create(
                resolved_execution_id, mode="direct",
                **({"metadata": {"site_facts": recorded_facts}} if recorded_facts else {}),
            )
            self._execution_root = store.executions_dir / resolved_execution_id
            self._execution_id = resolved_execution_id
            if recorded_facts:
                from jarvis_cd.core.site_facts import write_site_facts_file

                write_site_facts_file(self._execution_root, pending)
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
            self._begin_phase_timings(store, resolved_execution_id)
            self._record_scheduler_job(store, resolved_execution_id)
            self._components_waiting(self._execution_root)
            store.update(resolved_execution_id,
                         metadata={"component_states": _component_states_metadata()})
            # Configure all packages before starting.
            # This runs _configure() on each package, which sets up
            # environment variables (e.g., CHI_SERVER_CONF) needed by start().
            configure_started = time.monotonic()
            self.configure_all_packages()
            if getattr(self, "_phase_timings", None) is not None:
                self._phase_timings["configure"] = {
                    "seconds": round(time.monotonic() - configure_started, 3)}

            self.start()
            logger.pipeline("Pipeline started successfully. Stopping packages...")
            self.stop()
            self._end_phase_timings()
            self._write_execution_provenance(store, resolved_execution_id)
            self._record_site_facts(store, resolved_execution_id)
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
            self._end_phase_timings()
            self._write_execution_provenance(store, resolved_execution_id)
            self._record_site_facts(store, resolved_execution_id)
            failure_metadata = self._launch_failure_issues(
                store.executions_dir / resolved_execution_id, error
            )
            try:
                store.update(
                    resolved_execution_id,
                    state="failed",
                    terminal=True,
                    return_code=1,
                    error=_bounded_execution_error(error),
                    **({"metadata": failure_metadata} if failure_metadata else {}),
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
        pending = getattr(self, "_pending_site_facts", None)
        self._pending_site_facts = None
        recorded_facts = None
        if pending:
            from jarvis_cd.core.site_facts import SITE_FACTS_FILE, record_site_facts

            recorded_facts = record_site_facts(
                pending,
                full_in=str(store.executions_dir / resolved_execution_id / SITE_FACTS_FILE))
        store.create(
            resolved_execution_id,
            mode="direct",
            metadata={"direct_launch": direct_launch,
                      **({"site_facts": recorded_facts} if recorded_facts else {})},
        )
        execution_root = store.executions_dir / resolved_execution_id
        if recorded_facts:
            from jarvis_cd.core.site_facts import write_site_facts_file

            write_site_facts_file(execution_root, pending)
        process: Optional[subprocess.Popen[Any]] = None
        try:
            runtime_dir, input_dir, snapshot_sha256 = self._write_execution_snapshot(
                execution_root,
                {},
            )
            self._register_execution_snapshot_artifacts(
                store,
                resolved_execution_id,
            )
            prepare_direct_execution_lease(execution_root)
            stdout_path = execution_root / "stdout.log"
            stderr_path = execution_root / "stderr.log"
            for logical_name, path in (
                ("stdout", stdout_path),
                ("stderr", stderr_path),
            ):
                self._register_execution_file(
                    store,
                    resolved_execution_id,
                    logical_name=logical_name,
                    relative_path=path.relative_to(execution_root).as_posix(),
                    kind="log",
                    role=ArtifactRole.LOG,
                    structure=ArtifactStructure.STREAM,
                    state=ArtifactState.AVAILABLE,
                    media_type="text/plain",
                    format_name="log",
                    message="Execution output stream; content may grow until execution ends",
                )
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

    def _configure_package_instance(
        self, pkg_def: Dict[str, Any], pkg_type_label: str
    ) -> None:
        """
        Configure a single package or interceptor instance.

        :param pkg_def: Package definition dictionary
        :param pkg_type_label: Label for logging ("package" or "interceptor")
        """
        from jarvis_cd.core.pkg import Interceptor
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

                # Interceptor runtime mutations belong only to explicitly linked
                # packages. Configuration may prepare resources using an
                # interceptor-local environment, but must not leak that state into
                # every later package through the pipeline-wide environment.
                if not isinstance(pkg_instance, Interceptor):
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

        # Package and interceptor aliases share one pipeline namespace.
        existing_ids = {
            *(pkg["pkg_id"] for pkg in self.packages),
            *self.interceptors.keys(),
        }
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

        # Load once both to parse package-owned configuration and to identify
        # whether this definition is an Application, Service, or Interceptor.
        pkg_instance = self._load_package_instance(package_entry, self.env)

        # Apply configuration arguments if provided (before validation)
        if config_args:
            argparse = pkg_instance.get_argparse()
            try:
                # Use PkgArgParse to parse and convert arguments
                # Parse arguments - prepend 'configure' command
                argparse.parse(["configure"] + config_args)
                converted_args = argparse.kwargs.copy()

                # Update package configuration with converted values
                package_entry["config"].update(converted_args)
            except SystemExit as exc:
                detail = getattr(argparse, "error_message", None)
                suffix = f": {detail}" if detail else ""
                raise ValueError(
                    f"Invalid configuration arguments for package {pkg_id}{suffix}"
                ) from exc
            except Exception as exc:
                raise ValueError(
                    f"Failed to parse configuration for package {pkg_id}: {exc}"
                ) from exc

        # Validate that all required parameters have values (after applying config_args)
        self._validate_required_config(package_spec, package_entry["config"])

        from jarvis_cd.core.pkg import Interceptor

        if isinstance(pkg_instance, Interceptor):
            nested = package_entry["config"].get("interceptors", [])
            if nested:
                raise ValueError("Interceptor packages cannot reference interceptors")
            self.interceptors[pkg_id] = package_entry
            added_kind = "interceptor"
        else:
            self._validate_package_interceptor_references(package_entry)
            self.packages.append(package_entry)
            added_kind = "package"

        # Save updated configuration
        self.save()

        print(f"Added {added_kind} {package_spec} as {pkg_id} to pipeline")

    def rm(self, package_spec: str):
        """
        Remove a package from the pipeline.

        :param package_spec: Package specification to remove (pkg_id)
        """
        # Find and remove an application/service package.
        removed_package = None
        for i, pkg_def in enumerate(self.packages):
            if pkg_def["pkg_id"] == package_spec:
                removed_package = self.packages.pop(i)
                break

        # Interceptors are addressed through the same generic package identity.
        if removed_package is None and package_spec in self.interceptors:
            dependents = [
                pkg["pkg_id"]
                for pkg in self.packages
                if package_spec in pkg.get("config", {}).get("interceptors", [])
            ]
            if dependents:
                raise ValueError(
                    f"Cannot remove interceptor '{package_spec}'; referenced by: "
                    + ", ".join(dependents)
                )
            removed_package = self.interceptors.pop(package_spec)

        if removed_package is None:
            # List available packages to help the user
            available_ids = [pkg["pkg_id"] for pkg in self.packages]
            available_ids.extend(self.interceptors)
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

    def configure_package(self, pkg_id: str, config_args: List[str]) -> None:
        """
        Configure a specific package in the pipeline.

        :param pkg_id: Package ID to configure
        :param config_args: Configuration arguments as command-line args (e.g., ['--nprocs', '4', 'block=32m'])
        :raises ValueError: If an argument is unknown or package configuration fails
        """
        pkg_def = self._find_package_definition(pkg_id)

        if not pkg_def:
            raise ValueError(f"Package not found: {pkg_id}")

        original_config = deepcopy(pkg_def["config"])

        # Loading historically aliases the package instance's config to the
        # pipeline definition and may add defaults. Detach it so configuration
        # is transactional until the package completes successfully.
        pkg_instance = self._load_package_instance(pkg_def, self.env)
        working_config = deepcopy(pkg_instance.config)
        pkg_instance.config = working_config
        pkg_def["config"] = original_config

        argparse = pkg_instance.get_argparse()
        try:
            argparse.parse(["configure"] + config_args)
            converted_args = argparse.kwargs.copy()
            explicit_names = set(argparse.provided_args)
            missing = sorted(explicit_names - set(converted_args))
            if missing:
                raise ValueError(
                    "configuration parser did not return explicit parameters: "
                    + ", ".join(missing)
                )
            explicit_args = {
                name: converted_args[name] for name in sorted(explicit_names)
            }

            if not hasattr(pkg_instance, "configure"):
                raise ValueError(f"Package {pkg_id} has no configure method")

            # Package configuration may create files or validate cross-field state.
            # Do not mutate the durable pipeline definition until it succeeds.
            pkg_instance.configure(**explicit_args)
            pkg_def["config"] = deepcopy(pkg_instance.config)
            self.save()
        except SystemExit as exc:
            pkg_def["config"] = original_config
            detail = getattr(argparse, "error_message", None)
            suffix = f": {detail}" if detail else ""
            raise ValueError(
                f"Invalid configuration arguments for package {pkg_id}{suffix}"
            ) from exc
        except Exception as exc:
            pkg_def["config"] = original_config
            raise ValueError(f"Failed to configure package {pkg_id}: {exc}") from exc

        print(f"Configured package {pkg_id} successfully")
        print(f"Saved configuration for {pkg_id}")

    def show_package_readme(self, pkg_id: str):
        """
        Show README for a specific package in the pipeline.

        :param pkg_id: Package ID to show README for
        """
        pkg_def = self._find_package_definition(pkg_id)

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
        pkg_def = self._find_package_definition(pkg_id)
        if not pkg_def:
            raise ValueError(f"Package not found: {pkg_id}")
        try:
            pkg_instance = self._load_package_instance(pkg_def, self.env)
            pkg_instance.show_build_script()
        except Exception as e:
            print(f"Error showing build script for package {pkg_id}: {e}")

    def show_package_deploy_dockerfile(self, pkg_id: str):
        """Print Dockerfile.deploy for a package within this pipeline."""
        pkg_def = self._find_package_definition(pkg_id)
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
        pkg_def = self._find_package_definition(pkg_id)

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
        self.couplings = _couplings_from(pipeline_config.get("couplings"))

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

        self._validate_interceptor_links()

    def _load_install_publisher(self, policy: str, snapshot_marker):
        """Progress callback for a load's install phase inside a scheduler job.

        Mirrors the phase into the execution record's ``metadata.install``
        (``trigger: install_on_load``), so a run job that stopped on an
        install question (exit 3) or failure (exit 6) says so where an
        install job would. Outside a job it is a no-op.
        """
        execution_id = getattr(self, "_execution_id", None)
        if snapshot_marker is None or not execution_id:
            self._install_progress = None
            return lambda **_fields: None
        from jarvis_cd.core.installer import install_check_path, install_context_for
        from jarvis_cd.core.spack_install import PROGRESS_SCHEMA

        store = self._execution_store()
        paths = install_context_for(self, policy)
        progress: Dict[str, Any] = {
            "schema_version": PROGRESS_SCHEMA,
            "trigger": "install_on_load",
            "mode": policy,
            "phase": "starting",
            "log": str(paths.log_path),
            "issues": str(paths.issues_path),
            "record": str(paths.record_path),
            "check_record": str(install_check_path(self)),
        }

        def publish(**fields: Any) -> None:
            progress.update({k: v for k, v in fields.items() if v is not None})
            progress["updated_at"] = _utc_timestamp()
            try:
                store.update(execution_id, metadata={"install": dict(progress)})
            except Exception as exc:  # progress must never break the load
                print(f"WARNING: could not update execution install progress: {exc}")

        self._install_progress = publish
        publish()
        return publish

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
        self.couplings = _couplings_from(pipeline_def.get("couplings"))

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

        # Validate type-correct interceptor definitions and explicit links.
        self._validate_interceptor_links()

        # Propagate base_deploy_mode -> per-package deploy_mode
        self._propagate_deploy_mode()

        # Install phase: dispatch to per-installer plans based on each
        # package's install_method (falling back to base_deploy_mode for
        # YAMLs that don't set install_method explicitly). The policy is
        # ``install`` (default), ``check`` (verify, never build) or ``skip``
        # (``ppl install`` and ``ppl stop yaml`` run their own phase).
        from jarvis_cd.core.installer import Installer
        from jarvis_cd.core.spack_install import (
            BuildOutsideAllocation,
            InstallDecisionNeeded,
        )

        install_policy = (
            getattr(self, "_install_on_load", None)
            or os.environ.get("JARVIS_INSTALL_ON_LOAD", "").strip().lower()
            or "install"
        )
        if install_policy not in ("install", "check", "skip"):
            raise ValueError(
                "JARVIS_INSTALL_ON_LOAD must be 'install', 'check' or 'skip'"
            )
        pending_decision: Optional[RuntimeError] = None
        self._install_activation = None
        self._install_policy = install_policy
        if install_policy == "skip":
            self._install_activation = {
                "method": "none", "policy": "skip",
                "why_not_loaded": "install phase skipped (JARVIS_INSTALL_ON_LOAD=skip "
                                  "or a command that runs its own phase)",
            }
        else:
            from jarvis_cd.core.spack_install import InstallFailed

            publish = self._load_install_publisher(install_policy, snapshot_marker)
            install_started = time.monotonic()
            try:
                install_report = Installer.install_all(
                    self, mode=install_policy, use_capture=True, defer_unchecked=True)
                activation = dict(install_report.get("activation") or {})
                activation.setdefault("method", "none")
                if activation["method"] == "none":
                    activation.setdefault("why_not_loaded", "no installer loaded an env")
                activation["policy"] = install_policy
                activation["seconds"] = round(time.monotonic() - install_started, 3)
                if install_report.get("check"):
                    activation["check"] = install_report["check"]
                self._install_activation = activation
                self._install_load_report = install_report
                publish(phase="done", satisfied=bool(install_report.get("satisfied")),
                        activation=activation.get("method"))
            except (InstallDecisionNeeded, BuildOutsideAllocation) as needed:
                # Keep the loaded pipeline so the caller can answer, or submit
                # ``jarvis ppl install +submit``, then report why it stopped.
                pending_decision = needed
                issues = list(getattr(needed, "issues", None) or [])
                publish(
                    phase="needs_decision" if issues else "refused",
                    issue_count=len(issues),
                    open_issues=[issue.get("issue_id") for issue in issues],
                    issue_kinds=sorted({str(issue.get("kind")) for issue in issues}),
                    error=str(needed)[:2000],
                )
            except InstallFailed as failed:
                publish(
                    phase="failed", error=str(failed)[:2000],
                    open_issues=[issue.get("issue_id") for issue in failed.issues],
                    issue_kinds=sorted({str(issue.get("kind")) for issue in failed.issues}),
                )
                raise
            finally:
                self._install_progress = None

        # Save pipeline configuration and environment
        self.save()

        # A scheduler snapshot is an isolated execution copy, not an operator
        # navigation event. Never let concurrent queued jobs race on the
        # process-global current-pipeline record.
        if snapshot_marker is None:
            self.jarvis.set_current_pipeline(self.name)

        print(f"Loaded pipeline: {self.name}")
        print(f"Packages: {[pkg['pkg_id'] for pkg in self.packages]}")
        if pending_decision is not None:
            raise pending_decision

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

    def _find_package_definition(self, pkg_id: str) -> Optional[Dict[str, Any]]:
        """Return one application, service, or interceptor definition by alias."""
        for package in self.packages:
            if package["pkg_id"] == pkg_id:
                return package
        return self.interceptors.get(pkg_id)

    def get_pkg(self, pkg_id: str) -> Optional[Dict[str, Any]]:
        """Return one pipeline member regardless of its JARVIS package kind.

        Generic clients can use this method after ``append`` without knowing
        whether JARVIS stored the selected class as an application, service, or
        interceptor.

        :param pkg_id: Pipeline-local package alias.
        :return: The stored package definition, or ``None`` when absent.
        """
        return self._find_package_definition(pkg_id)

    def _validate_package_interceptor_references(self, package: Dict[str, Any]) -> None:
        """Validate one package's explicit references to pipeline interceptors."""
        package_id = str(package.get("pkg_id", "unknown"))
        references = package.get("config", {}).get("interceptors", [])
        if not isinstance(references, list):
            raise ValueError(
                f"Package '{package_id}' interceptors must be a list of aliases"
            )
        if not all(
            isinstance(reference, str) and reference for reference in references
        ):
            raise ValueError(
                f"Package '{package_id}' interceptor aliases must be non-empty strings"
            )
        if len(set(references)) != len(references):
            raise ValueError(f"Package '{package_id}' repeats an interceptor reference")
        missing = [
            reference for reference in references if reference not in self.interceptors
        ]
        if missing:
            raise ValueError(
                f"Package '{package_id}' references unknown interceptor: {missing[0]}"
            )

    def _validate_interceptor_links(self) -> None:
        """Fail closed unless all interceptor definitions and links are valid."""
        from jarvis_cd.core.pkg import Interceptor

        self._validate_unique_ids()
        for interceptor_id, definition in self.interceptors.items():
            instance = self._load_package_instance(definition, self.env)
            if not isinstance(instance, Interceptor):
                raise ValueError(
                    f"Pipeline interceptor '{interceptor_id}' is not an Interceptor package"
                )
            nested = definition.get("config", {}).get("interceptors", [])
            if nested:
                raise ValueError(
                    f"Interceptor package '{interceptor_id}' cannot reference interceptors"
                )
        for package in self.packages:
            self._validate_package_interceptor_references(package)

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

                        # Preserve the historical ``default=None`` convention,
                        # while allowing packages with conditional profiles to
                        # declare that a nullable-looking legacy setting is not
                        # unconditionally required. PkgArgParse already treats
                        # the explicit ``required`` field as authoritative.
                        required = menu_item.get("required")
                        if required is None:
                            required = default_value is None
                        if (
                            name
                            and required is True
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
            if interceptor_name not in self.interceptors:
                raise RuntimeError(
                    f"Package '{pkg_def['pkg_id']}' references unknown interceptor "
                    f"'{interceptor_name}'"
                )

            interceptor_def = self.interceptors[interceptor_name]

            # Register the interceptor's own provenance/artifact/service-runtime
            # sidecars, exactly as start() does for self.packages entries.
            # Without this, an interceptor that ran and injected LD_PRELOAD
            # correctly is invisible afterward: no entry in the execution
            # record's progress_files/artifact_files/service_runtime_files,
            # even though it executed. See #206.
            self._bind_package_execution_environment(interceptor_def)

            logger.success(f"[{interceptor_def['pkg_type']}] [MODIFY_ENV] BEGIN")

            interceptor_instance = self._load_package_instance(
                interceptor_def, self.env
            )
            from jarvis_cd.core.pkg import Interceptor

            if not isinstance(interceptor_instance, Interceptor):
                raise RuntimeError(
                    f"Pipeline interceptor '{interceptor_name}' is not an "
                    "Interceptor package"
                )

            # The package executor consumes mod_env, so the interceptor mutates
            # that exact object rather than a pipeline-global environment.
            interceptor_instance.mod_env = pkg_instance.mod_env
            interceptor_instance.env = pkg_instance.env
            try:
                interceptor_instance.modify_env()
            except Exception as exc:
                raise RuntimeError(
                    f"Interceptor '{interceptor_name}' failed for package "
                    f"'{pkg_def['pkg_id']}': {exc}"
                ) from exc

            logger.success(f"[{interceptor_def['pkg_type']}] [MODIFY_ENV] END")

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

    def _stop_containerized_pipeline(self) -> None:
        """
        Stop containerized pipeline:
        1. Stop each package via its stop() method
        2. Bring down the containers
        """
        from jarvis_cd.util.logger import logger
        from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo

        logger.info("Stopping containerized pipeline")

        # Stop packages in reverse order (same as non-containerized)
        failures: list[Exception] = []
        for pkg_def in reversed(self.packages):
            try:
                logger.success(f"[{pkg_def['pkg_type']}] [STOP] BEGIN")
                self._bind_package_execution_environment(pkg_def)
                package_id = str(pkg_def["pkg_id"])
                pkg_instance = self._started_package_instance(package_id)
                if pkg_instance is None:
                    pkg_instance = self._load_package_instance(pkg_def, self.env)
                if hasattr(pkg_instance, "stop"):
                    pkg_instance.stop()
                logger.success(f"[{pkg_def['pkg_type']}] [STOP] END")
            except Exception as e:
                logger.error(f"Error stopping package {pkg_def['pkg_id']}: {e}")
                failures.append(e)

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

        try:
            result = Exec(stop_cmd, exec_info).run()
            status_error = _executor_status_error(
                result,
                operation="container stop",
            )
            if status_error is not None:
                raise status_error
            logger.success("Containers stopped")
        except Exception as exc:
            logger.error(f"Error stopping pipeline containers: {exc}")
            failures.append(exc)

        if failures:
            raise ExceptionGroup("containerized pipeline stop failed", failures)

    def _kill_containerized_pipeline(self) -> None:
        """
        Force-kill containerized pipeline:
        1. Kill each package
        2. Force-remove the containers
        """
        from jarvis_cd.util.logger import logger
        from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo

        logger.info("Force-killing containerized pipeline")

        # Kill packages
        failures: list[Exception] = []
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
                failures.append(e)

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

        try:
            result = Exec(kill_cmd, exec_info).run()
            status_error = _executor_status_error(
                result,
                operation="container force-kill",
            )
            if status_error is not None:
                raise status_error
            logger.success("Containers force-killed")
        except Exception as exc:
            logger.error(f"Error force-killing pipeline containers: {exc}")
            failures.append(exc)

        if failures:
            raise ExceptionGroup("containerized pipeline force-kill failed", failures)
