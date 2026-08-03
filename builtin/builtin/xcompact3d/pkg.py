"""Launch native or containerized Xcompact3D simulations."""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from jarvis_cd.core.pkg import Application
from jarvis_cd.deployment import (
    ConfigurationCondition,
    ConfigurationInputBinding,
    ConfigurationRule,
    ExecutionProfile,
    PackageDeploymentContract,
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

_EXECUTABLE_CANDIDATES = ("xcompact3d", "incompact3d")
_MAX_INPUT_BYTES = 64 * 1024 * 1024


class Xcompact3d(Application):
    """Run one caller-defined Xcompact3D simulation.

    JARVIS copies a single input or a digest-verified multi-file input bundle
    into package-owned storage, launches the selected input under MPI, records
    the solver stream in that workspace, and exposes typed output artifacts.
    Scientific study composition remains a pipeline or benchmark concern.
    """

    def _init(self) -> None:
        self.xcompact3d_bin: str | None = None

    def _configure_menu(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "nprocs",
                "msg": "Number of MPI processes",
                "type": int,
                "default": 4,
            },
            {
                "name": "ppn",
                "msg": "MPI processes per node",
                "type": int,
                "default": 4,
            },
            {
                "name": "inputs",
                "msg": (
                    "Single Xcompact3D .i3d input file. JARVIS copies it into "
                    "the execution-owned output directory before launch."
                ),
                "type": str,
                "default": "",
                "input_binding": ConfigurationInputBinding(
                    kind="local_file", structure="regular_file"
                ).to_dict(),
            },
            {
                "name": "input_bundle",
                "msg": (
                    "Digest-verified multi-file JARVIS package input bundle. "
                    "All manifest files are staged into the execution-owned "
                    "output directory."
                ),
                "type": str,
                "default": "",
                "input_binding": ConfigurationInputBinding(
                    kind="local_file", structure="regular_file"
                ).to_dict(),
            },
            {
                "name": "input_path",
                "msg": (
                    "Relative manifest member to execute from input_bundle; "
                    "empty selects the manifest entrypoint"
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
                "name": "base_image",
                "msg": "Base image for a package-owned container build",
                "type": str,
                "default": "ubuntu:24.04",
            },
        ]

    def _deployment_contract(self) -> PackageDeploymentContract:
        """Describe Xcompact3D runtime, input, and completion semantics."""

        if self.config.get("deploy_mode") == "container":
            status = RuntimeStatus("unknown", "container_runtime_not_probed")
            capabilities: tuple[str, ...] = ()
        else:
            program = self._discover_executable(self._deployment_environment())
            if program is None:
                status = RuntimeStatus("unavailable", "software_not_found")
                capabilities = ()
            else:
                probe = probe_program(
                    program,
                    environment=self._deployment_environment(),
                    arguments=("--version",),
                    accepted_return_codes=(0, 1, 2),
                    timeout_seconds=5,
                )
                status = probe.status
                capabilities = (
                    ("mpi_execution", "incompressible_flow", "xcompact3d")
                    if status.usable is True
                    else ()
                )
        runtime = RuntimeRequirement(
            requirement_id="xcompact3d",
            description="MPI-enabled Xcompact3D runtime available through PATH",
            required_capabilities=(
                "mpi_execution",
                "incompressible_flow",
                "xcompact3d",
            ),
            available_capabilities=capabilities,
            status=status,
        )
        completed = ReadinessContract(
            mechanism="process_exit", condition="successful_exit"
        )
        return PackageDeploymentContract(
            package="builtin.xcompact3d",
            execution_profiles=(
                ExecutionProfile(
                    name="input_file",
                    execution_kind="batch",
                    when=(ConfigurationCondition("inputs", "is_not_empty"),),
                    runtime_requirements=("xcompact3d",),
                    readiness=completed,
                    description=(
                        "Run one caller-supplied Xcompact3D input copied into an "
                        "execution-owned working directory."
                    ),
                ),
                ExecutionProfile(
                    name="input_bundle",
                    execution_kind="batch",
                    when=(ConfigurationCondition("input_bundle", "is_not_empty"),),
                    runtime_requirements=("xcompact3d",),
                    readiness=completed,
                    description=(
                        "Run one selected input from a digest-verified multi-file "
                        "bundle in an execution-owned working directory."
                    ),
                ),
            ),
            runtime_requirements=(runtime,),
            configuration_rules=(
                ConfigurationRule(
                    when=(ConfigurationCondition("input_bundle", "is_empty"),),
                    requires=(ConfigurationCondition("input_path", "is_empty"),),
                    description="input_path is valid only with input_bundle.",
                ),
            ),
        )

    def _build_phase(self) -> tuple[str, str] | None:  # pyright: ignore[reportIncompatibleMethodOverride]
        if self.config.get("deploy_mode") != "container":
            return None
        base = self.config.get("base_image", "ubuntu:24.04")
        content = self._read_build_script("build.sh", {"BASE_IMAGE": base})
        return content, "adios2"

    def _build_deploy_phase(self) -> tuple[str, str] | None:  # pyright: ignore[reportIncompatibleMethodOverride]
        if self.config.get("deploy_mode") != "container":
            return None
        content = self._read_dockerfile(
            "Dockerfile.deploy",
            {
                "BUILD_IMAGE": self.build_image_name(),
                "DEPLOY_BASE": "ubuntu:24.04",
            },
        )
        return content, "adios2"

    def _configure(self, **kwargs: Any) -> None:
        """Validate the selected input profile and prepare owned output."""

        super()._configure(**kwargs)
        self._validate_configuration()
        if self.config.get("deploy_mode") == "default":
            self.xcompact3d_bin = self._discover_executable(
                self._deployment_environment()
            )
            self._ensure_output_dir()

    def _validate_configuration(self) -> None:
        """Reject absent, ambiguous, or malformed input and MPI settings."""

        for name in ("nprocs", "ppn"):
            value = self.config.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        inputs = self.config.get("inputs")
        bundle = self.config.get("input_bundle")
        if inputs not in (None, "") and bundle not in (None, ""):
            raise ValueError("inputs and input_bundle cannot be combined")
        for name, value in (("inputs", inputs), ("input_bundle", bundle)):
            if value not in (None, "") and not isinstance(value, str):
                raise TypeError(f"{name} must be a path string")
        if inputs in (None, "") and bundle in (None, ""):
            raise ValueError("Xcompact3D requires inputs or input_bundle")
        input_path = self.config.get("input_path")
        if input_path not in (None, "") and not isinstance(input_path, str):
            raise TypeError("input_path must be a relative path string")
        if bundle in (None, "") and input_path not in (None, ""):
            raise ValueError("input_path requires input_bundle")

    @staticmethod
    def _discover_executable(environment: dict[str, str]) -> str | None:
        """Return the first supported executable available through PATH."""

        for candidate in _EXECUTABLE_CANDIDATES:
            if shutil.which(candidate, path=environment.get("PATH")) is not None:
                return candidate
        return None

    def _output_dir(self) -> Path:
        """Return the normalized package-owned output root."""

        return self.resolve_shared_path(
            self.config.get("out"), field="out", default="run"
        )

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
                f"Failed to create Xcompact3D output directory {output_dir}: {failures}"
            )

    @staticmethod
    def _resolve_bundle_input(
        bundle: MaterializedInputBundle, requested: object
    ) -> Path:
        """Resolve one manifest-declared input without path inference."""

        if requested in (None, ""):
            return bundle.entrypoint
        if not isinstance(requested, str) or "\\" in requested:
            raise ValueError("input_path must be a confined manifest path")
        relative = PurePosixPath(requested)
        if relative.is_absolute() or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise ValueError("input_path must be a confined manifest path")
        declared = {item.path for item in bundle.manifest.files}
        normalized = relative.as_posix()
        if normalized not in declared:
            raise ValueError("input_path is not declared by the input bundle")
        selected = bundle.root / relative
        if selected.is_symlink() or not selected.is_file():
            raise ValueError("input_path is not a verified regular file")
        return selected

    def _stage_single_input(self, configured: str, output_dir: Path) -> Path:
        """Copy one bounded regular input into package-owned storage."""

        source = Path(
            os.path.abspath(os.path.expandvars(os.path.expanduser(configured)))
        )
        try:
            status = source.lstat()
        except OSError as exc:
            raise ValueError("inputs is not a readable regular file") from exc
        if (
            source.is_symlink()
            or not source.is_file()
            or status.st_size <= 0
            or status.st_size > _MAX_INPUT_BYTES
        ):
            raise ValueError("inputs is not a bounded regular file")
        destination = output_dir / source.name
        if destination.exists() or destination.is_symlink():
            raise ValueError("Xcompact3D staged input already exists")
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o600)
        return destination

    def _prepare_input(self) -> tuple[Path, Path]:
        """Materialize the selected input and return its path and working directory."""

        self._validate_configuration()
        self._ensure_output_dir()
        output_dir = self._output_dir()
        configured_bundle = self.config.get("input_bundle")
        if configured_bundle not in (None, ""):
            assert isinstance(configured_bundle, str)
            if self.shared_dir is None:
                raise RuntimeError("Xcompact3D input bundles require shared storage")
            bundle = extract_input_bundle(
                configured_bundle, Path(self.shared_dir) / "input-bundles"
            )
            selected = self._resolve_bundle_input(bundle, self.config.get("input_path"))
            relative = selected.relative_to(bundle.root)
            stage_input_bundle(bundle, output_dir)
            staged = output_dir / relative
            return staged, staged.parent

        configured_input = self.config.get("inputs")
        assert isinstance(configured_input, str) and configured_input
        staged = self._stage_single_input(configured_input, output_dir)
        return staged, output_dir

    def start(self) -> None:
        """Launch Xcompact3D and fail the pipeline on any rank failure."""

        input_path, cwd = self._prepare_input()
        if self.config.get("deploy_mode") == "container":
            executable = "xcompact3d"
            exec_kwargs: dict[str, Any] = {
                "port": self.ssh_port,
                "container": self._container_engine,
                "container_image": self.deploy_image_name(),
                "shared_dir": self.shared_dir,
                "private_dir": self.private_dir,
            }
        else:
            executable = self.xcompact3d_bin or self._discover_executable(self.mod_env)
            if executable is None:
                raise RuntimeError("Xcompact3D executable is unavailable through PATH")
            exec_kwargs = {}
        log_path = cwd / "xcompact3d.log"
        command = " ".join(
            (
                shlex.quote(executable),
                shlex.quote(str(input_path)),
                ">",
                shlex.quote(str(log_path)),
                "2>&1",
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
                **exec_kwargs,
            ),
        ).run()
        failures = {host: code for host, code in result.exit_code.items() if code != 0}
        if failures:
            raise RuntimeError(f"Xcompact3D execution failed: {failures}")

    def stop(self) -> None:
        """Do nothing because Xcompact3D runs to process completion."""

    def clean(self) -> None:
        """Remove only the exact configured Xcompact3D output directory."""

        output_dir = self._output_dir()
        if output_dir == Path(output_dir.anchor):
            raise ValueError("refusing to clean a filesystem root as Xcompact3D output")
        result = Rm(
            str(output_dir),
            self._node_exec_info(env=self.env),
            recursive=True,
        ).run()
        failures = {host: code for host, code in result.exit_code.items() if code != 0}
        if failures:
            raise RuntimeError(
                f"Failed to clean Xcompact3D output {output_dir}: {failures}"
            )
