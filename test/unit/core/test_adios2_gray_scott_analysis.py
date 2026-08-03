"""Tests for reusable paired Gray-Scott morphology analysis."""

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


def _module() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[3]
        / "builtin"
        / "builtin"
        / "adios2_gray_scott_analysis"
        / "pkg.py"
    )
    spec = spec_from_file_location("test_adios2_gray_scott_analysis_package", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load package: {path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _configuration(*, feed: float, kill: float, output: str) -> bytes:
    return json.dumps(
        {
            "Du": 0.2,
            "Dv": 0.1,
            "F": feed,
            "L": 64,
            "adios_config": "adios2.xml",
            "adios_memory_selection": False,
            "adios_span": False,
            "checkpoint": False,
            "checkpoint_freq": 2000,
            "checkpoint_output": f"checkpoint-{output}",
            "dt": 2.0,
            "k": kill,
            "mesh_type": "image",
            "noise": 0.0,
            "output": output,
            "plotgap": 100,
            "steps": 1000,
        },
        sort_keys=True,
    ).encode()


def _write_bundle(destination: Path, *, analysis_role: str = "analysis_source") -> Path:
    files = {
        "CMakeLists.txt": (
            "build_spec",
            b"project(gray_scott_analysis)\nadd_executable(gray-scott-analyze analysis.cpp)\n",
        ),
        "analysis.cpp": (analysis_role, b"int main() { return 0; }\n"),
        "config/high.json": (
            "scientific_input",
            _configuration(feed=0.03, kill=0.0545, output="high.bp"),
        ),
        "config/low.json": (
            "scientific_input",
            _configuration(feed=0.02, kill=0.048, output="low.bp"),
        ),
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
    manifest_payload = json.dumps(manifest, sort_keys=True).encode()
    with tarfile.open(destination, mode="w") as archive:
        info = tarfile.TarInfo(INPUT_BUNDLE_MANIFEST_NAME)
        info.size = len(manifest_payload)
        archive.addfile(info, io.BytesIO(manifest_payload))
        for name, (_role, payload) in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return destination


def _metrics(threshold: float) -> dict[str, float | int]:
    return {
        "active_fraction": 0.2,
        "active_threshold": threshold,
        "component_count": 3,
        "interface_density": 0.12,
        "largest_component_fraction_of_active": 0.8,
        "max": 0.7,
        "mean": 0.2,
        "min": 0.0,
        "standard_deviation": 0.15,
        "surface_to_active": 1.2,
    }


def _result(
    low: dict[str, Any], high: dict[str, Any], threshold: float
) -> dict[str, Any]:
    return {
        "cases": {
            "feed_high": {
                "configuration": high,
                "element_count": 64**3,
                "final_simulation_step": 1000,
                "output_steps": 10,
                "shape": [64, 64, 64],
                "u": _metrics(threshold),
                "v": _metrics(threshold),
            },
            "feed_low": {
                "configuration": low,
                "element_count": 64**3,
                "final_simulation_step": 1000,
                "output_steps": 10,
                "shape": [64, 64, 64],
                "u": _metrics(threshold),
                "v": _metrics(threshold),
            },
        },
        "comparison": {
            "max_absolute_v_difference": 0.5,
            "pearson_v_correlation": 0.25,
            "relative_v_l2_difference": 0.8,
            "v_rms_difference": 0.2,
        },
        "schema_version": "jarvis.gray-scott-morphology.v1",
    }


def test_analysis_bundle_resolves_declared_source_and_configuration(
    tmp_path: Path,
) -> None:
    """The build and paired configurations come only from declared bundle roles."""
    module = _module()
    archive = _write_bundle(tmp_path / "gray-scott.tar")
    bundle = extract_input_bundle(archive, tmp_path / "materialized")

    selected = module.resolve_analysis_bundle(
        bundle,
        "config/low.json",
        "config/high.json",
    )

    assert selected.build_spec == bundle.root / "CMakeLists.txt"
    assert selected.analysis_source == bundle.root / "analysis.cpp"
    assert selected.low.values["F"] == 0.02
    assert selected.high.values["F"] == 0.03

    mistyped = _write_bundle(tmp_path / "mistyped.tar", analysis_role="upstream_source")
    invalid = extract_input_bundle(mistyped, tmp_path / "invalid")
    with pytest.raises(ValueError, match="one analysis_source"):
        module.resolve_analysis_bundle(invalid, "config/low.json", "config/high.json")


def test_analysis_result_is_closed_and_bound_to_inputs(tmp_path: Path) -> None:
    """Result validation binds both configuration documents and the threshold."""
    module = _module()
    low = json.loads(_configuration(feed=0.02, kill=0.048, output="low.bp"))
    high = json.loads(_configuration(feed=0.03, kill=0.0545, output="high.bp"))
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(_result(low, high, 0.1)), encoding="utf-8")

    result = module.validate_result_document(
        result_path,
        low,
        high,
        active_threshold=0.1,
    )

    assert result["comparison"]["v_rms_difference"] == 0.2
    changed = _result(low, high, 0.2)
    result_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="threshold"):
        module.validate_result_document(
            result_path,
            low,
            high,
            active_threshold=0.1,
        )


def test_analysis_menu_and_deployment_expose_source_bundle_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agents see paired data, paired configurations, threshold, and result output."""
    module = _module()
    package = object.__new__(module.Adios2GrayScottAnalysis)
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

    assert menu["input_bundle"]["input_binding"]["kind"] == "local_file"
    assert menu["active_threshold"]["default"] == 0.1
    assert menu["low_input"]["default"] is None
    assert menu["high_input"]["default"] is None
    assert document is not None
    assert {profile["name"] for profile in document["execution_profiles"]} == {
        "installed_executable",
        "source_bundle",
    }


def test_analysis_configuration_preserves_agent_supplied_output_value(
    tmp_path: Path,
) -> None:
    """Configuration persistence retains the value verified by the MCP client."""
    module = _module()
    low_input = tmp_path / "low.bp"
    high_input = tmp_path / "high.bp"
    low_input.mkdir()
    high_input.mkdir()
    low_config = tmp_path / "low.json"
    high_config = tmp_path / "high.json"
    low_config.write_bytes(_configuration(feed=0.02, kill=0.048, output="low.bp"))
    high_config.write_bytes(_configuration(feed=0.03, kill=0.0545, output="high.bp"))
    package = object.__new__(module.Adios2GrayScottAnalysis)
    package.shared_dir = str(tmp_path / "shared")
    package.config = {
        "active_threshold": 0.1,
        "executable": "gray-scott-analyze",
        "high_configuration": str(high_config),
        "high_input": str(high_input),
        "input_bundle": "",
        "low_configuration": str(low_config),
        "low_input": str(low_input),
        "nprocs": 1,
        "output_file": "result.json",
        "ppn": 1,
    }

    package._configure(**package.config)

    assert package.config["output_file"] == "result.json"
    assert package._output_path() == (tmp_path / "shared" / "result.json").resolve()


class _SuccessfulExec:
    calls: list[tuple[str, Any]] = []

    def __init__(self, command: str, exec_info: Any) -> None:
        self.calls.append((command, exec_info))
        self.exit_code = {"ares": 0}

    def run(self) -> "_SuccessfulExec":
        return self


class _FailedExec:
    def __init__(self, _command: str, _exec_info: Any) -> None:
        self.exit_code = {"ares": 7}

    def run(self) -> "_FailedExec":
        return self


def test_analysis_bundle_builds_only_the_declared_analysis_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Source builds are confined to one staged tree and one analysis target."""
    module = _module()
    archive = _write_bundle(tmp_path / "gray-scott.tar")
    package = object.__new__(module.Adios2GrayScottAnalysis)
    package.shared_dir = str(tmp_path / "shared")
    package.mod_env = {}
    package.config = {
        "input_bundle": str(archive),
        "low_configuration": "config/low.json",
        "high_configuration": "config/high.json",
    }
    _SuccessfulExec.calls = []
    monkeypatch.setattr(module, "Exec", _SuccessfulExec)

    executable, low, high, cwd = package._prepare_bundle_run()

    run = (tmp_path / "shared" / "run").resolve()
    assert executable == str(run / "build" / "bin" / "gray-scott-analyze")
    assert low == run / "config" / "low.json"
    assert high == run / "config" / "high.json"
    assert cwd == run
    assert _SuccessfulExec.calls[-1][0].endswith(
        "--parallel 4 --target gray-scott-analyze"
    )


def test_analysis_propagates_native_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed analyzer cannot be converted into a successful package execution."""
    module = _module()
    low_input = tmp_path / "low.bp"
    high_input = tmp_path / "high.bp"
    low_input.mkdir()
    high_input.mkdir()
    low_config = tmp_path / "low.json"
    high_config = tmp_path / "high.json"
    low_config.write_bytes(_configuration(feed=0.02, kill=0.048, output="low.bp"))
    high_config.write_bytes(_configuration(feed=0.03, kill=0.0545, output="high.bp"))
    package = object.__new__(module.Adios2GrayScottAnalysis)
    package.config = {
        "active_threshold": 0.1,
        "executable": "gray-scott-analyze",
        "high_configuration": str(high_config),
        "high_input": str(high_input),
        "input_bundle": "",
        "low_configuration": str(low_config),
        "low_input": str(low_input),
        "nprocs": 1,
        "output_file": str(tmp_path / "result.json"),
        "ppn": 1,
    }
    package.mod_env = {}
    package.pipeline = SimpleNamespace(get_hostfile=lambda: None)
    package.runtime_line_callback = lambda: None
    monkeypatch.setattr(module, "Exec", _FailedExec)

    with pytest.raises(RuntimeError, match="Gray-Scott analysis.*ares=7"):
        package.start()
