"""Run a paired morphology analysis over ADIOS2 Gray-Scott outputs."""

from __future__ import annotations

import json
import math
import os
import shlex
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, NamedTuple, cast

from jarvis_cd.core.pkg import Application
from jarvis_cd.deployment import (
    ConfigurationCondition,
    ConfigurationInputBinding,
    ConfigurationRule,
    ExecutionProfile,
    PackageDeploymentContract,
    ProviderResolution,
    ReadinessContract,
    RuntimeRequirement,
    RuntimeStatus,
    probe_program,
)
from jarvis_cd.input_bundle import (
    MaterializedInputBundle,
    extract_input_bundle,
    stage_input_bundle,
)
from jarvis_cd.shell import Exec, LocalExecInfo, MpiExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Rm

_RESULT_SCHEMA = "jarvis.gray-scott-morphology.v1"
_BUILD_SPEC_ROLE = "build_spec"
_ANALYSIS_SOURCE_ROLE = "analysis_source"
_SCIENTIFIC_INPUT_ROLE = "scientific_input"
_CONFIGURATION_REQUIRED_FIELDS = {
    "Du",
    "Dv",
    "F",
    "L",
    "adios_config",
    "adios_memory_selection",
    "adios_span",
    "checkpoint",
    "checkpoint_freq",
    "checkpoint_output",
    "dt",
    "k",
    "mesh_type",
    "noise",
    "output",
    "plotgap",
    "steps",
}
_CONFIGURATION_OPTIONAL_FIELDS = {"restart", "restart_input"}
_CASE_FIELDS = {
    "configuration",
    "element_count",
    "final_simulation_step",
    "output_steps",
    "shape",
    "u",
    "v",
}
_METRIC_FIELDS = {
    "active_fraction",
    "active_threshold",
    "component_count",
    "interface_density",
    "largest_component_fraction_of_active",
    "max",
    "mean",
    "min",
    "standard_deviation",
    "surface_to_active",
}
_COMPARISON_FIELDS = {
    "max_absolute_v_difference",
    "pearson_v_correlation",
    "relative_v_l2_difference",
    "v_rms_difference",
}


class AnalysisConfiguration(NamedTuple):
    """One manifest-bound Gray-Scott configuration."""

    path: Path
    values: Mapping[str, object]


class GrayScottAnalysisBundle(NamedTuple):
    """Build inputs and paired scientific configurations from one bundle."""

    build_spec: Path
    analysis_source: Path
    low: AnalysisConfiguration
    high: AnalysisConfiguration


def _safe_bundle_member(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label} must name a declared scientific_input member")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must name a declared scientific_input member")
    return path.as_posix()


