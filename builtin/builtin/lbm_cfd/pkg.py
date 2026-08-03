"""Run a supplied LBM-CFD lattice-stencil comparison."""

from __future__ import annotations

import shlex
from pathlib import Path
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
    probe_program,
)
from jarvis_cd.shell import Exec, LocalExecInfo, MpiExecInfo

from jarvis_cd.input_bundle import extract_input_bundle
from builtin.lbm_cfd.contract import (
    DIMENSIONS,
    FINAL_STEP,
    LATTICES,
    RESULT_NAME,
    TIME_STEPS,
    materialize_run,
    result_summary_line,
    validate_lbm_bundle,
    validate_result_document,
    write_lbm_result,
)
from builtin.lbm_cfd.runtime import DeferredRuntimeCallback


class LbmCfd(Application):
    """Execute the same bounded bluff-body wake with three lattice stencils."""

    def _init(self) -> None:
        self._run_dir = None

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
                "msg": "Number of MPI processes per node",
                "type": int,
                "default": 4,
            },
            {
                "name": "input_bundle",
                "msg": "Immutable pinned LBM-CFD-3D application source bundle",
                "type": str,
                "default": "",
                "input_binding": ConfigurationInputBinding(
                    kind="local_file", structure="regular_file"
                ).to_dict(),
            },
        ]

    def _deployment_contract(self) -> PackageDeploymentContract:
        probe = probe_program(
            "mpicxx", environment=self._deployment_environment(), arguments=()
        )
        capabilities = ("mpi_cxx",) if probe.status.usable is True else ()
        runtime = RuntimeRequirement(
            requirement_id="mpi_cxx",
            description="MPI C++ compiler and launcher",
            required_capabilities=("mpi_cxx",),
            available_capabilities=capabilities,
            status=probe.status,
            provider_resolutions=(
                ProviderResolution(
                    provider="spack", query_kind="spec", query_value="openmpi"
                ),
            ),
        )
        return PackageDeploymentContract(
            package="builtin.lbm_cfd",
            execution_profiles=(
                ExecutionProfile(
                    name="supplied_stencil_comparison",
                    execution_kind="batch",
                    when=(ConfigurationCondition("input_bundle", "is_not_empty"),),
                    runtime_requirements=("mpi_cxx",),
                    readiness=ReadinessContract(
                        mechanism="process_exit", condition="successful_exit"
                    ),
                    description=(
                        "Build the pinned LBM-CFD source and compare D3Q15, D3Q19, "
                        "and D3Q27 vorticity fields for one bounded wake."
                    ),
                ),
            ),
            runtime_requirements=(runtime,),
            configuration_rules=(
                ConfigurationRule(
                    when=(ConfigurationCondition("input_bundle", "is_empty"),),
                    requires=(ConfigurationCondition("input_bundle", "is_not_empty"),),
                    description="The stencil comparison requires the immutable source bundle.",
                ),
            ),
        )

    def _configure(self, **kwargs: Any) -> None:
        super()._configure(**kwargs)
        config = cast(dict[str, Any], self.config)
        if config.get("deploy_mode", "default") != "default":
            raise ValueError("builtin.lbm_cfd supports native execution only")
        for parameter in ("nprocs", "ppn"):
            value = config.get(parameter)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{parameter} must be positive")
        configured = config.get("input_bundle")
        if not isinstance(configured, str) or not configured:
            raise ValueError("input_bundle is required")
        bundle = extract_input_bundle(
            Path(configured),
            self.resolve_shared_path("input-bundles", field="input bundle root"),
        )
        validate_lbm_bundle(bundle)

    @staticmethod
    def _require_success(result: Any, label: str) -> None:
        failures = {host: code for host, code in result.exit_code.items() if code != 0}
        if failures:
            raise RuntimeError(f"{label} failed: {failures}")

    def start(self) -> None:
        """Build once, run all three stencils, and finalize their field comparison."""

        config = cast(dict[str, Any], self.config)
        configured = config.get("input_bundle")
        if not isinstance(configured, str) or not configured:
            raise RuntimeError("LBM-CFD source bundle was not persisted")
        nprocs = config.get("nprocs")
        ppn = config.get("ppn")
        if not isinstance(nprocs, int) or isinstance(nprocs, bool) or nprocs <= 0:
            raise RuntimeError("LBM-CFD nprocs was not persisted")
        if not isinstance(ppn, int) or isinstance(ppn, bool) or ppn <= 0:
            raise RuntimeError("LBM-CFD ppn was not persisted")
        bundle = extract_input_bundle(
            Path(configured),
            self.resolve_shared_path("input-bundles", field="input bundle root"),
        )
        run_dir = materialize_run(
            bundle,
            self.resolve_shared_path("runs", field="run root"),
            self.mod_env.get("JARVIS_EXECUTION_ID"),
        )
        self._run_dir = run_dir
        config["out"] = str(run_dir)
        delegate = self.runtime_line_callback()
        preliminary = DeferredRuntimeCallback(delegate, terminal=False)
        terminal = DeferredRuntimeCallback(delegate, terminal=True)
        local_info = LocalExecInfo(
            env=self.mod_env, cwd=str(run_dir), timeout=900, line_callback=preliminary
        )
        self._require_success(
            Exec("make CXX=mpicxx CXXFLAGS='-std=c++14 -O3'", local_info).run(),
            "LBM-CFD build",
        )
        executable = shlex.quote(str(run_dir / "bin" / "lbmcfd3d"))
        dimensions = " ".join(str(value) for value in DIMENSIONS)
        for lattice in LATTICES:
            output = run_dir / lattice
            output.mkdir(mode=0o700)
            mpi_info = MpiExecInfo(
                nprocs=nprocs,
                ppn=ppn,
                hostfile=self.hostfile,
                env=self.mod_env,
                cwd=str(run_dir),
                timeout=1800,
                line_callback=preliminary,
            )
            command = " ".join(
                (
                    executable,
                    f"--{lattice}",
                    "--dim",
                    dimensions,
                    "--steps",
                    str(TIME_STEPS),
                    "--output-vorticity",
                    "--output-dir",
                    shlex.quote(str(output)),
                )
            )
            self._require_success(
                Exec(command, mpi_info).run(), f"LBM-CFD {lattice.upper()}"
            )
            if not (output / f"simulation_state_t{FINAL_STEP:05d}.vts").is_file():
                raise RuntimeError(f"LBM-CFD {lattice.upper()} omitted its final field")

        result_path = run_dir / RESULT_NAME
        document = write_lbm_result(run_dir, result_path, bundle)
        validate_result_document(run_dir, result_path, bundle)
        final_info = LocalExecInfo(
            env=self.mod_env, cwd=str(run_dir), timeout=30, line_callback=terminal
        )
        summary = result_summary_line(document)
        self._require_success(
            Exec(f"printf '%s\\n' {shlex.quote(summary)}", final_info).run(),
            "LBM-CFD result finalization",
        )

    def stop(self) -> None:
        """Do nothing because the comparison is a bounded batch process."""

    def clean(self) -> None:
        """Leave immutable inputs and validated outputs for artifact finalization."""
