"""
This module provides classes and methods to launch the Gray-Scott application.
Gray-Scott is a reaction-diffusion simulation.
"""

import shlex
import time
from pathlib import PurePosixPath
from typing import Any

from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir
from jarvis_cd.util.config_parser import JsonFile
from jarvis_cd.util.logger import Color


def _absolute_dataset_path(
    value: str,
    *,
    field_name: str,
    context: str = "",
) -> str:
    """Validate one normalized absolute dataset path for execution or cleanup."""
    path = value.strip()
    parsed = PurePosixPath(path)
    label = " ".join(part for part in (field_name, context, "path") if part)
    if (
        not path
        or not parsed.is_absolute()
        or len(parsed.parts) == 1
        or parsed.as_posix() != path
        or ".." in parsed.parts
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise ValueError(f"Gray-Scott refuses unsafe {label}: {value!r}")
    return path


class GrayScott(Application):
    """
    Merged Gray-Scott class supporting both default (bare-metal) and container deployment.

    Set deploy_mode='container' to build and run Gray-Scott inside a Docker/Podman/Apptainer
    container with CUDA+MPI+HDF5.  Set deploy_mode='default' to use a system-installed
    gray-scott binary via MPI with ADIOS2 I/O.
    """

    config: dict[str, Any]

    def _init(self) -> None:
        self.adios2_xml_path = f"{self.shared_dir}/adios2.xml"
        self.settings_json_path = f"{self.shared_dir}/settings-files.json"

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
                "default": None,
            },
            {
                "name": "executable",
                "msg": "IOWarp Gray-Scott executable or absolute path",
                "type": str,
                "default": "gray-scott",
            },
            {
                "name": "width",
                "msg": "Global grid width (columns)",
                "type": int,
                "default": 512,
            },
            {
                "name": "height",
                "msg": "Global grid height (rows)",
                "type": int,
                "default": 512,
            },
            {
                "name": "steps",
                "msg": "Total number of time steps",
                "type": int,
                "default": 5000,
            },
            {
                "name": "out_every",
                "msg": "Output interval (simulation steps between writes)",
                "type": int,
                "default": 500,
            },
            {
                "name": "outdir",
                "msg": "Output dataset path (default: package shared directory)",
                "type": str,
                "default": "",
            },
            {
                "name": "checkpoint",
                "msg": "Enable ADIOS2 restart checkpoints",
                "type": bool,
                "default": False,
            },
            {
                "name": "checkpoint_freq",
                "msg": "Checkpoint frequency in output intervals",
                "type": int,
                "default": 2000,
            },
            {
                "name": "checkpoint_output",
                "msg": "ADIOS2 checkpoint dataset path",
                "type": str,
                "default": "",
            },
            {
                "name": "adios_span",
                "msg": "Use the ADIOS2 span write API",
                "type": bool,
                "default": False,
            },
            {
                "name": "adios_memory_selection",
                "msg": "Use ADIOS2 memory selections for ghost cells",
                "type": bool,
                "default": False,
            },
            {
                "name": "mesh_type",
                "msg": "ADIOS2 visualization mesh schema",
                "type": str,
                "default": "image",
                "choices": ["image"],
            },
            {
                "name": "F",
                "msg": "Feed rate",
                "type": float,
                "default": 0.035,
            },
            {
                "name": "k",
                "msg": "Kill rate",
                "type": float,
                "default": 0.060,
            },
            {
                "name": "Du",
                "msg": "Diffusion coefficient for u",
                "type": float,
                "default": 0.16,
            },
            {
                "name": "Dv",
                "msg": "Diffusion coefficient for v",
                "type": float,
                "default": 0.08,
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
        ]

    # ------------------------------------------------------------------
    # Container Dockerfile generators
    # ------------------------------------------------------------------

    def _build_phase(self) -> Any:
        if self.config.get("deploy_mode") != "container":
            return None
        cuda_arch = self.config.get("cuda_arch", 80)
        content = self._read_build_script(
            "build.sh",
            {
                "BASE_IMAGE": self.config.get("base_image", "sci-hpc-base"),
                "CUDA_ARCH": cuda_arch,
            },
        )
        return content, f"cuda-{cuda_arch}"

    def _build_deploy_phase(self) -> Any:
        if self.config.get("deploy_mode") != "container":
            return None
        suffix = str(getattr(self, "_build_suffix", ""))
        content = self._read_dockerfile(
            "Dockerfile.deploy",
            {
                "BUILD_IMAGE": self.build_image_name(),
                "DEPLOY_BASE": "nvidia/cuda:12.6.0-runtime-ubuntu24.04",
            },
        )
        return content, suffix

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs: Any) -> None:
        """
        Configure Gray-Scott.

        Calls super()._configure() which updates self.config and (when
        deploy_mode == 'container') triggers build_phase / build_deploy_phase.

        In default mode, also creates the output directory, writes the
        ADIOS2 XML config, and saves the settings JSON file.
        """
        super()._configure(**kwargs)

        if self.config.get("deploy_mode") == "default":
            width = self.config.get("width", 512)
            height = self.config.get("height", 512)
            steps = self.config.get("steps", 5000)
            out_every = self.config.get("out_every", 500)
            for name, value in (
                ("width", width),
                ("height", height),
                ("steps", steps),
                ("out_every", out_every),
            ):
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise ValueError(f"Gray-Scott {name} must be a positive integer")
            if width != height:
                raise ValueError(
                    "Direct IOWarp Gray-Scott requires width and height to match"
                )
            if steps % out_every != 0:
                raise ValueError(
                    "Direct IOWarp Gray-Scott requires steps to be divisible by "
                    "out_every"
                )
            checkpoint_freq = self.config.get("checkpoint_freq", 2000)
            if self.config.get("checkpoint", False) and (
                isinstance(checkpoint_freq, bool)
                or not isinstance(checkpoint_freq, int)
                or checkpoint_freq <= 0
            ):
                raise ValueError(
                    "Gray-Scott checkpoint_freq must be a positive integer when "
                    "checkpointing is enabled"
                )
            output = self._resolve_output_path()
            checkpoint_output = self.config.get("checkpoint_output")
            if not isinstance(checkpoint_output, str) or not checkpoint_output.strip():
                checkpoint_output = f"{output}.checkpoint.bp"
            else:
                checkpoint_output = checkpoint_output.strip()
            checkpoint_output = _absolute_dataset_path(
                checkpoint_output,
                field_name="checkpoint_output",
            )
            self.config["checkpoint_output"] = checkpoint_output
            directories = [output]
            if self.config.get("checkpoint", False):
                checkpoint_parent = PurePosixPath(checkpoint_output).parent.as_posix()
                if checkpoint_parent != "/" and checkpoint_parent not in directories:
                    directories.append(checkpoint_parent)
            mkdir_result = Mkdir(
                directories,
                PsshExecInfo(hostfile=self.hostfile, env=self.env),
            ).run()
            self._raise_for_exec_failure(
                mkdir_result,
                operation="Gray-Scott output setup",
            )

            settings_json = {
                "L": width,
                "Du": self.config.get("Du", 0.16),
                "Dv": self.config.get("Dv", 0.08),
                "F": self.config.get("F", 0.035),
                "k": self.config.get("k", 0.060),
                "dt": 2.0,
                "plotgap": out_every,
                "steps": steps,
                "noise": 0.01,
                "output": output,
                "checkpoint": self.config.get("checkpoint", False),
                "checkpoint_freq": checkpoint_freq,
                "checkpoint_output": checkpoint_output,
                "adios_config": self.adios2_xml_path,
                "adios_span": self.config.get("adios_span", False),
                "adios_memory_selection": self.config.get(
                    "adios_memory_selection", False
                ),
                "mesh_type": self.config.get("mesh_type", "image"),
            }
            JsonFile(self.settings_json_path).save(settings_json)
            self.copy_template_file(
                f"{self.pkg_dir}/config/adios2.xml", self.adios2_xml_path
            )
        else:
            self._resolve_output_path()

    def _resolve_output_path(self) -> str:
        """Resolve an omitted output to durable package-shared storage."""
        output = self.config.get("outdir")
        if not isinstance(output, str) or not output.strip():
            if not isinstance(self.shared_dir, str) or not self.shared_dir.strip():
                raise RuntimeError(
                    "Gray-Scott requires its package shared directory before "
                    "resolving the default output path"
                )
            shared_dir = _absolute_dataset_path(
                self.shared_dir,
                field_name="package shared directory",
            )
            output = (PurePosixPath(shared_dir) / "gray-scott-output").as_posix()
        else:
            output = output.strip()
        output = _absolute_dataset_path(output, field_name="outdir")
        self.config["outdir"] = output
        return output

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Launch Gray-Scott.

        Branches on deploy_mode: uses MpiExecInfo with container engine for
        container mode, MpiExecInfo with hostfile and ADIOS2 settings JSON for default mode.
        """
        line_callback = self.runtime_line_callback()
        if self.config.get("deploy_mode") == "container":
            outdir = self.config.get("outdir")
            if not isinstance(outdir, str) or not outdir:
                raise RuntimeError("Gray-Scott output path was not configured")
            mkdir_result = Mkdir(outdir).run()
            self._raise_for_exec_failure(
                mkdir_result,
                operation="Gray-Scott output setup",
            )

            nprocs = self.config.get("nprocs", 4)
            inner = " ".join(
                [
                    "/usr/bin/gray_scott",
                    f"--width {self.config['width']}",
                    f"--height {self.config['height']}",
                    f"--steps {self.config['steps']}",
                    f"--out-every {self.config['out_every']}",
                    f"--outdir {shlex.quote(outdir)}",
                    f"--F {self.config['F']}",
                    f"--k {self.config['k']}",
                    f"--Du {self.config['Du']}",
                    f"--Dv {self.config['Dv']}",
                ]
            )
            result = Exec(
                inner,
                MpiExecInfo(
                    nprocs=nprocs,
                    ppn=self.config.get("ppn"),
                    container=self._container_engine,
                    container_image=self.deploy_image_name(),
                    shared_dir=self.shared_dir,
                    private_dir=self.private_dir,
                    gpu=True,
                    env=self.mod_env,
                    line_callback=line_callback,
                ),
            ).run()
            self._raise_for_exec_failure(result, operation="container Gray-Scott")
        else:
            start = time.time()
            executable = self.config.get("executable", "gray-scott")
            if not isinstance(executable, str) or not executable.strip():
                raise ValueError("Gray-Scott executable must be a non-empty string")
            result = Exec(
                " ".join(
                    [
                        shlex.quote(executable.strip()),
                        shlex.quote(self.settings_json_path),
                    ]
                ),
                MpiExecInfo(
                    nprocs=self.config["nprocs"],
                    ppn=self.config["ppn"],
                    hostfile=self.hostfile,
                    env=self.mod_env,
                    line_callback=line_callback,
                ),
            ).run()
            self._raise_for_exec_failure(result, operation="IOWarp Gray-Scott")
            end = time.time()
            self.log(f"TIME: {end - start:.2f} seconds", color=Color.GREEN)

    @staticmethod
    def _raise_for_exec_failure(result: Any, *, operation: str) -> None:
        """Raise with bounded stderr when an execution exits unsuccessfully."""
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
            diagnostic = ""
            stderr_by_host = getattr(result, "stderr", None)
            if isinstance(stderr_by_host, dict):
                messages: list[str] = []
                for host in sorted(failures):
                    stderr = stderr_by_host.get(host)
                    if isinstance(stderr, str) and stderr.strip():
                        messages.append(f"{host}: {' '.join(stderr.split())}")
                if messages:
                    bounded = "; ".join(messages)
                    if len(bounded) > 4096:
                        bounded = bounded[:4093] + "..."
                    diagnostic = f"; stderr: {bounded}"
            raise RuntimeError(
                f"{operation} failed with exit status: {details}{diagnostic}"
            )

    def stop(self) -> None:
        """Stop Gray-Scott (no-op — Gray-Scott runs to completion)."""
        pass

    def clean(self) -> None:
        """Remove only the configured output and checkpoint datasets."""
        paths = self._cleanup_paths()
        if not paths:
            return
        command = "rm -rf -- " + " ".join(shlex.quote(path) for path in paths)
        result = Exec(
            command,
            PsshExecInfo(hostfile=self.hostfile, env=self.env),
        ).run()
        self._raise_for_exec_failure(result, operation="Gray-Scott cleanup")

    def _cleanup_paths(self) -> list[str]:
        """Return de-duplicated, exact paths safe for package cleanup."""
        paths: list[str] = []
        for field_name in ("outdir", "checkpoint_output"):
            configured = self.config.get(field_name)
            if not isinstance(configured, str) or not configured.strip():
                continue
            path = _absolute_dataset_path(
                configured,
                field_name=field_name,
                context="cleanup",
            )
            if path not in paths:
                paths.append(path)
        return paths
