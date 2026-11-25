"""Keep every test off the person's real JARVIS home.

Tests run ``jarvis init``, ``jarvis mod create`` and the like, which write to
``~/.ppi-jarvis`` (or ``$JARVIS_ROOT``) and ``~/.ppi-jarvis-mods``. Before any
test is collected, the session points ``HOME`` (and ``USERPROFILE`` on
Windows) at a temporary directory and clears the person's ``JARVIS_*`` and
``XDG_*`` base-directory settings, so every default JARVIS path lands there.

The guard refuses to run when that redirection doesn't hold, stops the session
before a test that would point JARVIS at the real home, and stops it as soon
as anything in the real JARVIS homes changed.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

_HOME_VARIABLES = ("HOME", "USERPROFILE")
_CLEARED_PREFIXES = ("JARVIS_",)
_CLEARED_VARIABLES = (
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
)
_STATE_NAMES = (".ppi-jarvis", ".ppi-jarvis-mods")
_MAX_ENTRIES = 20000

Fingerprint = Optional[List[Tuple[str, bool, int, int]]]


def _resolved(path: Path) -> Path:
    try:
        return Path(os.path.abspath(path.expanduser())).resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(path))


def _account_homes() -> List[Path]:
    """Every directory the person's home could be, whatever HOME says."""
    candidates: List[Path] = []
    if sys.platform != "win32":
        import pwd

        try:
            candidates.append(Path(pwd.getpwuid(os.getuid()).pw_dir))
        except KeyError:
            pass
    for variable in _HOME_VARIABLES:
        value = os.environ.get(variable, "").strip()
        if value:
            candidates.append(Path(value))
    try:
        candidates.append(Path.home())
    except RuntimeError:
        pass
    homes: List[Path] = []
    for candidate in candidates:
        resolved = _resolved(candidate)
        if resolved not in homes:
            homes.append(resolved)
    return homes


def _inside(path: Path, roots: List[Path]) -> Optional[Path]:
    resolved = _resolved(path)
    for root in roots:
        if resolved == root or root in resolved.parents:
            return root
    return None


def _fingerprint(root: Path) -> Fingerprint:
    """Names, sizes and mtimes two levels deep; None when the root is absent."""
    if not os.path.lexists(root):
        return None
    entries: List[Tuple[str, bool, int, int]] = []
    stat = os.lstat(root)
    entries.append((".", root.is_dir(), stat.st_size, stat.st_mtime_ns))
    pending = [(root, 0)]
    while pending and len(entries) < _MAX_ENTRIES:
        directory, depth = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda entry: entry.name)
        except OSError:
            continue
        for child in children:
            try:
                child_stat = child.stat(follow_symlinks=False)
                is_dir = child.is_dir(follow_symlinks=False)
            except OSError:
                continue
            relative = os.path.relpath(child.path, root)
            entries.append(
                (relative, is_dir, child_stat.st_size, child_stat.st_mtime_ns)
            )
            if is_dir and depth < 1:
                pending.append((Path(child.path), depth + 1))
    return entries


class _HomeGuard:
    def __init__(self) -> None:
        self.homes: List[Path] = []
        self.real_roots: List[Path] = []
        self.snapshot: Dict[Path, Fingerprint] = {}
        self.saved: Dict[str, Optional[str]] = {}
        self.temporary_home: Optional[Path] = None
        self.changed: List[str] = []

    def start(self) -> None:
        self.homes = _account_homes()
        roots = [home / name for home in self.homes for name in _STATE_NAMES]
        configured = os.environ.get("JARVIS_ROOT", "").strip()
        if configured:
            roots.append(Path(configured))
        for root in roots:
            resolved = _resolved(root)
            if resolved not in self.real_roots:
                self.real_roots.append(resolved)
        self.snapshot = {root: _fingerprint(root) for root in self.real_roots}

        self.temporary_home = _resolved(
            Path(tempfile.mkdtemp(prefix="jarvis-test-home-"))
        )
        if _inside(self.temporary_home, self.real_roots):
            pytest.exit(
                f"refusing to run: the temporary test home {self.temporary_home} "
                "is inside a real JARVIS home",
                returncode=pytest.ExitCode.USAGE_ERROR,
            )
        names = [
            name
            for name in os.environ
            if name.startswith(_CLEARED_PREFIXES) or name in _CLEARED_VARIABLES
        ]
        for name in (*_HOME_VARIABLES, *names):
            self.saved.setdefault(name, os.environ.get(name))
        for name in names:
            os.environ.pop(name, None)
        for name in _HOME_VARIABLES:
            os.environ[name] = str(self.temporary_home)
        self._reset_jarvis()
        problem = self.problem(strict=True)
        if problem:
            pytest.exit(
                f"refusing to run: {problem}", returncode=pytest.ExitCode.USAGE_ERROR
            )

    @staticmethod
    def _reset_jarvis() -> None:
        module = sys.modules.get("jarvis_cd.core.config")
        if module is not None:
            module.Jarvis._instance = None

    def targets(self) -> List[Tuple[str, Path]]:
        """The JARVIS paths a test would write to right now."""
        home = Path.home()
        configured = os.environ.get("JARVIS_ROOT", "").strip()
        found = [
            ("JARVIS root", Path(configured) if configured else home / ".ppi-jarvis"),
            ("JARVIS modules", home / ".ppi-jarvis-mods"),
        ]
        module = sys.modules.get("jarvis_cd.core.config")
        instance = getattr(getattr(module, "Jarvis", None), "_instance", None)
        root = getattr(instance, "jarvis_root", None)
        if root is not None:
            found.append(("the Jarvis singleton's root", Path(root)))
        return found

    def problem(self, strict: bool = False) -> Optional[str]:
        """Why a test can't run now: a JARVIS path inside a real home.

        ``strict`` also requires ``Path.home()`` to be the temporary home; a
        test may point it at its own temporary directory.
        """
        home = _resolved(Path.home())
        if strict and home != self.temporary_home:
            return (
                f"Path.home() is {home}, not the temporary home {self.temporary_home}"
            )
        for label, path in self.targets():
            root = _inside(path, self.real_roots)
            if root is not None:
                return f"{label} {path} is inside the real JARVIS home {root}"
        return None

    def check_untouched(self) -> Optional[str]:
        for root, before in self.snapshot.items():
            if _fingerprint(root) != before:
                return (
                    f"the real JARVIS home {root} changed during the tests "
                    "(by a test, or by another JARVIS process using it)"
                )
        return None

    def finish(self) -> None:
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._reset_jarvis()
        if self.temporary_home is not None:
            shutil.rmtree(self.temporary_home, ignore_errors=True)


_GUARD = _HomeGuard()


def pytest_configure(config: pytest.Config) -> None:
    _GUARD.start()


def pytest_unconfigure(config: pytest.Config) -> None:
    _GUARD.finish()


@pytest.fixture(autouse=True)
def _keep_off_real_jarvis_home(request: pytest.FixtureRequest):
    """Stop the session before a test that would write to the real home."""
    problem = _GUARD.problem()
    if problem:
        pytest.exit(
            f"refusing to run {request.node.nodeid}: {problem}",
            returncode=pytest.ExitCode.INTERNAL_ERROR,
        )
    yield
    changed = _GUARD.check_untouched()
    if changed:
        _GUARD.changed.append(f"{request.node.nodeid}: {changed}")
        pytest.exit(
            f"stopping after {request.node.nodeid}: {changed}",
            returncode=pytest.ExitCode.INTERNAL_ERROR,
        )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    changed = _GUARD.check_untouched()
    if changed and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        print(f"\nJARVIS test home guard: {changed}", file=sys.stderr)
