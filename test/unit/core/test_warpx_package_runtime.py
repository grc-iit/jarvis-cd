"""Runtime-contract tests for the builtin JARVIS WarpX package."""

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


def _load_warpx_package() -> ModuleType:
    """Load the builtin package without changing repository imports."""

    path = (
        Path(__file__).resolve().parents[3] / "builtin" / "builtin" / "warpx" / "pkg.py"
    )
    spec = spec_from_file_location("test_warpx_runtime_package", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load WarpX package from {path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


warpx_package = _load_warpx_package()


class _CapturedExec:
    """Capture one launch and expose a configurable process result."""

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
    """Create a local test output directory and report success."""

    def __init__(self, path: str, exec_info: Any) -> None:
        self.path = path
        self.exec_info = exec_info
        self.exit_code = {"localhost": 0}

    def run(self) -> _CapturedMkdir:
        """Materialize the test directory."""

        Path(self.path).mkdir(parents=True, exist_ok=True)
        return self


class _CapturedRm:
    """Capture exact cleanup authority."""

    calls: list[tuple[str, bool]] = []

    def __init__(self, path: str, exec_info: Any, *, recursive: bool = False) -> None:
        self.path = path
        self.exec_info = exec_info
        self.recursive = recursive
        self.exit_code = {"localhost": 0}

    def run(self) -> _CapturedRm:
        """Record the cleanup without deleting test files."""

        self.calls.append((self.path, self.recursive))
        return self


@pytest.fixture(autouse=True)
def _capture_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep all unit tests free of process and remote-host side effects."""

    _CapturedExec.commands = []
    _CapturedExec.infos = []
    _CapturedExec.return_code = 0
    _CapturedRm.calls = []
    monkeypatch.setattr(warpx_package, "Exec", _CapturedExec)
    monkeypatch.setattr(warpx_package, "Mkdir", _CapturedMkdir)
    monkeypatch.setattr(warpx_package, "Rm", _CapturedRm)


def _package(tmp_path: Path, config: dict[str, Any]) -> Any:
    package = object.__new__(warpx_package.Warpx)
    package.config = config
    package.shared_dir = tmp_path / "shared"
    package.private_dir = tmp_path / "private"
    package.env = {}
    package.mod_env = {"PATH": "/runtime/bin"}
    package.warpx_bin = "warpx.3d.MPI.NOACC.DP.PDP"
    package.pipeline = SimpleNamespace(get_hostfile=lambda: Hostfile(find_ips=False))
    package.runtime_line_callback = lambda: None
    return package


def _write_bundle(destination: Path) -> Path:
    files = {
        "density-low/inputs": b"max_step = 40\n",
        "density-high/inputs": b"max_step = 40\nmy_constants.n0 = 4.e24\n",
    }
    manifest = {
        "schema_version": INPUT_BUNDLE_SCHEMA_VERSION,
        "entrypoint": "density-low/inputs",
        "files": [
            {
                "path": name,
                "role": "warpx_input",
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


def _base_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "deploy_mode": "default",
        "example": "custom",
        "input_bundle": "",
        "input_path": "",
        "inputs": "",
        "max_step": 50,
        "n_cell": "64 64 128",
        "nprocs": 4,
        "out": "run",
        "override_input_parameters": False,
        "plot_int": 10,
        "ppn": 4,
        "use_gpu": False,
    }
    config.update(overrides)
    return config


def test_agent_contract_exposes_owned_single_and_bundle_inputs() -> None:
    """Agents see staged input profiles without benchmark-specific settings."""

    package = object.__new__(warpx_package.Warpx)
    package.config = _base_config(example="laser_acceleration")
    package.env = {}
    package.mod_env = {}
    menu = {item["name"]: item for item in package._configure_menu()}

    assert menu["out"]["default"] == "run"
    assert menu["inputs"]["input_binding"]["kind"] == "local_file"
    assert menu["input_bundle"]["input_binding"]["kind"] == "local_file"
    assert menu["override_input_parameters"]["default"] is False
    contract = package._deployment_contract().to_dict()
    assert {profile["name"] for profile in contract["execution_profiles"]} == {
        "input_bundle",
        "input_file",
        "installed_example",
    }


def test_bundle_selects_one_manifest_input_and_preserves_scientific_values(
    tmp_path: Path,
) -> None:
    """Two package instances can select different files from one exact bundle."""

    bundle = _write_bundle(tmp_path / "warpx.tar")
    package = _package(
        tmp_path,
        _base_config(input_bundle=str(bundle), input_path="density-high/inputs"),
    )

    package.start()

    staged = tmp_path / "shared" / "run" / "density-high" / "inputs"
    assert staged.read_bytes().endswith(b"my_constants.n0 = 4.e24\n")
    command = _CapturedExec.commands[-1]
    assert shlex.quote(str(staged.resolve())) in command
    assert "max_step=" not in command
    assert "amr.n_cell=" not in command
    assert _CapturedExec.infos[-1].cwd == str(staged.parent.resolve())


def test_single_input_is_copied_to_owned_output_before_launch(tmp_path: Path) -> None:
    """An input binding never makes WarpX write beside the caller's source."""

    source = tmp_path / "caller" / "inputs"
    source.parent.mkdir()
    source.write_text("max_step = 7\n", encoding="utf-8")
    package = _package(tmp_path, _base_config(inputs=str(source)))

    package.start()

    staged = tmp_path / "shared" / "run" / "inputs"
    assert staged.read_bytes() == source.read_bytes()
    assert shlex.quote(str(staged.resolve())) in _CapturedExec.commands[-1]
    assert _CapturedExec.infos[-1].cwd == str(staged.parent.resolve())


def test_explicit_override_is_required_to_change_caller_input(tmp_path: Path) -> None:
    """Package defaults do not silently replace supplied scientific settings."""

    source = tmp_path / "inputs"
    source.write_text("max_step = 7\n", encoding="utf-8")
    package = _package(
        tmp_path,
        _base_config(
            inputs=str(source),
            max_step=12,
            n_cell="8 8 16",
            override_input_parameters=True,
            plot_int=-1,
        ),
    )

    package.start()

    command = _CapturedExec.commands[-1]
    assert "max_step=12" in command
    assert "amr.n_cell=8 8 16" in command
    assert "amr.plot_int=-1" in command


def test_ambiguous_or_undeclared_inputs_fail_before_launch(tmp_path: Path) -> None:
    """Input selection is explicit, closed, and free of path traversal."""

    bundle = _write_bundle(tmp_path / "warpx.tar")
    source = tmp_path / "inputs"
    source.write_text("max_step = 1\n", encoding="utf-8")
    package = _package(
        tmp_path,
        _base_config(inputs=str(source), input_bundle=str(bundle)),
    )
    with pytest.raises(ValueError, match="cannot be combined"):
        package.start()

    package.config = _base_config(
        input_bundle=str(bundle), input_path="../density-high/inputs"
    )
    with pytest.raises(ValueError, match="confined manifest path"):
        package.start()

    assert _CapturedExec.commands == []


def test_process_failure_is_not_silently_accepted(tmp_path: Path) -> None:
    """Any failing WarpX rank fails the package lifecycle."""

    source = tmp_path / "inputs"
    source.write_text("max_step = 1\n", encoding="utf-8")
    package = _package(tmp_path, _base_config(inputs=str(source)))
    _CapturedExec.return_code = 9

    with pytest.raises(RuntimeError, match="WarpX execution failed"):
        package.start()


def test_clean_uses_one_exact_recursive_output_without_wildcard(tmp_path: Path) -> None:
    """Cleanup cannot erase prefix-matching sibling directories."""

    package = _package(tmp_path, _base_config(out="results"))

    package.clean()

    assert _CapturedRm.calls == [
        (str((tmp_path / "shared" / "results").resolve()), True)
    ]
    assert "*" not in _CapturedRm.calls[0][0]


def test_default_hostfile_uses_local_output_lifecycle(tmp_path: Path) -> None:
    """Local execution does not require SSH for output directory management."""

    package = _package(tmp_path, _base_config(example="laser_acceleration"))

    assert isinstance(package._node_exec_info(), LocalExecInfo)
