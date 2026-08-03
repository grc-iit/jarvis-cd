"""Launch the ADIOS2 Gray-Scott reaction-diffusion application."""

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
from jarvis_cd.shell.process import Mkdir, Rm
from jarvis_cd.util.config_parser import JsonFile

_SCIENTIFIC_INPUT_ROLE = "scientific_input"
_BUILD_SPEC_ROLE = "build_spec"
_ADIOS_CONFIG_ROLE = "adios2_configuration"
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


class GrayScottBundleConfiguration(NamedTuple):
    """Validated paths and scientific values selected from one input bundle."""

    path: Path
    values: Mapping[str, object]
    build_spec: Path
    adios_config: Path


def _safe_bundle_member(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label} must name a declared scientific_input member")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must name a declared scientific_input member")
    return path.as_posix()


def _relative_output_path(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label} must be a relative bundle path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a relative bundle path")
    return path.as_posix()


def _finite_number(
    values: Mapping[str, object],
    name: str,
    *,
    positive: bool,
) -> float:
    value = values[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Gray-Scott {name} must be numeric")
    parsed = float(value)
    if (
        not math.isfinite(parsed)
        or (positive and parsed <= 0)
        or (not positive and parsed < 0)
    ):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"Gray-Scott {name} must be finite and {qualifier}")
    return parsed


def _positive_integer(values: Mapping[str, object], name: str) -> int:
    value = values[name]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Gray-Scott {name} must be a positive integer")
    return value


