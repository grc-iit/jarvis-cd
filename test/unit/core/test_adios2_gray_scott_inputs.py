"""Tests for portable ADIOS2 Gray-Scott source and configuration bundles."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from jarvis_cd.input_bundle import (
    INPUT_BUNDLE_MANIFEST_NAME,
    INPUT_BUNDLE_SCHEMA_VERSION,
    extract_input_bundle,
)


def _package_module(package: str) -> ModuleType:
    path = (
        Path(__file__).resolve().parents[3] / "builtin" / "builtin" / package / "pkg.py"
    )
    spec = spec_from_file_location(f"test_{package}_input_package", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load package: {path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _configuration(*, output: str = "fields.bp") -> bytes:
    return json.dumps(
        {
            "Du": 0.2,
            "Dv": 0.1,
            "F": 0.02,
            "L": 64,
            "adios_config": "adios2.xml",
            "adios_memory_selection": False,
            "adios_span": False,
            "checkpoint": False,
            "checkpoint_freq": 100,
            "checkpoint_output": "checkpoint.bp",
            "dt": 2.0,
            "k": 0.048,
            "mesh_type": "image",
            "noise": 0.0,
            "output": output,
            "plotgap": 10,
            "restart": False,
            "restart_input": "checkpoint.bp",
            "steps": 100,
        },
        sort_keys=True,
    ).encode("utf-8")


def _write_bundle(
    destination: Path,
    *,
    output: str = "fields.bp",
    selected_role: str = "scientific_input",
) -> Path:
    files = {
        "CMakeLists.txt": ("build_spec", b"project(gray_scott)\n"),
        "adios2.xml": ("adios2_configuration", b"<adios-config/>\n"),
        "config/high.json": (selected_role, _configuration(output=output)),
        "config/low.json": ("scientific_input", _configuration()),
        "src/main.cpp": ("upstream_source", b"int main() { return 0; }\n"),
    }
    manifest = {
        "schema_version": INPUT_BUNDLE_SCHEMA_VERSION,
        "entrypoint": "config/low.json",
        "files": [
            {
                "path": name,
                "role": role,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
            for name, (role, payload) in sorted(files.items())
        ],
    }
    manifest_payload = json.dumps(manifest, sort_keys=True).encode("utf-8")
    with tarfile.open(destination, mode="w") as archive:
        info = tarfile.TarInfo(INPUT_BUNDLE_MANIFEST_NAME)
        info.size = len(manifest_payload)
        archive.addfile(info, io.BytesIO(manifest_payload))
        for name, (_role, payload) in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return destination


def test_gray_scott_selects_only_manifest_declared_scientific_configuration(
    tmp_path: Path,
) -> None:
    """A selected regime is a verified bundle member, never a free path."""
    module = _package_module("adios2_gray_scott")
    archive = _write_bundle(tmp_path / "gray-scott.tar")
    bundle = extract_input_bundle(archive, tmp_path / "materialized")

    selected = module.resolve_bundle_configuration(bundle, "config/high.json")

    assert selected.path == bundle.root / "config" / "high.json"
    assert selected.values["F"] == 0.02
    assert selected.values["output"] == "fields.bp"
    assert selected.build_spec == bundle.root / "CMakeLists.txt"
    assert selected.adios_config == bundle.root / "adios2.xml"

    with pytest.raises(ValueError, match="declared scientific_input"):
        module.resolve_bundle_configuration(bundle, "CMakeLists.txt")
    with pytest.raises(ValueError, match="declared scientific_input"):
        module.resolve_bundle_configuration(bundle, "../outside.json")


@pytest.mark.parametrize(
    ("output", "selected_role", "message"),
    [
        ("/tmp/foreign.bp", "scientific_input", "relative bundle path"),
        ("fields.bp", "upstream_source", "declared scientific_input"),
    ],
)
def test_gray_scott_rejects_unsafe_or_mistyped_configuration_bundle(
    tmp_path: Path,
    output: str,
    selected_role: str,
    message: str,
) -> None:
    """Bundle data cannot redirect output or masquerade as scientific input."""
    module = _package_module("adios2_gray_scott")
    archive = _write_bundle(
        tmp_path / "gray-scott.tar",
        output=output,
        selected_role=selected_role,
    )
    bundle = extract_input_bundle(archive, tmp_path / "materialized")

    with pytest.raises(ValueError, match=message):
        module.resolve_bundle_configuration(bundle, "config/high.json")


def test_gray_scott_menu_and_deployment_expose_portable_bundle_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agents see one staged bundle and a confined member selector."""
    module = _package_module("adios2_gray_scott")
    package = object.__new__(module.Adios2GrayScott)
    package.config = {"input_bundle": "/inputs/gray-scott.tar"}
    package.env = {}
    package.mod_env = {}
    monkeypatch.setattr(
        module,
        "probe_program",
        lambda *_args, **_kwargs: SimpleNamespace(
            status=module.RuntimeStatus("ready", "runtime_probe_succeeded")
        ),
    )

    menu = {item["name"]: item for item in package._configure_menu()}
    document = package.describe_deployment()

    assert menu["input_bundle"]["input_binding"] == {
        "schema_version": "jarvis.configuration-input-binding.v1",
        "kind": "local_file",
        "structure": "regular_file",
    }
    assert menu["configuration_path"]["default"] == ""
    assert menu["out_file"]["default"] == ""
    assert document is not None
    assert {profile["name"] for profile in document["execution_profiles"]} == {
        "installed_executable",
        "source_bundle",
    }
    bundle_profile = next(
        profile
        for profile in document["execution_profiles"]
        if profile["name"] == "source_bundle"
    )
    assert bundle_profile["runtime_requirements"] == ["gray_scott_source_build"]


