"""Runtime-contract tests for the builtin WfCommons package."""

from __future__ import annotations

import shlex
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


def _load_package() -> ModuleType:
    """Load builtin WfCommons without relying on repository registration."""

    path = (
        Path(__file__).resolve().parents[3]
        / "builtin"
        / "builtin"
        / "wfcommons"
        / "pkg.py"
    )
    spec = spec_from_file_location("test_builtin_wfcommons", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load the WfCommons package from {path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wfcommons_package = _load_package()


class _CapturedExec:
    """Capture exact package commands and expose configurable process results."""

    commands: list[str] = []
    infos: list[Any] = []
    return_code = 0

    def __init__(self, command: str, exec_info: Any) -> None:
        self.command = command
        self.exec_info = exec_info
        self.exit_code = {"localhost": self.return_code}
        self.commands.append(command)
        self.infos.append(exec_info)

    def run(self) -> _CapturedExec:
        """Return the captured result."""

        return self


class _CapturedRm:
    """Capture package-owned cleanup without deleting test files."""

    calls: list[tuple[str, bool]] = []

    def __init__(self, path: str, exec_info: Any, *, recursive: bool = False) -> None:
        self.path = path
        self.exec_info = exec_info
        self.recursive = recursive
        self.exit_code = {"localhost": 0}

    def run(self) -> _CapturedRm:
        """Record the cleanup request."""

        self.calls.append((self.path, self.recursive))
        return self


@pytest.fixture(autouse=True)
def _capture_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep package tests free of external process side effects."""

    _CapturedExec.commands = []
    _CapturedExec.infos = []
    _CapturedExec.return_code = 0
    _CapturedRm.calls = []
    monkeypatch.setattr(wfcommons_package, "Exec", _CapturedExec)
    monkeypatch.setattr(wfcommons_package, "Rm", _CapturedRm)


def _package(tmp_path: Path, **updates: object) -> Any:
    """Build a minimally configured WfCommons package instance."""

    package = object.__new__(wfcommons_package.Wfcommons)
    package.shared_dir = str(tmp_path / "shared")
    package.private_dir = str(tmp_path / "private")
    package.pkg_dir = str(
        Path(__file__).resolve().parents[3] / "builtin" / "builtin" / "wfcommons"
    )
    package.env = {"PATH": "/runtime/bin"}
    package.mod_env = dict(package.env)
    package.config = {
        "clio_prefix": False,
        "cpu_work": 1,
        "data_footprint_mb": 8,
        "deploy_mode": "default",
        "drop_page_cache": False,
        "nprocs": 1,
        "out": "run",
        "percent_cpu": 1.0,
        "ppn": 1,
        "recipe": "epigenomics",
        "runtime_python": "/runtime/bin/python3",
        "seed": 424300,
        "timeout_seconds": 3600,
        "num_tasks": 100,
    }
    package.config.update(updates)
    package.runtime_line_callback = lambda: None
    return package


def test_agent_contract_exposes_one_reproducible_study_cell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agents configure science dimensions while operators own the runtime path."""

    package = _package(Path("."))
    probes: list[tuple[str, dict[str, str]]] = []

    def probe(program: str, **kwargs: Any) -> SimpleNamespace:
        probes.append((program, kwargs["environment"]))
        return SimpleNamespace(
            status=wfcommons_package.RuntimeStatus("ready", "runtime_probe_succeeded")
        )

    monkeypatch.setattr(
        wfcommons_package,
        "probe_program",
        probe,
    )

    menu = {item["name"]: item for item in package._configure_menu()}
    assert menu["data_footprint_mb"]["type"] is int
    assert menu["seed"]["type"] is int
    assert menu["runtime_python"]["agent_visible"] is False
    assert "venv" not in menu
    contract = package._deployment_contract().to_dict()
    assert probes[0][0] == "python3"
    assert probes[0][1]["PATH"].split(wfcommons_package.os.pathsep)[0] == "/runtime/bin"
    assert contract["package"] == "builtin.wfcommons"
    assert contract["execution_profiles"] == [
        {
            "name": "synthetic_workflow_cell",
            "execution_kind": "batch",
            "when": [{"parameter": "recipe", "operator": "is_not_empty"}],
            "runtime_requirements": ["bash", "wfcommons_runtime"],
            "readiness": {
                "mechanism": "process_exit",
                "condition": "successful_exit",
            },
            "description": "Generate and execute one deterministic WfBench workflow cell.",
        }
    ]


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"num_tasks": 0}, "num_tasks"),
        ({"data_footprint_mb": -1}, "data_footprint_mb"),
        ({"cpu_work": 0}, "cpu_work"),
        ({"percent_cpu": 1.1}, "percent_cpu"),
        ({"seed": -1}, "seed"),
        ({"nprocs": 2}, "single-process"),
    ],
)
def test_invalid_study_dimensions_fail_closed(
    tmp_path: Path, updates: dict[str, object], message: str
) -> None:
    """Invalid scientific and execution settings are rejected, never coerced."""

    package = _package(tmp_path, **updates)

    with pytest.raises(ValueError, match=message):
        package._validate_configuration()


def test_start_stages_pinned_schema_and_builds_one_cell_command(tmp_path: Path) -> None:
    """One package instance owns one result directory and one study cell."""

    package = _package(tmp_path)

    package.start()

    output = tmp_path / "shared" / "run"
    schema = output / "wfcommons-schema.json"
    assert schema.read_bytes() == package._schema_source().read_bytes()
    assert package.config["out"] == str(output.resolve())
    command = _CapturedExec.commands[-1]
    assert command.startswith(shlex.quote("/runtime/bin/python3"))
    assert "--recipe epigenomics" in command
    assert "--num-tasks 100" in command
    assert "--data-footprint-mb 8" in command
    assert "--seed 424300" in command
    assert shlex.quote(str(schema.resolve())) in command
    assert _CapturedExec.infos[-1].cwd == str(output.resolve())
    assert _CapturedExec.infos[-1].timeout == 3600


def test_start_propagates_driver_failure(tmp_path: Path) -> None:
    """A failed WfBench cell makes the JARVIS package fail."""

    package = _package(tmp_path)
    _CapturedExec.return_code = 17

    with pytest.raises(RuntimeError, match="WfCommons execution failed.*17"):
        package.start()


def test_clean_targets_only_the_normalized_package_output(tmp_path: Path) -> None:
    """Cleanup authority cannot expand beyond the package-owned output."""

    package = _package(tmp_path)
    package.clean()

    assert _CapturedRm.calls == [(str((tmp_path / "shared" / "run").resolve()), True)]
