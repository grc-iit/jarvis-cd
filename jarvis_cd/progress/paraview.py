"""Compatibility bridge to the package-local builtin ParaView provider."""

from pathlib import Path
from typing import Any, cast

from .discovery import load_progress_module
from .provider import RelayProgressAdapter, RelayProgressAdapterFactory

_MODULE = load_progress_module(
    Path(__file__).resolve().parents[2]
    / "builtin"
    / "builtin"
    / "paraview"
    / "progress.py"
)

ParaViewProgressAdapter = _MODULE.ParaViewProgressAdapter
ParaViewProgressReporter = _MODULE.ParaViewProgressReporter
_FACTORY = cast(RelayProgressAdapterFactory, _MODULE.adapter_from_package)


def adapter_from_package(package: dict[str, Any]) -> RelayProgressAdapter | None:
    """Create the package-local ParaView provider through a stable entry point."""
    return _FACTORY(package)


__all__ = [
    "ParaViewProgressAdapter",
    "ParaViewProgressReporter",
    "adapter_from_package",
]