def _validate_scientific_configuration(values: object) -> Mapping[str, object]:
    if not isinstance(values, dict):
        raise ValueError("Gray-Scott scientific configuration must be a JSON object")
    fields = set(values)
    missing = _CONFIGURATION_REQUIRED_FIELDS - fields
    unknown = fields - _CONFIGURATION_REQUIRED_FIELDS - _CONFIGURATION_OPTIONAL_FIELDS
    if missing or unknown:
        raise ValueError(
            "Gray-Scott scientific configuration fields are invalid: "
            f"missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    for name in ("Du", "Dv", "dt"):
        _finite_number(values, name, positive=True)
    for name in ("F", "k", "noise"):
        _finite_number(values, name, positive=False)
    for name in ("L", "steps", "plotgap", "checkpoint_freq"):
        _positive_integer(values, name)
    if values["plotgap"] > values["steps"]:
        raise ValueError("Gray-Scott plotgap cannot exceed steps")
    for name in (
        "checkpoint",
        "adios_span",
        "adios_memory_selection",
    ):
        if not isinstance(values[name], bool):
            raise ValueError(f"Gray-Scott {name} must be boolean")
    restart = values.get("restart", False)
    if not isinstance(restart, bool):
        raise ValueError("Gray-Scott restart must be boolean")
    mesh_type = values["mesh_type"]
    if not isinstance(mesh_type, str) or not mesh_type.strip():
        raise ValueError("Gray-Scott mesh_type must be a non-empty string")
    _relative_output_path(values["output"], label="Gray-Scott output")
    _relative_output_path(
        values["checkpoint_output"], label="Gray-Scott checkpoint output"
    )
    if restart:
        _relative_output_path(
            values.get("restart_input"), label="Gray-Scott restart input"
        )
    return values


def resolve_bundle_configuration(
    bundle: MaterializedInputBundle,
    configuration_path: str,
) -> GrayScottBundleConfiguration:
    """Resolve and validate one scientific configuration from a verified bundle."""

    selected_path = _safe_bundle_member(
        configuration_path or bundle.manifest.entrypoint,
        label="configuration_path",
    )
    roles = {item.path: item.role for item in bundle.manifest.files}
    if roles.get(selected_path) != _SCIENTIFIC_INPUT_ROLE:
        raise ValueError(
            "configuration_path must name a declared scientific_input member"
        )
    build_specs = [path for path, role in roles.items() if role == _BUILD_SPEC_ROLE]
    if len(build_specs) != 1:
        raise ValueError("Gray-Scott input bundle requires exactly one build_spec")
    selected = bundle.root / PurePosixPath(selected_path)
    try:
        values = json.loads(selected.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "Gray-Scott scientific configuration is not valid JSON"
        ) from exc
    validated = _validate_scientific_configuration(values)
    adios_path = _safe_bundle_member(
        validated["adios_config"],
        label="Gray-Scott adios_config",
    )
    if roles.get(adios_path) != _ADIOS_CONFIG_ROLE:
        raise ValueError(
            "Gray-Scott adios_config must name a declared adios2_configuration member"
        )
    return GrayScottBundleConfiguration(
        path=selected,
        values=validated,
        build_spec=bundle.root / PurePosixPath(build_specs[0]),
        adios_config=bundle.root / PurePosixPath(adios_path),
    )


def _combined_probe_status(*statuses: RuntimeStatus) -> RuntimeStatus:
    if statuses and all(status.usable is True for status in statuses):
        return RuntimeStatus("ready", "runtime_probe_succeeded")
    if any(status.usable is False for status in statuses):
        return RuntimeStatus("unavailable", "software_not_found")
    return RuntimeStatus("unknown", "runtime_probe_inconclusive")


class Adios2GrayScott(Application):
    """Run an installed or digest-bound source build of ADIOS2 Gray-Scott."""

    def _init(self) -> None:
        self.adios2_xml_path = f"{self.shared_dir}/adios2.xml"
        self.settings_json_path = f"{self.shared_dir}/settings-files.json"
        self.var_json_path = f"{self.shared_dir}/var.json"
        self.operator_json_path = f"{self.shared_dir}/operator.json"
        self.process: Any = None

    def _configure_menu(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "nprocs",
                "msg": "Number of processes to spawn",
                "type": int,
                "default": 4,
            },
            {"name": "ppn", "msg": "Processes per node", "type": int, "default": 16},
            {
                "name": "executable",
                "msg": "Installed ADIOS2 Gray-Scott executable available through PATH",
                "type": str,
                "default": "adios2-gray-scott",
            },
            {
                "name": "input_bundle",
                "msg": (
                    "Optional digest-verified source and scientific-configuration "
                    "bundle. Its manifest must declare one build_spec, an ADIOS2 "
                    "configuration, and one or more scientific_input files."
                ),
                "type": str,
                "default": "",
                "input_binding": ConfigurationInputBinding(
                    kind="local_file", structure="regular_file"
                ).to_dict(),
            },
            {
                "name": "configuration_path",
                "msg": (
                    "Relative scientific_input member selected from input_bundle; "
                    "empty selects the manifest entrypoint"
                ),
                "type": str,
                "default": "",
            },
            {"name": "L", "msg": "Grid size of cube", "type": int, "default": 32},
            {"name": "Du", "msg": "Diffusion rate of U", "type": float, "default": 0.2},
            {"name": "Dv", "msg": "Diffusion rate of V", "type": float, "default": 0.1},
            {"name": "F", "msg": "Feed rate of U", "type": float, "default": 0.01},
            {"name": "k", "msg": "Kill rate of V", "type": float, "default": 0.05},
            {"name": "dt", "msg": "Timestep", "type": float, "default": 2.0},
            {
                "name": "steps",
                "msg": "Total simulation steps",
                "type": int,
                "default": 100,
            },
            {
                "name": "plotgap",
                "msg": "Steps between outputs",
                "type": int,
                "default": 10,
            },
            {"name": "noise", "msg": "Initial noise", "type": float, "default": 0.01},
            {
                "name": "out_file",
                "msg": "Optional output dataset; empty uses execution-owned storage",
                "type": str,
                "default": "",
            },
            {
                "name": "checkpoint",
                "msg": "Write checkpoints",
                "type": bool,
                "default": True,
            },
            {
                "name": "checkpoint_freq",
                "msg": "Checkpoint interval",
                "type": int,
                "default": 70,
            },
            {
                "name": "checkpoint_output",
                "msg": "Checkpoint dataset",
                "type": str,
                "default": "ckpt.bp",
            },
            {
                "name": "restart",
                "msg": "Restart from a checkpoint",
                "type": bool,
                "default": False,
            },
            {
                "name": "restart_input",
                "msg": "Restart checkpoint",
                "type": str,
                "default": "ckpt.bp",
            },
            {
                "name": "adios_span",
                "msg": "Enable ADIOS span mode",
                "type": bool,
                "default": False,
            },
            {
                "name": "adios_memory_selection",
                "msg": "Enable ADIOS memory selection",
                "type": bool,
                "default": False,
            },
            {
                "name": "mesh_type",
                "msg": "Mesh representation",
                "type": str,
                "default": "image",
            },
            {
                "name": "engine",
                "msg": "ADIOS2 engine",
                "choices": [
                    "bp5",
                    "hermes",
                    "bp5_derived",
                    "hermes_derived",
                    "iowarp",
                    "iowarp_derived",
                    "sst",
                ],
                "type": str,
                "default": "bp5",
            },
            {
                "name": "full_run",
                "msg": "Execute postprocessing",
                "type": bool,
                "default": True,
            },
            {"name": "limit", "msg": "Value-tracking limit", "type": int, "default": 0},
            {
                "name": "db_path",
                "msg": "Metadata database path",
                "type": str,
                "default": "benchmark_metadata.db",
            },
            {
                "name": "Execution_order",
                "msg": "IOWarp execution order",
                "type": str,
                "default": "1",
            },
            {
                "name": "run_async",
                "msg": "Run producer asynchronously",
                "type": bool,
                "default": False,
            },
        ]

    def _deployment_contract(self) -> PackageDeploymentContract:
        environment = self._deployment_environment()
        installed_probe = probe_program(
            "adios2-gray-scott", environment=environment, arguments=("--help",)
        )
        cmake_probe = probe_program(
            "cmake", environment=environment, arguments=("--version",)
        )
        adios_probe = probe_program(
            "adios2-config", environment=environment, arguments=("--version",)
        )
        mpi_c_probe = probe_program(
            "mpicc", environment=environment, arguments=("--version",)
        )
        mpi_cxx_probe = probe_program(
            "mpic++", environment=environment, arguments=("--version",)
        )
        source_status = _combined_probe_status(
            cmake_probe.status,
            adios_probe.status,
            mpi_c_probe.status,
            mpi_cxx_probe.status,
        )
        installed_capabilities = (
            ("mpi_execution", "reaction_diffusion")
            if installed_probe.status.usable is True
            else ()
        )
        source_capabilities = (
            ("mpi_execution", "reaction_diffusion", "source_build")
            if source_status.usable is True
            else ()
        )
        completed = ReadinessContract("process_exit", "successful_exit")
        return PackageDeploymentContract(
            package="builtin.adios2_gray_scott",
            execution_profiles=(
                ExecutionProfile(
                    name="installed_executable",
                    execution_kind="batch",
                    when=(ConfigurationCondition("input_bundle", "is_empty"),),
                    runtime_requirements=("gray_scott_installed",),
                    readiness=completed,
                    description="Run an installed Gray-Scott producer using package parameters.",
                ),
                ExecutionProfile(
                    name="source_bundle",
                    execution_kind="batch",
                    when=(ConfigurationCondition("input_bundle", "is_not_empty"),),
                    runtime_requirements=("gray_scott_source_build",),
                    readiness=completed,
                    description=(
                        "Build verified source and run one manifest-declared "
                        "scientific configuration in an execution-owned workspace."
                    ),
                ),
            ),
            runtime_requirements=(
                RuntimeRequirement(
                    requirement_id="gray_scott_installed",
                    description="Installed MPI ADIOS2 Gray-Scott producer",
                    required_capabilities=("mpi_execution", "reaction_diffusion"),
                    available_capabilities=installed_capabilities,
                    status=installed_probe.status,
                ),
                RuntimeRequirement(
                    requirement_id="gray_scott_source_build",
                    description="CMake and MPI-enabled ADIOS2 development runtime",
                    required_capabilities=(
                        "mpi_execution",
                        "reaction_diffusion",
                        "source_build",
                    ),
                    available_capabilities=source_capabilities,
                    status=source_status,
                    provider_resolutions=(
                        ProviderResolution("spack", "spec", "adios2+mpi"),
                        ProviderResolution("spack", "spec", "cmake"),
                        ProviderResolution("spack", "spec", "openmpi"),
                    ),
                ),
            ),
            configuration_rules=(
                ConfigurationRule(
                    when=(ConfigurationCondition("input_bundle", "is_empty"),),
                    requires=(
                        ConfigurationCondition("configuration_path", "is_empty"),
                    ),
                    description="configuration_path is valid only with input_bundle.",
                ),
            ),
        )

    def _configure(self, **kwargs: Any) -> None:
        super()._configure(**kwargs)
        self._validate_common_configuration()
        config = cast(dict[str, Any], self.config)
        configured_bundle = config.get("input_bundle")
        if configured_bundle not in (None, ""):
            if not isinstance(configured_bundle, str):
                raise TypeError("input_bundle must be a path string")
            bundle = extract_input_bundle(
                configured_bundle, self._shared_root() / "input-bundles"
            )
            selected = resolve_bundle_configuration(
                bundle, str(config.get("configuration_path") or "")
            )
            self._apply_scientific_values(selected.values)
            return
        if config.get("configuration_path") not in (None, ""):
            raise ValueError("configuration_path requires input_bundle")
        self._configure_generated_settings()

    def _shared_root(self) -> Path:
        shared_dir = self.shared_dir
        if not isinstance(shared_dir, (str, os.PathLike)) or not os.fspath(shared_dir):
            raise RuntimeError("Gray-Scott requires a JARVIS package shared directory")
        return Path(shared_dir)

    def _validate_common_configuration(self) -> None:
        for name in ("nprocs", "ppn"):
            value = self.config.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        executable = self.config.get("executable")
        if not isinstance(executable, str) or not executable.strip():
            raise ValueError("ADIOS2 Gray-Scott executable must be a non-empty string")

    def _apply_scientific_values(self, values: Mapping[str, object]) -> None:
        config = cast(dict[str, Any], self.config)
        mapping = {name: name for name in values if name != "output"}
        mapping["output"] = "out_file"
        for source, destination in mapping.items():
            config[destination] = values[source]

    def _configure_generated_settings(self) -> None:
        config = cast(dict[str, Any], self.config)
        if not config["out_file"]:
            config["out_file"] = str(
                self.resolve_shared_path(
                    "gray-scott-output/data/out.bp", field="out_file"
                )
            )
        output_dir = os.path.dirname(str(config["out_file"]))
        db_dir = os.path.dirname(
            str(self.resolve_shared_path(config["db_path"], field="db_path"))
        )
        Mkdir(
            [output_dir, db_dir], PsshExecInfo(hostfile=self.hostfile, env=self.env)
        ).run()
        self._write_settings(
            Path(self.settings_json_path), config, self.adios2_xml_path
        )
        self._configure_adios_template()

    def _configure_adios_template(self) -> None:
        engine = str(self.config["engine"]).lower()
        if engine in {"bp5", "bp5_derived"}:
            self.copy_template_file(
                f"{self.pkg_dir}/config/adios2.xml", self.adios2_xml_path
            )
        elif engine == "sst":
            self.copy_template_file(
                f"{self.pkg_dir}/config/sst.xml", self.adios2_xml_path
            )
        elif engine in {"hermes", "hermes_derived", "iowarp", "iowarp_derived"}:
            template = "hermes.xml" if engine.startswith("hermes") else "iowarp.xml"
            self.copy_template_file(
                f"{self.pkg_dir}/config/{template}",
                self.adios2_xml_path,
                replacements={
                    "PPN": self.config["ppn"],
                    "VARFILE": self.var_json_path,
                    "OPFILE": self.operator_json_path,
                    "DBFILE": self.config["db_path"],
                    "Order": self.config["Execution_order"],
                },
            )
            self.copy_template_file(
                f"{self.pkg_dir}/config/var.yaml", self.var_json_path
            )
            self.copy_template_file(
                f"{self.pkg_dir}/config/operator.yaml", self.operator_json_path
            )
        else:
            raise ValueError(f"unsupported ADIOS2 engine: {engine}")

    @staticmethod
    def _write_settings(
        path: Path, values: Mapping[str, object], adios_config: str
    ) -> None:
        payload = {
            "L": values["L"],
            "Du": values["Du"],
            "Dv": values["Dv"],
            "F": values["F"],
            "k": values["k"],
            "dt": values["dt"],
            "plotgap": values["plotgap"],
            "steps": values["steps"],
            "noise": values["noise"],
            "output": values["out_file"],
            "checkpoint": values["checkpoint"],
            "checkpoint_freq": values["checkpoint_freq"],
            "checkpoint_output": values["checkpoint_output"],
            "restart": values.get("restart", False),
            "restart_input": values.get("restart_input", "ckpt.bp"),
            "adios_span": values["adios_span"],
            "adios_memory_selection": values["adios_memory_selection"],
            "mesh_type": values["mesh_type"],
            "adios_config": adios_config,
        }
        JsonFile(str(path)).save(payload)

    def _prepare_bundle_run(self) -> tuple[str, str]:
        configured = self.config.get("input_bundle")
        if not isinstance(configured, str) or not configured:
            raise RuntimeError("Gray-Scott input bundle was not persisted")
        bundle = extract_input_bundle(configured, self._shared_root() / "input-bundles")
        selected = resolve_bundle_configuration(
            bundle, str(self.config.get("configuration_path") or "")
        )
        run_dir = self.resolve_shared_path("run", field="input_bundle workspace")
        stage_input_bundle(bundle, run_dir)
        relative_configuration = selected.path.relative_to(bundle.root)
        relative_build_spec = selected.build_spec.relative_to(bundle.root)
        relative_adios = selected.adios_config.relative_to(bundle.root)
        staged_values = dict(selected.values)
        for source in ("output", "checkpoint_output", "restart_input"):
            value = staged_values.get(source)
            if value is not None:
                staged_values[source] = str(run_dir / PurePosixPath(str(value)))
        self._apply_scientific_values(staged_values)
        self.adios2_xml_path = str(run_dir / relative_adios)
        self.settings_json_path = str(run_dir / relative_configuration)
        output_dir = Path(str(self.config["out_file"])).parent
        output_dir.mkdir(parents=True, exist_ok=True)
        if self.config.get("checkpoint"):
            Path(str(self.config["checkpoint_output"])).parent.mkdir(
                parents=True, exist_ok=True
            )
        self._write_settings(
            Path(self.settings_json_path), self.config, self.adios2_xml_path
        )
        build_dir = run_dir / "build"
        source_dir = (run_dir / relative_build_spec).parent
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
            Exec(configure, local_info).run(), operation="Gray-Scott configure"
        )
        build = f"cmake --build {shlex.quote(str(build_dir))} --parallel 4 --target gray-scott"
        self._raise_for_exec_failure(
            Exec(build, local_info).run(), operation="Gray-Scott build"
        )
        return str(build_dir / "bin" / "gray-scott"), str(run_dir)

    def start(self) -> None:
        """Build when requested and launch the selected Gray-Scott simulation."""
        configured_bundle = self.config.get("input_bundle")
        if configured_bundle not in (None, ""):
            executable, cwd = self._prepare_bundle_run()
        else:
            executable = str(self.config.get("executable") or "")
            cwd = str(Path(self.settings_json_path).parent)
        command = f"{shlex.quote(executable)} {shlex.quote(self.settings_json_path)}"
        derived = int(str(self.config["engine"]).lower().endswith("_derived"))
        self.process = Exec(
            f"{command} {derived}",
            MpiExecInfo(
                nprocs=self.config["nprocs"],
                ppn=self.config["ppn"],
                hostfile=self.hostfile,
                env=self.mod_env,
                cwd=cwd,
                exec_async=self.config["run_async"],
                line_callback=self.runtime_line_callback(),
            ),
        )
        result = self.process.run()
        if not self.config["run_async"]:
            self._raise_for_exec_failure(result, operation="ADIOS2 Gray-Scott")

    def wait(self) -> None:
        """Wait for an asynchronous producer and propagate its terminal status."""
        if self.process:
            self.process.wait_all()
            self._raise_for_exec_failure(
                self.process, operation="ADIOS2 Gray-Scott async producer"
            )

    def stop(self) -> None:
        """Wait for asynchronous work or terminate a still-owned process."""
        if self.config.get("run_async", False) and self.process:
            self.wait()
        elif self.process:
            self.process.kill_all()

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

    def clean(self) -> None:
        """Remove configured simulation output and restart state."""
        output_file: list[object] = [
            self.config.get("out_file"),
            self.config.get("checkpoint_output"),
            self.config.get("db_path"),
        ]
        Rm(
            [
                os.fspath(value)
                for value in output_file
                if isinstance(value, (str, os.PathLike)) and os.fspath(value)
            ],
            PsshExecInfo(hostfile=self.hostfile),
        ).run()
