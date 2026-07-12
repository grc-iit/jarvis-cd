"""Tests for the lightweight :mod:`jarvis_cd.core` import boundary."""

from __future__ import annotations

import subprocess
import sys


def test_core_public_exports_remain_compatible() -> None:
    """Legacy public class imports resolve to their canonical definitions."""
    from jarvis_cd.core import (
        Application,
        ContainerApplication,
        ContainerService,
        Service,
    )
    from jarvis_cd.core.pkg import Application as CanonicalApplication
    from jarvis_cd.core.pkg import Service as CanonicalService

    assert Application is CanonicalApplication
    assert Service is CanonicalService
    assert ContainerApplication is CanonicalApplication
    assert ContainerService is CanonicalService


def test_execution_module_invocation_is_warning_free() -> None:
    """The execution CLI module is not imported before runpy executes it."""
    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::RuntimeWarning",
            "-m",
            "jarvis_cd.core.execution",
            "--help",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert "RuntimeWarning" not in result.stderr
