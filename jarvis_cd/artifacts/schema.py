"""Typed, application-independent artifact records for JARVIS executions."""

from __future__ import annotations

import json
import math
import re
import secrets
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Final, Mapping, cast
from urllib.parse import urlsplit

ARTIFACT_SCHEMA_VERSION: Final = "jarvis.artifact.v1"
MAX_ARTIFACT_EVENT_BYTES: Final = 64 * 1024
MAX_IDENTITY_TEXT: Final = 256
MAX_LOCATION_TEXT: Final = 4096
MAX_MESSAGE_TEXT: Final = 4096
_ARTIFACT_ID_PATTERN: Final = re.compile(r"^art_[A-Za-z0-9_-]{22,86}$")
_CHECKSUM_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9_-]*:[A-Fa-f0-9]{16,256}$")
_MEDIA_TYPE_PATTERN: Final = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$"
)
_URI_SCHEME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9+.-]*$")
_UNSAFE_URI_SCHEMES: Final = frozenset({"data", "file", "javascript"})
PROCESS_EXIT_RECONCILIATION_KEY: Final = "jarvis_process_exit"
_EVENT_FIELDS: Final = frozenset(
    {
        "schema_version",
        "package_name",
        "package_id",
        "execution_id",
        "artifact_id",
        "logical_name",
        "kind",
        "role",
        "structure",
        "ownership",
        "state",
        "location",
        "media_type",
        "format",
        "size_bytes",
        "checksum",
        "message",
        "revision",
        "sequence",
        "observed_at_epoch",
        "metadata",
    }
)


class ArtifactState(StrEnum):
    """Lifecycle state of one generated artifact."""

    PRODUCING = "producing"
    AVAILABLE = "available"
    FINALIZED = "finalized"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class ArtifactRole(StrEnum):
    """Execution-level purpose of an artifact."""

    INTERMEDIATE = "intermediate"
    OUTPUT = "output"
    LOG = "log"
    CHECKPOINT = "checkpoint"
    PROVENANCE = "provenance"
    VALIDATION = "validation"


class ArtifactStructure(StrEnum):
    """Physical shape resolved by one artifact reference."""

    FILE = "file"
    DIRECTORY = "directory"
    COLLECTION = "collection"
    STREAM = "stream"


class ArtifactLocationKind(StrEnum):
    """Namespace used to resolve an artifact location."""

    EXECUTION_PATH = "execution_path"
    CLUSTER_PATH = "cluster_path"
    EXTERNAL_URI = "external_uri"


class ArtifactOwnership(StrEnum):
    """Cleanup authority associated with generated artifact content."""

    EXECUTION = "execution"
    EXTERNAL = "external"
    SHARED = "shared"


JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class ArtifactLocation:
    """Safe, transport-neutral location of an artifact.

    Execution-relative paths are serialized with POSIX separators and cannot
    escape the execution root. External locations must be non-file URIs, so a
    manifest never turns an untrusted absolute path into local filesystem
    authority.
    """

    kind: ArtifactLocationKind
    value: str

    def __post_init__(self) -> None:
        """Validate the location namespace and representation."""
        if not isinstance(self.kind, ArtifactLocationKind):
            raise ValueError("artifact location kind must be an ArtifactLocationKind")
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("artifact location value must be non-empty")
        if len(self.value) > MAX_LOCATION_TEXT:
            raise ValueError("artifact location exceeds maximum length")
        if any(ord(character) < 32 for character in self.value):
            raise ValueError("artifact location cannot contain control characters")
        if self.kind is ArtifactLocationKind.EXECUTION_PATH:
            _validate_execution_relative_location(self.value)
        elif self.kind is ArtifactLocationKind.CLUSTER_PATH:
            _validate_cluster_path(self.value)
        else:
            _validate_external_uri(self.value)

    @classmethod
    def execution_relative(cls, path: str | PurePosixPath) -> "ArtifactLocation":
        """Create a location resolved relative to an owned execution root."""
        value = path.as_posix() if isinstance(path, PurePosixPath) else path
        return cls(ArtifactLocationKind.EXECUTION_PATH, value)

    @classmethod
    def cluster_path(cls, path: str | PurePosixPath) -> "ArtifactLocation":
        """Create an opaque absolute path reference on the executing cluster."""
        value = path.as_posix() if isinstance(path, PurePosixPath) else path
        return cls(ArtifactLocationKind.CLUSTER_PATH, value)

    @classmethod
    def external_uri(cls, uri: str) -> "ArtifactLocation":
        """Create a location resolved by an external non-file URI provider."""
        return cls(ArtifactLocationKind.EXTERNAL_URI, uri)

    def as_dict(self) -> dict[str, str]:
        """Serialize this location into the stable artifact schema."""
        return {"kind": self.kind.value, "value": self.value}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactLocation":
        """Parse and validate a serialized artifact location."""
        unknown = set(value) - {"kind", "value"}
        if unknown:
            raise ValueError(f"unknown artifact location fields: {sorted(unknown)}")
        kind = _required_str(value, "kind")
        try:
            typed_kind = ArtifactLocationKind(kind)
        except ValueError as exc:
            raise ValueError(f"invalid artifact location kind: {kind!r}") from exc
        return cls(kind=typed_kind, value=_required_str(value, "value"))


