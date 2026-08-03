"""Runtime-contract tests for the builtin JARVIS Xcompact3D package."""

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
        / "xcompact3d"
        / "pkg.py"
    )
    spec = spec_from_file_location("test_xcompact3d_runtime_package", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load Xcompact3D package from {path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


xcompact3d_package = _load_package()


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
        """Record cleanup without deleting test files."""

        self.calls.append((self.path, self.recursive))
        return self


@pytest.fixture(autouse=True)
def _capture_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep package tests free of process and remote-host side effects."""

    _CapturedExec.commands = []
    _CapturedExec.infos = []
    _CapturedExec.return_code = 0
    _CapturedRm.calls = []
    monkeypatch.setattr(xcompact3d_package, "Exec", _CapturedExec)
    monkeypatch.setattr(xcompact3d_package, "Mkdir", _CapturedMkdir)
    monkeypatch.setattr(xcompact3d_package, "Rm", _CapturedRm)


def _package(tmp_path: Path, config: dict[str, Any]) -> Any:
    package = object.__new__(xcompact3d_package.Xcompact3d)
    package.config = config
    package.shared_dir = tmp_path / "shared"
    package.private_dir = tmp_path / "private"
    package.env = {}
    package.mod_env = {"PATH": "/runtime/bin"}
    package.xcompact3d_bin = "xcompact3d"
    package.pipeline = SimpleNamespace(get_hostfile=lambda: Hostfile(find_ips=False))
    package.runtime_line_callback = lambda: None
    return package


def _write_bundle(destination: Path) -> Path:
    files = {
        "channel/input_test_x.i3d": b"&BasicParam\n idir_stream=1\n/End\n",
        "channel/input_test_z.i3d": b"&BasicParam\n idir_stream=3\n/End\n",
        "channel/adios2_config.xml": b"<adios-config/>\n",
    }
    manifest = {
        "schema_version": INPUT_BUNDLE_SCHEMA_VERSION,
        "entrypoint": "channel/input_test_x.i3d",
        "files": [
            {
                "path": name,
                "role": "xcompact3d_input" if name.endswith(".i3d") else "support",
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
        "input_bundle": "",
        "input_path": "",
        "inputs": "",
        "nprocs": 4,
        "out": "run",
        "ppn": 4,
    }
    config.update(overrides)
    return config


def test_agent_contract_exposes_generic_single_and_bundle_inputs() -> None:
    """Agents see ordinary Xcompact3D inputs, not benchmark-specific axes."""

    package = object.__new__(xcompact3d_package.Xcompact3d)
    package.config = _base_config()
    package.env = {}
    package.mod_env = {}
    menu = {item["name"]: item for item in package._configure_menu()}

    assert menu["out"]["default"] == "run"
    assert menu["inputs"]["input_binding"]["kind"] == "local_file"
    assert menu["input_bundle"]["input_binding"]["kind"] == "local_file"
    assert "axis" not in menu
    contract = package._deployment_contract().to_dict()
    assert {profile["name"] for profile in contract["execution_profiles"]} == {
        "input_bundle",
        "input_file",
    }


def test_bundle_selects_one_manifest_input_without_modifying_it(tmp_path: Path) -> None:
    """Independent package instances can select distinct files from one bundle."""

    bundle = _write_bundle(tmp_path / "channel.tar")
    package = _package(
        tmp_path,
        _base_config(input_bundle=str(bundle), input_path="channel/input_test_z.i3d"),
    )

    package.start()

    staged = tmp_path / "shared" / "run" / "channel" / "input_test_z.i3d"
    launch_input = staged.parent / "jarvis-input.i3d"
    assert staged.read_bytes().endswith(b"idir_stream=3\n/End\n")
    assert launch_input.read_bytes() == staged.read_bytes()
    command = _CapturedExec.commands[-1]
    assert command.startswith("xcompact3d jarvis-input.i3d ")
    assert shlex.quote(str(staged.resolve())) not in command
    assert str((staged.parent / "xcompact3d.log").resolve()) in command
    assert _CapturedExec.infos[-1].cwd == str(staged.parent.resolve())


def test_single_input_is_copied_to_owned_output_before_launch(tmp_path: Path) -> None:
    """A caller input is never executed in its source directory."""

    source = tmp_path / "caller" / "input.i3d"
    source.parent.mkdir()
    source.write_text("&BasicParam\n idir_stream=1\n/End\n", encoding="utf-8")
    package = _package(tmp_path, _base_config(inputs=str(source)))

    package.start()

    staged = tmp_path / "shared" / "run" / "input.i3d"
    launch_input = staged.parent / "jarvis-input.i3d"
    assert staged.read_bytes() == source.read_bytes()
    assert launch_input.read_bytes() == staged.read_bytes()
    assert _CapturedExec.commands[-1].startswith("xcompact3d jarvis-input.i3d ")
    assert shlex.quote(str(staged.resolve())) not in _CapturedExec.commands[-1]
    assert _CapturedExec.infos[-1].cwd == str(staged.parent.resolve())


def test_reserved_runtime_input_name_cannot_replace_a_bundle_member(
    tmp_path: Path,
) -> None:
    """A bundle cannot make the short runtime alias overwrite another input."""

    bundle = _write_bundle(tmp_path / "channel.tar")
    package = _package(
        tmp_path,
        _base_config(input_bundle=str(bundle), input_path="channel/input_test_z.i3d"),
    )
    collision = tmp_path / "shared" / "run" / "channel" / "jarvis-input.i3d"
    collision.parent.mkdir(parents=True)
    collision.write_text("unrelated\n", encoding="utf-8")

    with pytest.raises(ValueError, match="runtime input alias already exists"):
        package.start()

    assert collision.read_text(encoding="utf-8") == "unrelated\n"
    assert _CapturedExec.commands == []


def test_ambiguous_or_undeclared_inputs_fail_before_launch(tmp_path: Path) -> None:
    """Input selection is explicit and rejects traversal or ambiguity."""

    bundle = _write_bundle(tmp_path / "channel.tar")
    source = tmp_path / "input.i3d"
    source.write_text("&BasicParam\n/End\n", encoding="utf-8")
    package = _package(
        tmp_path,
        _base_config(inputs=str(source), input_bundle=str(bundle)),
    )
    with pytest.raises(ValueError, match="cannot be combined"):
        package.start()

    package.config = _base_config(
        input_bundle=str(bundle), input_path="../channel/input_test_z.i3d"
    )
    with pytest.raises(ValueError, match="confined manifest path"):
        package.start()

    package.config = _base_config()
    with pytest.raises(ValueError, match="requires inputs or input_bundle"):
        package.start()

    assert _CapturedExec.commands == []


def test_process_failure_is_not_silently_accepted(tmp_path: Path) -> None:
    """Any failing Xcompact3D rank fails the package lifecycle."""

    source = tmp_path / "input.i3d"
    source.write_text("&BasicParam\n/End\n", encoding="utf-8")
    package = _package(tmp_path, _base_config(inputs=str(source)))
    _CapturedExec.return_code = 9

    with pytest.raises(RuntimeError, match="Xcompact3D execution failed"):
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
    """Local output preparation does not require SSH."""

    package = _package(tmp_path, _base_config())

    assert isinstance(package._node_exec_info(), LocalExecInfo)
