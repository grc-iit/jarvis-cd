"""Generate and execute one bounded WfCommons/WfBench workflow cell."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import sysconfig
from pathlib import Path
from typing import Any

from jarvis_cd.core.pkg import Application
from jarvis_cd.deployment import (
    ConfigurationCondition,
    ExecutionProfile,
    PackageDeploymentContract,
    ProviderResolution,
    ReadinessContract,
    RuntimeRequirement,
    RuntimeStatus,
    probe_program,
)
from jarvis_cd.shell import Exec, LocalExecInfo
from jarvis_cd.shell.process import Rm

RECIPES = (
    "montage",
    "genome",
    "cycles",
    "blast",
    "bwa",
    "srasearch",
    "epigenomics",
    "seismology",
    "soykb",
    "rnaseq",
)
_EXPECTED_WFCOMMONS_VERSION = "1.4"
_MAX_TASKS = 100_000
_MAX_DATA_FOOTPRINT_MB = 1_000_000
_MAX_TIMEOUT_SECONDS = 86_400


class Wfcommons(Application):
    """Run one deterministic synthetic workflow study cell.

    JARVIS owns runtime selection, the pinned WfFormat schema, the output
    directory, process completion, and artifact reporting. A prepared runtime
    must already contain the expected WfCommons version. Runtime installation
    is intentionally outside scheduled scientific execution.
    """

    def _init(self) -> None:
        """Initialize the package without mutable global runtime state."""

    def _configure_menu(self) -> list[dict[str, Any]]:
        """Describe one scientific workflow cell and its execution bounds."""

        return [
            {
                "name": "recipe",
                "msg": "Synthetic scientific workflow recipe",
                "type": str,
                "choices": list(RECIPES),
                "default": "montage",
            },
            {
                "name": "num_tasks",
                "msg": "Requested number of generated workflow tasks",
                "type": int,
                "default": 100,
            },
            {
                "name": "data_footprint_mb",
                "msg": "Total generated workflow data footprint in MB; 0 uses recipe defaults",
                "type": int,
                "default": 0,
            },
            {
                "name": "seed",
                "msg": "Random seed controlling generated workflow topology",
                "type": int,
                "default": 424_200,
            },
            {
                "name": "cpu_work",
                "msg": "Positive WfBench CPU work units per task",
                "type": int,
                "default": 1,
            },
            {
                "name": "percent_cpu",
                "msg": "Fraction of WfBench work threads assigned to CPU work",
                "type": float,
                "default": 1.0,
            },
            {
                "name": "drop_page_cache",
                "msg": "Request per-file POSIX_FADV_DONTNEED behavior in WfBench",
                "type": bool,
                "default": False,
            },
            {
                "name": "clio_prefix",
                "msg": "Prefix translated workflow data paths with clio::",
                "type": bool,
                "default": False,
            },
            {
                "name": "out",
                "msg": "Package-owned output directory relative to the shared root",
                "type": str,
                "default": "run",
            },
            {
                "name": "timeout_seconds",
                "msg": "Maximum elapsed time for this workflow cell",
                "type": int,
                "default": 3600,
            },
            {
                "name": "nprocs",
                "msg": "Scheduler process count; WfCommons cell execution is single-process",
                "type": int,
                "default": 1,
            },
            {
                "name": "ppn",
                "msg": "Scheduler processes per node; must be one",
                "type": int,
                "default": 1,
            },
            {
                "name": "runtime_python",
                "msg": "Operator-owned Python executable containing WfCommons 1.4",
                "type": str,
                "default": "",
                "agent_visible": False,
            },
            {
                "name": "base_image",
                "msg": "Base image for an operator-built container runtime",
                "type": str,
                "default": "ubuntu:24.04",
                "agent_visible": False,
            },
        ]

    def _deployment_contract(self) -> PackageDeploymentContract:
        """Describe the prepared runtime and successful-exit contract."""

        completed = ReadinessContract(
            mechanism="process_exit", condition="successful_exit"
        )
        if self.config.get("deploy_mode") == "container":
            wfcommons_status = RuntimeStatus("unknown", "container_runtime_not_probed")
            bash_status = RuntimeStatus("unknown", "container_runtime_not_probed")
            wfcommons_capabilities: tuple[str, ...] = ()
            bash_capabilities: tuple[str, ...] = ()
        else:
            environment = self._deployment_environment()
            python = self._runtime_python()
            expected = self._expected_version()
            version_probe = probe_program(
                python,
                environment=environment,
                arguments=(
                    "-c",
                    (
                        "import wfcommons; import sys; "
                        f"sys.exit(0 if getattr(wfcommons, '__version__', '') == {expected!r} else 3)"
                    ),
                ),
            )
            bash_probe = probe_program(
                "bash", environment=environment, arguments=("--version",)
            )
            wfcommons_status = version_probe.status
            bash_status = bash_probe.status
            wfcommons_capabilities = (
                ("synthetic_workflow_generation", "wfbench_execution", "wfformat")
                if wfcommons_status.usable is True
                else ()
            )
            bash_capabilities = (
                ("bash_workflow_execution",) if bash_status.usable is True else ()
            )
        runtime = RuntimeRequirement(
            requirement_id="wfcommons_runtime",
            description=(
                f"Prepared Python runtime containing WfCommons {_EXPECTED_WFCOMMONS_VERSION}"
            ),
            required_capabilities=(
                "synthetic_workflow_generation",
                "wfbench_execution",
                "wfformat",
            ),
            available_capabilities=wfcommons_capabilities,
            status=wfcommons_status,
            provider_resolutions=(
                ProviderResolution(
                    provider="path", query_kind="program", query_value="python3"
                ),
            ),
        )
        bash = RuntimeRequirement(
            requirement_id="bash",
            description="Bash runtime used by the generated WfBench workflow",
            required_capabilities=("bash_workflow_execution",),
            available_capabilities=bash_capabilities,
            status=bash_status,
            provider_resolutions=(
                ProviderResolution(
                    provider="path", query_kind="program", query_value="bash"
                ),
            ),
        )
        return PackageDeploymentContract(
            package="builtin.wfcommons",
            execution_profiles=(
                ExecutionProfile(
                    name="synthetic_workflow_cell",
                    execution_kind="batch",
                    when=(ConfigurationCondition("recipe", "is_not_empty"),),
                    runtime_requirements=("bash", "wfcommons_runtime"),
                    readiness=completed,
                    description=(
                        "Generate and execute one deterministic WfBench workflow cell."
                    ),
                ),
            ),
            runtime_requirements=(bash, runtime),
        )

    def _build_phase(self) -> tuple[str, str] | None:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Build an operator-owned container runtime when requested."""

        if self.config.get("deploy_mode") != "container":
            return None
        content = self._read_build_script(
            "build.sh", {"BASE_IMAGE": self.config.get("base_image", "ubuntu:24.04")}
        )
        return content, "py311"

    def _build_deploy_phase(self) -> tuple[str, str] | None:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Create the deploy image containing the prepared runtime."""

        if self.config.get("deploy_mode") != "container":
            return None
        content = self._read_dockerfile(
            "Dockerfile.deploy",
            {
                "BUILD_IMAGE": self.build_image_name(),
                "DEPLOY_BASE": "ubuntu:24.04",
            },
        )
        return content, "py311"

    def _configure(self, **kwargs: Any) -> None:
        """Persist a valid cell without installing software or touching outputs."""

        super()._configure(**kwargs)
        self._validate_configuration()

    @staticmethod
    def _require_int(value: object, name: str, *, minimum: int, maximum: int) -> int:
        """Return one bounded integer or fail without coercion."""

        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < minimum
            or value > maximum
        ):
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
        return value

    def _validate_configuration(self) -> None:
        """Reject ambiguous, unsafe, or unsupported study settings."""

        recipe = self.config.get("recipe")
        if recipe not in RECIPES:
            raise ValueError(f"recipe must be one of: {', '.join(RECIPES)}")
        self._require_int(
            self.config.get("num_tasks"), "num_tasks", minimum=1, maximum=_MAX_TASKS
        )
        self._require_int(
            self.config.get("data_footprint_mb"),
            "data_footprint_mb",
            minimum=0,
            maximum=_MAX_DATA_FOOTPRINT_MB,
        )
        self._require_int(
            self.config.get("cpu_work"), "cpu_work", minimum=1, maximum=1_000_000
        )
        self._require_int(self.config.get("seed"), "seed", minimum=0, maximum=2**32 - 1)
        self._require_int(
            self.config.get("timeout_seconds"),
            "timeout_seconds",
            minimum=1,
            maximum=_MAX_TIMEOUT_SECONDS,
        )
        percent_cpu = self.config.get("percent_cpu")
        if (
            isinstance(percent_cpu, bool)
            or not isinstance(percent_cpu, (int, float))
            or not 0 < float(percent_cpu) <= 1
        ):
            raise ValueError("percent_cpu must be greater than 0 and at most 1")
        if self.config.get("nprocs") != 1 or self.config.get("ppn") != 1:
            raise ValueError(
                "WfCommons workflow cells require single-process execution"
            )
        runtime_python = self.config.get("runtime_python", "")
        if not isinstance(runtime_python, str):
            raise ValueError("runtime_python must be a path string")
        out = self.config.get("out")
        if not isinstance(out, str) or not out.strip():
            raise ValueError("out must be a non-empty path")

    def _expected_version(self) -> str:
        """Return the package-owned WfCommons version pin."""

        return _EXPECTED_WFCOMMONS_VERSION

    def _runtime_python(self) -> str:
        """Resolve the operator-prepared Python executable."""

        if self.config.get("deploy_mode") == "container":
            return "/opt/wfcommons-env/bin/python3"
        configured = self.config.get("runtime_python")
        if isinstance(configured, str) and configured:
            return os.path.expandvars(os.path.expanduser(configured))
        environment = getattr(self, "mod_env", {})
        from_environment = environment.get("WFCOMMONS_PYTHON")
        if from_environment:
            return from_environment
        return sys.executable

    def _output_dir(self) -> Path:
        """Return the package-owned output directory."""

        return self.resolve_shared_path(
            self.config.get("out"), field="out", default="run"
        )

    def _schema_source(self) -> Path:
        """Return the repository-pinned WfFormat schema."""

        package_dir = self._package_dir()
        package_copy = package_dir / "wfcommons-schema.json"
        if package_copy.is_file() and not package_copy.is_symlink():
            return package_copy
        repository_copy = package_dir.parents[2] / "wfcommons-schema.json"
        if repository_copy.is_file() and not repository_copy.is_symlink():
            return repository_copy
        installed_copy = Path(sysconfig.get_path("data")) / "wfcommons-schema.json"
        if installed_copy.is_file() and not installed_copy.is_symlink():
            return installed_copy
        raise RuntimeError("Pinned WfFormat schema is missing from the package")

    def _package_dir(self) -> Path:
        """Return the configured package directory as an absolute path."""

        package_dir = self.pkg_dir
        if not isinstance(package_dir, str) or not package_dir:
            raise RuntimeError("WfCommons package directory is unavailable")
        return Path(package_dir).resolve()

    @staticmethod
    def _failures(result: Any) -> dict[str, int]:
        """Return all nonzero host exits from one JARVIS shell result."""

        return {host: code for host, code in result.exit_code.items() if code != 0}

    def _driver_arguments(self, *, schema_path: Path) -> list[str]:
        """Return one shell-safe driver argument vector."""

        arguments = [
            "--recipe",
            str(self.config["recipe"]),
            "--num-tasks",
            str(self.config["num_tasks"]),
            "--cpu-work",
            str(self.config["cpu_work"]),
            "--data-footprint-mb",
            str(self.config["data_footprint_mb"]),
            "--percent-cpu",
            str(self.config["percent_cpu"]),
            "--seed",
            str(self.config["seed"]),
            "--schema-file",
            str(schema_path),
            "--expected-wfcommons-version",
            self._expected_version(),
            "--out",
            str(self._output_dir()),
        ]
        if self.config.get("clio_prefix"):
            arguments.append("--clio-prefix")
        return arguments

    def start(self) -> None:
        """Generate and execute one cell and fail on any process error."""

        self._validate_configuration()
        output_dir = self._output_dir()
        if output_dir.exists():
            raise ValueError(f"WfCommons output already exists: {output_dir}")
        output_dir.mkdir(parents=True, mode=0o700)
        schema_source = self._schema_source()
        schema_path = output_dir / "wfcommons-schema.json"
        shutil.copyfile(schema_source, schema_path, follow_symlinks=False)
        os.chmod(schema_path, 0o400)
        self.config["out"] = str(output_dir)  # pyright: ignore[reportArgumentType]

        if self.config.get("deploy_mode") == "container":
            driver = "/opt/wfcommons-driver/run_wfbench.py"
            info = LocalExecInfo(
                env=dict(self.mod_env),
                cwd=str(output_dir),
                timeout=self.config["timeout_seconds"],
                line_callback=self.runtime_line_callback(),
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
            )
        else:
            driver = str(self._package_dir() / "run_wfbench.py")
            environment = dict(self.mod_env)
            if self.config.get("drop_page_cache"):
                environment["WFBENCH_DROP_CACHE"] = "1"
            info = LocalExecInfo(
                env=environment,
                cwd=str(output_dir),
                timeout=self.config["timeout_seconds"],
                line_callback=self.runtime_line_callback(),
            )
        command = " ".join(
            shlex.quote(value)
            for value in (
                self._runtime_python(),
                driver,
                *self._driver_arguments(schema_path=schema_path),
            )
        )
        result = Exec(command, info).run()
        failures = self._failures(result)
        if failures:
            rendered = ", ".join(f"{host}={code}" for host, code in failures.items())
            raise RuntimeError(f"WfCommons execution failed: {rendered}")

    def stop(self) -> None:
        """Do nothing because the workflow cell runs to process completion."""

    def clean(self) -> None:
        """Remove only the exact package-owned output directory."""

        output_dir = self._output_dir()
        if output_dir == Path(output_dir.anchor):
            raise ValueError("refusing to clean a filesystem root as WfCommons output")
        result = Rm(str(output_dir), LocalExecInfo(env=self.env), recursive=True).run()
        failures = self._failures(result)
        if failures:
            raise RuntimeError(
                f"Failed to clean WfCommons output {output_dir}: {failures}"
            )

    def _get_stat(self, stat_dict: dict[str, Any]) -> None:
        """Expose the requested workflow cell in JARVIS statistics."""

        stat_dict[f"{self.pkg_id}.recipe"] = self.config["recipe"]
        stat_dict[f"{self.pkg_id}.num_tasks"] = self.config["num_tasks"]
        stat_dict[f"{self.pkg_id}.data_footprint_mb"] = self.config["data_footprint_mb"]
        stat_dict[f"{self.pkg_id}.runtime"] = getattr(self, "start_time", None)


__all__ = ["RECIPES", "Wfcommons"]
