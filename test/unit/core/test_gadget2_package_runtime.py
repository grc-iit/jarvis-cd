"""Runtime-contract tests for the builtin Gadget2 package."""

from __future__ import annotations

import hashlib
import io
import json
import shlex
import tarfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from jarvis_cd.input_bundle import (
    INPUT_BUNDLE_MANIFEST_NAME,
    INPUT_BUNDLE_SCHEMA_VERSION,
)
from jarvis_cd.shell import LocalExecInfo
from jarvis_cd.util.hostfile import Hostfile


def _load_package() -> ModuleType:
    """Load the builtin package without changing repository imports."""

    path = (
        Path(__file__).resolve().parents[3]
        / "builtin"
        / "builtin"
        / "gadget2"
        / "pkg.py"
    )
    spec = spec_from_file_location("test_gadget2_runtime_package", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load Gadget2 package from {path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gadget2_package = _load_package()


class _CapturedExec:
    """Capture launches and expose a configurable process result."""

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


class _CapturedMkdir:
    """Create the requested test directory."""

    def __init__(self, path: str, exec_info: Any) -> None:
        self.path = path
        self.exec_info = exec_info
        self.exit_code = {"localhost": 0}

    def run(self) -> _CapturedMkdir:
        """Materialize the directory and report success."""

        Path(self.path).mkdir(parents=True, exist_ok=True)
        return self


class _CapturedRm:
    """Capture exact cleanup authority without deleting files."""

    calls: list[tuple[str, bool]] = []

    def __init__(self, path: str, exec_info: Any, *, recursive: bool = False) -> None:
        self.path = path
        self.exec_info = exec_info
        self.recursive = recursive
        self.exit_code = {"localhost": 0}

    def run(self) -> _CapturedRm:
        """Record the requested cleanup."""

        self.calls.append((self.path, self.recursive))
        return self


@pytest.fixture(autouse=True)
def _capture_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep package tests free of process and remote-host side effects."""

    _CapturedExec.commands = []
    _CapturedExec.infos = []
    _CapturedExec.return_code = 0
    _CapturedRm.calls = []
    monkeypatch.setattr(gadget2_package, "Exec", _CapturedExec)
    monkeypatch.setattr(gadget2_package, "Mkdir", _CapturedMkdir)
    monkeypatch.setattr(gadget2_package, "Rm", _CapturedRm)


def _base_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "deploy_mode": "default",
        "input_bundle": "",
        "parameter_path": "",
        "out": "run",
        "nprocs": 2,
        "ppn": 2,
        "exec_mode": "mpi",
        "gadget2_path": None,
        "test_case": "gassphere",
        "output": None,
        "time_max": 0.05,
        "buffer_size": 15.0,
        "part_alloc_factor": 1.5,
        "tree_alloc_factor": 0.9,
    }
    config.update(overrides)
    return config


def _package(tmp_path: Path, config: dict[str, Any]) -> Any:
    package = object.__new__(gadget2_package.Gadget2)
    package.config = config
    package.shared_dir = tmp_path / "shared"
    package.private_dir = tmp_path / "private"
    package.env = {}
    package.mod_env = {"PATH": "/runtime/bin"}
    package.gadget2_bin = "Gadget2"
    package.pipeline = SimpleNamespace(
        get_hostfile=lambda: Hostfile(find_ips=False),
        _has_containerized_packages=lambda: False,
    )
    package.runtime_line_callback = lambda: None
    return package


def _write_bundle(destination: Path) -> Path:
    files = {
        "galaxy/galaxy.param": (
            b"InitCondFile ICs/galaxy.dat\nOutputDir output/\nEnergyFile energy.txt\n"
        ),
        "galaxy/ICs/galaxy.dat": b"gadget2-initial-condition\x00\x01",
    }
    manifest = {
        "schema_version": INPUT_BUNDLE_SCHEMA_VERSION,
        "entrypoint": "galaxy/galaxy.param",
        "files": [
            {
                "path": name,
                "role": (
                    "gadget2_parameter_file"
                    if name.endswith(".param")
                    else "gadget2_initial_condition"
                ),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
            for name, payload in files.items()
        ],
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
    with tarfile.open(destination, mode="w") as archive:
        info = tarfile.TarInfo(INPUT_BUNDLE_MANIFEST_NAME)
        info.size = len(manifest_bytes)
        archive.addfile(info, io.BytesIO(manifest_bytes))
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return destination


def test_agent_contract_exposes_bundle_profile_and_hides_legacy_defaults() -> None:
    """Agents receive a scientific input contract rather than stock demos."""

    package = object.__new__(gadget2_package.Gadget2)
    package.config = _base_config()
    package.env = {}
    package.mod_env = {}

    menu = {item["name"]: item for item in package._configure_menu()}
    contract = package._deployment_contract().to_dict()

    assert menu["input_bundle"]["input_binding"] == {
        "schema_version": "jarvis.configuration-input-binding.v1",
        "kind": "local_file",
        "structure": "regular_file",
    }
    assert menu["parameter_path"]["default"] == ""
    assert menu["out"]["default"] == "run"
    assert all(
        menu[name]["agent_visible"] is False
        for name in {
            "gadget2_path",
            "test_case",
            "output",
            "time_max",
            "buffer_size",
            "part_alloc_factor",
            "tree_alloc_factor",
            "exec_mode",
        }
    )
    assert [profile["name"] for profile in contract["execution_profiles"]] == [
        "input_bundle"
    ]
    assert [item["id"] for item in contract["runtime_requirements"]] == ["gadget2"]


def test_bundle_is_verified_and_staged_before_native_launch(tmp_path: Path) -> None:
    """The solver runs from an owned copy while caller bytes remain immutable."""

    bundle = _write_bundle(tmp_path / "galaxy.tar")
    source_bytes = bundle.read_bytes()
    package = _package(tmp_path, _base_config(input_bundle=str(bundle)))

    package.start()

    workdir = tmp_path / "shared" / "run" / "galaxy"
    staged_parameter = workdir / "galaxy.param"
    staged_ic = workdir / "ICs" / "galaxy.dat"
    assert staged_parameter.read_bytes().startswith(b"InitCondFile")
    assert staged_ic.read_bytes() == b"gadget2-initial-condition\x00\x01"
    assert bundle.read_bytes() == source_bytes
    command = _CapturedExec.commands[-1]
    assert command == "Gadget2 galaxy.param"
    assert _CapturedExec.infos[-1].cwd == str(workdir.resolve())
    assert isinstance(_CapturedExec.infos[-1], gadget2_package.MpiExecInfo)


def test_parameter_override_must_name_a_declared_bundle_file(tmp_path: Path) -> None:
    """Alternate parameter selection cannot escape or guess outside the manifest."""

    bundle = _write_bundle(tmp_path / "galaxy.tar")
    package = _package(
        tmp_path,
        _base_config(input_bundle=str(bundle), parameter_path="../galaxy.param"),
    )

    with pytest.raises(ValueError, match="confined manifest path"):
        package.start()

    package.config["parameter_path"] = "galaxy/missing.param"
    with pytest.raises(ValueError, match="not declared by the input bundle"):
        package.start()

    assert _CapturedExec.commands == []


def test_native_configuration_fails_before_launch(tmp_path: Path) -> None:
    """Missing input and invalid MPI resources are local configuration errors."""

    package = _package(tmp_path, _base_config())
    with pytest.raises(ValueError, match="requires input_bundle"):
        package.start()

    bundle = _write_bundle(tmp_path / "galaxy.tar")
    package.config = _base_config(input_bundle=str(bundle), nprocs=0)
    with pytest.raises(ValueError, match="nprocs must be a positive integer"):
        package.start()

    assert _CapturedExec.commands == []


def test_native_process_failure_fails_the_package_lifecycle(tmp_path: Path) -> None:
    """A failing Gadget2 rank cannot become a successful JARVIS execution."""

    bundle = _write_bundle(tmp_path / "galaxy.tar")
    package = _package(tmp_path, _base_config(input_bundle=str(bundle)))
    _CapturedExec.return_code = 9

    with pytest.raises(RuntimeError, match="Gadget2 execution failed"):
        package.start()


def test_legacy_stock_profile_retains_its_existing_launch_shape(tmp_path: Path) -> None:
    """Existing configured stock pipelines still run their generated parameter."""

    output = tmp_path / "legacy output"
    output.mkdir()
    parameter = output / "gassphere.param"
    parameter.write_text("TimeMax 0.05\n", encoding="utf-8")
    binary = tmp_path / "Gadget2 runtime"
    binary.write_bytes(b"runtime-placeholder")
    package = _package(
        tmp_path,
        _base_config(
            output=str(output),
            paramfile=str(parameter),
            binary=str(binary),
        ),
    )

    package.start()

    inner = " ".join(
        (
            "cd",
            shlex.quote(str(output.resolve())),
            "&&",
            shlex.quote(str(binary)),
            "gassphere.param",
        )
    )
    assert _CapturedExec.commands[-1] == f"bash -c {shlex.quote(inner)}"
    assert _CapturedExec.infos[-1].cwd == str(output.resolve())
    assert isinstance(_CapturedExec.infos[-1], gadget2_package.MpiExecInfo)


def test_native_command_quotes_runtime_and_parameter_names(tmp_path: Path) -> None:
    """Runtime discovery cannot turn a valid path into shell syntax."""

    bundle = _write_bundle(tmp_path / "galaxy.tar")
    package = _package(tmp_path, _base_config(input_bundle=str(bundle)))
    package.gadget2_bin = "/runtime with space/Gadget2"

    package.start()

    assert _CapturedExec.commands[-1] == (
        f"{shlex.quote('/runtime with space/Gadget2')} galaxy.param"
    )


def test_clean_uses_one_exact_recursive_output_without_wildcard(tmp_path: Path) -> None:
    """Cleanup cannot erase prefix-matching sibling directories."""

    package = _package(tmp_path, _base_config(out="results"))

    package.clean()

    assert _CapturedRm.calls == [
        (str((tmp_path / "shared" / "results").resolve()), True)
    ]
    assert "*" not in _CapturedRm.calls[0][0]


def test_default_hostfile_uses_local_output_lifecycle(tmp_path: Path) -> None:
    """Local output preparation does not require SSH."""

    package = _package(tmp_path, _base_config())

    assert isinstance(package._node_exec_info(), LocalExecInfo)
