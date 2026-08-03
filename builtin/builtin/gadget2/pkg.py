"""Launch native or containerized Gadget2 simulations."""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, cast

from jarvis_cd.core.pkg import Application
from jarvis_cd.deployment import (
    ConfigurationCondition,
    ConfigurationInputBinding,
    ConfigurationRule,
    ExecutionProfile,
    PackageDeploymentContract,
    ReadinessContract,
    RuntimeRequirement,
    probe_program,
)
from jarvis_cd.input_bundle import (
    MaterializedInputBundle,
    extract_input_bundle,
    stage_input_bundle,
)
from jarvis_cd.shell import Exec, LocalExecInfo, MpiExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm

_LEGACY_TEST_CASES = (
    "gassphere",
    "galaxy",
    "cluster",
    "lcdm_gas",
    "lcdm_gas-ngen",
)
_GADGET2_PATH_CONTAINER = "/opt/gadget2"
_BINARY_REL_PATH = "build/bin/Gadget2"
_EXECUTABLE_CANDIDATES = ("Gadget2", "gadget2")


class Gadget2(Application):
    """Run one caller-defined Gadget2 parameter and initial-condition bundle.

    The native profile stages a digest-verified multi-file bundle into one
    execution-owned directory and launches exactly one declared parameter file.
    The historical stock-case/container profile remains available for existing
    pipelines but is not advertised as an agent-facing scientific contract.
    """

    def _init(self) -> None:
        self.gadget2_bin: str | None = None

    def _configure_menu(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "input_bundle",
                "msg": (
                    "Digest-verified JARVIS input bundle containing one Gadget2 "
                    "parameter file and every referenced initial-condition or "
                    "support file. The bundle is copied into execution-owned "
                    "storage before launch."
                ),
                "type": str,
                "default": "",
                "input_binding": ConfigurationInputBinding(
                    kind="local_file", structure="regular_file"
                ).to_dict(),
            },
            {
                "name": "parameter_path",
                "msg": (
                    "Relative manifest member to execute; empty selects the "
                    "input-bundle entrypoint"
                ),
                "type": str,
                "default": "",
            },
            {
                "name": "out",
                "msg": (
                    "Output directory. Relative paths resolve under the JARVIS "
                    "package shared directory."
                ),
                "type": str,
                "default": "run",
            },
            {
                "name": "nprocs",
                "msg": "Total number of MPI ranks",
                "type": int,
                "default": 2,
            },
            {
                "name": "ppn",
                "msg": "MPI ranks per node",
                "type": int,
                "default": 2,
            },
            {
                "name": "gadget2_path",
                "msg": "Legacy source-tree path used by stock examples",
                "type": str,
                "default": None,
                "agent_visible": False,
            },
            {
                "name": "test_case",
                "msg": "Legacy bundled Gadget2 stock-case template",
                "type": str,
                "default": "gassphere",
                "choices": list(_LEGACY_TEST_CASES),
                "agent_visible": False,
            },
            {
                "name": "output",
                "msg": "Legacy stock-case output directory",
                "type": str,
                "default": None,
                "agent_visible": False,
            },
            {
                "name": "time_max",
                "msg": "Legacy stock-case maximum simulation time",
                "type": float,
                "default": 0.05,
                "agent_visible": False,
            },
            {
                "name": "buffer_size",
                "msg": "Legacy stock-case communication buffer size in MB",
                "type": float,
                "default": 15.0,
                "agent_visible": False,
            },
            {
                "name": "part_alloc_factor",
                "msg": "Legacy stock-case particle allocation factor",
                "type": float,
                "default": 1.5,
                "agent_visible": False,
            },
            {
                "name": "tree_alloc_factor",
                "msg": "Legacy stock-case tree allocation factor",
                "type": float,
                "default": 0.9,
                "agent_visible": False,
            },
            {
                "name": "exec_mode",
                "msg": "Legacy multi-node launch mode",
                "type": str,
                "default": "mpi",
                "choices": ["mpi", "pssh"],
                "agent_visible": False,
            },
        ]

    def _deployment_contract(self) -> PackageDeploymentContract:
        """Describe native Gadget2 runtime, input, and completion semantics."""

        environment = self._deployment_environment()
        program = self._discover_executable(environment)
        probe = probe_program(
            program or "Gadget2",
            environment=environment,
            accepted_return_codes=(0, 1),
            timeout_seconds=5,
        )
        capabilities = (
            ("gadget2", "mpi_execution", "particle_simulation")
            if program is not None and probe.status.usable is True
            else ()
        )
        runtime = RuntimeRequirement(
            requirement_id="gadget2",
            description="MPI-enabled Gadget2 runtime available through PATH",
            required_capabilities=(
                "gadget2",
                "mpi_execution",
                "particle_simulation",
            ),
            available_capabilities=capabilities,
            status=probe.status,
        )
        return PackageDeploymentContract(
            package="builtin.gadget2",
            execution_profiles=(
                ExecutionProfile(
                    name="input_bundle",
                    execution_kind="batch",
                    when=(ConfigurationCondition("input_bundle", "is_not_empty"),),
                    runtime_requirements=("gadget2",),
                    readiness=ReadinessContract(
                        mechanism="process_exit", condition="successful_exit"
                    ),
                    description=(
                        "Run one caller-supplied Gadget2 parameter and "
                        "initial-condition bundle under MPI."
                    ),
                ),
            ),
            runtime_requirements=(runtime,),
            configuration_rules=(
                ConfigurationRule(
                    when=(ConfigurationCondition("input_bundle", "is_empty"),),
                    requires=(ConfigurationCondition("parameter_path", "is_empty"),),
                    description="parameter_path is valid only with input_bundle.",
                ),
            ),
        )

    def _build_phase(self) -> tuple[str, str] | None:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Build the retained legacy container image when requested."""

        if self.config.get("deploy_mode") != "container":
            return None
        return self._read_build_script("build.sh", {}), "default"

    def _build_deploy_phase(self) -> tuple[str, str] | None:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Create the retained legacy container deployment image."""

        if self.config.get("deploy_mode") != "container":
            return None
        base = getattr(self.pipeline, "container_base", "ubuntu:22.04")
        content = self._read_dockerfile(
            "Dockerfile.deploy",
            {
                "BUILD_IMAGE": self.build_image_name(),
                "DEPLOY_BASE": base,
            },
        )
        return content, ""

    def _configure(self, **kwargs: Any) -> None:
        """Validate the selected profile and prepare package-owned storage."""

        super()._configure(**kwargs)
        if self._uses_native_bundle():
            self._validate_native_configuration()
            if self.config.get("deploy_mode") == "default":
                self.gadget2_bin = self._discover_executable(
                    self._deployment_environment()
                )
            self._ensure_output_dir()
            return
        self._configure_legacy()

    def _uses_native_bundle(self) -> bool:
        """Return whether the explicit scientific input profile is selected."""

        return self.config.get("input_bundle") not in (None, "")

    @staticmethod
    def _discover_executable(environment: dict[str, str]) -> str | None:
        """Return the first supported Gadget2 executable available through PATH."""

        for candidate in _EXECUTABLE_CANDIDATES:
            if shutil.which(candidate, path=environment.get("PATH")) is not None:
                return candidate
        return None

    def _validate_native_configuration(self) -> None:
        """Reject missing bundle, unsafe parameter selection, and invalid MPI sizes."""

        bundle = self.config.get("input_bundle")
        if not isinstance(bundle, str) or not bundle:
            raise ValueError("native Gadget2 requires input_bundle")
        parameter_path = self.config.get("parameter_path")
        if parameter_path not in (None, "") and not isinstance(parameter_path, str):
            raise TypeError("parameter_path must be a relative path string")
        values: dict[str, int] = {}
        for name in ("nprocs", "ppn"):
            value = self.config.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
            values[name] = value
        if values["ppn"] > values["nprocs"]:
            raise ValueError("ppn cannot exceed nprocs")
        if self.config.get("deploy_mode") != "default":
            raise ValueError("input_bundle requires native deploy_mode=default")

    def _output_dir(self) -> Path:
        """Return the normalized package-owned output root."""

        if self._uses_native_bundle():
            value = self.config.get("out")
            return self.resolve_shared_path(value, field="out", default="run")
        value = self.config.get("output")
        if value in (None, "") and self.config.get("out") not in (None, "", "run"):
            value = self.config.get("out")
        return self.resolve_shared_path(value, field="output", default="gadget2_out")

    def _node_exec_info(self, **kwargs: Any) -> LocalExecInfo | PsshExecInfo:
        """Return local or parallel-SSH execution according to the hostfile."""

        hostfile = self.hostfile
        if hostfile is None or hostfile.is_local():
            return LocalExecInfo(**kwargs)
        return PsshExecInfo(hostfile=hostfile, **kwargs)

    def _ensure_output_dir(self) -> None:
        """Create the exact output root on every participating host."""

        output_dir = str(self._output_dir())
        result = Mkdir(output_dir, self._node_exec_info(env=self.env)).run()
        failures = {host: code for host, code in result.exit_code.items() if code != 0}
        if failures:
            raise RuntimeError(
                f"Failed to create Gadget2 output directory {output_dir}: {failures}"
            )

    @staticmethod
    def _resolve_bundle_parameter(
        bundle: MaterializedInputBundle, requested: object
    ) -> Path:
        """Resolve one manifest-declared parameter file without path inference."""

        if requested in (None, ""):
            selected = bundle.entrypoint
        else:
            if not isinstance(requested, str) or "\\" in requested:
                raise ValueError("parameter_path must be a confined manifest path")
            relative = PurePosixPath(requested)
            if relative.is_absolute() or any(
                part in {"", ".", ".."} for part in relative.parts
            ):
                raise ValueError("parameter_path must be a confined manifest path")
            declared = {item.path for item in bundle.manifest.files}
            if relative.as_posix() not in declared:
                raise ValueError("parameter_path is not declared by the input bundle")
            selected = bundle.root / relative
        if selected.suffix.casefold() != ".param":
            raise ValueError("selected Gadget2 parameter_path must end in .param")
        if selected.is_symlink() or not selected.is_file():
            raise ValueError("selected Gadget2 parameter_path is not a regular file")
        return selected

    def _prepare_native_input(self) -> Path:
        """Verify and copy one complete simulation bundle into owned storage."""

        self._validate_native_configuration()
        self._ensure_output_dir()
        configured = self.config.get("input_bundle")
        assert isinstance(configured, str) and configured
        if self.shared_dir is None:
            raise RuntimeError("Gadget2 input bundles require shared storage")
        bundle = extract_input_bundle(
            configured, Path(self.shared_dir) / "input-bundles"
        )
        selected = self._resolve_bundle_parameter(
            bundle, self.config.get("parameter_path")
        )
        relative = selected.relative_to(bundle.root)
        stage_input_bundle(bundle, self._output_dir())
        staged = self._output_dir() / relative
        if staged.is_symlink() or not staged.is_file():
            raise RuntimeError("staged Gadget2 parameter file is unavailable")
        return staged

    def _start_native(self) -> None:
        """Launch the staged scientific input and propagate every rank failure."""

        parameter = self._prepare_native_input()
        executable = self.gadget2_bin or self._discover_executable(self.mod_env)
        if executable is None:
            raise RuntimeError("Gadget2 executable is unavailable through PATH")
        command = " ".join((shlex.quote(executable), shlex.quote(parameter.name)))
        result = Exec(
            command,
            MpiExecInfo(
                nprocs=self.config["nprocs"],
                ppn=self.config["ppn"],
                hostfile=self.hostfile,
                env=self.mod_env,
                cwd=str(parameter.parent),
                line_callback=self.runtime_line_callback(),
            ),
        ).run()
        failures = {host: code for host, code in result.exit_code.items() if code != 0}
        if failures:
            raise RuntimeError(f"Gadget2 execution failed: {failures}")

    def _configure_legacy(self) -> None:
        """Preserve the historical stock-case/container configuration."""

        config = cast(dict[str, Any], self.config)
        if config.get("parameter_path") not in (None, ""):
            raise ValueError("parameter_path requires input_bundle")
        if config.get("deploy_mode") == "container":
            gadget2_path = _GADGET2_PATH_CONTAINER
        else:
            configured_path = (
                config.get("gadget2_path")
                or self.env.get("GADGET2_PATH")
                or os.environ.get("GADGET2_PATH")
            )
            if not isinstance(configured_path, str) or not configured_path:
                raise RuntimeError(
                    "GADGET2_PATH is not set. Set gadget2_path for the legacy "
                    "stock profile or provide input_bundle for the native profile."
                )
            gadget2_path = configured_path
            if not os.path.isdir(gadget2_path):
                raise RuntimeError(
                    f"gadget2_path does not exist on the local node: {gadget2_path}"
                )
        config["gadget2_path"] = gadget2_path
        self.setenv("GADGET2_PATH", gadget2_path)
        self._ensure_output_dir()
        test_case = config.get("test_case", "gassphere")
        if not isinstance(test_case, str) or test_case not in _LEGACY_TEST_CASES:
            raise ValueError(f"unsupported legacy Gadget2 test_case: {test_case!r}")
        package_dir = self.pkg_dir
        if not isinstance(package_dir, str) or not package_dir:
            raise RuntimeError("Gadget2 package directory is unavailable")
        parameter_input = os.path.join(package_dir, "paramfiles", f"{test_case}.param")
        parameter_output = self._output_dir() / f"{test_case}.param"
        self.copy_template_file(
            parameter_input,
            str(parameter_output),
            replacements={
                "REPO_DIR": gadget2_path,
                "OUTPUT_DIR": str(self._output_dir()),
                "TIME_MAX": config["time_max"],
                "BUFFER_SIZE": config["buffer_size"],
                "PART_ALLOC_FACTOR": config["part_alloc_factor"],
                "TREE_ALLOC_FACTOR": config["tree_alloc_factor"],
            },
        )
        config["paramfile"] = str(parameter_output)
        binary = os.path.join(gadget2_path, _BINARY_REL_PATH)
        if config.get("deploy_mode") != "container" and not os.path.isfile(binary):
            raise RuntimeError(f"Gadget2 binary not found at {binary}")
        config["binary"] = binary

    def _use_remote(self) -> bool:
        """Return whether the configured hostfile names remote nodes."""

        return self.hostfile is not None and not self.hostfile.is_local()

    def _container_kwargs(self) -> dict[str, Any]:
        """Return legacy container launch arguments when applicable."""

        if self.config.get("deploy_mode") != "container":
            return {}
        return {
            "container": self._container_engine,
            "container_image": self.deploy_image_name(),
            "shared_dir": self.shared_dir,
            "private_dir": self.private_dir,
        }

    def _legacy_exec_info(self, cwd: str) -> LocalExecInfo | MpiExecInfo | PsshExecInfo:
        """Return the historical stock-case execution mode."""

        kwargs: dict[str, Any] = {
            "env": self.mod_env,
            "cwd": cwd,
            **self._container_kwargs(),
        }
        if self.config.get("exec_mode", "mpi") == "mpi":
            return MpiExecInfo(
                nprocs=self.config["nprocs"],
                ppn=self.config["ppn"],
                hostfile=self.hostfile if self._use_remote() else None,
                port=self.ssh_port,
                **kwargs,
            )
        if self._use_remote():
            return PsshExecInfo(hostfile=self.hostfile, **kwargs)
        return LocalExecInfo(**kwargs)

    def _start_legacy(self) -> None:
        """Run the retained stock-case profile."""

        config = cast(dict[str, Any], self.config)
        cwd = str(self._output_dir())
        parameter_file = config.get("paramfile")
        binary = config.get("binary")
        if not isinstance(parameter_file, str) or not isinstance(binary, str):
            raise RuntimeError("legacy Gadget2 launch configuration is incomplete")
        parameter_name = os.path.basename(parameter_file)
        inner = " ".join(
            (
                "cd",
                shlex.quote(cwd),
                "&&",
                shlex.quote(binary),
                shlex.quote(parameter_name),
            )
        )
        command = f"bash -c {shlex.quote(inner)}"
        result = Exec(command, self._legacy_exec_info(cwd)).run()
        failures = {host: code for host, code in result.exit_code.items() if code != 0}
        if failures:
            raise RuntimeError(f"Gadget2 execution failed: {failures}")

    def start(self) -> None:
        """Run the explicit native bundle or retained legacy stock profile."""

        if self._uses_native_bundle():
            self._start_native()
        elif "paramfile" in self.config and "binary" in self.config:
            self._start_legacy()
        else:
            self._validate_native_configuration()

    def stop(self) -> None:
        """Do nothing because Gadget2 runs to process completion."""

    def clean(self) -> None:
        """Remove only the exact configured Gadget2 output directory."""

        output_dir = self._output_dir()
        if output_dir == Path(output_dir.anchor):
            raise ValueError("refusing to clean a filesystem root as Gadget2 output")
        result = Rm(
            str(output_dir),
            self._node_exec_info(env=self.env),
            recursive=True,
        ).run()
        failures = {host: code for host, code in result.exit_code.items() if code != 0}
        if failures:
            raise RuntimeError(
                f"Failed to clean Gadget2 output {output_dir}: {failures}"
            )

    def _get_stat(self, stat_dict: dict[str, Any]) -> None:
        """Report the package runtime through the legacy statistics surface."""

        stat_dict[f"{self.pkg_id}.runtime"] = getattr(self, "start_time", None)
