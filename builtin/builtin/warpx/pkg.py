"""Launch native or containerized WarpX particle-in-cell simulations."""

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

_EXECUTABLE_CANDIDATES = (
    "warpx.3d.MPI.NOACC.DP.PDP",
    "warpx.3d.MPI.CUDA.SP",
    "warpx.3d",
    "warpx",
)
_MAX_INPUT_BYTES = 16 * 1024 * 1024


class Warpx(Application):
    """Run one WarpX simulation from an example or caller-owned input.

    Caller inputs are copied into a package-owned working directory before
    launch. Multi-file studies use a digest-verified JARVIS input bundle and
    may select any manifest-declared member as the WarpX input file. JARVIS
    owns MPI launch, environment interception, process completion, and output
    artifact reporting.
    """

    def _init(self) -> None:
        self.warpx_bin: str | None = None

    def _configure_menu(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "nprocs",
                "msg": "Number of MPI processes",
                "type": int,
                "default": 2,
            },
            {
                "name": "ppn",
                "msg": "Processes per node",
                "type": int,
                "default": 2,
            },
            {
                "name": "inputs",
                "msg": (
                    "Optional single WarpX inputs file. JARVIS copies it into "
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
                    "Optional digest-verified multi-file JARVIS package input "
                    "bundle. All manifest files are staged into the execution-"
                    "owned output directory."
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
                "name": "example",
                "msg": "Installed WarpX example to run when no caller input is supplied",
                "type": str,
                "choices": ["laser_acceleration", "uniform_plasma", "custom"],
                "default": "laser_acceleration",
            },
            {
                "name": "override_input_parameters",
                "msg": (
                    "Apply max_step, n_cell, out, and plot_int command-line "
                    "overrides to caller-supplied inputs"
                ),
                "type": bool,
                "default": False,
            },
            {
                "name": "max_step",
                "msg": "Total number of time steps for examples or explicit overrides",
                "type": int,
                "default": 50,
            },
            {
                "name": "n_cell",
                "msg": "Base grid cells as three positive integers: nx ny nz",
                "type": str,
                "default": "64 64 128",
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
                "name": "plot_int",
                "msg": "Plot output interval (-1 to disable)",
                "type": int,
                "default": 10,
            },
            {
                "name": "cuda_arch",
                "msg": "CUDA architecture code (80=A100, 90=H100, 70=V100)",
                "type": int,
                "default": 80,
            },
            {
                "name": "base_image",
                "msg": "Base container image for a package-owned container build",
                "type": str,
                "default": "sci-hpc-base",
            },
            {
                "name": "use_gpu",
                "msg": "Build and launch the CUDA/GPU container profile",
                "type": bool,
                "default": False,
            },
        ]

    def _deployment_contract(self) -> PackageDeploymentContract:
        """Describe WarpX runtime, input, and successful-exit semantics."""

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
                    arguments=("--help",),
                    accepted_return_codes=(0, 1),
                )
                status = probe.status
                capabilities = (
                    ("mpi_execution", "particle_in_cell", "warpx_3d")
                    if status.usable is True
                    else ()
                )
        runtime = RuntimeRequirement(
            requirement_id="warpx_3d",
            description="Three-dimensional WarpX runtime available through PATH",
            required_capabilities=("mpi_execution", "particle_in_cell", "warpx_3d"),
            available_capabilities=capabilities,
            status=status,
            provider_resolutions=(
                ProviderResolution(
                    provider="spack", query_kind="spec", query_value="warpx"
                ),
            ),
        )
        completed = ReadinessContract(
            mechanism="process_exit", condition="successful_exit"
        )
        return PackageDeploymentContract(
            package="builtin.warpx",
            execution_profiles=(
                ExecutionProfile(
                    name="installed_example",
                    execution_kind="batch",
                    when=(
                        ConfigurationCondition("inputs", "is_empty"),
                        ConfigurationCondition("input_bundle", "is_empty"),
                    ),
                    runtime_requirements=("warpx_3d",),
                    readiness=completed,
                    description="Run one installed WarpX example with explicit bounds.",
                ),
                ExecutionProfile(
                    name="input_file",
                    execution_kind="batch",
                    when=(ConfigurationCondition("inputs", "is_not_empty"),),
                    runtime_requirements=("warpx_3d",),
                    readiness=completed,
                    description=(
                        "Run one caller-supplied WarpX input copied into an "
                        "execution-owned working directory."
                    ),
                ),
                ExecutionProfile(
                    name="input_bundle",
                    execution_kind="batch",
                    when=(ConfigurationCondition("input_bundle", "is_not_empty"),),
                    runtime_requirements=("warpx_3d",),
                    readiness=completed,
                    description=(
                        "Run one manifest-selected input from a digest-verified "
                        "multi-file bundle in an execution-owned working directory."
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
        base = self.config.get("base_image", "sci-hpc-base")
        use_gpu = self.config.get("use_gpu", False) or "sci-hpc" in str(base)
        cuda_arch = self.config.get("cuda_arch", 80)
        if use_gpu:
            content = self._read_build_script(
                "build.sh", {"BASE_IMAGE": base, "CUDA_ARCH": cuda_arch}
            )
            return content, f"3d-cuda-{cuda_arch}"
        content = self._read_build_script("cpu/build.sh", {"BASE_IMAGE": base})
        return content, "3d-cpu"

    def _build_deploy_phase(self) -> tuple[str, str] | None:  # pyright: ignore[reportIncompatibleMethodOverride]
        if self.config.get("deploy_mode") != "container":
            return None
        base = self.config.get("base_image", "sci-hpc-base")
        use_gpu = self.config.get("use_gpu", False) or "sci-hpc" in str(base)
        deploy_file = "Dockerfile.deploy" if use_gpu else "cpu/Dockerfile.deploy"
        deploy_base = (
            "nvidia/cuda:12.6.0-runtime-ubuntu24.04" if use_gpu else "ubuntu:24.04"
        )
        suffix = str(getattr(self, "_build_suffix", ""))
        content = self._read_dockerfile(
            deploy_file,
            {"BUILD_IMAGE": self.build_image_name(), "DEPLOY_BASE": deploy_base},
        )
        return content, suffix

    def _configure(self, **kwargs: Any) -> None:
        """Validate the selected WarpX profile and prepare owned output."""

        super()._configure(**kwargs)
        self._validate_configuration()
        if self.config.get("deploy_mode") == "default":
            self.warpx_bin = self._discover_executable(self._deployment_environment())
            self._ensure_output_dir()

    def _validate_configuration(self) -> None:
        """Reject ambiguous inputs and invalid MPI or override settings."""

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
        input_path = self.config.get("input_path")
        if input_path not in (None, "") and not isinstance(input_path, str):
            raise TypeError("input_path must be a relative path string")
        if bundle in (None, "") and input_path not in (None, ""):
            raise ValueError("input_path requires input_bundle")
        if inputs in (None, "") and bundle in (None, ""):
            if self.config.get("example") == "custom":
                raise ValueError(
                    "custom WarpX execution requires inputs or input_bundle"
                )
            self._validate_overrides()
        elif self.config.get("override_input_parameters"):
            self._validate_overrides()

    def _validate_overrides(self) -> None:
        """Validate command-line overrides without interpreting scientific input."""

        max_step = self.config.get("max_step")
        if isinstance(max_step, bool) or not isinstance(max_step, int) or max_step <= 0:
            raise ValueError("max_step must be a positive integer")
        plot_int = self.config.get("plot_int")
        if (
            isinstance(plot_int, bool)
            or not isinstance(plot_int, int)
            or plot_int == 0
            or plot_int < -1
        ):
            raise ValueError("plot_int must be -1 or a positive integer")
        n_cell = self.config.get("n_cell")
        if not isinstance(n_cell, str):
            raise TypeError("n_cell must contain three positive integers")
        try:
            cells = tuple(int(value) for value in n_cell.split())
        except ValueError as exc:
            raise ValueError("n_cell must contain three positive integers") from exc
        if len(cells) != 3 or any(value <= 0 for value in cells):
            raise ValueError("n_cell must contain three positive integers")

    @staticmethod
    def _discover_executable(environment: dict[str, str]) -> str | None:
        """Return the first supported WarpX program name available through PATH."""

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
                f"Failed to create WarpX output directory {output_dir}: {failures}"
            )

    @staticmethod
    def _resolve_bundle_input(
        bundle: MaterializedInputBundle, requested: object
    ) -> Path:
        """Resolve one manifest-declared WarpX input without path inference."""

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
            raise ValueError("WarpX staged input already exists")
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o600)
        return destination

    def _prepare_input(self) -> tuple[Path, Path, bool]:
        """Materialize the selected input and return input, cwd, and caller flag."""

        self._validate_configuration()
        self._ensure_output_dir()
        output_dir = self._output_dir()
        configured_bundle = self.config.get("input_bundle")
        if configured_bundle not in (None, ""):
            assert isinstance(configured_bundle, str)
            if self.shared_dir is None:
                raise RuntimeError("WarpX input bundles require package shared storage")
            bundle = extract_input_bundle(
                configured_bundle, Path(self.shared_dir) / "input-bundles"
            )
            selected = self._resolve_bundle_input(bundle, self.config.get("input_path"))
            relative = selected.relative_to(bundle.root)
            stage_input_bundle(bundle, output_dir)
            staged = output_dir / relative
            return staged, staged.parent, True

        configured_input = self.config.get("inputs")
        if configured_input not in (None, ""):
            assert isinstance(configured_input, str)
            staged = self._stage_single_input(configured_input, output_dir)
            return staged, output_dir, True

        example = str(self.config.get("example") or "laser_acceleration")
        example_dir = Path("/opt/warpx/Examples/Physics_applications") / example
        return example_dir / "inputs_base_3d", example_dir, False

    def _runtime_arguments(self, *, caller_input: bool) -> list[str]:
        """Return output-only or explicitly authorized scientific overrides."""

        if caller_input and not self.config.get("override_input_parameters"):
            return []
        output_dir = self._output_dir()
        return [
            f"max_step={self.config['max_step']}",
            f"amr.n_cell={self.config['n_cell']}",
            f"amr.plot_file={output_dir / 'plt'}",
            f"amr.plot_int={self.config['plot_int']}",
        ]

    def start(self) -> None:
        """Launch WarpX and fail the pipeline on any rank failure."""

        input_path, cwd, caller_input = self._prepare_input()
        if self.config.get("deploy_mode") == "container":
            executable = "/opt/warpx/build/bin/warpx.3d"
            exec_kwargs: dict[str, Any] = {
                "port": self.ssh_port,
                "container": self._container_engine,
                "container_image": self.deploy_image_name(),
                "shared_dir": self.shared_dir,
                "private_dir": self.private_dir,
                "gpu": self.config.get("use_gpu", False),
            }
        else:
            executable = self.warpx_bin or self._discover_executable(self.mod_env)
            if executable is None:
                raise RuntimeError("WarpX executable is unavailable through PATH")
            exec_kwargs = {}
        command = " ".join(
            (
                shlex.quote(executable),
                shlex.quote(str(input_path)),
                *(
                    shlex.quote(value)
                    for value in self._runtime_arguments(caller_input=caller_input)
                ),
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
            raise RuntimeError(f"WarpX execution failed: {failures}")

    def stop(self) -> None:
        """Do nothing because WarpX runs to process completion."""

    def clean(self) -> None:
        """Remove only the exact configured WarpX output directory."""

        output_dir = self._output_dir()
        if output_dir == Path(output_dir.anchor):
            raise ValueError("refusing to clean a filesystem root as WarpX output")
        result = Rm(
            str(output_dir),
            self._node_exec_info(env=self.env),
            recursive=True,
        ).run()
        failures = {host: code for host, code in result.exit_code.items() if code != 0}
        if failures:
            raise RuntimeError(f"Failed to clean WarpX output {output_dir}: {failures}")
