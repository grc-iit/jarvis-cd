"""Run ADIOS2 PDF Calc against a Gray-Scott scientific dataset."""

from __future__ import annotations

import os
import shlex
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Any, cast

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


def _combined_probe_status(*statuses: RuntimeStatus) -> RuntimeStatus:
    if statuses and all(status.usable is True for status in statuses):
        return RuntimeStatus("ready", "runtime_probe_succeeded")
    if any(status.usable is False for status in statuses):
        return RuntimeStatus("unavailable", "software_not_found")
    return RuntimeStatus("unknown", "runtime_probe_inconclusive")


def _bundle_member_for_role(
    bundle: MaterializedInputBundle,
    role: str,
) -> Path:
    members = [item.path for item in bundle.manifest.files if item.role == role]
    if len(members) != 1:
        raise ValueError(f"PDF Calc input bundle requires exactly one {role}")
    return bundle.root / PurePosixPath(members[0])


def validate_pdf_source_bundle(bundle: MaterializedInputBundle) -> None:
    """Require the source build and ADIOS2 configuration used by PDF Calc."""
    _bundle_member_for_role(bundle, "build_spec")
    _bundle_member_for_role(bundle, "adios2_configuration")


class Adios2PdfCalc(Application):
    """Calculate per-slice PDFs using an installed or bundle-built executable."""

    def _init(self) -> None:
        self.adios2_xml_path = f"{self.shared_dir}/adios2.xml"

    def _configure_menu(self) -> list[dict[str, Any]]:
        return [
            {"name": "nprocs", "msg": "Number of processes", "type": int, "default": 2},
            {"name": "ppn", "msg": "Processes per node", "type": int, "default": 16},
            {
                "name": "executable",
                "msg": "Installed pdf_calc executable available through PATH",
                "type": str,
                "default": "pdf_calc",
            },
            {
                "name": "input_bundle",
                "msg": (
                    "Optional digest-verified PDF Calc source bundle declaring "
                    "one build_spec and one adios2_configuration"
                ),
                "type": str,
                "default": "",
                "input_binding": ConfigurationInputBinding(
                    kind="local_file", structure="regular_file"
                ).to_dict(),
            },
            {
                "name": "input_file",
                "msg": "Gray-Scott input dataset",
                "type": str,
                "default": None,
            },
            {
                "name": "output_file",
                "msg": "PDF output dataset",
                "type": str,
                "default": None,
            },
            {
                "name": "nbins",
                "msg": "Number of PDF bins",
                "type": int,
                "default": 1000,
            },
            {
                "name": "output_inputdata",
                "msg": "Copy original variables (YES/NO)",
                "type": str,
                "default": "NO",
            },
            {
                "name": "wait_for_producer",
                "msg": "Wait for the input dataset",
                "type": bool,
                "default": True,
            },
            {
                "name": "engine",
                "msg": "ADIOS2 engine",
                "choices": ["bp5", "sst"],
                "type": str,
                "default": "bp5",
            },
        ]

    def _deployment_contract(self) -> PackageDeploymentContract:
        environment = self._deployment_environment()
        installed_probe = probe_program(
            "pdf_calc", environment=environment, arguments=("--help",)
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
            ("mpi_execution", "probability_density")
            if installed_probe.status.usable is True
            else ()
        )
        source_capabilities = (
            ("mpi_execution", "probability_density", "source_build")
            if source_status.usable is True
            else ()
        )
        completed = ReadinessContract("process_exit", "successful_exit")
        return PackageDeploymentContract(
            package="builtin.adios2_pdf_calc",
            execution_profiles=(
                ExecutionProfile(
                    name="installed_executable",
                    execution_kind="batch",
                    when=(ConfigurationCondition("input_bundle", "is_empty"),),
                    runtime_requirements=("pdf_calc_installed",),
                    readiness=completed,
                ),
                ExecutionProfile(
                    name="source_bundle",
                    execution_kind="batch",
                    when=(ConfigurationCondition("input_bundle", "is_not_empty"),),
                    runtime_requirements=("pdf_calc_source_build",),
                    readiness=completed,
                    description=(
                        "Build verified PDF Calc source in an execution-owned "
                        "workspace before analyzing the configured input dataset."
                    ),
                ),
            ),
            runtime_requirements=(
                RuntimeRequirement(
                    requirement_id="pdf_calc_installed",
                    description="Installed MPI ADIOS2 PDF Calc executable",
                    required_capabilities=("mpi_execution", "probability_density"),
                    available_capabilities=installed_capabilities,
                    status=installed_probe.status,
                ),
                RuntimeRequirement(
                    requirement_id="pdf_calc_source_build",
                    description="CMake and MPI-enabled ADIOS2 development runtime",
                    required_capabilities=(
                        "mpi_execution",
                        "probability_density",
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
                    when=(ConfigurationCondition("input_file", "is_empty"),),
                    requires=(ConfigurationCondition("input_file", "is_not_empty"),),
                    description="PDF Calc requires one input dataset.",
                ),
                ConfigurationRule(
                    when=(ConfigurationCondition("output_file", "is_empty"),),
                    requires=(ConfigurationCondition("output_file", "is_not_empty"),),
                    description="PDF Calc requires one output dataset.",
                ),
            ),
        )

    def _configure(self, **kwargs: Any) -> None:
        super()._configure(**kwargs)
        self._validate_configuration()
        config = cast(dict[str, Any], self.config)
        configured_bundle = config.get("input_bundle")
        if configured_bundle not in (None, ""):
            if not isinstance(configured_bundle, str):
                raise TypeError("input_bundle must be a path string")
            bundle = extract_input_bundle(
                configured_bundle, self._shared_root() / "input-bundles"
            )
            validate_pdf_source_bundle(bundle)
            return
        template = (
            "sst.xml" if str(self.config["engine"]).lower() == "sst" else "adios2.xml"
        )
        self.copy_template_file(
            f"{self.pkg_dir}/config/{template}", self.adios2_xml_path
        )

    def _shared_root(self) -> Path:
        shared_dir = self.shared_dir
        if not isinstance(shared_dir, (str, os.PathLike)) or not os.fspath(shared_dir):
            raise RuntimeError("PDF Calc requires a JARVIS package shared directory")
        return Path(shared_dir)

    def _validate_configuration(self) -> None:
        for name in ("nprocs", "ppn", "nbins"):
            value = self.config.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("input_file", "output_file"):
            value = self.config.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} parameter is required for PDF Calc")
        executable = self.config.get("executable")
        if not isinstance(executable, str) or not executable.strip():
            raise ValueError("PDF Calc executable must be a non-empty string")
        output_inputdata = str(self.config.get("output_inputdata", "NO")).upper()
        if output_inputdata not in {"YES", "NO"}:
            raise ValueError("output_inputdata must be YES or NO")

    def _prepare_bundle_run(self) -> tuple[str, Path, Path]:
        configured = self.config.get("input_bundle")
        if not isinstance(configured, str) or not configured:
            raise RuntimeError("PDF Calc input bundle was not persisted")
        bundle = extract_input_bundle(configured, self._shared_root() / "input-bundles")
        validate_pdf_source_bundle(bundle)
        build_spec = _bundle_member_for_role(bundle, "build_spec")
        adios_config = _bundle_member_for_role(bundle, "adios2_configuration")
        run_dir = self.resolve_shared_path("run", field="input_bundle workspace")
        stage_input_bundle(bundle, run_dir)
        staged_build_spec = run_dir / build_spec.relative_to(bundle.root)
        staged_adios_config = run_dir / adios_config.relative_to(bundle.root)
        runtime_adios = run_dir / "adios2.xml"
        if staged_adios_config != runtime_adios:
            shutil.copyfile(staged_adios_config, runtime_adios)
        build_dir = run_dir / "build"
        local_info = LocalExecInfo(env=self.mod_env, cwd=str(run_dir), timeout=900)
        configure = " ".join(
            (
                "cmake",
                "-S",
                shlex.quote(str(staged_build_spec.parent)),
                "-B",
                shlex.quote(str(build_dir)),
                "-DCMAKE_BUILD_TYPE=Release",
                "-DCMAKE_C_COMPILER=mpicc",
                "-DCMAKE_CXX_COMPILER=mpic++",
            )
        )
        self._raise_for_exec_failure(
            Exec(configure, local_info).run(), operation="PDF Calc configure"
        )
        build = f"cmake --build {shlex.quote(str(build_dir))} --parallel 4 --target pdf_calc"
        self._raise_for_exec_failure(
            Exec(build, local_info).run(), operation="PDF Calc build"
        )
        return str(build_dir / "bin" / "pdf_calc"), run_dir, runtime_adios

    def start(self) -> None:
        """Run PDF Calc and propagate its authoritative process status."""
        self._validate_configuration()
        config = cast(dict[str, Any], self.config)
        configured_bundle = config.get("input_bundle")
        if configured_bundle not in (None, ""):
            executable, working_dir, runtime_adios = self._prepare_bundle_run()
        else:
            executable = str(config["executable"])
            input_path = Path(os.path.expandvars(str(config["input_file"]))).resolve()
            working_dir = input_path.parent
            runtime_adios = working_dir / "adios2.xml"
            shutil.copyfile(self.adios2_xml_path, runtime_adios)
        input_file = Path(os.path.expandvars(str(config["input_file"]))).resolve()
        output_value = str(config["output_file"])
        output_file = (
            Path(os.path.expandvars(output_value)).resolve()
            if Path(output_value).is_absolute()
            else self.resolve_shared_path(output_value, field="output_file")
        )
        output_file.parent.mkdir(parents=True, exist_ok=True)
        config["input_file"] = str(input_file)
        config["output_file"] = str(output_file)
        if self.config.get("wait_for_producer", True):
            self._wait_for_input(input_file)
        command = " ".join(
            (
                shlex.quote(executable),
                shlex.quote(str(input_file)),
                shlex.quote(str(output_file)),
                str(self.config["nbins"]),
            )
        )
        if str(self.config["output_inputdata"]).upper() == "YES":
            command += " YES"
        result = Exec(
            command,
            MpiExecInfo(
                nprocs=self.config["nprocs"],
                ppn=self.config["ppn"],
                hostfile=self.hostfile,
                env=self.mod_env,
                cwd=str(working_dir),
                line_callback=self.runtime_line_callback(),
            ),
        ).run()
        self._raise_for_exec_failure(result, operation="PDF Calc")
        if not runtime_adios.is_file() and configured_bundle not in (None, ""):
            raise RuntimeError("PDF Calc ADIOS2 configuration was not materialized")

    def _wait_for_input(self, input_file: Path) -> None:
        if str(self.config["engine"]).lower() == "sst":
            time.sleep(10)
            return
        for _ in range(60):
            if input_file.exists():
                time.sleep(5)
                return
            time.sleep(1)
        raise TimeoutError("PDF Calc input dataset was not available after 60 seconds")

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
        """PDF Calc is a bounded batch application with no persistent process."""

    def clean(self) -> None:
        """Remove the configured PDF output dataset."""
        output = self.config.get("output_file")
        if output:
            Rm(str(output), PsshExecInfo(hostfile=self.hostfile)).run()
