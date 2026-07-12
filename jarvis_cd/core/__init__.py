"""Core JARVIS-CD classes and utilities with lazy compatibility exports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .container_pkg import ContainerApplication, ContainerService
    from .pkg import Application, Service

__all__ = [
    "Application",
    "Service",
    "ContainerApplication",
    "ContainerService",
]


def __getattr__(name: str) -> Any:
    """Resolve legacy public classes without eager package import cycles."""
    if name in {"Application", "Service"}:
        from .pkg import Application, Service

        value = {"Application": Application, "Service": Service}[name]
    elif name in {"ContainerApplication", "ContainerService"}:
        from .container_pkg import ContainerApplication, ContainerService

        value = {
            "ContainerApplication": ContainerApplication,
            "ContainerService": ContainerService,
        }[name]
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include lazy compatibility exports in module introspection."""
    return sorted(set(globals()) | set(__all__))
