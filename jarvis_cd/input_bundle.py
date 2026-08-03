"""Digest-verified materialization for package-owned multi-file inputs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Mapping, cast

from jarvis_cd.util.private_path import reject_private_path_redirection

INPUT_BUNDLE_SCHEMA_VERSION = "jarvis.package-input-bundle.v1"
INPUT_BUNDLE_MANIFEST_NAME = "jarvis-input-manifest.json"
_COMPLETION_MARKER_NAME = ".jarvis-input-bundle.json"
_SHA256_CHARACTERS = frozenset("0123456789abcdef")


class InputBundleError(ValueError):
    """Raised when a package input bundle is malformed, unsafe, or changed."""


@dataclass(frozen=True, slots=True)
class InputBundleFile:
    """One digest-bound regular file declared by an input bundle."""

    path: str
    role: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class InputBundleManifest:
    """The closed, validated manifest carried by an input bundle."""

    entrypoint: str
    files: tuple[InputBundleFile, ...]


@dataclass(frozen=True, slots=True)
class MaterializedInputBundle:
    """A completely verified input bundle below a package-owned root."""

    root: Path
    entrypoint: Path
    manifest: InputBundleManifest
    bundle_sha256: str


def extract_input_bundle(
    bundle: str | os.PathLike[str],
    destination_root: str | os.PathLike[str],
    *,
    max_archive_bytes: int = 512 * 1024 * 1024,
    max_total_bytes: int = 4 * 1024 * 1024 * 1024,
    max_files: int = 4096,
) -> MaterializedInputBundle:
    """Validate and atomically extract one package input bundle.

    The archive must contain only regular files and exactly one closed v1
    manifest. Member paths are relative POSIX paths, links are prohibited, and
    every declared byte count and SHA-256 digest is verified before publication.
    Reusing a bundle is idempotent only while the materialized tree remains
    byte-identical to its manifest.

    :param bundle: Regular tar-compatible archive to materialize.
    :param destination_root: Package-owned directory for content-addressed trees.
    :param max_archive_bytes: Maximum compressed archive size.
    :param max_total_bytes: Maximum declared uncompressed payload size.
    :param max_files: Maximum declared payload file count.
    :return: The verified content-addressed materialization.
    :raises InputBundleError: If any safety, identity, or bound check fails.
    """

    _validate_positive_bound(max_archive_bytes, "max_archive_bytes")
    _validate_positive_bound(max_total_bytes, "max_total_bytes")
    _validate_positive_bound(max_files, "max_files")
    source = _safe_regular_file(bundle, max_bytes=max_archive_bytes)
    bundle_sha256 = _sha256_file(source)
    destination_parent = _safe_destination_root(destination_root)
    destination = destination_parent / bundle_sha256
    marker = destination / _COMPLETION_MARKER_NAME
    if destination.exists():
        return _load_existing_bundle(
            destination,
            source=source,
            marker=marker,
            bundle_sha256=bundle_sha256,
            max_files=max_files,
            max_total_bytes=max_total_bytes,
        )

    temporary = Path(
        tempfile.mkdtemp(prefix=f".{bundle_sha256}.", dir=destination_parent)
    )
    try:
        manifest = _extract_archive(
            source,
            temporary,
            max_files=max_files,
            max_total_bytes=max_total_bytes,
        )
        marker_payload = {
            "bundle_sha256": bundle_sha256,
            "schema_version": INPUT_BUNDLE_SCHEMA_VERSION,
        }
        marker_path = temporary / _COMPLETION_MARKER_NAME
        marker_path.write_text(
            json.dumps(marker_payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(marker_path, 0o600)
        try:
            os.replace(temporary, destination)
        except OSError:
            if not destination.exists():
                raise
            # Another execution may have published the same digest while this
            # one was verifying it. Publication is atomic, so reuse only after
            # validating that winner against the same source and bounds.
            shutil.rmtree(temporary, ignore_errors=True)
            return _load_existing_bundle(
                destination,
                source=source,
                marker=marker,
                bundle_sha256=bundle_sha256,
                max_files=max_files,
                max_total_bytes=max_total_bytes,
            )
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    _validate_materialized(destination, manifest)
    return MaterializedInputBundle(
        root=destination,
        entrypoint=destination / PurePosixPath(manifest.entrypoint),
        manifest=manifest,
        bundle_sha256=bundle_sha256,
    )


def stage_input_bundle(
    bundle: MaterializedInputBundle,
    destination_root: str | os.PathLike[str],
) -> Path:
    """Copy verified bundle payloads into one new mutable run workspace.

    Package inputs remain immutable in their content-addressed materialization.
    Applications that write beside their inputs receive private copies in the
    execution-owned output directory. Existing files are never overwritten.

    :param bundle: Previously verified input-bundle materialization.
    :param destination_root: Package-owned mutable working directory.
    :return: The staged entrypoint path.
    :raises InputBundleError: If a destination collides or any source changed.
    """

    _validate_materialized(bundle.root, bundle.manifest)
    destination = _safe_destination_root(destination_root)
    staged: list[Path] = []
    try:
        for item in bundle.manifest.files:
            source = bundle.root / PurePosixPath(item.path)
            target = destination / PurePosixPath(item.path)
            if target.exists() or target.is_symlink():
                raise InputBundleError(
                    f"input bundle staging destination already exists: {item.path}"
                )
            with source.open("rb") as stream:
                _copy_member(
                    stream,
                    target,
                    expected_size=item.size_bytes,
                    expected_sha256=item.sha256,
                )
            staged.append(target)
    except Exception:
        for path in reversed(staged):
            path.unlink(missing_ok=True)
        raise
    return destination / PurePosixPath(bundle.manifest.entrypoint)


def _validate_positive_bound(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InputBundleError(f"{name} must be a positive integer")


def _safe_regular_file(
    value: str | os.PathLike[str],
    *,
    max_bytes: int,
) -> Path:
    raw = os.path.expanduser(os.path.expandvars(os.fspath(value)))
    if not raw or any(ord(character) < 32 for character in raw):
        raise InputBundleError("input bundle must be a printable path")
    path = Path(os.path.abspath(raw))
    try:
        reject_private_path_redirection(path)
        status = path.lstat()
    except (OSError, RuntimeError) as exc:
        raise InputBundleError("input bundle is not a readable regular file") from exc
    if (
        not stat.S_ISREG(status.st_mode)
        or path.is_symlink()
        or status.st_size <= 0
        or status.st_size > max_bytes
    ):
        raise InputBundleError("input bundle archive size or type is invalid")
    return path


def _safe_destination_root(value: str | os.PathLike[str]) -> Path:
    root = Path(os.path.abspath(Path(value).expanduser()))
    try:
        reject_private_path_redirection(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        reject_private_path_redirection(root)
    except (OSError, RuntimeError) as exc:
        raise InputBundleError("input bundle destination root is unsafe") from exc
    if not root.is_dir() or root.is_symlink():
        raise InputBundleError("input bundle destination root must be a real directory")
    return root


def _sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise InputBundleError(f"{field} must be a non-empty bounded string")
    if "\\" in value:
        raise InputBundleError(f"{field} must use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise InputBundleError(f"{field} must be a confined relative path")
    return path.as_posix()


def _parse_manifest(payload: bytes, *, max_files: int) -> InputBundleManifest:
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputBundleError("input bundle manifest is not valid UTF-8 JSON") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "entrypoint",
        "files",
    }:
        raise InputBundleError(
            "input bundle manifest fields are not the closed v1 contract"
        )
    if raw["schema_version"] != INPUT_BUNDLE_SCHEMA_VERSION:
        raise InputBundleError("unsupported input bundle manifest schema")
    raw_files = raw["files"]
    if not isinstance(raw_files, list) or not 1 <= len(raw_files) <= max_files:
        raise InputBundleError("input bundle files must be a non-empty bounded list")
    files: list[InputBundleFile] = []
    seen: set[str] = set()
    for index, raw_file in enumerate(raw_files):
        if not isinstance(raw_file, dict) or set(raw_file) != {
            "path",
            "role",
            "sha256",
            "size_bytes",
        }:
            raise InputBundleError(f"input bundle file {index} fields are invalid")
        path = _safe_relative_path(raw_file["path"], field=f"files[{index}].path")
        role = raw_file["role"]
        sha256 = raw_file["sha256"]
        size_bytes = raw_file["size_bytes"]
        if (
            path in {INPUT_BUNDLE_MANIFEST_NAME, _COMPLETION_MARKER_NAME}
            or path in seen
        ):
            raise InputBundleError(f"duplicate or reserved input bundle path: {path}")
        if not isinstance(role, str) or not role or len(role) > 128:
            raise InputBundleError(f"files[{index}].role is invalid")
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or not set(sha256).issubset(_SHA256_CHARACTERS)
        ):
            raise InputBundleError(f"files[{index}].sha256 is invalid")
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
        ):
            raise InputBundleError(f"files[{index}].size_bytes is invalid")
        seen.add(path)
        files.append(
            InputBundleFile(
                path=path,
                role=cast(str, role),
                sha256=cast(str, sha256),
                size_bytes=cast(int, size_bytes),
            )
        )
    entrypoint = _safe_relative_path(raw["entrypoint"], field="entrypoint")
    if entrypoint not in seen:
        raise InputBundleError("input bundle entrypoint is not a declared file")
    return InputBundleManifest(entrypoint=entrypoint, files=tuple(files))


def _extract_archive(
    source: Path,
    temporary: Path,
    *,
    max_files: int,
    max_total_bytes: int,
) -> InputBundleManifest:
    try:
        archive = tarfile.open(source, mode="r:*")
    except (OSError, tarfile.TarError) as exc:
        raise InputBundleError("input bundle is not a readable tar archive") from exc
    with archive:
        members = _bounded_archive_members(archive, max_files=max_files)
        by_name: dict[str, tarfile.TarInfo] = {}
        for member in members:
            name = _safe_relative_path(member.name, field="archive member")
            if name in by_name:
                raise InputBundleError(f"duplicate input bundle member: {name}")
            if not member.isfile():
                raise InputBundleError(
                    f"input bundle member is not a regular file: {name}"
                )
            by_name[name] = member
        manifest_member = by_name.get(INPUT_BUNDLE_MANIFEST_NAME)
        if manifest_member is None or manifest_member.size > 1024 * 1024:
            raise InputBundleError("input bundle manifest is missing or oversized")
        manifest_stream = archive.extractfile(manifest_member)
        if manifest_stream is None:
            raise InputBundleError("input bundle manifest could not be read")
        manifest_payload = manifest_stream.read(1024 * 1024 + 1)
        manifest = _parse_manifest(manifest_payload, max_files=max_files)
        declared = {item.path: item for item in manifest.files}
        if set(by_name) != {INPUT_BUNDLE_MANIFEST_NAME, *declared}:
            raise InputBundleError(
                "input bundle members do not exactly match the manifest"
            )
        if sum(item.size_bytes for item in manifest.files) > max_total_bytes:
            raise InputBundleError("input bundle payload exceeds the allowed bound")
        manifest_path = temporary / INPUT_BUNDLE_MANIFEST_NAME
        manifest_path.write_bytes(manifest_payload)
        os.chmod(manifest_path, 0o600)
        for item in manifest.files:
            member = by_name[item.path]
            if member.size != item.size_bytes:
                raise InputBundleError(
                    f"input bundle member size differs from manifest: {item.path}"
                )
            stream = archive.extractfile(member)
            if stream is None:
                raise InputBundleError(
                    f"input bundle member could not be read: {item.path}"
                )
            _copy_member(
                stream,
                temporary / PurePosixPath(item.path),
                expected_size=item.size_bytes,
                expected_sha256=item.sha256,
            )
    return manifest


def _bounded_archive_members(
    archive: tarfile.TarFile,
    *,
    max_files: int,
) -> tuple[tarfile.TarInfo, ...]:
    """Read at most the permitted number of tar headers.

    ``TarFile.getmembers`` materializes every header before the caller can
    enforce a bound. Iteration lets malformed archives fail as soon as the
    manifest plus the maximum payload count has been exceeded.
    """

    members: list[tarfile.TarInfo] = []
    try:
        for member in archive:
            members.append(member)
            if len(members) > max_files + 1:
                raise InputBundleError(
                    "input bundle member count is outside the allowed bound"
                )
    except tarfile.TarError as exc:
        raise InputBundleError("input bundle archive headers are invalid") from exc
    if len(members) < 2:
        raise InputBundleError("input bundle member count is outside the allowed bound")
    return tuple(members)


def _read_archive_manifest(
    source: Path,
    *,
    max_files: int,
) -> InputBundleManifest:
    """Read and validate the source archive's closed manifest contract."""

    try:
        archive = tarfile.open(source, mode="r:*")
    except (OSError, tarfile.TarError) as exc:
        raise InputBundleError("input bundle is not a readable tar archive") from exc
    with archive:
        members = _bounded_archive_members(archive, max_files=max_files)
        manifest_member: tarfile.TarInfo | None = None
        seen: set[str] = set()
        for member in members:
            name = _safe_relative_path(member.name, field="archive member")
            if name in seen:
                raise InputBundleError(f"duplicate input bundle member: {name}")
            if not member.isfile():
                raise InputBundleError(
                    f"input bundle member is not a regular file: {name}"
                )
            seen.add(name)
            if name == INPUT_BUNDLE_MANIFEST_NAME:
                manifest_member = member
        if manifest_member is None or manifest_member.size > 1024 * 1024:
            raise InputBundleError("input bundle manifest is missing or oversized")
        stream = archive.extractfile(manifest_member)
        if stream is None:
            raise InputBundleError("input bundle manifest could not be read")
        payload = stream.read(1024 * 1024 + 1)
    return _parse_manifest(payload, max_files=max_files)