def _finite_number(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite")
    return parsed


def _positive_integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _validate_configuration_document(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Gray-Scott configuration must be a JSON object")
    fields = set(value)
    missing = _CONFIGURATION_REQUIRED_FIELDS - fields
    unknown = fields - _CONFIGURATION_REQUIRED_FIELDS - _CONFIGURATION_OPTIONAL_FIELDS
    if missing or unknown:
        raise ValueError(
            "Gray-Scott configuration fields are invalid: "
            f"missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    for name in ("Du", "Dv", "dt"):
        if _finite_number(value[name], label=f"Gray-Scott {name}") <= 0:
            raise ValueError(f"Gray-Scott {name} must be positive")
    for name in ("F", "k", "noise"):
        if _finite_number(value[name], label=f"Gray-Scott {name}") < 0:
            raise ValueError(f"Gray-Scott {name} must be non-negative")
    for name in ("L", "steps", "plotgap", "checkpoint_freq"):
        _positive_integer(value[name], label=f"Gray-Scott {name}")
    if cast(int, value["plotgap"]) > cast(int, value["steps"]):
        raise ValueError("Gray-Scott plotgap cannot exceed steps")
    for name in ("checkpoint", "adios_span", "adios_memory_selection"):
        if not isinstance(value[name], bool):
            raise ValueError(f"Gray-Scott {name} must be boolean")
    if "restart" in value and not isinstance(value["restart"], bool):
        raise ValueError("Gray-Scott restart must be boolean")
    for name in ("adios_config", "checkpoint_output", "mesh_type", "output"):
        if not isinstance(value[name], str) or not value[name]:
            raise ValueError(f"Gray-Scott {name} must be a non-empty string")
    return value


def _load_configuration(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Gray-Scott configuration is not valid UTF-8 JSON") from exc
    return _validate_configuration_document(value)


def resolve_analysis_bundle(
    bundle: MaterializedInputBundle,
    low_configuration: str,
    high_configuration: str,
) -> GrayScottAnalysisBundle:
    """Resolve the sole analyzer build and two declared configurations."""
    roles = {item.path: item.role for item in bundle.manifest.files}
    build_specs = [path for path, role in roles.items() if role == _BUILD_SPEC_ROLE]
    analysis_sources = [
        path for path, role in roles.items() if role == _ANALYSIS_SOURCE_ROLE
    ]
    if len(build_specs) != 1:
        raise ValueError("Gray-Scott analysis bundle requires one build_spec")
    if len(analysis_sources) != 1:
        raise ValueError("Gray-Scott analysis bundle requires one analysis_source")
    low_member = _safe_bundle_member(low_configuration, label="low_configuration")
    high_member = _safe_bundle_member(high_configuration, label="high_configuration")
    if low_member == high_member:
        raise ValueError("paired Gray-Scott configurations must be distinct")
    for label, member in (
        ("low_configuration", low_member),
        ("high_configuration", high_member),
    ):
        if roles.get(member) != _SCIENTIFIC_INPUT_ROLE:
            raise ValueError(f"{label} must name a declared scientific_input member")
    low_path = bundle.root / PurePosixPath(low_member)
    high_path = bundle.root / PurePosixPath(high_member)
    return GrayScottAnalysisBundle(
        build_spec=bundle.root / PurePosixPath(build_specs[0]),
        analysis_source=bundle.root / PurePosixPath(analysis_sources[0]),
        low=AnalysisConfiguration(low_path, _load_configuration(low_path)),
        high=AnalysisConfiguration(high_path, _load_configuration(high_path)),
    )


def _validate_metrics(value: object, *, threshold: float, label: str) -> None:
    if not isinstance(value, dict) or set(value) != _METRIC_FIELDS:
        raise ValueError(f"{label} metric fields are invalid")
    for name in _METRIC_FIELDS - {"component_count"}:
        _finite_number(value[name], label=f"{label}.{name}")
    if value["active_threshold"] != threshold:
        raise ValueError(f"{label} active threshold differs from the request")
    if (
        isinstance(value["component_count"], bool)
        or not isinstance(value["component_count"], int)
        or value["component_count"] < 0
    ):
        raise ValueError(f"{label}.component_count must be a non-negative integer")


def _validate_case(
    value: object,
    expected_configuration: Mapping[str, object],
    *,
    threshold: float,
    label: str,
) -> None:
    if not isinstance(value, dict) or set(value) != _CASE_FIELDS:
        raise ValueError(f"{label} result fields are invalid")
    if value["configuration"] != expected_configuration:
        raise ValueError(f"{label} configuration differs from the bound input")
    _positive_integer(value["element_count"], label=f"{label}.element_count")
    if (
        isinstance(value["final_simulation_step"], bool)
        or not isinstance(value["final_simulation_step"], int)
        or value["final_simulation_step"] < 0
    ):
        raise ValueError(f"{label}.final_simulation_step must be non-negative")
    _positive_integer(value["output_steps"], label=f"{label}.output_steps")
    shape = value["shape"]
    if not isinstance(shape, list) or len(shape) != 3:
        raise ValueError(f"{label}.shape must have three dimensions")
    for index, extent in enumerate(shape):
        _positive_integer(extent, label=f"{label}.shape[{index}]")
    if math.prod(cast(list[int], shape)) != value["element_count"]:
        raise ValueError(f"{label}.shape does not match element_count")
    _validate_metrics(value["u"], threshold=threshold, label=f"{label}.u")
    _validate_metrics(value["v"], threshold=threshold, label=f"{label}.v")


def validate_result_document(
    path: Path,
    low_configuration: Mapping[str, object],
    high_configuration: Mapping[str, object],
    *,
    active_threshold: float,
) -> dict[str, Any]:
    """Validate a closed result bound to both configurations and threshold."""
    try:
        status = path.lstat()
        if (
            path.is_symlink()
            or not path.is_file()
            or not 0 < status.st_size <= 16 * 1024 * 1024
        ):
            raise ValueError("Gray-Scott analysis result is not a bounded regular file")
        value = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Gray-Scott analysis result is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "cases",
        "comparison",
    }:
        raise ValueError("Gray-Scott analysis result fields are invalid")
    if value["schema_version"] != _RESULT_SCHEMA:
        raise ValueError("Gray-Scott analysis result schema is invalid")
    cases = value["cases"]
    if not isinstance(cases, dict) or set(cases) != {"feed_low", "feed_high"}:
        raise ValueError("Gray-Scott analysis cases are invalid")
    _validate_case(
        cases["feed_low"],
        low_configuration,
        threshold=active_threshold,
        label="feed_low",
    )
    _validate_case(
        cases["feed_high"],
        high_configuration,
        threshold=active_threshold,
        label="feed_high",
    )
    comparison = value["comparison"]
    if not isinstance(comparison, dict) or set(comparison) != _COMPARISON_FIELDS:
        raise ValueError("Gray-Scott comparison fields are invalid")
    for name in _COMPARISON_FIELDS:
        _finite_number(comparison[name], label=f"comparison.{name}")
    return cast(dict[str, Any], value)


def _combined_probe_status(*statuses: RuntimeStatus) -> RuntimeStatus:
    if statuses and all(status.usable is True for status in statuses):
        return RuntimeStatus("ready", "runtime_probe_succeeded")
    if any(status.usable is False for status in statuses):
        return RuntimeStatus("unavailable", "software_not_found")
    return RuntimeStatus("unknown", "runtime_probe_inconclusive")


class Adios2GrayScottAnalysis(Application):
    """Compare morphology from two completed Gray-Scott BP datasets."""

    def _configure_menu(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "nprocs",
                "msg": "Analyzer process count",
                "type": int,
                "default": 1,
            },
            {
                "name": "ppn",
                "msg": "Analyzer processes per node",
                "type": int,
                "default": 1,
            },
            {
                "name": "executable",
                "msg": "Installed gray-scott-analyze executable",
                "type": str,
                "default": "gray-scott-analyze",
            },
            {
                "name": "input_bundle",
                "msg": "Optional digest-verified analyzer source and paired configuration bundle",
                "type": str,
                "default": "",
                "input_binding": ConfigurationInputBinding(
                    kind="local_file", structure="regular_file"
                ).to_dict(),
            },
            {
                "name": "low_input",
                "msg": "Completed low-feed ADIOS2 BP dataset",
                "type": str,
                "default": None,
            },
            {
                "name": "low_configuration",
                "msg": "Low-feed configuration path or scientific_input bundle member",
                "type": str,
                "default": None,
            },
            {
                "name": "high_input",
                "msg": "Completed high-feed ADIOS2 BP dataset",
                "type": str,
                "default": None,
            },
            {
                "name": "high_configuration",
                "msg": "High-feed configuration path or scientific_input bundle member",
                "type": str,
                "default": None,
            },
            {
                "name": "active_threshold",
                "msg": "V concentration threshold used for morphology",
                "type": float,
                "default": 0.1,
            },
            {
                "name": "output_file",
                "msg": "Closed JSON morphology comparison",
                "type": str,
                "default": "gray-scott-morphology.json",
            },
        ]

    def _deployment_contract(self) -> PackageDeploymentContract:
        environment = self._deployment_environment()
        installed = probe_program(
            "gray-scott-analyze",
            environment=environment,
            arguments=("--help",),
            accepted_return_codes=(0, 1),
        )
        source = _combined_probe_status(
            probe_program(
                "cmake", environment=environment, arguments=("--version",)
            ).status,
            probe_program(
                "adios2-config", environment=environment, arguments=("--version",)
            ).status,
            probe_program(
                "mpic++", environment=environment, arguments=("--version",)
            ).status,
        )
        completed = ReadinessContract("process_exit", "successful_exit")
        return PackageDeploymentContract(
            package="builtin.adios2_gray_scott_analysis",
            execution_profiles=(
                ExecutionProfile(
                    name="installed_executable",
                    execution_kind="batch",
                    when=(ConfigurationCondition("input_bundle", "is_empty"),),
                    runtime_requirements=("gray_scott_analysis_installed",),
                    readiness=completed,
                    description="Analyze two completed BP datasets with an installed executable.",
                ),
                ExecutionProfile(
                    name="source_bundle",
                    execution_kind="batch",
                    when=(ConfigurationCondition("input_bundle", "is_not_empty"),),
                    runtime_requirements=("gray_scott_analysis_source_build",),
                    readiness=completed,
                    description="Build one verified analyzer target and compare two completed BP datasets.",
                ),
            ),
            runtime_requirements=(
                RuntimeRequirement(
                    requirement_id="gray_scott_analysis_installed",
                    description="Installed ADIOS2 Gray-Scott morphology analyzer",
                    required_capabilities=("gray_scott_morphology",),
                    available_capabilities=(
                        ("gray_scott_morphology",)
                        if installed.status.usable is True
                        else ()
                    ),
                    status=installed.status,
                ),
                RuntimeRequirement(
                    requirement_id="gray_scott_analysis_source_build",
                    description="CMake, C++ compiler, and ADIOS2 development runtime",
                    required_capabilities=("gray_scott_morphology", "source_build"),
                    available_capabilities=(
                        ("gray_scott_morphology", "source_build")
                        if source.usable is True
                        else ()
                    ),
                    status=source,
                    provider_resolutions=(
                        ProviderResolution("spack", "spec", "adios2"),
                        ProviderResolution("spack", "spec", "cmake"),
                    ),
                ),
            ),
            configuration_rules=(
                ConfigurationRule(
                    when=(ConfigurationCondition("input_bundle", "is_empty"),),
                    requires=(
                        ConfigurationCondition("low_configuration", "is_not_empty"),
                        ConfigurationCondition("high_configuration", "is_not_empty"),
                    ),
                    description="Installed analysis requires two external configuration files.",
                ),
            ),
        )

    def _configure(self, **kwargs: Any) -> None:
        super()._configure(**kwargs)
        output = self.resolve_shared_path(
            self.config.get("output_file"),
            field="output_file",
            default="gray-scott-morphology.json",
        )
        cast(dict[str, Any], self.config)["output_file"] = str(output)
        self._validate_configuration()
        configured = self.config.get("input_bundle")
        if configured not in (None, ""):
            if not isinstance(configured, str):
                raise TypeError("input_bundle must be a path string")
            bundle = extract_input_bundle(
                configured, self._shared_root() / "input-bundles"
            )
            resolve_analysis_bundle(
                bundle,
                str(self.config["low_configuration"]),
                str(self.config["high_configuration"]),
            )

    def _shared_root(self) -> Path:
        shared_dir = self.shared_dir
        if not isinstance(shared_dir, (str, os.PathLike)) or not os.fspath(shared_dir):
            raise RuntimeError(
                "Gray-Scott analysis requires a package shared directory"
            )
        return Path(shared_dir)

    @staticmethod
    def _absolute_existing_path(value: object, *, label: str, directory: bool) -> Path:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} is required")
        path = Path(os.path.expandvars(value))
        if not path.is_absolute() or path.is_symlink():
            raise ValueError(f"{label} must be a normalized absolute path")
        if directory and not path.is_dir():
            raise ValueError(f"{label} must be an existing BP directory")
        if not directory and not path.is_file():
            raise ValueError(f"{label} must be an existing regular file")
        return path.resolve(strict=True)

    def _validate_configuration(self) -> None:
        for name in ("nprocs", "ppn"):
            _positive_integer(self.config.get(name), label=name)
        threshold = _finite_number(
            self.config.get("active_threshold"), label="active_threshold"
        )
        if threshold < 0:
            raise ValueError("active_threshold must be non-negative")
        executable = self.config.get("executable")
        if not isinstance(executable, str) or not executable.strip():
            raise ValueError(
                "Gray-Scott analysis executable must be a non-empty string"
            )
        self._absolute_existing_path(
            self.config.get("low_input"), label="low_input", directory=True
        )
        self._absolute_existing_path(
            self.config.get("high_input"), label="high_input", directory=True
        )
        output = self.config.get("output_file")
        if not isinstance(output, str) or not Path(output).is_absolute():
            raise ValueError("output_file must resolve to an absolute path")
        if self.config.get("input_bundle") in (None, ""):
            self._absolute_existing_path(
                self.config.get("low_configuration"),
                label="low_configuration",
                directory=False,
            )
            self._absolute_existing_path(
                self.config.get("high_configuration"),
                label="high_configuration",
                directory=False,
            )

    def _prepare_bundle_run(self) -> tuple[str, Path, Path, Path]:
        configured = self.config.get("input_bundle")
        if not isinstance(configured, str) or not configured:
            raise RuntimeError("Gray-Scott analysis input bundle was not persisted")
        bundle = extract_input_bundle(configured, self._shared_root() / "input-bundles")
        selected = resolve_analysis_bundle(
            bundle,
            str(self.config.get("low_configuration") or ""),
            str(self.config.get("high_configuration") or ""),
        )
        run_dir = self.resolve_shared_path("run", field="input_bundle workspace")
        stage_input_bundle(bundle, run_dir)
        build_dir = run_dir / "build"
        source_dir = (run_dir / selected.build_spec.relative_to(bundle.root)).parent
        local_info = LocalExecInfo(env=self.mod_env, cwd=str(run_dir), timeout=900)
        configure = " ".join(
            (
                "cmake",
                "-S",
                shlex.quote(str(source_dir)),
                "-B",
                shlex.quote(str(build_dir)),
                "-DCMAKE_BUILD_TYPE=Release",
                "-DCMAKE_C_COMPILER=mpicc",
                "-DCMAKE_CXX_COMPILER=mpic++",
            )
        )
        self._raise_for_exec_failure(
            Exec(configure, local_info).run(), operation="Gray-Scott analysis configure"
        )
        build = f"cmake --build {shlex.quote(str(build_dir))} --parallel 4 --target gray-scott-analyze"
        self._raise_for_exec_failure(
            Exec(build, local_info).run(), operation="Gray-Scott analysis build"
        )
        low = run_dir / selected.low.path.relative_to(bundle.root)
        high = run_dir / selected.high.path.relative_to(bundle.root)
        return str(build_dir / "bin" / "gray-scott-analyze"), low, high, run_dir

    def start(self) -> None:
        """Build if requested, analyze both inputs, and validate the result."""
        self._validate_configuration()
        configured = self.config.get("input_bundle")
        if configured not in (None, ""):
            executable, low_config, high_config, cwd = self._prepare_bundle_run()
        else:
            executable = str(self.config["executable"])
            low_config = self._absolute_existing_path(
                self.config["low_configuration"],
                label="low_configuration",
                directory=False,
            )
            high_config = self._absolute_existing_path(
                self.config["high_configuration"],
                label="high_configuration",
                directory=False,
            )
            cwd = Path(str(self.config["output_file"])).parent
        low_input = self._absolute_existing_path(
            self.config["low_input"], label="low_input", directory=True
        )
        high_input = self._absolute_existing_path(
            self.config["high_input"], label="high_input", directory=True
        )
        output = Path(str(self.config["output_file"]))
        output.parent.mkdir(parents=True, exist_ok=True)
        threshold = _finite_number(
            self.config.get("active_threshold"), label="active_threshold"
        )
        command = " ".join(
            shlex.quote(str(value))
            for value in (
                executable,
                low_input,
                low_config,
                high_input,
                high_config,
                threshold,
                output,
            )
        )
        result = Exec(
            command,
            MpiExecInfo(
                nprocs=self.config["nprocs"],
                ppn=self.config["ppn"],
                hostfile=self.hostfile,
                env=self.mod_env,
                cwd=str(cwd),
                line_callback=self.runtime_line_callback(),
            ),
        ).run()
        self._raise_for_exec_failure(result, operation="Gray-Scott analysis")
        validate_result_document(
            output,
            _load_configuration(low_config),
            _load_configuration(high_config),
            active_threshold=threshold,
        )

    @staticmethod
    def _raise_for_exec_failure(result: Any, *, operation: str) -> None:
        exit_codes = getattr(result, "exit_code", None)
        if not isinstance(exit_codes, dict) or not exit_codes:
            raise RuntimeError(f"{operation} returned no process exit status")
        failures = {
            str(host): code
            for host, code in exit_codes.items()
            if isinstance(code, bool) or not isinstance(code, int) or code != 0
        }
        if failures:
            details = ", ".join(
                f"{host}={code!r}" for host, code in sorted(failures.items())
            )
            raise RuntimeError(f"{operation} failed with exit status: {details}")

    def stop(self) -> None:
        """The morphology analyzer is a bounded batch application."""

    def clean(self) -> None:
        """Remove the configured comparison result."""
        output = self.config.get("output_file")
        if isinstance(output, str) and output:
            Rm(output, PsshExecInfo(hostfile=self.hostfile)).run()


__all__ = [
    "Adios2GrayScottAnalysis",
    "AnalysisConfiguration",
    "GrayScottAnalysisBundle",
    "resolve_analysis_bundle",
    "validate_result_document",
]
