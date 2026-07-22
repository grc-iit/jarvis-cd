"""Durable materialization for package-declared local configuration inputs."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from jarvis_cd.deployment import ConfigurationInputBinding
from jarvis_cd.util.private_path import reject_private_path_redirection

MAX_CONFIGURATION_INPUT_BYTES = 16 * 1024 * 1024
_PARAMETER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_SUFFIX_PATTERN = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


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
        payload, digest = _read_bounded_regular_file(source, parameter=parameter)
        target = _materialized_target(
            shared_root,
            parameter=parameter,
            source=source,
            payload=payload,
            digest=digest,
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
        source_payload, source_digest = _read_bounded_regular_file(
            source,
            parameter=parameter,
        )
        shared_root = Path(os.path.abspath(Path(shared_dir).expanduser()))
        expected_root = shared_root / "configuration-inputs" / parameter
        target = Path(os.path.abspath(os.fspath(materialized)))
        reject_private_path_redirection(target)
        if target.parent != expected_root or target.name != _materialized_name(
            source, source_digest
        ):
            return False
        target_payload, target_digest = _read_bounded_regular_file(
            target,
            parameter=parameter,
            require_single_link=True,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return source_digest == target_digest and source_payload == target_payload


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


def _read_bounded_regular_file(
    path: Path,
    *,
    parameter: str,
    require_single_link: bool = False,
) -> tuple[bytes, str]:
    """Read one stable regular file through a no-follow descriptor."""
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
    try:
        opened_before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_before.st_mode)
            or _is_path_redirection(opened_before)
            or _file_identity(opened_before) != _file_identity(linked_before)
            or (require_single_link and opened_before.st_nlink != 1)
        ):
            raise ValueError(
                f"declared configuration input {parameter!r} changed before reading"
            )
        chunks: list[bytes] = []
        remaining = MAX_CONFIGURATION_INPUT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        opened_after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        linked_after = path.lstat()
    except OSError as exc:
        raise ValueError(
            f"declared configuration input {parameter!r} changed while reading"
        ) from exc
    if (
        len(payload) > MAX_CONFIGURATION_INPUT_BYTES
        or len(payload) != opened_before.st_size
        or _is_path_redirection(linked_after)
        or (require_single_link and linked_after.st_nlink != 1)
        or _file_identity(opened_after) != _file_identity(opened_before)
        or _file_identity(linked_after) != _file_identity(opened_after)
    ):
        raise ValueError(
            f"declared configuration input {parameter!r} changed while reading"
        )
    return payload, hashlib.sha256(payload).hexdigest()


def _materialized_target(
    shared_root: Path,
    *,
    parameter: str,
    source: Path,
    payload: bytes,
    digest: str,
) -> Path:
    bindings_root = shared_root / "configuration-inputs"
    parameter_root = bindings_root / parameter
    for directory in (bindings_root, parameter_root):
        reject_private_path_redirection(directory)
        directory.mkdir(mode=0o700, exist_ok=True)
        reject_private_path_redirection(directory)
        if os.name != "nt":
            directory.chmod(0o700)

    target = parameter_root / _materialized_name(source, digest)
    if target == source:
        return target
    if target.exists():
        existing, existing_digest = _read_bounded_regular_file(
            target,
            parameter=parameter,
            require_single_link=True,
        )
        if existing_digest == digest and existing == payload:
            return target

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{digest}.",
        suffix=".tmp",
        dir=parameter_root,
    )
    temporary = Path(temporary_name)
    descriptor_open = True
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short write while materializing configuration input")
            offset += written
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
    persisted, persisted_digest = _read_bounded_regular_file(
        target,
        parameter=parameter,
        require_single_link=True,
    )
    if persisted_digest != digest or persisted != payload:
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
