"""The session's home guard (test/conftest.py) keeps JARVIS off the real home."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from jarvis_cd.core.config import Jarvis

if TYPE_CHECKING:
    from test.conftest import _HomeGuard


def _guard() -> "_HomeGuard":
    for name, module in list(sys.modules.items()):
        guard = getattr(module, "_GUARD", None)
        if guard is not None and name.endswith("conftest"):
            return cast("_HomeGuard", guard)
    pytest.fail("the home guard in test/conftest.py is not loaded")


def test_home_is_the_temporary_home():
    guard = _guard()
    home = Path(os.path.abspath(Path.home())).resolve()
    assert home == guard.temporary_home
    assert home not in guard.homes
    assert os.environ["HOME"] == str(guard.temporary_home)
    assert "JARVIS_ROOT" not in os.environ


def test_default_jarvis_root_is_under_the_temporary_home():
    guard = _guard()
    saved = Jarvis._instance
    Jarvis._instance = None
    try:
        root = Path(Jarvis.get_instance().jarvis_root).resolve()
    finally:
        Jarvis._instance = saved
    assert guard.temporary_home in root.parents
    for real in guard.real_roots:
        assert root != real and real not in root.parents


def test_guard_refuses_a_root_inside_the_real_home(monkeypatch):
    guard = _guard()
    real = guard.real_roots[0]
    monkeypatch.setenv("JARVIS_ROOT", str(real / "config"))
    problem = guard.problem()
    assert problem is not None and "inside the real JARVIS home" in problem
    monkeypatch.delenv("JARVIS_ROOT")
    assert guard.problem() is None


def test_guard_detects_a_change_in_a_real_home(tmp_path, monkeypatch):
    guard = _guard()
    watched = tmp_path / "watched-home"
    watched.mkdir()
    monkeypatch.setitem(guard.snapshot, watched, [])
    assert guard.check_untouched() is not None
    monkeypatch.delitem(guard.snapshot, watched)
    assert guard.check_untouched() is None
