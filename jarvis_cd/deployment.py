"""Versioned, path-free package deployment and readiness contracts.

Packages own the semantic facts in these contracts.  JARVIS supplies only the
strict schema and serialization boundary so callers such as ``jarvis_describe``
do not need application-specific prompt instructions.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from shutil import which
from typing import Any, Literal, Mapping, Sequence

PACKAGE_DEPLOYMENT_SCHEMA_VERSION = "jarvis.package-deployment.v1"

ExecutionKind = Literal["batch", "service"]
ReadinessMechanism = Literal[
    "process_exit",
    "progress_event",
    "service_runtime",
]
RuntimeState = Literal["ready", "unavailable", "unknown"]
ConditionOperator = Literal[
    "equals",
    "greater_than",
    "is_empty",
    "is_not_empty",
]
JsonScalar = str | int | float | bool | None

_TOKEN_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")
_PARAMETER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_token(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _TOKEN_PATTERN.fullmatch(value) is None:
        raise ValueError(
            f"{field_name} must be a lowercase semantic token, got {value!r}"
        )


def _validate_text(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{field_name} must be non-empty printable text")


def _validate_unique_tokens(values: tuple[str, ...], field_name: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} cannot contain duplicates")
    for value in values:
        _validate_token(value, field_name)


def _looks_absolute(value: str) -> bool:
    """Recognize both POSIX and Windows absolute paths on every host OS."""
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


@dataclass(frozen=True, slots=True)
class ConfigurationCondition:
    """One machine-readable predicate over a package configuration parameter."""

    parameter: str
    operator: ConditionOperator
    value: JsonScalar = None

    def __post_init__(self) -> None:
        """Reject ambiguous conditions at the package boundary."""
        if (
            not isinstance(self.parameter, str)
            or _PARAMETER_PATTERN.fullmatch(self.parameter) is None
        ):
            raise ValueError(f"invalid configuration parameter: {self.parameter!r}")
        if self.operator not in {
            "equals",
            "greater_than",
            "is_empty",
            "is_not_empty",
        }:
            raise ValueError(f"unsupported condition operator: {self.operator!r}")
        if self.operator in {"is_empty", "is_not_empty"} and self.value is not None:
            raise ValueError(f"{self.operator} conditions cannot include a value")
        if self.operator == "greater_than" and (
            isinstance(self.value, bool) or not isinstance(self.value, (int, float))
        ):
            raise ValueError("greater_than conditions require a numeric value")

    def to_dict(self) -> dict[str, Any]:
        """Serialize the condition without implementation-only fields."""
        value: dict[str, Any] = {
            "parameter": self.parameter,
            "operator": self.operator,
        }
        if self.operator not in {"is_empty", "is_not_empty"}:
            value["value"] = self.value
        return value


@dataclass(frozen=True, slots=True)
class ConfigurationRule:
    """Configuration requirements activated when every ``when`` predicate holds."""

    when: tuple[ConfigurationCondition, ...]
    requires: tuple[ConfigurationCondition, ...]
    description: str

    def __post_init__(self) -> None:
        """Require every rule to carry actionable semantics."""
        if not self.when:
            raise ValueError("configuration rules require at least one condition")
        if not self.requires:
            raise ValueError("configuration rules require at least one requirement")
        _validate_text(self.description, "configuration rule description")

    def to_dict(self) -> dict[str, Any]:
        """Serialize one conditional configuration rule."""
        return {
            "when": [condition.to_dict() for condition in self.when],
            "requires": [condition.to_dict() for condition in self.requires],
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class ProviderResolution:
    """A provider-native query that may satisfy a runtime requirement.

    The query is a semantic selector such as a Spack spec, never an install
    prefix, executable path, or source directory.
    """

    provider: str
    query_kind: str
    query_value: str

    def __post_init__(self) -> None:
        """Validate provider identity and keep resolution hints path-free."""
        _validate_token(self.provider, "runtime provider")
        _validate_token(self.query_kind, "provider query kind")
        _validate_text(self.query_value, "provider query value")
        if _looks_absolute(self.query_value):
            raise ValueError("provider queries cannot expose an absolute path")

    def to_dict(self) -> dict[str, Any]:
        """Serialize the provider-neutral resolution envelope."""
        return {
            "provider": self.provider,
            "query": {
                "kind": self.query_kind,
                "value": self.query_value,
            },
        }


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """Current usability result for one package-owned runtime probe."""

    state: RuntimeState
    reason_code: str

    def __post_init__(self) -> None:
        """Keep usability and diagnostics finite and machine-readable."""
        if self.state not in {"ready", "unavailable", "unknown"}:
            raise ValueError(f"unsupported runtime state: {self.state!r}")
        _validate_token(self.reason_code, "runtime status reason code")

    @property
    def usable(self) -> bool | None:
        """Return the tri-state usability projection required by agent callers."""
        if self.state == "ready":
            return True
        if self.state == "unavailable":
            return False
        return None

    def to_dict(self) -> dict[str, Any]:
        """Serialize status without leaking probe commands or resolved paths."""
        return {
            "state": self.state,
            "usable": self.usable,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class ProgramProbeResult:
    """Internal result of a bounded program usability probe."""

    status: RuntimeStatus
    output: str = ""


def probe_program(
    program: str,
    *,
    environment: Mapping[str, str],
    arguments: Sequence[str] = ("--help",),
    timeout_seconds: float = 15,
) -> ProgramProbeResult:
    """Probe a program through ``PATH`` without exposing its resolved location.

    Packages choose the semantic program and interpret any advertised
    capabilities.  This helper only performs bounded, side-effect-free process
    discovery and execution.
    """
    _validate_text(program, "runtime program")
    if _looks_absolute(program):
        raise ValueError("runtime probes must use a semantic program name")
    if timeout_seconds <= 0:
        raise ValueError("runtime probe timeout must be positive")
    resolved = which(program, path=environment.get("PATH"))
    if resolved is None:
        return ProgramProbeResult(RuntimeStatus("unavailable", "software_not_found"))
    try:
        completed = subprocess.run(
            [resolved, *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(environment),
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        return ProgramProbeResult(RuntimeStatus("unavailable", "runtime_probe_failed"))
    if completed.returncode != 0:
        return ProgramProbeResult(RuntimeStatus("unavailable", "runtime_probe_failed"))
    return ProgramProbeResult(
        RuntimeStatus("ready", "runtime_probe_succeeded"),
        output=f"{completed.stdout}\n{completed.stderr}",
    )


@dataclass(frozen=True, slots=True)
class RuntimeRequirement:
    """One semantic software dependency and its current capability state."""

    requirement_id: str
    description: str
    required_capabilities: tuple[str, ...]
    available_capabilities: tuple[str, ...]
    status: RuntimeStatus
    provider_resolutions: tuple[ProviderResolution, ...] = ()

    def __post_init__(self) -> None:
        """Validate capability claims and provider alternatives."""
        _validate_token(self.requirement_id, "runtime requirement ID")
        _validate_text(self.description, "runtime requirement description")
        if not self.required_capabilities:
            raise ValueError("runtime requirements need at least one capability")
        _validate_unique_tokens(
            self.required_capabilities,
            "required runtime capabilities",
        )
        _validate_unique_tokens(
            self.available_capabilities,
            "available runtime capabilities",
        )
        if self.status.usable is True and not set(self.required_capabilities).issubset(
            self.available_capabilities
        ):
            raise ValueError("a ready runtime must advertise every required capability")
        provider_keys = {
            (resolution.provider, resolution.query_kind, resolution.query_value)
            for resolution in self.provider_resolutions
        }
        if len(provider_keys) != len(self.provider_resolutions):
            raise ValueError("runtime provider resolutions cannot contain duplicates")

    def to_dict(self) -> dict[str, Any]:
        """Serialize one path-free runtime requirement."""
        return {
            "id": self.requirement_id,
            "description": self.description,
            "required_capabilities": list(self.required_capabilities),
            "available_capabilities": list(self.available_capabilities),
            "status": self.status.to_dict(),
            "provider_resolutions": [
                resolution.to_dict() for resolution in self.provider_resolutions
            ],
        }


@dataclass(frozen=True, slots=True)
class ReadinessContract:
    """How a caller observes that an execution became useful or completed."""

    mechanism: ReadinessMechanism
    condition: str
    capability: str | None = None

    def __post_init__(self) -> None:
        """Validate generic readiness semantics."""
        if self.mechanism not in {
            "process_exit",
            "progress_event",
            "service_runtime",
        }:
            raise ValueError(f"unsupported readiness mechanism: {self.mechanism!r}")
        _validate_token(self.condition, "readiness condition")
        if self.capability is not None:
            _validate_token(self.capability, "readiness capability")
        if self.mechanism == "service_runtime" and self.capability is None:
            raise ValueError("service runtime readiness requires a capability")
        if self.mechanism != "service_runtime" and self.capability is not None:
            raise ValueError(
                "only service runtime readiness can name a service capability"
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize readiness without scheduler- or site-specific details."""
        value: dict[str, Any] = {
            "mechanism": self.mechanism,
            "condition": self.condition,
        }
        if self.capability is not None:
            value["capability"] = self.capability
        return value


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    """One selectable package execution mode."""

    name: str
    execution_kind: ExecutionKind
    when: tuple[ConfigurationCondition, ...]
    runtime_requirements: tuple[str, ...]
    readiness: ReadinessContract

    def __post_init__(self) -> None:
        """Validate a complete execution profile."""
        _validate_token(self.name, "execution profile name")
        if self.execution_kind not in {"batch", "service"}:
            raise ValueError(f"unsupported execution kind: {self.execution_kind!r}")
        if not self.when:
            raise ValueError("execution profiles require selection conditions")
        if not self.runtime_requirements:
            raise ValueError("execution profiles require a runtime")
        _validate_unique_tokens(
            self.runtime_requirements,
            "execution profile runtime requirements",
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize one agent-selectable execution profile."""
        return {
            "name": self.name,
            "execution_kind": self.execution_kind,
            "when": [condition.to_dict() for condition in self.when],
            "runtime_requirements": list(self.runtime_requirements),
            "readiness": self.readiness.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class PackageDeploymentContract:
    """Complete package-owned deployment metadata exposed to generic clients."""

    package: str
    execution_profiles: tuple[ExecutionProfile, ...]
    runtime_requirements: tuple[RuntimeRequirement, ...]
    configuration_rules: tuple[ConfigurationRule, ...] = ()
    schema_version: str = PACKAGE_DEPLOYMENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Validate references and the exact supported contract version."""
        if self.schema_version != PACKAGE_DEPLOYMENT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported package deployment schema: {self.schema_version!r}"
            )
        _validate_token(self.package, "package identity")
        if not self.execution_profiles:
            raise ValueError("deployment contracts require an execution profile")
        if not self.runtime_requirements:
            raise ValueError("deployment contracts require a runtime requirement")

        profile_names = [profile.name for profile in self.execution_profiles]
        if len(set(profile_names)) != len(profile_names):
            raise ValueError("execution profile names must be unique")
        requirements = {
            requirement.requirement_id for requirement in self.runtime_requirements
        }
        if len(requirements) != len(self.runtime_requirements):
            raise ValueError("runtime requirement IDs must be unique")
        for profile in self.execution_profiles:
            missing = set(profile.runtime_requirements) - requirements
            if missing:
                raise ValueError(
                    f"execution profile {profile.name!r} references unknown runtime "
                    f"requirements: {sorted(missing)}"
                )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the stable contract consumed by package describers."""
        return {
            "schema_version": self.schema_version,
            "package": self.package,
            "execution_profiles": [
                profile.to_dict() for profile in self.execution_profiles
            ],
            "runtime_requirements": [
                requirement.to_dict() for requirement in self.runtime_requirements
            ],
            "configuration_rules": [
                rule.to_dict() for rule in self.configuration_rules
            ],
        }


__all__ = [
    "PACKAGE_DEPLOYMENT_SCHEMA_VERSION",
    "ConfigurationCondition",
    "ConfigurationRule",
    "ExecutionProfile",
    "PackageDeploymentContract",
    "ProgramProbeResult",
    "ProviderResolution",
    "ReadinessContract",
    "RuntimeRequirement",
    "RuntimeStatus",
    "probe_program",
]
