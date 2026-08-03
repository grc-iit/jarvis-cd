"""Durable materialization for package-declared local configuration inputs."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from jarvis_cd.deployment import ConfigurationInputBinding
from jarvis_cd.util.private_path import reject_private_path_redirection

MAX_CONFIGURATION_INPUT_BYTES = 512 * 1024 * 1024
_PARAMETER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_SUFFIX_PATTERN = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


@dataclass(frozen=True, slots=True)
class _FileFingerprint:
    """Stable size and SHA-256 identity for one bounded regular file."""

    size_bytes: int
    sha256: str


def materialize_configuration_inputs(
    *,
    menu: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    shared_dir: str | os.PathLike[str],
) -> dict[str, Any]:
    """Copy declared caller-local files into package-owned shared storage.

    The returned configuration replaces each nonempty declared input with an
    absolute content-addressed path. No setting is treated as a file unless its
    package menu carries the exact versioned ``input_binding`` descriptor.
    """
    shared_root = Path(shared_dir).expanduser()
    if not shared_root.is_absolute():
        raise ValueError("configuration input shared directory must be absolute")
    shared_root = Path(os.path.abspath(shared_root))
    reject_private_path_redirection(shared_root)
    shared_root.mkdir(parents=True, exist_ok=True)
    reject_private_path_redirection(shared_root)

    materialized = dict(config)
    seen_parameters: set[str] = set()
    for item in menu:
        raw_binding = item.get("input_binding")
        if raw_binding is None:
            continue
        parameter = item.get("name")
        if (
            not isinstance(parameter, str)
            or _PARAMETER_PATTERN.fullmatch(parameter) is None
        ):
            raise ValueError("configuration input binding requires a valid parameter")
        if parameter in seen_parameters:
            raise ValueError(
                f"configuration input binding is duplicated for {parameter!r}"
            )
        seen_parameters.add(parameter)
        if not isinstance(raw_binding, Mapping):
            raise ValueError(
                f"configuration input binding for {parameter!r} must be an object"
            )
        binding = ConfigurationInputBinding.from_dict(
            cast(Mapping[str, object], raw_binding)
        )
        if binding.kind != "local_file" or binding.structure != "regular_file":
            raise ValueError(
                f"unsupported configuration input binding for {parameter!r}"
            )
        value = materialized.get(parameter)
        if value is None or value == "":
            continue
        if not isinstance(value, (str, os.PathLike)):
            raise TypeError(
                f"declared configuration input {parameter!r} must be a path string"
            )
        source = _configuration_input_source(value, parameter=parameter)
        fingerprint = _fingerprint_bounded_regular_file(source, parameter=parameter)
        target = _materialized_target(
            shared_root,
            parameter=parameter,
            source=source,
            fingerprint=fingerprint,
        )
        materialized[parameter] = str(target)
    return materialized


def configuration_input_materialization_matches(
    *,
    menu: Sequence[Mapping[str, Any]],
    parameter: str,
    requested: object,
    materialized: object,
    shared_dir: str | os.PathLike[str],
) -> bool:
    """Verify one declared rewrite against its source bytes and owned root."""
    if not isinstance(requested, (str, os.PathLike)) or not isinstance(
        materialized, (str, os.PathLike)
    ):
        return False
    declaration = next(
        (
            item
            for item in menu
            if item.get("name") == parameter and item.get("input_binding") is not None
        ),
        None,
    )
    if declaration is None:
        return False
    raw_binding = declaration.get("input_binding")
    if not isinstance(raw_binding, Mapping):
        return False
    try:
        ConfigurationInputBinding.from_dict(cast(Mapping[str, object], raw_binding))
        source = _configuration_input_source(requested, parameter=parameter)
        source_fingerprint = _fingerprint_bounded_regular_file(
            source,
            parameter=parameter,
        )
        shared_root = Path(os.path.abspath(Path(shared_dir).expanduser()))
        expected_root = shared_root / "configuration-inputs" / parameter
        target = Path(os.path.abspath(os.fspath(materialized)))
        reject_private_path_redirection(target)
        if target.parent != expected_root or target.name != _materialized_name(
            source, source_fingerprint.sha256
        ):
            return False
        target_fingerprint = _fingerprint_bounded_regular_file(
            target,
            parameter=parameter,
            require_single_link=True,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return source_fingerprint == target_fingerprint


def _configuration_input_source(
    value: str | os.PathLike[str],
    *,
    parameter: str,
) -> Path:
    raw = os.path.expanduser(os.path.expandvars(os.fspath(value)))
    if not raw or any(ord(character) < 32 for character in raw):
        raise ValueError(
            f"declared configuration input {parameter!r} must be a printable path"
        )
    source = Path(os.path.abspath(raw))
    try:
        reject_private_path_redirection(source)
    except RuntimeError as exc:
        raise ValueError(
            f"declared configuration input {parameter!r} cannot traverse links"
        ) from exc
    return source


def _fingerprint_bounded_regular_file(
    path: Path,
    *,
    parameter: str,
    require_single_link: bool = False,
) -> _FileFingerprint:
    """Hash one stable bounded file without retaining its payload in memory."""

    descriptor, linked_before, opened_before = _open_bounded_regular_file(
        path,
        parameter=parameter,
        require_single_link=require_single_link,
    )
    digest = hashlib.sha256()
    observed_size = 0
    try:
        while chunk := os.read(descriptor, 1024 * 1024):
            observed_size += len(chunk)
            if observed_size > MAX_CONFIGURATION_INPUT_BYTES:
                raise ValueError(
                    f"declared configuration input {parameter!r} exceeded its bound"
                )
            digest.update(chunk)
        opened_after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    _validate_file_after_read(
        path,
        parameter=parameter,
        linked_before=linked_before,
        opened_before=opened_before,
        opened_after=opened_after,
        observed_size=observed_size,
        require_single_link=require_single_link,
    )
    return _FileFingerprint(observed_size, digest.hexdigest())


def _open_bounded_regular_file(
    path: Path,
    *,
    parameter: str,
    require_single_link: bool,
) -> tuple[int, os.stat_result, os.stat_result]:
    """Open one stable bounded regular file without following its final link."""

    try:
        linked_before = path.lstat()
    except OSError as exc:
        raise ValueError(
            f"declared configuration input {parameter!r} is not readable"
        ) from exc
    if (
        not stat.S_ISREG(linked_before.st_mode)
        or _is_path_redirection(linked_before)
        or linked_before.st_nlink < 1
        or (require_single_link and linked_before.st_nlink != 1)
        or linked_before.st_size > MAX_CONFIGURATION_INPUT_BYTES
    ):
        raise ValueError(
            f"declared configuration input {parameter!r} must be one bounded "
            "regular file"
        )
    flags = (
        os.O_RDONLY
        | cast(int, getattr(os, "O_BINARY", 0))
        | cast(int, getattr(os, "O_CLOEXEC", 0))
        | cast(int, getattr(os, "O_NOFOLLOW", 0))
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(
            f"declared configuration input {parameter!r} could not be opened safely"
        ) from exc
    opened_before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened_before.st_mode)
        or _is_path_redirection(opened_before)
        or _file_identity(opened_before) != _file_identity(linked_before)
        or (require_single_link and opened_before.st_nlink != 1)
    ):
        os.close(descriptor)
        raise ValueError(
            f"declared configuration input {parameter!r} changed before reading"
        )
    return descriptor, linked_before, opened_before


def _validate_file_after_read(
    path: Path,
    *,
    parameter: str,
    linked_before: os.stat_result,
    opened_before: os.stat_result,
    opened_after: os.stat_result,
    observed_size: int,
    require_single_link: bool,
) -> None:
    """Reject a file that changed identity while it was being streamed."""

    try:
        linked_after = path.lstat()
    except OSError as exc:
        raise ValueError(
            f"declared configuration input {parameter!r} changed while reading"
        ) from exc
    if (
        observed_size > MAX_CONFIGURATION_INPUT_BYTES
        or observed_size != opened_before.st_size
        or _is_path_redirection(linked_after)
        or (require_single_link and linked_after.st_nlink != 1)
        or _file_identity(opened_after) != _file_identity(opened_before)
        or _file_identity(linked_after) != _file_identity(opened_after)
    ):
        raise ValueError(
            f"declared configuration input {parameter!r} changed while reading"
        )


def _copy_bounded_regular_file(
    source: Path,
    destination_descriptor: int,
    *,
    parameter: str,
    expected: _FileFingerprint,
) -> None:
    """Stream one stable source into an already-created private destination."""

    source_descriptor, linked_before, opened_before = _open_bounded_regular_file(
        source,
        parameter=parameter,
        require_single_link=False,
    )
    digest = hashlib.sha256()
    observed_size = 0
    try:
        while chunk := os.read(source_descriptor, 1024 * 1024):
            observed_size += len(chunk)
            if observed_size > MAX_CONFIGURATION_INPUT_BYTES:
                raise ValueError(
                    f"declared configuration input {parameter!r} exceeded its bound"
                )
            digest.update(chunk)
            offset = 0
            while offset < len(chunk):
                written = os.write(destination_descriptor, chunk[offset:])
                if written <= 0:
                    raise OSError("short write while materializing configuration input")
                offset += written
        opened_after = os.fstat(source_descriptor)
    finally:
        os.close(source_descriptor)
    _validate_file_after_read(
        source,
        parameter=parameter,
        linked_before=linked_before,
        opened_before=opened_before,
        opened_after=opened_after,
        observed_size=observed_size,
        require_single_link=False,
    )
    observed = _FileFingerprint(observed_size, digest.hexdigest())
    if observed != expected:
        raise ValueError(
            f"declared configuration input {parameter!r} changed before materialization"
        )


def _materialized_target(
    shared_root: Path,
    *,
    parameter: str,
    source: Path,
    fingerprint: _FileFingerprint,
) -> Path:
    bindings_root = shared_root / "configuration-inputs"
    parameter_root = bindings_root / parameter
    for directory in (bindings_root, parameter_root):
        reject_private_path_redirection(directory)
        directory.mkdir(mode=0o700, exist_ok=True)
        reject_private_path_redirection(directory)
        if os.name != "nt":
            directory.chmod(0o700)

    target = parameter_root / _materialized_name(source, fingerprint.sha256)
    if target == source:
        return target
    if target.exists():
        existing = _fingerprint_bounded_regular_file(
            target,
            parameter=parameter,
            require_single_link=True,
        )
        if existing == fingerprint:
            return target

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{fingerprint.sha256}.",
        suffix=".tmp",
        dir=parameter_root,
    )
    temporary = Path(temporary_name)
    descriptor_open = True
    try:
        _copy_bounded_regular_file(
            source,
            descriptor,
            parameter=parameter,
            expected=fingerprint,
        )
        os.fsync(descriptor)
        if os.name != "nt":
            descriptor_chmod = cast(Any, getattr(os, "fchmod"))
            descriptor_chmod(descriptor, 0o400)
        os.close(descriptor)
        descriptor_open = False
        os.replace(temporary, target)
        if os.name != "nt":
            target.chmod(0o400)
            _fsync_directory(parameter_root)
    finally:
        if descriptor_open:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()
    persisted = _fingerprint_bounded_regular_file(
        target,
        parameter=parameter,
        require_single_link=True,
    )
    if persisted != fingerprint:
        raise RuntimeError(
            f"materialized configuration input {parameter!r} failed verification"
        )
    return target


def _materialized_name(source: Path, digest: str) -> str:
    """Return the deterministic safe filename for one input snapshot."""
    suffix = source.suffix
    if _SAFE_SUFFIX_PATTERN.fullmatch(suffix) is None:
        suffix = ""
    return f"{digest}{suffix}"


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    stable_ctime_ns = 0 if os.name == "nt" else value.st_ctime_ns
    return (
        value.st_mode,
        value.st_dev,
        value.st_ino,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        stable_ctime_ns,
    )


def _is_path_redirection(value: os.stat_result) -> bool:
    attributes = cast(int, getattr(value, "st_file_attributes", 0))
    reparse_flag = cast(int, getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(value.st_mode) or bool(attributes & reparse_flag)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "MAX_CONFIGURATION_INPUT_BYTES",
    "configuration_input_materialization_matches",
    "materialize_configuration_inputs",
]
