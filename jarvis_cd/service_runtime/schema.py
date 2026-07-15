"""Typed service-runtime records owned by durable JARVIS executions."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Final, Mapping, cast

SERVICE_RUNTIME_SCHEMA_VERSION: Final = "jarvis.service-runtime.v1"
DATASET_DESCRIPTOR_SCHEMA_VERSION: Final = "jarvis.dataset-descriptor.v1"
SERVICE_RUNTIME_SNAPSHOT_SCHEMA_VERSION: Final = "jarvis.execution.service-runtimes.v1"
MAX_SERVICE_RUNTIME_BYTES: Final = 256 * 1024
MAX_DATASET_MEMBERS: Final = 512
MAX_DATASET_ARRAYS: Final = 256
MAX_TEXT_BYTES: Final = 4096
_IDENTITY_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_ARTIFACT_ID_PATTERN: Final = re.compile(r"^art_[A-Za-z0-9_-]{22,86}$")
_SHA256_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
_REPORT_FIELDS: Final = frozenset(
    {
        "schema_version",
        "execution_id",
        "package_name",
        "package_id",
        "service_instance_id",
        "revision",
        "lifecycle",
        "host",
        "port",
        "protocol",
        "health_path",
        "live_data_path",
        "events_path",
        "state_path",
        "command_path",
        "delivery_mode",
        "dataset_descriptor",
        "message",
        "observed_at_epoch",
    }
)
_DESCRIPTOR_FIELDS: Final = frozenset(
    {
        "schema_version",
        "dataset_id",
        "kind",
        "format",
        "members",
        "arrays",
        "bounds",
        "fingerprint",
        "source_artifact",
    }
)
_MEMBER_FIELDS: Final = frozenset({"index", "location", "timestep"})
_ARRAY_FIELDS: Final = frozenset({"name", "association", "components", "units"})
_FINGERPRINT_FIELDS: Final = frozenset({"algorithm", "digest"})
_SOURCE_ARTIFACT_FIELDS: Final = frozenset({"artifact_id", "sha256"})


class ServiceLifecycle(StrEnum):
    """Lifecycle states reported by an owned network service."""

    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class ServiceProtocol(StrEnum):
    """Application protocols supported by the runtime contract."""

    HTTP = "http"
    HTTPS = "https"


@dataclass(frozen=True, slots=True)
class DatasetMember:
    """One ordered, cluster-local source member in a bounded dataset view."""

    index: int
    location: str
    timestep: float | None = None

    def __post_init__(self) -> None:
        """Validate ordering identity and a normalized absolute location."""
        if isinstance(self.index, bool) or not isinstance(self.index, int):
            raise ValueError("dataset member index must be an integer")
        if self.index < 0:
            raise ValueError("dataset member index cannot be negative")
        _validate_cluster_path(self.location)
        if self.timestep is not None:
            _validate_finite(self.timestep, "dataset member timestep")

    def to_dict(self) -> dict[str, Any]:
        """Serialize this member using the descriptor schema."""
        value: dict[str, Any] = {
            "index": self.index,
            "location": self.location,
        }
        if self.timestep is not None:
            value["timestep"] = self.timestep
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DatasetMember":
        """Parse one member without accepting silent schema extensions."""
        _reject_unknown(value, _MEMBER_FIELDS, "dataset member")
        index = value.get("index")
        location = value.get("location")
        timestep = value.get("timestep")
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("dataset member index must be an integer")
        if not isinstance(location, str):
            raise ValueError("dataset member location must be a string")
        if timestep is not None and (
            isinstance(timestep, bool) or not isinstance(timestep, (int, float))
        ):
            raise ValueError("dataset member timestep must be numeric")
        return cls(
            index=index,
            location=location,
            timestep=float(timestep) if timestep is not None else None,
        )


@dataclass(frozen=True, slots=True)
class DatasetArray:
    """One discovered data-array fact, never a visualization selection."""

    name: str
    association: str
    components: int
    units: str | None = None

    def __post_init__(self) -> None:
        """Validate bounded intrinsic array metadata."""
        _validate_text(self.name, "dataset array name", maximum=512)
        if self.association not in {"point", "cell", "field"}:
            raise ValueError("dataset array association must be point, cell, or field")
        if (
            isinstance(self.components, bool)
            or not isinstance(self.components, int)
            or not 1 <= self.components <= 64
        ):
            raise ValueError("dataset array components must be between 1 and 64")
        if self.units is not None:
            _validate_text(self.units, "dataset array units", maximum=256)

    def to_dict(self) -> dict[str, Any]:
        """Serialize one intrinsic array description."""
        value: dict[str, Any] = {
            "name": self.name,
            "association": self.association,
            "components": self.components,
        }
        if self.units is not None:
            value["units"] = self.units
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DatasetArray":
        """Parse one array description."""
        _reject_unknown(value, _ARRAY_FIELDS, "dataset array")
        name = value.get("name")
        association = value.get("association")
        components = value.get("components")
        units = value.get("units")
        if not isinstance(name, str) or not isinstance(association, str):
            raise ValueError("dataset array name and association must be strings")
        if isinstance(components, bool) or not isinstance(components, int):
            raise ValueError("dataset array components must be an integer")
        if units is not None and not isinstance(units, str):
            raise ValueError("dataset array units must be a string")
        return cls(name, association, components, units)


@dataclass(frozen=True, slots=True)
class DatasetDescriptor:
    """Intrinsic dataset identity and bounded discovery facts.

    The descriptor deliberately has no camera, colormap, threshold, filter, or
    scene-recipe fields. Those are runtime commands, not dataset identity.
    """

    dataset_id: str
    kind: str
    format: str
    members: tuple[DatasetMember, ...]
    fingerprint: str
    arrays: tuple[DatasetArray, ...] = ()
    bounds: tuple[float, float, float, float, float, float] | None = None
    source_artifact_id: str | None = None
    source_artifact_sha256: str | None = None
    schema_version: str = DATASET_DESCRIPTOR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Reject ambiguous identity and visualization-specific metadata."""
        if self.schema_version != DATASET_DESCRIPTOR_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported dataset descriptor schema: {self.schema_version!r}"
            )
        _validate_identity(self.dataset_id, "dataset_id")
        _validate_text(self.kind, "dataset kind", maximum=256)
        _validate_text(self.format, "dataset format", maximum=256)
        if not self.members or len(self.members) > MAX_DATASET_MEMBERS:
            raise ValueError(
                f"dataset descriptor requires 1-{MAX_DATASET_MEMBERS} members"
            )
        if tuple(member.index for member in self.members) != tuple(
            range(len(self.members))
        ):
            raise ValueError("dataset members must have contiguous ordered indexes")
        locations = [member.location for member in self.members]
        if len(locations) != len(set(locations)):
            raise ValueError("dataset member locations must be unique")
        if len(self.arrays) > MAX_DATASET_ARRAYS:
            raise ValueError("dataset descriptor has too many arrays")
        array_keys = [(array.association, array.name) for array in self.arrays]
        if len(array_keys) != len(set(array_keys)):
            raise ValueError("dataset array identities must be unique")
        if self.bounds is not None:
            if len(self.bounds) != 6:
                raise ValueError("dataset bounds must contain six values")
            for value in self.bounds:
                _validate_finite(value, "dataset bound")
            for lower, upper in zip(self.bounds[::2], self.bounds[1::2]):
                if lower > upper:
                    raise ValueError("dataset bound lower values cannot exceed upper")
        _validate_sha256(self.fingerprint, "dataset fingerprint")
        if (self.source_artifact_id is None) is not (
            self.source_artifact_sha256 is None
        ):
            raise ValueError(
                "dataset source artifact requires both artifact_id and sha256"
            )
        if self.source_artifact_id is not None:
            if _ARTIFACT_ID_PATTERN.fullmatch(self.source_artifact_id) is None:
                raise ValueError("dataset source artifact_id is invalid")
            assert self.source_artifact_sha256 is not None
            _validate_sha256(
                self.source_artifact_sha256,
                "dataset source artifact sha256",
            )
        calculated = calculate_dataset_fingerprint(
            dataset_id=self.dataset_id,
            kind=self.kind,
            format=self.format,
            members=self.members,
            arrays=self.arrays,
            bounds=self.bounds,
            source_artifact_id=self.source_artifact_id,
            source_artifact_sha256=self.source_artifact_sha256,
        )
        if self.fingerprint != calculated:
            raise ValueError(
                "dataset fingerprint does not match the canonical intrinsic descriptor"
            )
        self.to_json()

    @property
    def canonical_digest(self) -> str:
        """Return the SHA-256 digest of this exact descriptor document."""
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Serialize the stable descriptor schema."""
        value: dict[str, Any] = {
            "schema_version": self.schema_version,
            "dataset_id": self.dataset_id,
            "kind": self.kind,
            "format": self.format,
            "members": [member.to_dict() for member in self.members],
            "arrays": [array.to_dict() for array in self.arrays],
            "bounds": list(self.bounds) if self.bounds is not None else None,
            "fingerprint": {
                "algorithm": "sha256",
                "digest": self.fingerprint,
            },
            "source_artifact": None,
        }
        if self.source_artifact_id is not None:
            value["source_artifact"] = {
                "artifact_id": self.source_artifact_id,
                "sha256": self.source_artifact_sha256,
            }
        return value

    def to_json(self) -> str:
        """Serialize one canonical descriptor document."""
        return _canonical_json(self.to_dict(), "dataset descriptor")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DatasetDescriptor":
        """Parse and strictly validate a descriptor mapping."""
        _reject_unknown(value, _DESCRIPTOR_FIELDS, "dataset descriptor")
        schema_version = _required_str(value, "schema_version", "dataset descriptor")
        dataset_id = _required_str(value, "dataset_id", "dataset descriptor")
        kind = _required_str(value, "kind", "dataset descriptor")
        format_name = _required_str(value, "format", "dataset descriptor")
        members_value = value.get("members")
        arrays_value = value.get("arrays", [])
        bounds_value = value.get("bounds")
        fingerprint_value = value.get("fingerprint")
        source_value = value.get("source_artifact")
        if not isinstance(members_value, list):
            raise ValueError("dataset descriptor members must be a list")
        if not isinstance(arrays_value, list):
            raise ValueError("dataset descriptor arrays must be a list")
        if not isinstance(fingerprint_value, dict):
            raise ValueError("dataset descriptor fingerprint must be an object")
        _reject_unknown(
            fingerprint_value,
            _FINGERPRINT_FIELDS,
            "dataset fingerprint",
        )
        if fingerprint_value.get("algorithm") != "sha256":
            raise ValueError("dataset fingerprint algorithm must be sha256")
        fingerprint = fingerprint_value.get("digest")
        if not isinstance(fingerprint, str):
            raise ValueError("dataset fingerprint digest must be a string")
        members = tuple(
            DatasetMember.from_dict(_required_mapping(item, "dataset member"))
            for item in members_value
        )
        arrays = tuple(
            DatasetArray.from_dict(_required_mapping(item, "dataset array"))
            for item in arrays_value
        )
        bounds: tuple[float, float, float, float, float, float] | None = None
        if bounds_value is not None:
            if not isinstance(bounds_value, list) or len(bounds_value) != 6:
                raise ValueError("dataset descriptor bounds must be six values or null")
            parsed_bounds: list[float] = []
            for item in bounds_value:
                if isinstance(item, bool) or not isinstance(item, (int, float)):
                    raise ValueError("dataset descriptor bounds must be numeric")
                parsed_bounds.append(float(item))
            bounds = cast(
                tuple[float, float, float, float, float, float],
                tuple(parsed_bounds),
            )
        source_artifact_id: str | None = None
        source_artifact_sha256: str | None = None
        if source_value is not None:
            source = _required_mapping(source_value, "dataset source artifact")
            _reject_unknown(
                source,
                _SOURCE_ARTIFACT_FIELDS,
                "dataset source artifact",
            )
            source_artifact_id = _required_str(
                source,
                "artifact_id",
                "dataset source artifact",
            )
            source_artifact_sha256 = _required_str(
                source,
                "sha256",
                "dataset source artifact",
            )
        return cls(
            schema_version=schema_version,
            dataset_id=dataset_id,
            kind=kind,
            format=format_name,
            members=members,
            arrays=arrays,
            bounds=bounds,
            fingerprint=fingerprint,
            source_artifact_id=source_artifact_id,
            source_artifact_sha256=source_artifact_sha256,
        )

    @classmethod
    def from_json(cls, payload: str) -> "DatasetDescriptor":
        """Parse a duplicate-key-safe JSON descriptor."""
        value = _load_json(payload, "dataset descriptor")
        return cls.from_dict(value)


@dataclass(frozen=True, slots=True)
class ServiceRuntimeReport:
    """One durable observation of an execution-owned network service."""

    execution_id: str
    package_name: str
    package_id: str
    service_instance_id: str
    revision: int
    lifecycle: ServiceLifecycle
    host: str
    port: int
    protocol: ServiceProtocol
    health_path: str
    live_data_path: str
    events_path: str
    state_path: str
    command_path: str
    dataset_descriptor: DatasetDescriptor
    message: str | None = None
    observed_at_epoch: float = field(default_factory=time.time)
    delivery_mode: str = "push"
    schema_version: str = SERVICE_RUNTIME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Validate a complete, agent-queryable service observation."""
        if self.schema_version != SERVICE_RUNTIME_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported service runtime schema: {self.schema_version!r}"
            )
        for field_name, value in (
            ("execution_id", self.execution_id),
            ("package_name", self.package_name),
            ("package_id", self.package_id),
            ("service_instance_id", self.service_instance_id),
        ):
            _validate_identity(value, field_name)
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise ValueError("service runtime revision must be a positive integer")
        if not isinstance(self.lifecycle, ServiceLifecycle):
            raise ValueError("service runtime lifecycle must be a ServiceLifecycle")
        _validate_host(self.host)
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65535
        ):
            raise ValueError("service runtime port must be between 1 and 65535")
        if not isinstance(self.protocol, ServiceProtocol):
            raise ValueError("service runtime protocol must be a ServiceProtocol")
        paths = (
            self.health_path,
            self.live_data_path,
            self.events_path,
            self.state_path,
            self.command_path,
        )
        for path in paths:
            _validate_http_path(path)
        if len(set(paths)) != len(paths):
            raise ValueError("service runtime endpoint paths must be distinct")
        if self.delivery_mode != "push":
            raise ValueError("service runtime delivery_mode must be push")
        if self.message is not None:
            _validate_text(self.message, "service runtime message")
        _validate_finite(self.observed_at_epoch, "service runtime observed_at_epoch")
        if self.observed_at_epoch < 0:
            raise ValueError("service runtime observed_at_epoch cannot be negative")
        self.to_json()

    @property
    def base_url(self) -> str:
        """Return the service origin without granting desktop routing authority."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.protocol.value}://{host}:{self.port}"

    def to_dict(self) -> dict[str, Any]:
        """Serialize the stable service-runtime schema."""
        return {
            "schema_version": self.schema_version,
            "execution_id": self.execution_id,
            "package_name": self.package_name,
            "package_id": self.package_id,
            "service_instance_id": self.service_instance_id,
            "revision": self.revision,
            "lifecycle": self.lifecycle.value,
            "host": self.host,
            "port": self.port,
            "protocol": self.protocol.value,
            "health_path": self.health_path,
            "live_data_path": self.live_data_path,
            "events_path": self.events_path,
            "state_path": self.state_path,
            "command_path": self.command_path,
            "delivery_mode": self.delivery_mode,
            "dataset_descriptor": self.dataset_descriptor.to_dict(),
            "message": self.message,
            "observed_at_epoch": self.observed_at_epoch,
        }

    def to_json(self) -> str:
        """Serialize a bounded canonical JSONL payload."""
        payload = _canonical_json(self.to_dict(), "service runtime report")
        if len(payload.encode("utf-8")) > MAX_SERVICE_RUNTIME_BYTES:
            raise ValueError("service runtime report exceeds maximum encoded size")
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ServiceRuntimeReport":
        """Parse a report and reject unversioned extensions."""
        _reject_unknown(value, _REPORT_FIELDS, "service runtime report")
        descriptor_value = _required_mapping(
            value.get("dataset_descriptor"),
            "service runtime dataset_descriptor",
        )
        revision = value.get("revision")
        port = value.get("port")
        observed = value.get("observed_at_epoch")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise ValueError("service runtime revision must be an integer")
        if isinstance(port, bool) or not isinstance(port, int):
            raise ValueError("service runtime port must be an integer")
        if isinstance(observed, bool) or not isinstance(observed, (int, float)):
            raise ValueError("service runtime observed_at_epoch must be numeric")
        message = value.get("message")
        if message is not None and not isinstance(message, str):
            raise ValueError("service runtime message must be a string or null")
        try:
            lifecycle = ServiceLifecycle(
                _required_str(value, "lifecycle", "service runtime")
            )
            protocol = ServiceProtocol(
                _required_str(value, "protocol", "service runtime")
            )
        except ValueError as exc:
            raise ValueError("invalid service runtime lifecycle or protocol") from exc
        return cls(
            schema_version=_required_str(
                value,
                "schema_version",
                "service runtime",
            ),
            execution_id=_required_str(value, "execution_id", "service runtime"),
            package_name=_required_str(value, "package_name", "service runtime"),
            package_id=_required_str(value, "package_id", "service runtime"),
            service_instance_id=_required_str(
                value,
                "service_instance_id",
                "service runtime",
            ),
            revision=revision,
            lifecycle=lifecycle,
            host=_required_str(value, "host", "service runtime"),
            port=port,
            protocol=protocol,
            health_path=_required_str(value, "health_path", "service runtime"),
            live_data_path=_required_str(
                value,
                "live_data_path",
                "service runtime",
            ),
            events_path=_required_str(value, "events_path", "service runtime"),
            state_path=_required_str(value, "state_path", "service runtime"),
            command_path=_required_str(value, "command_path", "service runtime"),
            delivery_mode=_required_str(
                value,
                "delivery_mode",
                "service runtime",
            ),
            dataset_descriptor=DatasetDescriptor.from_dict(descriptor_value),
            message=message,
            observed_at_epoch=float(observed),
        )

    @classmethod
    def from_json(cls, payload: str) -> "ServiceRuntimeReport":
        """Parse one bounded, duplicate-key-safe report."""
        if len(payload.encode("utf-8")) > MAX_SERVICE_RUNTIME_BYTES:
            raise ValueError("service runtime report exceeds maximum encoded size")
        return cls.from_dict(_load_json(payload, "service runtime report"))


def validate_service_runtime_history(reports: list[ServiceRuntimeReport]) -> None:
    """Validate immutable identities, revisions, and lifecycle transitions."""
    latest: dict[str, ServiceRuntimeReport] = {}
    transitions = {
        ServiceLifecycle.STARTING: {
            ServiceLifecycle.STARTING,
            ServiceLifecycle.READY,
            ServiceLifecycle.DEGRADED,
            ServiceLifecycle.STOPPING,
            ServiceLifecycle.STOPPED,
            ServiceLifecycle.FAILED,
        },
        ServiceLifecycle.READY: {
            ServiceLifecycle.READY,
            ServiceLifecycle.DEGRADED,
            ServiceLifecycle.STOPPING,
            ServiceLifecycle.STOPPED,
            ServiceLifecycle.FAILED,
        },
        ServiceLifecycle.DEGRADED: {
            ServiceLifecycle.READY,
            ServiceLifecycle.DEGRADED,
            ServiceLifecycle.STOPPING,
            ServiceLifecycle.STOPPED,
            ServiceLifecycle.FAILED,
        },
        ServiceLifecycle.STOPPING: {
            ServiceLifecycle.STOPPING,
            ServiceLifecycle.STOPPED,
            ServiceLifecycle.FAILED,
        },
        ServiceLifecycle.STOPPED: set(),
        ServiceLifecycle.FAILED: set(),
    }
    stream_identity: tuple[str, str, str] | None = None
    for report in reports:
        identity = (
            report.execution_id,
            report.package_name,
            report.package_id,
        )
        if stream_identity is None:
            stream_identity = identity
        elif identity != stream_identity:
            raise ValueError("service runtime package identity changed within a store")
        previous = latest.get(report.service_instance_id)
        if previous is None:
            if report.revision != 1:
                raise ValueError("first service runtime revision must be 1")
            if report.lifecycle not in {
                ServiceLifecycle.STARTING,
                ServiceLifecycle.READY,
                ServiceLifecycle.FAILED,
            }:
                raise ValueError("first service runtime lifecycle is invalid")
        else:
            if report.revision != previous.revision + 1:
                raise ValueError("service runtime revisions must increase by one")
            if report.lifecycle not in transitions[previous.lifecycle]:
                raise ValueError(
                    "invalid service runtime lifecycle transition: "
                    f"{previous.lifecycle.value} -> {report.lifecycle.value}"
                )
            immutable = (
                "execution_id",
                "package_name",
                "package_id",
                "service_instance_id",
                "host",
                "port",
                "protocol",
                "health_path",
                "live_data_path",
                "events_path",
                "state_path",
                "command_path",
                "delivery_mode",
                "dataset_descriptor",
            )
            for name in immutable:
                if getattr(report, name) != getattr(previous, name):
                    raise ValueError(f"service runtime {name} cannot change")
            if report.observed_at_epoch < previous.observed_at_epoch:
                raise ValueError("service runtime timestamps cannot move backward")
        latest[report.service_instance_id] = report


def calculate_dataset_fingerprint(
    *,
    dataset_id: str,
    kind: str,
    format: str,
    members: tuple[DatasetMember, ...],
    arrays: tuple[DatasetArray, ...] = (),
    bounds: tuple[float, float, float, float, float, float] | None = None,
    source_artifact_id: str | None = None,
    source_artifact_sha256: str | None = None,
) -> str:
    """Hash canonical intrinsic descriptor content, excluding the digest itself.

    This fingerprint authenticates the ordered catalog description without
    reading potentially multi-terabyte member contents. When content identity
    is known, ``source_artifact_sha256`` carries that separate integrity fact.
    """
    source_artifact: dict[str, str] | None = None
    if source_artifact_id is not None or source_artifact_sha256 is not None:
        if source_artifact_id is None or source_artifact_sha256 is None:
            raise ValueError(
                "dataset source artifact requires both artifact_id and sha256"
            )
        source_artifact = {
            "artifact_id": source_artifact_id,
            "sha256": source_artifact_sha256,
        }
    document = {
        "schema_version": DATASET_DESCRIPTOR_SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "kind": kind,
        "format": format,
        "members": [member.to_dict() for member in members],
        "arrays": [array.to_dict() for array in arrays],
        "bounds": list(bounds) if bounds is not None else None,
        "source_artifact": source_artifact,
    }
    return hashlib.sha256(
        _canonical_json(document, "dataset fingerprint input").encode("utf-8")
    ).hexdigest()


def _validate_identity(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _IDENTITY_PATTERN.fullmatch(value) is None:
        raise ValueError(f"service runtime {field_name} is invalid")


def _validate_text(
    value: str, field_name: str, *, maximum: int = MAX_TEXT_BYTES
) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be bounded printable text")


def _validate_cluster_path(value: str) -> None:
    _validate_text(value, "dataset member location")
    if "\\" in value:
        raise ValueError("dataset member locations must use POSIX separators")
    path = PurePosixPath(value)
    if not path.is_absolute() or path.as_posix() != value or ".." in path.parts:
        raise ValueError("dataset member locations must be normalized absolute paths")


def _validate_sha256(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _validate_finite(value: float, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be numeric")
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")


def _validate_host(value: str) -> None:
    _validate_text(value, "service runtime host", maximum=255)
    if value in {"0.0.0.0", "::", "*", "localhost"}:
        raise ValueError("service runtime host must be an advertised reachable host")
    try:
        ipaddress.ip_address(value)
        return
    except ValueError:
        pass
    labels = value.rstrip(".").split(".")
    if any(
        not label
        or len(label) > 63
        or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label) is None
        for label in labels
    ):
        raise ValueError("service runtime host must be a DNS name or IP address")


def _validate_http_path(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or len(value.encode("ascii", errors="ignore")) != len(value)
        or len(value) > 256
        or any(character in value for character in "?#\\")
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise ValueError(
            "service runtime endpoint paths must be bounded absolute paths"
        )
    if PurePosixPath(value).as_posix() != value or ".." in PurePosixPath(value).parts:
        raise ValueError("service runtime endpoint paths must be normalized")


def _canonical_json(value: object, label: str) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must contain finite JSON values") from exc


def _load_json(payload: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return cast(dict[str, Any], value)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_unknown(
    value: Mapping[str, Any],
    expected: frozenset[str],
    label: str,
) -> None:
    unknown = set(value) - expected
    if unknown:
        raise ValueError(f"unknown {label} fields: {sorted(unknown)}")


def _required_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return cast(dict[str, Any], value)


def _required_str(value: Mapping[str, Any], key: str, label: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ValueError(f"{label} {key} must be a string")
    return item


__all__ = [
    "DATASET_DESCRIPTOR_SCHEMA_VERSION",
    "MAX_DATASET_ARRAYS",
    "MAX_DATASET_MEMBERS",
    "MAX_SERVICE_RUNTIME_BYTES",
    "SERVICE_RUNTIME_SCHEMA_VERSION",
    "SERVICE_RUNTIME_SNAPSHOT_SCHEMA_VERSION",
    "DatasetArray",
    "DatasetDescriptor",
    "DatasetMember",
    "ServiceLifecycle",
    "ServiceProtocol",
    "ServiceRuntimeReport",
    "calculate_dataset_fingerprint",
    "validate_service_runtime_history",
]