@dataclass(frozen=True, slots=True)
class ArtifactEvent:
    """One durable observation in a generated artifact lifecycle."""

    package_name: str
    package_id: str
    execution_id: str
    artifact_id: str
    logical_name: str
    kind: str
    role: ArtifactRole
    structure: ArtifactStructure
    ownership: ArtifactOwnership
    state: ArtifactState = ArtifactState.AVAILABLE
    location: ArtifactLocation | None = None
    media_type: str | None = None
    format: str | None = None
    size_bytes: int | None = None
    checksum: str | None = None
    message: str | None = None
    revision: int = 1
    sequence: int = 1
    observed_at_epoch: float = field(default_factory=time.time)
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)
    schema_version: str = ARTIFACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Reject ambiguous, unsafe, or non-JSON artifact observations."""
        if self.schema_version != ARTIFACT_SCHEMA_VERSION:
            raise ValueError(f"unsupported artifact schema: {self.schema_version!r}")
        for enum_name, value, enum_type in (
            ("role", self.role, ArtifactRole),
            ("structure", self.structure, ArtifactStructure),
            ("ownership", self.ownership, ArtifactOwnership),
            ("state", self.state, ArtifactState),
        ):
            if not isinstance(value, enum_type):
                raise ValueError(
                    f"artifact {enum_name} must be an {enum_type.__name__}"
                )
        for field_name, value in (
            ("package_name", self.package_name),
            ("package_id", self.package_id),
            ("execution_id", self.execution_id),
            ("logical_name", self.logical_name),
            ("kind", self.kind),
        ):
            _validate_identity_text(field_name, value)
        if not isinstance(self.artifact_id, str) or not _ARTIFACT_ID_PATTERN.fullmatch(
            self.artifact_id
        ):
            raise ValueError("artifact_id must be an opaque JARVIS artifact identifier")
        if self.location is not None and not isinstance(
            self.location, ArtifactLocation
        ):
            raise ValueError("artifact location must be an ArtifactLocation")
        if (
            self.location is not None
            and self.location.kind is ArtifactLocationKind.EXECUTION_PATH
            and self.ownership is not ArtifactOwnership.EXECUTION
        ):
            raise ValueError("execution-path artifacts must be owned by the execution")
        if (
            self.location is not None
            and self.location.kind is not ArtifactLocationKind.EXECUTION_PATH
            and self.ownership is ArtifactOwnership.EXECUTION
        ):
            raise ValueError(
                "execution-owned artifacts must use an execution-relative location"
            )
        if self.state in {ArtifactState.AVAILABLE, ArtifactState.FINALIZED} and (
            self.location is None
        ):
            raise ValueError(f"artifact state {self.state.value!r} requires a location")
        if self.media_type is not None and not _MEDIA_TYPE_PATTERN.fullmatch(
            self.media_type
        ):
            raise ValueError("artifact media_type must be a valid type/subtype")
        if self.format is not None:
            _validate_identity_text("format", self.format)
        if self.size_bytes is not None and (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ValueError("artifact size_bytes must be a non-negative integer")
        if self.checksum is not None and not _CHECKSUM_PATTERN.fullmatch(self.checksum):
            raise ValueError("artifact checksum must be algorithm:hex-digest")
        if self.message is not None:
            if not isinstance(self.message, str) or not self.message.strip():
                raise ValueError("artifact message must be non-empty when supplied")
            if len(self.message) > MAX_MESSAGE_TEXT:
                raise ValueError("artifact message exceeds maximum length")
        for field_name, value in (
            ("revision", self.revision),
            ("sequence", self.sequence),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"artifact {field_name} must be a positive integer")
        _validate_finite("observed_at_epoch", self.observed_at_epoch)
        if self.observed_at_epoch < 0:
            raise ValueError("artifact observed_at_epoch cannot be negative")
        _canonical_json(dict(self.metadata))

    @property
    def terminal(self) -> bool:
        """Return whether the producer declared this artifact terminal.

        JARVIS may append one authoritative ``FINALIZED`` to ``INCOMPLETE``
        correction if the owning package process subsequently exits nonzero.
        """
        return self.state in {
            ArtifactState.FINALIZED,
            ArtifactState.INCOMPLETE,
            ArtifactState.FAILED,
        }

    def as_dict(self) -> dict[str, JsonValue]:
        """Serialize this event to the stable JARVIS artifact schema."""
        value: dict[str, JsonValue] = {
            "schema_version": self.schema_version,
            "package_name": self.package_name,
            "package_id": self.package_id,
            "execution_id": self.execution_id,
            "artifact_id": self.artifact_id,
            "logical_name": self.logical_name,
            "kind": self.kind,
            "role": self.role.value,
            "structure": self.structure.value,
            "ownership": self.ownership.value,
            "state": self.state.value,
            "revision": self.revision,
            "sequence": self.sequence,
            "observed_at_epoch": self.observed_at_epoch,
            "metadata": dict(self.metadata),
        }
        if self.location is not None:
            value["location"] = cast(dict[str, JsonValue], self.location.as_dict())
        for key, item in (
            ("media_type", self.media_type),
            ("format", self.format),
            ("size_bytes", self.size_bytes),
            ("checksum", self.checksum),
            ("message", self.message),
        ):
            if item is not None:
                value[key] = item
        return value

    def to_json(self) -> str:
        """Return one canonical JSON representation suitable for JSONL."""
        encoded = _canonical_json(self.as_dict())
        if len(encoded.encode("utf-8")) > MAX_ARTIFACT_EVENT_BYTES:
            raise ValueError("artifact event exceeds maximum encoded size")
        return encoded

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactEvent":
        """Parse and validate a mapping using the stable artifact schema."""
        unknown = set(value) - _EVENT_FIELDS
        if unknown:
            raise ValueError(f"unknown artifact event fields: {sorted(unknown)}")
        metadata = value.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("artifact metadata must be an object")
        location_value = value.get("location")
        if location_value is not None and not isinstance(location_value, dict):
            raise ValueError("artifact location must be an object")
        try:
            role = ArtifactRole(_required_str(value, "role"))
            structure = ArtifactStructure(_required_str(value, "structure"))
            ownership = ArtifactOwnership(_required_str(value, "ownership"))
            state = ArtifactState(_required_str(value, "state"))
        except ValueError as exc:
            raise ValueError("invalid artifact role, structure, or state") from exc
        return cls(
            schema_version=_required_str(value, "schema_version"),
            package_name=_required_str(value, "package_name"),
            package_id=_required_str(value, "package_id"),
            execution_id=_required_str(value, "execution_id"),
            artifact_id=_required_str(value, "artifact_id"),
            logical_name=_required_str(value, "logical_name"),
            kind=_required_str(value, "kind"),
            role=role,
            structure=structure,
            ownership=ownership,
            state=state,
            location=(
                ArtifactLocation.from_dict(location_value)
                if location_value is not None
                else None
            ),
            media_type=_optional_str(value, "media_type"),
            format=_optional_str(value, "format"),
            size_bytes=_optional_int(value, "size_bytes"),
            checksum=_optional_str(value, "checksum"),
            message=_optional_str(value, "message"),
            revision=_required_int(value, "revision"),
            sequence=_required_int(value, "sequence"),
            observed_at_epoch=_required_number(value, "observed_at_epoch"),
            metadata=cast(dict[str, JsonValue], metadata),
        )

    @classmethod
    def from_json(cls, payload: str) -> "ArtifactEvent":
        """Parse one bounded JSON artifact event."""
        if len(payload.encode("utf-8")) > MAX_ARTIFACT_EVENT_BYTES:
            raise ValueError("artifact event exceeds maximum encoded size")
        try:
            value = json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("artifact event is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("artifact event must be a JSON object")
        return cls.from_dict(cast(dict[str, Any], value))


def new_artifact_id() -> str:
    """Return a cryptographically random, opaque artifact identifier."""
    return f"art_{secrets.token_urlsafe(18)}"


def validate_artifact_history(events: list[ArtifactEvent]) -> None:
    """Validate ordering, identity, revisions, and lifecycle transitions."""
    stream_identity: tuple[str, str, str] | None = None
    previous_sequence = 0
    latest: dict[str, ArtifactEvent] = {}
    for event in events:
        identity = (event.execution_id, event.package_name, event.package_id)
        if stream_identity is None:
            stream_identity = identity
        elif identity != stream_identity:
            raise ValueError(
                "artifact event identity must remain stable within a store"
            )
        if event.sequence != previous_sequence + 1:
            raise ValueError(
                "artifact event sequences must be contiguous and increasing"
            )
        previous_sequence = event.sequence
        previous = latest.get(event.artifact_id)
        if previous is None:
            if event.revision != 1:
                raise ValueError("a new artifact must begin at revision 1")
        else:
            _validate_artifact_revision(previous, event)
        latest[event.artifact_id] = event


def _validate_artifact_revision(
    previous: ArtifactEvent,
    event: ArtifactEvent,
) -> None:
    if event.revision != previous.revision + 1:
        raise ValueError("artifact revisions must be contiguous and increasing")
    for field_name in (
        "logical_name",
        "kind",
        "role",
        "structure",
        "ownership",
    ):
        if getattr(event, field_name) != getattr(previous, field_name):
            raise ValueError(f"artifact {field_name} cannot change after registration")
    for field_name in ("location", "media_type", "format"):
        before = getattr(previous, field_name)
        after = getattr(event, field_name)
        if before is not None and after != before:
            raise ValueError(f"artifact {field_name} cannot change after being set")
    if previous.size_bytes is not None:
        if event.size_bytes is None:
            raise ValueError("artifact size_bytes cannot be cleared")
        if event.size_bytes < previous.size_bytes:
            raise ValueError("artifact size_bytes cannot decrease")
    if previous.checksum is not None and event.checksum != previous.checksum:
        raise ValueError("artifact checksum cannot change after being set")
    allowed = {
        ArtifactState.PRODUCING: {
            ArtifactState.PRODUCING,
            ArtifactState.AVAILABLE,
            ArtifactState.FINALIZED,
            ArtifactState.INCOMPLETE,
            ArtifactState.FAILED,
        },
        ArtifactState.AVAILABLE: {
            ArtifactState.AVAILABLE,
            ArtifactState.FINALIZED,
            ArtifactState.INCOMPLETE,
            ArtifactState.FAILED,
        },
        ArtifactState.FINALIZED: (
            {ArtifactState.INCOMPLETE}
            if _is_process_exit_reconciliation(previous, event)
            else set()
        ),
        ArtifactState.INCOMPLETE: set(),
        ArtifactState.FAILED: set(),
    }[previous.state]
    if event.state not in allowed:
        raise ValueError(
            f"artifact state cannot transition from {previous.state.value!r} "
            f"to {event.state.value!r}"
        )


def _is_process_exit_reconciliation(
    previous: ArtifactEvent,
    event: ArtifactEvent,
) -> bool:
    """Recognize the sole JARVIS-owned correction to a finalized artifact."""
    if (
        previous.state is not ArtifactState.FINALIZED
        or event.state is not ArtifactState.INCOMPLETE
    ):
        return False
    details = event.metadata.get(PROCESS_EXIT_RECONCILIATION_KEY)
    if not isinstance(details, dict) or set(details) != {
        "reported_state",
        "return_code",
        "source",
    }:
        return False
    return_code = details.get("return_code")
    if (
        isinstance(return_code, bool)
        or not isinstance(return_code, int)
        or return_code == 0
        or details.get("reported_state") != ArtifactState.FINALIZED.value
        or details.get("source") != "jarvis_process_owner"
    ):
        return False
    expected_metadata = dict(previous.metadata)
    expected_metadata[PROCESS_EXIT_RECONCILIATION_KEY] = dict(details)
    return dict(event.metadata) == expected_metadata


def _validate_execution_relative_location(value: str) -> None:
    if "\\" in value:
        raise ValueError(
            "execution-relative artifact locations must use '/' separators"
        )
    path = PurePosixPath(value)
    if path.is_absolute() or value.startswith("/"):
        raise ValueError("execution-relative artifact location cannot be absolute")
    if value.endswith("/") or "//" in value:
        raise ValueError("execution-relative artifact location is not normalized")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("execution-relative artifact location cannot escape its root")
    if path.parts and ":" in path.parts[0]:
        raise ValueError("execution-relative artifact location cannot contain a drive")
    if path.as_posix() != value:
        raise ValueError("execution-relative artifact location is not normalized")


def _validate_external_uri(value: str) -> None:
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if not scheme or not _URI_SCHEME_PATTERN.fullmatch(scheme):
        raise ValueError("external artifact location must have a valid URI scheme")
    if len(scheme) == 1:
        raise ValueError("external artifact location cannot be a drive path")
    if scheme in _UNSAFE_URI_SCHEMES:
        raise ValueError(f"external artifact URI scheme is not allowed: {scheme}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("external artifact URI cannot contain user information")
    if scheme in {"gs", "http", "https", "s3"} and not parsed.netloc:
        raise ValueError("external artifact URI requires an authority")


def _validate_cluster_path(value: str) -> None:
    if "\\" in value:
        raise ValueError("cluster artifact paths must use '/' separators")
    path = PurePosixPath(value)
    if not path.is_absolute() or not value.startswith("/"):
        raise ValueError("cluster artifact path must be absolute")
    if value == "/" or value.endswith("/") or "//" in value:
        raise ValueError("cluster artifact path is not a concrete normalized path")
    if any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise ValueError("cluster artifact path cannot contain traversal components")
    if path.as_posix() != value:
        raise ValueError("cluster artifact path is not normalized")


def _validate_identity_text(field_name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"artifact {field_name} must be non-empty")
    if len(value) > MAX_IDENTITY_TEXT:
        raise ValueError(f"artifact {field_name} exceeds maximum length")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"artifact {field_name} cannot contain control characters")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("artifact event must contain finite JSON values") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _validate_finite(field_name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"artifact {field_name} must be numeric")
    if not math.isfinite(value):
        raise ValueError(f"artifact {field_name} must be finite")


def _required_str(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ValueError(f"artifact {key} must be a string")
    return item


def _optional_str(value: Mapping[str, Any], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str):
        raise ValueError(f"artifact {key} must be a string")
    return item


def _required_int(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise ValueError(f"artifact {key} must be an integer")
    return item


def _optional_int(value: Mapping[str, Any], key: str) -> int | None:
    item = value.get(key)
    if item is None:
        return None
    if isinstance(item, bool) or not isinstance(item, int):
        raise ValueError(f"artifact {key} must be an integer")
    return item


def _required_number(value: Mapping[str, Any], key: str) -> float:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, (int, float)):
        raise ValueError(f"artifact {key} must be numeric")
    return float(item)
