"""Typed, application-independent progress records for JARVIS executions."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Mapping, cast

PROGRESS_SCHEMA_VERSION: Final = "jarvis.progress.v1"
MAX_PROGRESS_EVENT_BYTES: Final = 64 * 1024
MAX_IDENTITY_TEXT: Final = 256
MAX_MESSAGE_TEXT: Final = 4096
_EVENT_FIELDS: Final = frozenset(
    {
        "schema_version",
        "package_name",
        "package_id",
        "execution_id",
        "label",
        "state",
        "current",
        "total",
        "unit",
        "message",
        "sequence",
        "observed_at_epoch",
        "determinate",
        "metadata",
    }
)


class ProgressState(StrEnum):
    """Lifecycle state represented by a package progress observation."""

    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    READY = "ready"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One durable package progress observation.

    ``total`` is optional by design. An event is determinate only when its
    producer has supplied a real, positive total; JARVIS never invents one.
    """

    package_name: str
    package_id: str
    execution_id: str
    label: str
    state: ProgressState = ProgressState.RUNNING
    current: float | None = None
    total: float | None = None
    unit: str | None = None
    message: str | None = None
    sequence: int = 0
    observed_at_epoch: float = field(default_factory=time.time)
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)
    schema_version: str = PROGRESS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Reject ambiguous, non-finite, or non-JSON progress at creation."""
        if self.schema_version != PROGRESS_SCHEMA_VERSION:
            raise ValueError(f"unsupported progress schema: {self.schema_version!r}")
        if not isinstance(self.state, ProgressState):
            raise ValueError("progress state must be a ProgressState")
        for field_name, value in (
            ("package_name", self.package_name),
            ("package_id", self.package_id),
            ("execution_id", self.execution_id),
            ("label", self.label),
        ):
            if not value or not value.strip():
                raise ValueError(f"progress {field_name} must be non-empty")
            if len(value) > MAX_IDENTITY_TEXT:
                raise ValueError(f"progress {field_name} exceeds maximum length")
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 0
        ):
            raise ValueError("progress sequence must be a non-negative integer")
        _validate_finite("observed_at_epoch", self.observed_at_epoch)
        if self.observed_at_epoch < 0:
            raise ValueError("progress observed_at_epoch cannot be negative")
        if self.current is not None:
            _validate_finite("current", self.current)
            if self.current < 0:
                raise ValueError("progress current cannot be negative")
        if self.total is not None:
            _validate_finite("total", self.total)
            if self.total <= 0:
                raise ValueError("progress total must be positive")
            if self.current is None:
                raise ValueError("determinate progress requires current")
            if self.current > self.total:
                raise ValueError("progress current cannot exceed total")
        if self.unit is not None and not self.unit.strip():
            raise ValueError("progress unit must be non-empty when supplied")
        if self.unit is not None and len(self.unit) > MAX_IDENTITY_TEXT:
            raise ValueError("progress unit exceeds maximum length")
        if self.message is not None and not self.message.strip():
            raise ValueError("progress message must be non-empty when supplied")
        if self.message is not None and len(self.message) > MAX_MESSAGE_TEXT:
            raise ValueError("progress message exceeds maximum length")
        _canonical_json(dict(self.metadata))

    @property
    def determinate(self) -> bool:
        """Return whether the producer supplied a real quantitative total."""
        return self.current is not None and self.total is not None

    def as_dict(self) -> dict[str, JsonValue]:
        """Serialize this event to the stable JARVIS progress schema."""
        value: dict[str, JsonValue] = {
            "schema_version": self.schema_version,
            "package_name": self.package_name,
            "package_id": self.package_id,
            "execution_id": self.execution_id,
            "label": self.label,
            "state": self.state.value,
            "sequence": self.sequence,
            "observed_at_epoch": self.observed_at_epoch,
            "determinate": self.determinate,
            "metadata": dict(self.metadata),
        }
        for key, item in (
            ("current", self.current),
            ("total", self.total),
            ("unit", self.unit),
            ("message", self.message),
        ):
            if item is not None:
                value[key] = item
        return value

    def as_adapter_record(self) -> dict[str, object]:
        """Project the event into the existing relay adapter record shape."""
        metadata: dict[str, object] = {
            **dict(self.metadata),
            "schema_version": self.schema_version,
            "package_name": self.package_name,
            "package_id": self.package_id,
            "execution_id": self.execution_id,
            "run_id": self.execution_id,
            "progress_state": self.state.value,
            "sequence": self.sequence,
            "observed_at_epoch": self.observed_at_epoch,
            "determinate": self.determinate,
        }
        record: dict[str, object] = {
            "label": self.label,
            "message": self.message or self.label,
            "metadata": metadata,
        }
        if self.current is not None:
            record["current"] = self.current
        if self.total is not None:
            record["total"] = self.total
        if self.unit is not None:
            record["unit"] = self.unit
        return record

    def to_json(self) -> str:
        """Return one canonical JSON representation suitable for JSONL."""
        encoded = _canonical_json(self.as_dict())
        if len(encoded.encode("utf-8")) > MAX_PROGRESS_EVENT_BYTES:
            raise ValueError("progress event exceeds maximum encoded size")
        return encoded

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProgressEvent":
        """Parse and validate a mapping using the stable progress schema."""
        unknown = set(value) - _EVENT_FIELDS
        if unknown:
            raise ValueError(f"unknown progress event fields: {sorted(unknown)}")
        metadata = value.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("progress metadata must be an object")
        state = _required_str(value, "state")
        try:
            typed_state = ProgressState(state)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid progress state: {state!r}") from exc
        event = cls(
            schema_version=_required_str(value, "schema_version"),
            package_name=_required_str(value, "package_name"),
            package_id=_required_str(value, "package_id"),
            execution_id=_required_str(value, "execution_id"),
            label=_required_str(value, "label"),
            state=typed_state,
            current=_optional_number(value, "current"),
            total=_optional_number(value, "total"),
            unit=_optional_str(value, "unit"),
            message=_optional_str(value, "message"),
            sequence=_required_int(value, "sequence"),
            observed_at_epoch=_required_number(value, "observed_at_epoch"),
            metadata=cast(dict[str, JsonValue], metadata),
        )
        claimed_determinate = value.get("determinate")
        if not isinstance(claimed_determinate, bool):
            raise ValueError("progress determinate must be boolean")
        if claimed_determinate is not event.determinate:
            raise ValueError("progress determinate does not match current and total")
        return event

    @classmethod
    def from_json(cls, payload: str) -> "ProgressEvent":
        """Parse one bounded JSON event."""
        if len(payload.encode("utf-8")) > MAX_PROGRESS_EVENT_BYTES:
            raise ValueError("progress event exceeds maximum encoded size")
        try:
            value = json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("progress event is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("progress event must be a JSON object")
        return cls.from_dict(cast(dict[str, Any], value))


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
        raise ValueError("progress event must contain finite JSON values") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _validate_finite(field_name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"progress {field_name} must be numeric")
    try:
        finite = math.isfinite(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"progress {field_name} must be finite") from exc
    if not finite:
        raise ValueError(f"progress {field_name} must be finite")


def _required_str(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ValueError(f"progress {key} must be a string")
    return item


def _optional_str(value: Mapping[str, Any], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str):
        raise ValueError(f"progress {key} must be a string")
    return item


def _required_int(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise ValueError(f"progress {key} must be an integer")
    return item


def _required_number(value: Mapping[str, Any], key: str) -> float:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, (int, float)):
        raise ValueError(f"progress {key} must be numeric")
    return float(item)


def _optional_number(value: Mapping[str, Any], key: str) -> float | None:
    item = value.get(key)
    if item is None:
        return None
    if isinstance(item, bool) or not isinstance(item, (int, float)):
        raise ValueError(f"progress {key} must be numeric")
    return float(item)
