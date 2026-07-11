"""Runtime-contract tests for the builtin JARVIS LAMMPS package."""

from __future__ import annotations

import shlex
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


def _load_lammps_package() -> ModuleType:
    """Load the package implementation without changing JARVIS repo imports."""
    package_path = (
        Path(__file__).resolve().parents[3]
        / "builtin"
        / "builtin"
        / "lammps"
        / "pkg.py"
    )
    spec = spec_from_file_location("test_lammps_runtime_package", package_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load the LAMMPS package from {package_path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lammps_package = _load_lammps_package()


class _CapturedExec:
    """Capture a package launch without executing the application."""

    commands: list[str] = []

    def __init__(self, command: str, exec_info: Any) -> None:
        self.command = command
        self.exec_info = exec_info
        self.exit_code = {"localhost": 0}
        self.commands.append(command)

    def run(self) -> _CapturedExec:
        """Record the launch only."""
        return self


class _FailedCleanupExec(_CapturedExec):
    """Capture a launch whose package-owned log cleanup fails."""

    def __init__(self, command: str, exec_info: Any) -> None:
        super().__init__(command, exec_info)
        if command.startswith("rm -f "):
            self.exit_code = {"node1": 1}


def test_default_launch_owns_deterministic_lammps_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The builtin package must launch LAMMPS with its declared progress log."""
    output_dir = tmp_path / "output with spaces"
    script = tmp_path / "input with spaces.lmp"
    package = object.__new__(lammps_package.Lammps)
    package.config = {
        "kokkos_gpu": False,
        "lmp_bin": "/opt/spack/bin/lmp",
        "nprocs": 2,
        "out": str(output_dir),
        "ppn": 2,
        "script": str(script),
    }
    package.pipeline = SimpleNamespace(get_hostfile=lambda: None)
    package.env = {}
    package.mod_env = {}
    _CapturedExec.commands = []
    monkeypatch.setattr(lammps_package, "Exec", _CapturedExec)

    package.start()

    assert len(_CapturedExec.commands) == 2
    expected_log = output_dir.resolve() / "log.lammps"
    assert _CapturedExec.commands[0] == f"rm -f {shlex.quote(str(expected_log))}"
    command = _CapturedExec.commands[1]
    assert f"-log {shlex.quote(str(expected_log))}" in command
    assert f"-in {shlex.quote(str(script))}" in command


def test_container_launch_creates_log_directory_before_lammps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Container LAMMPS must be able to open its explicit log at startup."""
    output_dir = tmp_path / "container output"
    package = object.__new__(lammps_package.Lammps)
    package.config = {
        "deploy_mode": "container",
        "kokkos_gpu": False,
        "nprocs": 2,
        "out": str(output_dir),
        "ppn": 2,
        "script": None,
    }
    package.pipeline = SimpleNamespace(
        _has_containerized_packages=lambda: True,
        container_engine="docker",
        container_ssh_port=22,
        get_hostfile=lambda: None,
        name="example",
    )
    package.env = {}
    package.mod_env = {}
    package.private_dir = tmp_path
    package.shared_dir = tmp_path
    _CapturedExec.commands = []
    monkeypatch.setattr(lammps_package, "Exec", _CapturedExec)

    package.start()

    assert len(_CapturedExec.commands) == 2
    expected_log = output_dir.resolve() / "log.lammps"
    assert _CapturedExec.commands[0] == f"rm -f {shlex.quote(str(expected_log))}"
    argv = shlex.split(_CapturedExec.commands[1])
    assert argv[:2] == ["bash", "-c"]
    assert argv[2] == (
        f"mkdir -p {shlex.quote(str(output_dir.resolve()))} "
        f"&& exec /usr/local/bin/lmp -log {shlex.quote(str(expected_log))}"
    )


def test_launch_fails_closed_when_stale_log_cannot_be_removed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cleanup failure cannot silently expose stale progress as a new run."""
    package = object.__new__(lammps_package.Lammps)
    package.config = {
        "kokkos_gpu": False,
        "lmp_bin": "/opt/spack/bin/lmp",
        "nprocs": 1,
        "out": str(tmp_path),
        "ppn": 1,
        "script": None,
    }
    package.pipeline = SimpleNamespace(get_hostfile=lambda: None)
    package.env = {}
    package.mod_env = {}
    _FailedCleanupExec.commands = []
    monkeypatch.setattr(lammps_package, "Exec", _FailedCleanupExec)

    with pytest.raises(RuntimeError, match="Failed to remove stale LAMMPS log"):
        package.start()

    assert len(_FailedCleanupExec.commands) == 1