def _copy_member(
    source: IO[bytes],
    destination: Path,
    *,
    expected_size: int,
    expected_sha256: str,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256()
    written = 0
    try:
        with destination.open("xb") as stream:
            os.chmod(destination, 0o600)
            while chunk := source.read(1024 * 1024):
                written += len(chunk)
                if written > expected_size:
                    raise InputBundleError(
                        f"input bundle member exceeds declared size: {destination}"
                    )
                digest.update(chunk)
                stream.write(chunk)
    except OSError as exc:
        raise InputBundleError(
            f"input bundle member could not be materialized: {destination}"
        ) from exc
    if written != expected_size or digest.hexdigest() != expected_sha256:
        raise InputBundleError(
            f"input bundle member digest or size mismatch: {destination}"
        )


def _load_existing_bundle(
    destination: Path,
    *,
    source: Path,
    marker: Path,
    bundle_sha256: str,
    max_files: int,
    max_total_bytes: int,
) -> MaterializedInputBundle:
    try:
        reject_private_path_redirection(destination)
        recorded = json.loads(marker.read_text(encoding="utf-8"))
        manifest = _parse_manifest(
            (destination / INPUT_BUNDLE_MANIFEST_NAME).read_bytes(),
            max_files=max_files,
        )
        source_manifest = _read_archive_manifest(source, max_files=max_files)
    except (InputBundleError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        raise InputBundleError(
            "existing input bundle destination is incomplete"
        ) from exc
    expected_marker: Mapping[str, str] = {
        "bundle_sha256": bundle_sha256,
        "schema_version": INPUT_BUNDLE_SCHEMA_VERSION,
    }
    if recorded != expected_marker:
        raise InputBundleError(
            "existing input bundle destination has the wrong identity"
        )
    if manifest != source_manifest:
        raise InputBundleError(
            "existing input bundle manifest differs from the source archive"
        )
    if sum(item.size_bytes for item in manifest.files) > max_total_bytes:
        raise InputBundleError("input bundle payload exceeds the allowed bound")
    _validate_materialized(destination, manifest)
    return MaterializedInputBundle(
        root=destination,
        entrypoint=destination / PurePosixPath(manifest.entrypoint),
        manifest=manifest,
        bundle_sha256=bundle_sha256,
    )


def _validate_materialized(root: Path, manifest: InputBundleManifest) -> None:
    expected = {
        INPUT_BUNDLE_MANIFEST_NAME,
        _COMPLETION_MARKER_NAME,
        *(item.path for item in manifest.files),
    }
    try:
        reject_private_path_redirection(root)
        observed: set[str] = set()
        for path in root.rglob("*"):
            if path.is_symlink():
                raise InputBundleError("materialized input bundle contains a link")
            if path.is_file():
                observed.add(path.relative_to(root).as_posix())
        if observed != expected:
            raise InputBundleError(
                "materialized input bundle contains unexpected or missing files"
            )
        for item in manifest.files:
            path = root / PurePosixPath(item.path)
            status = path.lstat()
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_size != item.size_bytes
                or _sha256_file(path) != item.sha256
            ):
                raise InputBundleError(
                    f"materialized input bundle member changed: {item.path}"
                )
    except OSError as exc:
        raise InputBundleError(
            "materialized input bundle could not be verified"
        ) from exc


__all__ = [
    "INPUT_BUNDLE_MANIFEST_NAME",
    "INPUT_BUNDLE_SCHEMA_VERSION",
    "InputBundleError",
    "InputBundleFile",
    "InputBundleManifest",
    "MaterializedInputBundle",
    "extract_input_bundle",
    "stage_input_bundle",
]
