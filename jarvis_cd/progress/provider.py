"""Generic package progress provider protocol and stream utilities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from .schema import JsonValue, ProgressEvent, ProgressState


@dataclass(frozen=True, slots=True)
class ProgressObservation:
    """Application-specific progress interpreted without execution identity.

    JARVIS core supplies package and execution identity when it persists an
    observation. Providers therefore cannot select an authoritative sidecar or
    impersonate another package through this object.
    """

    label: str
    state: ProgressState = ProgressState.RUNNING
    current: float | None = None
    total: float | None = None
    unit: str | None = None
    message: str | None = None
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate the observation with the same rules as persisted events."""
        ProgressEvent(
            package_name="provider",
            package_id="provider",
            execution_id="provider",
            label=self.label,
            state=self.state,
            current=self.current,
            total=self.total,
            unit=self.unit,
            message=self.message,
            sequence=0,
            metadata=self.metadata,
        ).to_json()


@runtime_checkable
class PackageProgressProvider(Protocol):
    """Minimal JARVIS-owned progress contract implemented beside a package."""

    def observe_progress(self, text: str) -> list[ProgressObservation]:
        """Interpret application stdout and return typed observations."""
        ...

    def finalize_progress(self) -> list[ProgressObservation]:
        """Flush a final unterminated application-output fragment."""
        ...

    def reset_progress(self) -> None:
        """Reset parsing after the underlying application stream is replaced."""
        ...


@runtime_checkable
class ProcessExitProgressProvider(Protocol):
    """Optional provider extension for JARVIS-owned process completion."""

    def finalize_progress_for_exit(self, return_code: int) -> list[ProgressObservation]:
        """Finalize observations using the authoritative process return code."""
        ...


@runtime_checkable
class RelayProgressAdapter(Protocol):
    """Legacy clio-relay adapter contract kept outside the JARVIS core SPI."""

    package_name: str
    package_id: str
    package_version: str
    run_id: str
    adapter_name: str
    application_profile: str | None

    def observe_jarvis_stdout(self, text: str) -> list[dict[str, object]]:
        """Observe text framed by JARVIS package lifecycle markers."""
        ...

    def observe_stdout(self, text: str) -> list[dict[str, object]]:
        """Observe text from a trusted package-owned stream or sidecar."""
        ...

    def finalize_jarvis_stdout(self) -> list[dict[str, object]]:
        """Flush an incomplete final JARVIS stdout line."""
        ...

    def finalize_stdout(self) -> list[dict[str, object]]:
        """Flush an incomplete final package-owned stream line."""
        ...

    def reset_stdout(self) -> None:
        """Reset parsing after a package-owned sidecar is replaced."""
        ...

    def progress_log_paths(self) -> list[Path]:
        """Return explicitly shared package-owned progress sidecars."""
        ...

    def package_load_probe_python(self) -> str | None:
        """Return an optional Python probe for the provider implementation."""
        ...

    def acceptance_progress_valid(self, metadata: dict[str, Any]) -> bool:
        """Validate package-specific evidence in an observed record."""
        ...


ProgressProviderFactory = Callable[[dict[str, Any]], PackageProgressProvider | None]
RelayProgressAdapterFactory = Callable[[dict[str, Any]], RelayProgressAdapter | None]


class LineBuffer:
    """Turn arbitrary text chunks into complete lines without losing fragments."""

    def __init__(self) -> None:
        self.fragment = ""

    def feed(self, text: str, *, finalize: bool = False) -> list[str]:
        """Return complete lines and retain any incomplete final fragment."""
        lines = (self.fragment + text).splitlines(keepends=True)
        if not finalize and lines and not lines[-1].endswith(("\n", "\r")):
            self.fragment = lines.pop()
        else:
            self.fragment = ""
        return [line.rstrip("\r\n") for line in lines]

    def reset(self) -> None:
        """Discard a buffered fragment."""
        self.fragment = ""


class PackageScopeFilter:
    """Expose lines only while JARVIS identifies the owning package as active."""

    def __init__(self, package_name: str) -> None:
        self.package_name = package_name
        self.active = False

    def observe(self, line: str) -> str | None:
        """Return an in-scope line, or ``None`` for markers/unrelated output."""
        stripped = line.strip()
        if stripped == f"[{self.package_name}] [START] BEGIN":
            self.active = True
            return None
        if stripped == f"[{self.package_name}] [START] END":
            self.active = False
            return None
        return line if self.active else None

    def reset(self) -> None:
        """Close the active package scope."""
        self.active = False