class _FailedExec:
    def __init__(self, _command: str, _exec_info: Any) -> None:
        self.exit_code = {"ares": 9}

    def run(self) -> "_FailedExec":
        return self


class _SuccessfulExec:
    calls: list[tuple[str, Any]] = []

    def __init__(self, command: str, exec_info: Any) -> None:
        self.calls.append((command, exec_info))
        self.exit_code = {"ares": 0}

    def run(self) -> "_SuccessfulExec":
        return self


def test_gray_scott_bundle_materializes_one_execution_owned_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Build and scientific paths derive only from the verified staged tree."""
    module = _package_module("adios2_gray_scott")
    archive = _write_bundle(tmp_path / "gray-scott.tar")
    shared = tmp_path / "shared"
    package = object.__new__(module.Adios2GrayScott)
    package.shared_dir = str(shared)
    package.mod_env = {}
    package.config = {
        "input_bundle": str(archive),
        "configuration_path": "config/high.json",
    }
    _SuccessfulExec.calls = []
    monkeypatch.setattr(module, "Exec", _SuccessfulExec)

    executable, cwd = package._prepare_bundle_run()

    run = shared / "run"
    assert executable == str(run / "build" / "bin" / "gray-scott")
    assert cwd == str(run.resolve())
    assert [call[0] for call in _SuccessfulExec.calls] == [
        (
            f"cmake -S {module.shlex.quote(str(run.resolve()))} "
            f"-B {module.shlex.quote(str(run.resolve() / 'build'))} "
            "-DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=mpicc "
            "-DCMAKE_CXX_COMPILER=mpic++"
        ),
        (
            "cmake --build "
            f"{module.shlex.quote(str(run.resolve() / 'build'))} "
            "--parallel 4 --target gray-scott"
        ),
    ]
    settings = json.loads((run / "config" / "high.json").read_text(encoding="utf-8"))
    assert settings["F"] == 0.02
    assert settings["output"] == str(run.resolve() / "fields.bp")
    assert settings["adios_config"] == str(run.resolve() / "adios2.xml")
    assert package.config["out_file"] == str(run.resolve() / "fields.bp")


def test_pdf_calc_uses_explicit_executable_and_propagates_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PDF Calc has no developer checkout path and cannot hide a failed run."""
    module = _package_module("adios2_pdf_calc")
    package = object.__new__(module.Adios2PdfCalc)
    package.config = {
        "engine": "bp5",
        "executable": "pdf_calc",
        "input_file": str(tmp_path / "fields.bp"),
        "output_file": str(tmp_path / "pdf.bp"),
        "nbins": 128,
        "nprocs": 1,
        "ppn": 1,
        "output_inputdata": "NO",
        "wait_for_producer": False,
    }
    package.adios2_xml_path = str(tmp_path / "adios2.xml")
    package.shared_dir = str(tmp_path)
    package.pipeline = SimpleNamespace(get_hostfile=lambda: object())
    package.mod_env = {}
    package.runtime_line_callback = lambda: None
    monkeypatch.setattr(module.shutil, "copyfile", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "Exec", _FailedExec)

    with pytest.raises(RuntimeError, match="PDF Calc.*ares=9"):
        package.start()

    assert module.__file__ is not None
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "/workspace/external" not in source
