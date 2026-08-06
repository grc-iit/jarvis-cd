"""Runtime-contract tests for the builtin JARVIS IOR package."""

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import shlex
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from jarvis_cd.deployment import ProgramProbeResult, RuntimeStatus


def _load_ior_package() -> ModuleType:
    """Load the package implementation without changing repository imports."""
    package_path = (
        Path(__file__).resolve().parents[3] / "builtin" / "builtin" / "ior" / "pkg.py"
    )
    spec = spec_from_file_location("test_ior_runtime_package", package_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load the IOR package from {package_path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ior_package = _load_ior_package()


class _ExecResult:
    """Return configured per-host process results without launching IOR."""

    exit_code: dict[str, int] = {"localhost": 0}
    stderr: dict[str, str] = {}
    stdout: dict[str, str] = {}
    commands: list[object] = []

    def __init__(self, command: object, exec_info: Any) -> None:
        self.command = command
        self.exec_info = exec_info
        self.commands.append(command)

    def run(self) -> _ExecResult:
        """Return this captured result."""
        return self


def _ior_instance() -> Any:
    """Construct the minimal package state needed by contract and launch tests."""
    package = object.__new__(ior_package.Ior)
    package.config = {
        "api": "POSIX",
        "block": "1m",
        "direct": False,
        "fpp": False,
        "log": "",
        "nprocs": 1,
        "out": "ior.bin",
        "ppn": 1,
        "read": False,
        "reps": 1,
        "write": True,
        "xfer": "64k",
    }
    package.env = {}
    package.mod_env = {}
    package.private_dir = None
    package.shared_dir = None
    package.pipeline = SimpleNamespace(
        _has_containerized_packages=lambda: False,
        get_hostfile=lambda: None,
    )
    return package


def test_deployment_contract_declares_spack_runtime_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable IOR binary must advertise its provider-native resolution."""
    package = _ior_instance()
    monkeypatch.setattr(
        ior_package,
        "probe_program",
        lambda *args, **kwargs: ProgramProbeResult(
            RuntimeStatus("unavailable", "software_not_found")
        ),
    )

    contract = package._deployment_contract().to_dict()

    assert contract["package"] == "builtin.ior"
    runtime = contract["runtime_requirements"][0]
    assert runtime["status"]["usable"] is False
    assert runtime["provider_resolutions"] == [
        {"provider": "spack", "query": {"kind": "spec", "value": "ior"}}
    ]
    assert contract["execution_profiles"][0]["runtime_requirements"] == ["ior"]


def test_start_propagates_failed_ior_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JARVIS cannot report success when any IOR process failed to launch."""
    package = _ior_instance()
    package.config["log"] = "results/ior output.log"
    _ExecResult.exit_code = {"compute-01": 127, "compute-02": 127}
    _ExecResult.stdout = {
        "compute-01": "bash: line 1: ior: command not found\n",
        "compute-02": "bash: line 1: ior: command not found\n",
    }
    _ExecResult.stderr = {}
    _ExecResult.commands = []
    monkeypatch.setattr(ior_package, "Exec", _ExecResult)

    with pytest.raises(RuntimeError, match="ior: command not found"):
        package.start()

    command_list = _ExecResult.commands[0]
    assert isinstance(command_list, list)
    logged_command = command_list[1]["cmd"]
    argv = shlex.split(logged_command)
    assert argv[:4] == ["bash", "-o", "pipefail", "-c"]
    assert argv[4].endswith("2>&1 | tee 'results/ior output.log'")


def test_start_accepts_successful_ior_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful per-host process map remains a successful package launch."""
    package = _ior_instance()
    _ExecResult.exit_code = {"compute-01": 0, "compute-02": 0}
    _ExecResult.stdout = {}
    _ExecResult.stderr = {}
    monkeypatch.setattr(ior_package, "Exec", _ExecResult)

    package.start()
