"""Runtime-contract tests for the builtin Gadget2 package."""

from __future__ import annotations

import hashlib
import io
import json
import shlex
import sys
import tarfile
from importlib import import_module
from pathlib import Path, PurePosixPath
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

    repository_root = Path(__file__).resolve().parents[3] / "builtin"
    sys.path.insert(0, str(repository_root))
    try:
        return import_module("builtin.gadget2.pkg")
    finally:
        sys.path.remove(str(repository_root))


gadget2_package = _load_package()


class _CapturedExec:
    """Capture launches and expose a configurable process result."""

    commands: list[str] = []
    infos: list[Any] = []
    return_code = 0
    emit_products = True
    product_bytes = b"scientific-product\n"

    def __init__(self, command: str, exec_info: Any) -> None:
        self.command = command
        self.exec_info = exec_info
        self.exit_code = {"localhost": self.return_code}
        self.commands.append(command)
        self.infos.append(exec_info)

    def run(self) -> _CapturedExec:
        """Materialize parameter-declared products and return the result."""

        if (
            self.return_code == 0
            and self.emit_products
            and not self.command.startswith("bash -c ")
        ):
            cwd = Path(self.exec_info.cwd)
            parameter_name = shlex.split(self.command)[-1]
            parameter = cwd / parameter_name
            contract = gadget2_package.parse_gadget2_output_contract(
                parameter.read_text(encoding="utf-8"),
                parameter_path=PurePosixPath(cwd.name) / parameter_name,
            )
            run_root = cwd.parent
            for relative in (contract.energy_file, contract.info_file):
                product = run_root.joinpath(*relative.parts)
                product.parent.mkdir(parents=True, exist_ok=True)
                product.write_bytes(self.product_bytes)
            snapshot_base = run_root.joinpath(*contract.snapshot_file_base.parts)
            snapshot_base.parent.mkdir(parents=True, exist_ok=True)
            snapshot_base.with_name(f"{snapshot_base.name}_000").write_bytes(
                self.product_bytes
            )
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
    _CapturedExec.emit_products = True
    _CapturedExec.product_bytes = b"scientific-product\n"
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


def _write_bundle(
    destination: Path,
    *,
    parameter: bytes = (
        b"InitCondFile ICs/galaxy.dat\nOutputDir output/\n"
        b"EnergyFile energy.txt\nInfoFile info.txt\nSnapshotFileBase snapshot\n"
    ),
) -> Path:
    files = {
        "galaxy/galaxy.param": parameter,
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
    assert menu["gadget2_path"]["default"] is None
    assert menu["gadget2_path"]["required"] is False
    assert menu["output"]["default"] is None
    assert menu["output"]["required"] is False
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
    assert contract["execution_profiles"][0]["readiness"] == {
        "mechanism": "process_exit",
        "condition": "successful_exit_with_required_products",
    }


def test_native_validation_treats_absent_deploy_mode_as_default() -> None:
    """New pipeline members resolve an omitted deploy mode to native execution."""
    package = object.__new__(gadget2_package.Gadget2)
    package.config = _base_config(input_bundle="galaxy.tar")
    del package.config["deploy_mode"]

    package._validate_native_configuration()


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
    assert (workdir / "output").is_dir()
    assert bundle.read_bytes() == source_bytes
    command = _CapturedExec.commands[-1]
    assert command == "Gadget2 galaxy.param"
    assert _CapturedExec.infos[-1].cwd == str(workdir.resolve())
    assert isinstance(_CapturedExec.infos[-1], gadget2_package.MpiExecInfo)


@pytest.mark.parametrize(
    ("parameter", "message"),
    [
        (
            b"EnergyFile energy.txt\nInfoFile info.txt\nSnapshotFileBase snapshot\n",
            "declare OutputDir exactly once",
        ),
        (
            b"OutputDir output/\nOutputDir other/\nEnergyFile energy.txt\n"
            b"InfoFile info.txt\nSnapshotFileBase snapshot\n",
            "declare OutputDir exactly once",
        ),
        (
            b"OutputDir ../escape/\nEnergyFile energy.txt\nInfoFile info.txt\n"
            b"SnapshotFileBase snapshot\n",
            "confined POSIX path",
        ),
        (
            b"OutputDir /tmp/escape/\nEnergyFile energy.txt\nInfoFile info.txt\n"
            b"SnapshotFileBase snapshot\n",
            "confined POSIX path",
        ),
        (
            b"OutputDir C:/escape/\nEnergyFile energy.txt\nInfoFile info.txt\n"
            b"SnapshotFileBase snapshot\n",
            "confined POSIX path",
        ),
        (
            b"OutputDir output\\escape\nEnergyFile energy.txt\nInfoFile info.txt\n"
            b"SnapshotFileBase snapshot\n",
            "confined POSIX path",
        ),
        (
            b"OutputDir output path\nEnergyFile energy.txt\nInfoFile info.txt\n"
            b"SnapshotFileBase snapshot\n",
            "one path token",
        ),
    ],
)
def test_native_output_directory_is_single_and_confined(
    tmp_path: Path,
    parameter: bytes,
    message: str,
) -> None:
    """A supplied parameter cannot direct Gadget2 outside staged storage."""

    bundle = _write_bundle(tmp_path / "galaxy.tar", parameter=parameter)
    package = _package(tmp_path, _base_config(input_bundle=str(bundle)))

    with pytest.raises(ValueError, match=message):
        package.start()

    assert _CapturedExec.commands == []


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


def test_zero_exit_without_scientific_products_fails_lifecycle(tmp_path: Path) -> None:
    """Gadget2's zero-status fatal paths cannot become successful executions."""

    bundle = _write_bundle(tmp_path / "galaxy.tar")
    package = _package(tmp_path, _base_config(input_bundle=str(bundle)))
    _CapturedExec.emit_products = False

    with pytest.raises(RuntimeError, match="without required non-empty products"):
        package.start()


def test_zero_byte_scientific_products_fail_lifecycle(tmp_path: Path) -> None:
    """Placeholder files do not satisfy the native completion contract."""

    bundle = _write_bundle(tmp_path / "galaxy.tar")
    package = _package(tmp_path, _base_config(input_bundle=str(bundle)))
    _CapturedExec.product_bytes = b""

    with pytest.raises(RuntimeError, match="EnergyFile, InfoFile, SnapshotFileBase"):
        package.start()


def test_completion_uses_parameter_declared_product_names(tmp_path: Path) -> None:
    """The native lifecycle does not guess stock Gadget2 filenames."""

    parameter = (
        b"InitCondFile ICs/galaxy.dat\nOutputDir products/\n"
        b"EnergyFile conserved.dat\nInfoFile progress.log\n"
        b"SnapshotFileBase states/galaxy\n"
    )
    bundle = _write_bundle(tmp_path / "galaxy.tar", parameter=parameter)
    package = _package(tmp_path, _base_config(input_bundle=str(bundle)))

    package.start()

    root = tmp_path / "shared" / "run" / "galaxy" / "products"
    assert (root / "conserved.dat").stat().st_size > 0
    assert (root / "progress.log").stat().st_size > 0
    assert (root / "states" / "galaxy_000").stat().st_size > 0


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
