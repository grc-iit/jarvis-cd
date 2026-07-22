"""
This module provides classes and methods to launch the LAMMPS application.
LAMMPS (Large-scale Atomic/Molecular Massively Parallel Simulator) is a
classical molecular-dynamics code from Sandia National Laboratories.
"""

import hashlib
import os
import shlex
import tempfile
from pathlib import Path
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
from jarvis_cd.shell import Exec, LocalExecInfo, MpiExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm


class Lammps(Application):
    """
    Merged LAMMPS class supporting both default (bare-metal) and container deployment.

    Set deploy_mode='container' to build and run LAMMPS inside a Docker/Podman/Apptainer
    container with Kokkos CUDA.  Set deploy_mode='default' to use a
    system-installed lmp binary via MPI.
    """

    def _init(self) -> None:
        pass

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
                "msg": "Processes per node",
                "type": int,
                "default": 4,
            },
            {
                "name": "script",
                "msg": (
                    "Optional native LAMMPS input script. In reduced Lennard-Jones "
                    "units, `lattice fcc <density>` sets the FCC number density and "
                    "`region ... units lattice` expresses unit-cell counts; no "
                    "separate box rescaling is required. Every created atom type "
                    "must receive a positive mass before velocity or integration "
                    "commands; for the standard reduced Lennard-Jones single-type "
                    "case, use `mass 1 1.0`. Empty selects the package-owned bounded "
                    "Lennard-Jones workload."
                ),
                "type": str,
                "default": "",
                "input_binding": ConfigurationInputBinding(
                    kind="local_file",
                    structure="regular_file",
                ).to_dict(),
            },
            {
                "name": "cuda_arch",
                "msg": "CUDA architecture code (80=A100, 90=H100, 70=V100)",
                "type": int,
                "default": 80,
            },
            {
                "name": "base_image",
                "msg": "Base Docker image for build container",
                "type": str,
                "default": "sci-hpc-base",
            },
            {
                "name": "out",
                "msg": (
                    "Output directory for results. Relative paths resolve under "
                    "the JARVIS package shared directory; '.' uses that durable "
                    "execution-owned root."
                ),
                "type": str,
                "default": ".",
            },
            {
                "name": "kokkos_gpu",
                "msg": "Enable Kokkos GPU (CUDA) acceleration",
                "type": bool,
                "default": False,
            },
            {
                "name": "num_gpus",
                "msg": "Number of GPUs per node",
                "type": int,
                "default": 1,
            },
            {
                "name": "io_dump_interval",
                "msg": (
                    "Trajectory interval for the package-owned Lennard-Jones "
                    "workload selected when script is empty"
                ),
                "type": int,
                "default": 100,
            },
            {
                "name": "io_lattice_size",
                "msg": "FCC lattice size per dimension (4*N^3 atoms) for generated input",
                "type": int,
                "default": 80,
            },
            {
                "name": "io_run_steps",
                "msg": "Total steps for the package-owned generated workload",
                "type": int,
                "default": 5000,
            },
        ]

    def _deployment_contract(self) -> PackageDeploymentContract:
        """Describe portable LAMMPS deployment and completion semantics."""
        if self.config.get("deploy_mode") == "container":
            status = RuntimeStatus("unknown", "container_runtime_not_probed")
            capabilities: tuple[str, ...] = ()
        else:
            probe = probe_program(
                "lmp",
                environment=self._deployment_environment(),
                arguments=("-help",),
            )
            status = probe.status
            capabilities = (
                ("mpi_execution", "molecular_dynamics") if status.usable is True else ()
            )
        runtime = RuntimeRequirement(
            requirement_id="lammps",
            description="LAMMPS runtime able to execute molecular dynamics under MPI",
            required_capabilities=("mpi_execution", "molecular_dynamics"),
            available_capabilities=capabilities,
            status=status,
            provider_resolutions=(
                ProviderResolution(
                    provider="spack",
                    query_kind="spec",
                    query_value="lammps",
                ),
            ),
        )
        completed = ReadinessContract(
            mechanism="process_exit",
            condition="successful_exit",
        )
        return PackageDeploymentContract(
            package="builtin.lammps",
            execution_profiles=(
                ExecutionProfile(
                    name="generated_workload",
                    execution_kind="batch",
                    when=(ConfigurationCondition("script", "is_empty"),),
                    runtime_requirements=("lammps",),
                    readiness=completed,
                    description=(
                        "Built-in bounded Lennard-Jones smoke workload with a "
                        "package-generated input script and trajectory output."
                    ),
                ),
                ExecutionProfile(
                    name="input_script",
                    execution_kind="batch",
                    when=(ConfigurationCondition("script", "is_not_empty"),),
                    runtime_requirements=("lammps",),
                    readiness=completed,
                    description=(
                        "Caller-authored native LAMMPS input. For reduced-unit FCC "
                        "systems, express target number density directly with "
                        "`lattice fcc <density>` and cell counts with "
                        "`region ... units lattice`. Assign every created atom type "
                        "a positive mass before velocity or integration commands; "
                        "for the standard reduced Lennard-Jones single-type case, "
                        "use `mass 1 1.0`."
                    ),
                ),
            ),
            runtime_requirements=(runtime,),
            configuration_rules=(
                ConfigurationRule(
                    when=(ConfigurationCondition("script", "is_empty"),),
                    requires=(
                        ConfigurationCondition("io_dump_interval", "greater_than", 0),
                        ConfigurationCondition("io_lattice_size", "greater_than", 0),
                        ConfigurationCondition("io_run_steps", "greater_than", 0),
                    ),
                    description=(
                        "The generated workload requires positive bounded size, "
                        "duration, and trajectory interval values."
                    ),
                ),
            ),
        )

    # ------------------------------------------------------------------
    # Container Dockerfile generators
    # ------------------------------------------------------------------

    def _build_phase(self) -> tuple[str, str] | None:  # pyright: ignore[reportIncompatibleMethodOverride]
        if self.config.get("deploy_mode") != "container":
            return None
        base = self.config.get("base_image", "sci-hpc-base")
        use_gpu = self.config.get("kokkos_gpu", False)
        cuda_arch = self.config.get("cuda_arch", 80)
        if use_gpu:
            cmake_extra = (
                f"-DPKG_KOKKOS=ON "
                f"-DKokkos_ENABLE_CUDA=ON "
                f'"-DKokkos_ARCH_AMPERE{cuda_arch}=ON" '
            )
            suffix = f"kokkos-gpu-{cuda_arch}"
        else:
            cmake_extra = ""
            suffix = "cpu"
        content = self._read_build_script(
            "build.sh",
            {
                "BASE_IMAGE": base,
                "CMAKE_EXTRA": cmake_extra,
            },
        )
        return content, suffix

    def _build_deploy_phase(self) -> tuple[str, str] | None:  # pyright: ignore[reportIncompatibleMethodOverride]
        if self.config.get("deploy_mode") != "container":
            return None
        use_gpu = self.config.get("kokkos_gpu", False)
        deploy_base = (
            "nvidia/cuda:12.6.0-runtime-ubuntu24.04" if use_gpu else "ubuntu:24.04"
        )
        suffix = str(getattr(self, "_build_suffix", ""))
        content = self._read_dockerfile(
            "Dockerfile.deploy",
            {
                "BUILD_IMAGE": self.build_image_name(),
                "DEPLOY_BASE": deploy_base,
            },
        )
        return content, suffix

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs: Any) -> None:
        """
        Configure LAMMPS.

        Calls super()._configure() which updates self.config and (when
        deploy_mode == 'container') triggers build_phase / build_deploy_phase.

        In default mode, also creates the output directory on all nodes.
        """
        super()._configure(**kwargs)

        self._validate_legacy_runtime_configuration()
        self._validate_workload_configuration()

        if self.config.get("deploy_mode") == "default":
            self._ensure_output_dir()

    def _validate_legacy_runtime_configuration(self) -> None:
        """Reject concrete launcher overrides while accepting the old default."""
        configured = self.config.get("lmp_bin")
        if configured in (None, "", "lmp"):
            return
        raise ValueError(
            "LAMMPS lmp_bin is no longer configurable; make LAMMPS available "
            "through the JARVIS pipeline execution environment PATH"
        )

    def _validate_workload_configuration(self) -> None:
        """Ensure every launch selects a real user or generated input."""
        script = self.config.get("script")
        if script not in (None, ""):
            if not isinstance(script, str):
                raise TypeError("script must be a path string")
            return
        raw_values: dict[str, object] = {
            "io_dump_interval": self.config.get("io_dump_interval", 100),
            "io_lattice_size": self.config.get("io_lattice_size", 80),
            "io_run_steps": self.config.get("io_run_steps", 5000),
        }
        for name, value in raw_values.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _log_path(self) -> str:
        """Return the deterministic package-owned LAMMPS thermo log path."""
        return os.path.join(self._output_dir(), "log.lammps")

    def _output_dir(self) -> str:
        """Return the normalized package output directory."""
        return str(self.resolve_shared_path(self.config.get("out"), field="out"))

    def _ensure_output_dir(self) -> None:
        """Create the resolved output directory on every participating host."""
        created = Mkdir(
            self._output_dir(),
            self._node_exec_info(env=self.env),
        ).run()
        failures = {host: code for host, code in created.exit_code.items() if code != 0}
        if failures:
            raise RuntimeError(
                f"Failed to create LAMMPS output directory {self._output_dir()}: "
                f"{failures}"
            )

    def _node_exec_info(self, **kwargs: Any) -> LocalExecInfo | PsshExecInfo:
        """Return local or parallel-SSH execution according to the hostfile."""
        hostfile = self.hostfile
        if hostfile is None or hostfile.is_local():
            return LocalExecInfo(**kwargs)
        return PsshExecInfo(hostfile=hostfile, **kwargs)

    def _remove_stale_log(self) -> None:
        """Remove the previous log before relay polling and LAMMPS startup."""
        exec_args: dict[str, Any] = {"env": self.mod_env}
        if self.config.get("deploy_mode") == "container":
            exec_args.update(
                {
                    "container": self._container_engine,
                    "container_image": self.deploy_image_name(),
                    "gpu": self.config.get("kokkos_gpu", False),
                    "private_dir": self.private_dir,
                    "shared_dir": self.shared_dir,
                }
            )
        cleanup = Exec(
            f"rm -f {shlex.quote(self._log_path())}",
            self._node_exec_info(**exec_args),
        ).run()
        failures = {host: code for host, code in cleanup.exit_code.items() if code != 0}
        if failures:
            raise RuntimeError(
                f"Failed to remove stale LAMMPS log {self._log_path()}: {failures}"
            )

    def _generated_input_script(self) -> str | None:
        """Create the package-owned Lennard-Jones input when requested.

        This workload is a LAMMPS package semantic, independent of whether
        JARVIS launches a system/Spack binary or a container image. The input
        lives in the pipeline shared directory so every allocated node sees
        the same immutable launch input.
        """
        self._validate_workload_configuration()
        script: object = self.config.get("script")
        if script not in (None, ""):
            assert isinstance(script, str)
            return os.path.expanduser(os.path.expandvars(script))

        interval: object = self.config.get("io_dump_interval", 100)
        raw_values: dict[str, object] = {
            "io_dump_interval": interval,
            "io_lattice_size": self.config.get("io_lattice_size", 80),
            "io_run_steps": self.config.get("io_run_steps", 5000),
        }
        values: dict[str, int] = {}
        for name, value in raw_values.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
            values[name] = value

        output_dir = self._output_dir()
        if any(ord(character) < 32 for character in output_dir):
            raise ValueError("out cannot contain control characters")
        output_token = shlex.quote(output_dir)
        dump_token = shlex.quote(os.path.join(output_dir, "dump.*.lammpstrj"))
        content = (
            f"shell mkdir -p {output_token}\n"
            "units lj\n"
            "atom_style atomic\n"
            "lattice fcc 0.8442\n"
            f"region box block 0 {values['io_lattice_size']} "
            f"0 {values['io_lattice_size']} 0 {values['io_lattice_size']}\n"
            "create_box 1 box\n"
            "create_atoms 1 box\n"
            "mass 1 1.0\n"
            "velocity all create 1.44 87287 loop geom\n"
            "pair_style lj/cut 2.5\n"
            "pair_coeff 1 1 1.0 1.0 2.5\n"
            "neighbor 0.3 bin\n"
            "neigh_modify every 10 delay 0 check no\n"
            "fix 1 all nve\n"
            f"dump d1 all custom {values['io_dump_interval']} {dump_token} "
            "id type x y z vx vy vz\n"
            "dump_modify d1 sort id\n"
            f"thermo {values['io_dump_interval']}\n"
            "timestep 0.005\n"
            f"run {values['io_run_steps']}\n"
        )

        if self.shared_dir is None:
            raise RuntimeError(
                "LAMMPS generated input requires a pipeline shared directory"
            )
        shared_dir = Path(self.shared_dir)
        shared_dir.mkdir(parents=True, exist_ok=True)
        content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        script_path = shared_dir / f"generated_io_input-{content_digest}.lmp"
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{script_path.name}.",
                suffix=".tmp",
                dir=shared_dir,
                delete=False,
            ) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, script_path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
        return str(script_path)

    def start(self) -> None:
        """
        Launch LAMMPS.

        Branches on deploy_mode: uses MpiExecInfo with container engine for
        container mode, MpiExecInfo with hostfile for default mode.
        """
        self._validate_legacy_runtime_configuration()
        script_path = self._generated_input_script()
        self._ensure_output_dir()
        self._remove_stale_log()
        line_callback = self.progress_line_callback()
        if self.config.get("deploy_mode") == "container":
            cmd = ["/usr/local/bin/lmp", f"-log {shlex.quote(self._log_path())}"]
            if script_path:
                cmd.append(f"-in {shlex.quote(os.path.expandvars(script_path))}")
            if self.config.get("kokkos_gpu"):
                n_gpus = self.config.get("num_gpus", 1)
                cmd += [f"-k on g {n_gpus}", "-sf kk", "-pk kokkos cuda/aware on"]

            lammps_command = " ".join(cmd)
            output_dir = shlex.quote(os.path.dirname(self._log_path()))
            container_command = shlex.quote(
                f"mkdir -p {output_dir} && exec {lammps_command}"
            )
            result = Exec(
                f"bash -c {container_command}",
                MpiExecInfo(
                    nprocs=self.config["nprocs"],
                    ppn=self.config["ppn"],
                    hostfile=self.hostfile,
                    port=self.ssh_port,
                    container=self._container_engine,
                    container_image=self.deploy_image_name(),
                    shared_dir=self.shared_dir,
                    private_dir=self.private_dir,
                    gpu=self.config.get("kokkos_gpu", False),
                    env=self.mod_env,
                    line_callback=line_callback,
                ),
            ).run()
        else:
            cmd = [
                "lmp",
                f"-log {shlex.quote(self._log_path())}",
            ]
            if script_path:
                cmd.append(f"-in {shlex.quote(script_path)}")
            if self.config.get("kokkos_gpu"):
                n_gpus = self.config.get("num_gpus", 1)
                cmd += [f"-k on g {n_gpus}", "-sf kk", "-pk kokkos cuda/aware on"]

            result = Exec(
                " ".join(cmd),
                MpiExecInfo(
                    nprocs=self.config["nprocs"],
                    ppn=self.config["ppn"],
                    hostfile=self.hostfile,
                    env=self.mod_env,
                    cwd=self._output_dir(),
                    line_callback=line_callback,
                ),
            ).run()
        failures = {host: code for host, code in result.exit_code.items() if code != 0}
        if failures:
            raise RuntimeError(f"LAMMPS execution failed: {failures}")

    def stop(self) -> None:
        """Stop LAMMPS (no-op — LAMMPS runs to completion)."""
        pass

    def clean(self) -> None:
        """Remove LAMMPS output directory."""
        output_dir = self._output_dir()
        output_path = Path(output_dir)
        if output_path == Path(output_path.anchor):
            raise ValueError("refusing to clean a filesystem root as LAMMPS output")
        removed = Rm(
            output_dir,
            self._node_exec_info(env=self.env),
            recursive=True,
        ).run()
        failures = {host: code for host, code in removed.exit_code.items() if code != 0}
        if failures:
            raise RuntimeError(
                f"Failed to clean LAMMPS output directory {output_dir}: {failures}"
            )
