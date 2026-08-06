"""Launch native DLIO Benchmark workloads with explicit execution semantics."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any, cast

from jarvis_cd.core.pkg import Application
from jarvis_cd.deployment import (
    ConfigurationCondition,
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
from jarvis_cd.shell.process import Rm
from jarvis_cd.runtime_callback import RuntimePhaseLineCallback

_WORKLOAD_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_CACHE_POLICIES = ("none", "sync", "drop_caches")


class DlioBenchmark(Application):
    """Run one configured DLIO data-generation and training I/O workload."""

    def _init(self) -> None:
        """Initialize the bounded batch package."""

    def _configure_menu(self) -> list[dict[str, Any]]:
        """Return agent-visible DLIO workload and execution controls."""
        return [
            {
                "name": "workload",
                "msg": "Installed DLIO workload profile name",
                "type": str,
                "default": "unet3d_a100",
                "choices": [],
                "args": [],
            },
            {
                "name": "generate_data",
                "msg": "Generate the configured dataset before the training I/O phase",
                "type": bool,
                "default": False,
                "choices": [],
                "args": [],
            },
            {
                "name": "evaluation",
                "msg": "Run the selected workload's evaluation I/O phase",
                "type": bool,
                "default": False,
                "choices": [],
                "args": [],
            },
            {
                "name": "checkpoint_supported",
                "msg": "Whether the selected workload has checkpoint configuration",
                "type": bool,
                "default": True,
                "choices": [],
                "args": [],
            },
            {
                "name": "checkpoint",
                "msg": "Write checkpoints during the training I/O phase",
                "type": bool,
                "default": True,
                "choices": [],
                "args": [],
            },
            {
                "name": "data_path",
                "msg": (
                    "Dataset directory. Empty uses data/<workload> below the "
                    "execution-owned package shared directory."
                ),
                "type": str,
                "default": "",
                "choices": [],
                "args": [],
            },
            {
                "name": "output_path",
                "msg": (
                    "Native DLIO output directory. Relative paths resolve below "
                    "the execution-owned package shared directory."
                ),
                "type": str,
                "default": "output",
                "choices": [],
                "args": [],
            },
            {
                "name": "checkpoint_path",
                "msg": (
                    "Checkpoint directory. Empty uses checkpoints/<workload> below "
                    "the execution-owned package shared directory."
                ),
                "type": str,
                "default": "",
                "choices": [],
                "args": [],
            },
            {
                "name": "num_files_train",
                "msg": "Number of files in the training dataset",
                "type": int,
                "default": None,
                "choices": [],
                "args": [],
            },
            {
                "name": "num_samples_per_file",
                "msg": "Samples represented by each generated training file",
                "type": int,
                "default": None,
                "choices": [],
                "args": [],
            },
            {
                "name": "record_length_bytes",
                "msg": "Generated training-record payload size in bytes",
                "type": int,
                "default": None,
                "choices": [],
                "args": [],
            },
            {
                "name": "record_length_bytes_resize",
                "msg": (
                    "Generated record resize target in bytes; empty uses "
                    "record_length_bytes when that value is set"
                ),
                "type": int,
                "default": None,
                "choices": [],
                "args": [],
            },
            {
                "name": "batch_size",
                "msg": "Samples read per training iteration",
                "type": int,
                "default": None,
                "choices": [],
                "args": [],
            },
            {
                "name": "read_threads",
                "msg": "Reader threads per DLIO rank",
                "type": int,
                "default": None,
                "choices": [],
                "args": [],
            },
            {
                "name": "epochs",
                "msg": "Training I/O epochs",
                "type": int,
                "default": None,
                "choices": [],
                "args": [],
            },
            {
                "name": "train_computation_time",
                "msg": "Emulated computation time in seconds per training iteration",
                "type": float,
                "default": None,
                "choices": [],
                "args": [],
            },
            {
                "name": "checkpoint_size_bytes",
                "msg": "Emulated model checkpoint size in bytes",
                "type": int,
                "default": None,
                "choices": [],
                "args": [],
            },
            {
                "name": "checkpoint_fsync",
                "msg": "Require DLIO to fsync checkpoint writes",
                "type": bool,
                "default": False,
                "choices": [],
                "args": [],
            },
            {
                "name": "checkpoint_after_epoch",
                "msg": "First epoch after which DLIO writes a checkpoint",
                "type": int,
                "default": None,
                "choices": [],
                "args": [],
            },
            {
                "name": "epochs_between_checkpoints",
                "msg": "Number of epochs between checkpoints",
                "type": int,
                "default": None,
                "choices": [],
                "args": [],
            },
            {
                "name": "nprocs",
                "msg": "Number of MPI ranks",
                "type": int,
                "default": 8,
            },
            {
                "name": "ppn",
                "msg": "MPI ranks per node",
                "type": int,
                "default": 8,
            },
            {
                "name": "timeout_seconds",
                "msg": "Maximum runtime in seconds for each DLIO MPI phase",
                "type": int,
                "default": 3600,
            },
            {
                "name": "tracing",
                "msg": "Enable DFTracer through its runtime environment contract",
                "type": bool,
                "default": False,
            },
            {
                "name": "cache_policy",
                "msg": (
                    "Cache handling before training: none, unprivileged sync, or "
                    "explicit noninteractive privileged drop_caches"
                ),
                "type": str,
                "default": "none",
                "choices": list(_CACHE_POLICIES),
                "args": [],
            },
        ]

    def _deployment_contract(self) -> PackageDeploymentContract:
        """Describe the pre-resolved native DLIO runtime and batch completion."""
        probe = probe_program(
            "dlio_benchmark",
            environment=self._deployment_environment(),
            arguments=("--help",),
        )
        capabilities = (
            ("distributed_training_io", "native_dlio_outputs")
            if probe.status.usable is True
            else ()
        )
        runtime = RuntimeRequirement(
            requirement_id="dlio",
            description=(
                "DLIO Benchmark runtime resolved in the pipeline environment before "
                "execution"
            ),
            required_capabilities=("distributed_training_io", "native_dlio_outputs"),
            available_capabilities=capabilities,
            status=probe.status,
            provider_resolutions=(
                ProviderResolution(
                    provider="path",
                    query_kind="program",
                    query_value="dlio_benchmark",
                ),
            ),
        )
        completed = ReadinessContract(
            mechanism="process_exit",
            condition="successful_exit",
        )
        return PackageDeploymentContract(
            package="builtin.dlio_benchmark",
            execution_profiles=(
                ExecutionProfile(
                    name="train_existing_data",
                    execution_kind="batch",
                    when=(ConfigurationCondition("generate_data", "equals", False),),
                    runtime_requirements=("dlio",),
                    readiness=completed,
                    description=(
                        "Run one native DLIO workload configuration against an "
                        "existing dataset and retain its native outputs."
                    ),
                ),
                ExecutionProfile(
                    name="generate_and_train",
                    execution_kind="batch",
                    when=(ConfigurationCondition("generate_data", "equals", True),),
                    runtime_requirements=("dlio",),
                    readiness=completed,
                    description=(
                        "Generate the configured dataset, then run one native DLIO "
                        "workload configuration and retain both product groups."
                    ),
                ),
            ),
            runtime_requirements=(runtime,),
            configuration_rules=(
                ConfigurationRule(
                    when=(ConfigurationCondition("checkpoint", "equals", True),),
                    requires=(
                        ConfigurationCondition("checkpoint_supported", "equals", True),
                    ),
                    description=(
                        "Checkpoint output can be enabled only for a workload whose "
                        "DLIO profile supports checkpoint configuration."
                    ),
                ),
            ),
        )

    def _configure(self, **kwargs: Any) -> None:
        """Validate execution controls and persist normalized artifact paths."""
        super()._configure(**kwargs)
        self._validate_configuration()
        config = cast(dict[str, Any], self.config)
        config["data_path"] = str(self._data_path())
        config["output_path"] = str(self._output_path())
        config["checkpoint_path"] = str(self._checkpoint_path())

    def _validate_configuration(self) -> None:
        """Reject unsafe or ambiguous values before any process is launched."""
        config = cast(dict[str, Any], self.config)
        if config.get("deploy_mode", "default") != "default":
            raise ValueError("builtin.dlio_benchmark supports native execution only")
        workload = config.get("workload")
        if (
            not isinstance(workload, str)
            or _WORKLOAD_PATTERN.fullmatch(workload) is None
        ):
            raise ValueError("workload must be a DLIO profile token")
        for field in (
            "generate_data",
            "evaluation",
            "checkpoint_supported",
            "checkpoint",
            "checkpoint_fsync",
            "tracing",
        ):
            if not isinstance(config.get(field), bool):
                raise TypeError(f"{field} must be a boolean")
        if config["checkpoint"] and not config["checkpoint_supported"]:
            raise ValueError("checkpoint requires checkpoint_supported=true")
        for field in ("nprocs", "ppn", "timeout_seconds"):
            self._positive_integer(field, required=True)
        if cast(int, config["ppn"]) > cast(int, config["nprocs"]):
            raise ValueError("ppn cannot exceed nprocs")
        for field in (
            "num_files_train",
            "num_samples_per_file",
            "record_length_bytes",
            "record_length_bytes_resize",
            "batch_size",
            "read_threads",
            "epochs",
            "checkpoint_size_bytes",
            "checkpoint_after_epoch",
            "epochs_between_checkpoints",
        ):
            self._positive_integer(field, required=False)
        computation_time = config.get("train_computation_time")
        if computation_time is not None and (
            isinstance(computation_time, bool)
            or not isinstance(computation_time, (int, float))
            or computation_time < 0
        ):
            raise ValueError("train_computation_time must be a non-negative number")
        cache_policy = config.get("cache_policy")
        if cache_policy not in _CACHE_POLICIES:
            raise ValueError(
                "cache_policy must be one of: " + ", ".join(_CACHE_POLICIES)
            )
        self._data_path()
        self._output_path()
        self._checkpoint_path()

    def _positive_integer(self, field: str, *, required: bool) -> int | None:
        """Validate one optional or required positive integer setting."""
        value = self.config.get(field)
        if value is None and not required:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field} must be a positive integer")
        return value

    def _data_path(self) -> Path:
        """Return the configured dataset directory."""
        return self.resolve_shared_path(
            self.config.get("data_path"),
            field="data_path",
            default=f"data/{self.config['workload']}",
        )

    def _output_path(self) -> Path:
        """Return the package-owned native-output directory."""
        return self.resolve_shared_path(
            self.config.get("output_path"),
            field="output_path",
            default="output",
        )

    def _checkpoint_path(self) -> Path:
        """Return the configured checkpoint directory."""
        return self.resolve_shared_path(
            self.config.get("checkpoint_path"),
            field="checkpoint_path",
            default=f"checkpoints/{self.config['workload']}",
        )

    def _command(self, *, generate_only: bool, output_path: Path) -> str:
        """Build one shell-safe native DLIO invocation."""
        workflow_checkpoint = bool(
            not generate_only
            and self.config["checkpoint_supported"]
            and self.config["checkpoint"]
        )
        overrides = [
            f"workload={self.config['workload']}",
            f"++workload.workflow.generate_data={'true' if generate_only else 'false'}",
            f"++workload.workflow.train={'false' if generate_only else 'true'}",
            "++workload.workflow.evaluation="
            + ("true" if self.config["evaluation"] and not generate_only else "false"),
            f"++workload.dataset.data_folder={self._data_path()}",
            f"++workload.output.folder={output_path}",
        ]
        if not self.config["evaluation"]:
            overrides.append("++workload.dataset.num_files_eval=0")
        optional = (
            ("num_files_train", "workload.dataset.num_files_train"),
            ("num_samples_per_file", "workload.dataset.num_samples_per_file"),
            ("record_length_bytes", "workload.dataset.record_length_bytes"),
            (
                "record_length_bytes_resize",
                "workload.dataset.record_length_bytes_resize",
            ),
            ("batch_size", "workload.reader.batch_size"),
            ("read_threads", "workload.reader.read_threads"),
            ("epochs", "workload.train.epochs"),
            ("train_computation_time", "workload.train.computation_time"),
        )
        for field, setting in optional:
            value = self.config.get(field)
            if field == "record_length_bytes_resize" and value is None:
                value = self.config.get("record_length_bytes")
            if value is not None:
                overrides.append(f"++{setting}={value}")
        if self.config["checkpoint_supported"]:
            overrides.extend(
                (
                    "++workload.workflow.checkpoint="
                    + ("true" if workflow_checkpoint else "false"),
                    f"++workload.checkpoint.checkpoint_folder={self._checkpoint_path()}",
                )
            )
            if not generate_only:
                checkpoint_size = self.config.get("checkpoint_size_bytes")
                if checkpoint_size is not None:
                    overrides.append(
                        f"++workload.model.model_size_bytes={checkpoint_size}"
                    )
                overrides.append(
                    "++workload.checkpoint.fsync="
                    + ("true" if self.config["checkpoint_fsync"] else "false")
                )
                for field, setting in (
                    ("checkpoint_after_epoch", "checkpoint_after_epoch"),
                    ("epochs_between_checkpoints", "epochs_between_checkpoints"),
                ):
                    value = self.config.get(field)
                    if value is not None:
                        overrides.append(f"++workload.checkpoint.{setting}={value}")
        return " ".join(
            ("dlio_benchmark", *(shlex.quote(value) for value in overrides))
        )

    def _node_exec_info(self, **kwargs: Any) -> LocalExecInfo | PsshExecInfo:
        """Return local or PSSH execution for node-scoped cache operations."""
        hostfile = self.hostfile
        if hostfile is None or hostfile.is_local():
            return LocalExecInfo(**kwargs)
        return PsshExecInfo(hostfile=hostfile, **kwargs)

    @staticmethod
    def _raise_for_exec_failure(result: Any, operation: str) -> None:
        """Require a concrete zero status from every participating process."""
        exit_codes = getattr(result, "exit_code", None)
        if not isinstance(exit_codes, dict) or not exit_codes:
            raise RuntimeError(f"{operation} returned no process exit status")
        failures = {
            str(host): code
            for host, code in exit_codes.items()
            if isinstance(code, bool) or not isinstance(code, int) or code != 0
        }
        if failures:
            rendered = ", ".join(
                f"{host}={code!r}" for host, code in sorted(failures.items())
            )
            raise RuntimeError(f"{operation} failed with exit status: {rendered}")

    def _apply_cache_policy(self) -> None:
        """Apply only the exact cache policy selected by the operator or agent."""
        policy = self.config["cache_policy"]
        if policy == "none":
            return
        if policy == "sync":
            command = "sync"
        elif policy == "drop_caches":
            command = "sync && sudo -n sh -c 'echo 3 > /proc/sys/vm/drop_caches'"
        else:
            raise ValueError(f"unsupported cache policy: {policy!r}")
        result = Exec(command, self._node_exec_info(env=self.env, timeout=60)).run()
        self._raise_for_exec_failure(result, f"DLIO cache policy {policy}")

    def start(self) -> None:
        """Generate optional input data, apply cache policy, and run DLIO."""
        self._validate_configuration()
        output_path = self._output_path()
        training_output = output_path / "training"
        paths = [output_path, training_output]
        if self.config["generate_data"]:
            paths.extend((self._data_path(), output_path / "data-generation"))
        if self.config["checkpoint_supported"] and self.config["checkpoint"]:
            paths.append(self._checkpoint_path())
        for path in paths:
            path.mkdir(parents=True, exist_ok=True)

        environment = dict(self.mod_env)
        if self.config["tracing"]:
            environment.update(
                {
                    "DFTRACER_ENABLE": "1",
                    "DFTRACER_INC_METADATA": "1",
                }
            )
        callback = self.runtime_line_callback()
        intermediate_callback = (
            RuntimePhaseLineCallback(callback, terminal=False)
            if callback is not None
            else None
        )
        terminal_callback = (
            RuntimePhaseLineCallback(callback, terminal=True)
            if callback is not None
            else None
        )
        try:
            if self.config["generate_data"]:
                generation_output = output_path / "data-generation"
                generation = Exec(
                    self._command(generate_only=True, output_path=generation_output),
                    MpiExecInfo(
                        env=environment,
                        hostfile=self.hostfile,
                        nprocs=self.config["nprocs"],
                        ppn=self.config["ppn"],
                        cwd=str(generation_output),
                        line_callback=intermediate_callback,
                        timeout=self.config["timeout_seconds"],
                    ),
                ).run()
                self._raise_for_exec_failure(generation, "DLIO data generation")

            self._apply_cache_policy()
            workload = Exec(
                self._command(generate_only=False, output_path=training_output),
                MpiExecInfo(
                    env=environment,
                    hostfile=self.hostfile,
                    nprocs=self.config["nprocs"],
                    ppn=self.config["ppn"],
                    cwd=str(training_output),
                    line_callback=terminal_callback,
                    timeout=self.config["timeout_seconds"],
                ),
            ).run()
            self._raise_for_exec_failure(workload, "DLIO workload")
        except Exception:
            if terminal_callback is not None:
                terminal_callback.finalize_process(1)
            raise

    def stop(self) -> None:
        """Do nothing because DLIO stages are bounded synchronous processes."""

    def clean(self) -> None:
        """Remove only package-owned products below the package shared root."""
        if self.shared_dir is None:
            raise RuntimeError("DLIO cleanup requires a package shared directory")
        shared = Path(self.shared_dir).resolve(strict=False)
        candidates = [self._output_path(), self._checkpoint_path()]
        if self.config.get("generate_data"):
            candidates.append(self._data_path())
        owned = [str(path) for path in candidates if path.is_relative_to(shared)]
        if not owned:
            return
        result = Rm(
            owned,
            self._node_exec_info(env=self.env),
            recursive=True,
        ).run()
        self._raise_for_exec_failure(result, "DLIO package cleanup")


__all__ = ["DlioBenchmark", "RuntimeStatus"]
