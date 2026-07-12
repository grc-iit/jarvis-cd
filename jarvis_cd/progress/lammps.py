"""Compatibility bridge to the package-local builtin LAMMPS provider.

New integrations should use package-local JARVIS discovery. This module keeps
the historical relay entry point without importing JARVIS repository names.
"""

from pathlib import Path
from typing import Any, cast

from .discovery import load_progress_module
from .provider import RelayProgressAdapter, RelayProgressAdapterFactory

_MODULE = load_progress_module(
    Path(__file__).resolve().parents[2]
    / "builtin"
    / "builtin"
    / "lammps"
    / "progress.py"
)

LammpsThermoProgressAdapter = _MODULE.LammpsThermoProgressAdapter
_FACTORY = cast(RelayProgressAdapterFactory, _MODULE.adapter_from_package)


def adapter_from_package(package: dict[str, Any]) -> RelayProgressAdapter | None:
    """Create the package-local LAMMPS provider through the legacy entry point."""
    return _FACTORY(package)


__all__ = ["LammpsThermoProgressAdapter", "adapter_from_package"]
