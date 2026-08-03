"""Run a supplied three-angle NACA 0012 OpenFOAM study under MPI."""

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
from builtin.openfoam.contract import (
    ANGLES,
    CASE_NAMES,
    RESULT_NAME,
    materialize_run,
    result_summary_line,
    validate_airfoil_bundle,
    validate_result_document,
    write_incidence_result,
)
from builtin.openfoam.runtime import (
    DeferredRuntimeCallback,
    resolve_openfoam_environment,
    resolve_openfoam_executable,
)


class OpenfoamAirfoil(Application):
    """Execute three digest-bound OpenFOAM airfoil incidence cases."""

    def _init(self) -> None:
        self._run_dir = None

    def _configure_menu(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "nprocs",
                "msg": "Number of MPI processes; the supplied decomposition requires four",
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
                "name": "input_bundle",
                "msg": (
                    "Immutable OpenFOAM NACA 0012 bundle for 0, 6, and 12 degree "
                    "incidence cases under jarvis.package-input-bundle.v1"
                ),
                "type": str,
                "default": "",
                "input_binding": ConfigurationInputBinding(
                    kind="local_file",
                    structure="regular_file",
                ).to_dict(),
            },
        ]

    def _deployment_contract(self) -> PackageDeploymentContract:
        probe = probe_program(
            "simpleFoam",
            environment=self._deployment_environment(),
            arguments=("-help",),
        )
        capabilities = (
            ("mpi_execution", "steady_incompressible_cfd", "force_coefficients")
            if probe.status.usable is True
            else ()
        )
        runtime = RuntimeRequirement(
            requirement_id="openfoam",
            description="OpenFOAM simpleFoam runtime under MPI",
            required_capabilities=(
                "mpi_execution",
                "steady_incompressible_cfd",
                "force_coefficients",
            ),
            available_capabilities=capabilities,
            status=probe.status,
            provider_resolutions=(
                ProviderResolution(
                    provider="spack",
                    query_kind="spec",
                    query_value="openfoam@2312",
                ),
            ),
        )
        return PackageDeploymentContract(
            package="builtin.openfoam",
            execution_profiles=(
                ExecutionProfile(
                    name="supplied_airfoil_incidence",
                    execution_kind="batch",
                    when=(ConfigurationCondition("input_bundle", "is_not_empty"),),
                    runtime_requirements=("openfoam",),
                    readiness=ReadinessContract(
                        mechanism="process_exit",
                        condition="successful_exit",
                    ),
                    description=(
                        "Run the three supplied NACA 0012 incidence cases and compare "
                        "their force coefficients."
                    ),
                ),
                ExecutionProfile(
                    name="legacy_case_script",
                    execution_kind="batch",
                    when=(
                        ConfigurationCondition("input_bundle", "is_empty"),
                        ConfigurationCondition("script_location", "is_not_empty"),
                    ),
                    runtime_requirements=("openfoam",),
                    readiness=ReadinessContract(
                        mechanism="process_exit",
                        condition="successful_exit",
                    ),
                    description="Run an operator-provided OpenFOAM case script.",
                ),
            ),
            runtime_requirements=(runtime,),
            configuration_rules=(
                ConfigurationRule(
                    when=(ConfigurationCondition("input_bundle", "is_empty"),),
                    requires=(
                        ConfigurationCondition("script_location", "is_not_empty"),
                    ),
                    description=(
                        "Without a supplied study bundle, the legacy launcher requires "
                        "an operator-provided case directory."
                    ),
                ),
            ),
        )

    def _configure(self, **kwargs: Any) -> None:
        Application._configure(self, **kwargs)
        config = cast(dict[str, Any], self.config)
        if config.get("deploy_mode", "default") != "default":
            raise ValueError(
                "builtin.openfoam supplied-input profile supports native execution only"
            )
        if config.get("nprocs") != 4:
            raise ValueError(
                "the supplied OpenFOAM decomposition requires exactly four ranks"
            )
        ppn = config.get("ppn")
        if not isinstance(ppn, int) or isinstance(ppn, bool) or ppn < 1:
            raise ValueError("ppn must be a positive integer")
        configured = config.get("input_bundle")
        if not isinstance(configured, str) or not configured:
            raise ValueError("input_bundle is required")
        extracted = extract_input_bundle(
            Path(configured),
            self.resolve_shared_path("input-bundles", field="input bundle root"),
        )
        validate_airfoil_bundle(extracted)

    @staticmethod
    def _require_success(result: Any, label: str) -> None:
        failures = {host: code for host, code in result.exit_code.items() if code != 0}
        if failures:
            raise RuntimeError(f"{label} failed: {failures}")

    def start(self) -> None:
        """Run all incidence cases and finalize a typed coefficient comparison."""

        config = cast(dict[str, Any], self.config)
        configured = config.get("input_bundle")
        if not isinstance(configured, str) or not configured:
            raise RuntimeError("OpenFOAM airfoil bundle was not persisted")
        ppn = config.get("ppn")
        if not isinstance(ppn, int) or isinstance(ppn, bool) or ppn < 1:
            raise RuntimeError("OpenFOAM ppn was not persisted")
        bundle = extract_input_bundle(
            Path(configured),
            self.resolve_shared_path("input-bundles", field="input bundle root"),
        )
        validate_airfoil_bundle(bundle)
        run_dir = materialize_run(
            bundle,
            self.resolve_shared_path("runs", field="run root"),
            self.mod_env.get("JARVIS_EXECUTION_ID"),
        )
        self._run_dir = run_dir
        config["out"] = str(run_dir)
        execution_environment = resolve_openfoam_environment(self.mod_env)
        decompose = shlex.quote(
            str(resolve_openfoam_executable(execution_environment, "decomposePar"))
        )
        solver = shlex.quote(
            str(resolve_openfoam_executable(execution_environment, "simpleFoam"))
        )
        reconstruct = shlex.quote(
            str(resolve_openfoam_executable(execution_environment, "reconstructPar"))
        )
        delegate = self.runtime_line_callback()
        preliminary = DeferredRuntimeCallback(delegate, terminal=False)

        for angle in ANGLES:
            case_dir = run_dir / CASE_NAMES[angle]
            local_info = LocalExecInfo(
                env=execution_environment,
                cwd=str(case_dir),
                timeout=900,
                line_callback=preliminary,
            )
            self._require_success(
                Exec(f"{decompose} -force", local_info).run(),
                f"OpenFOAM {angle}-degree decomposition",
            )
            mpi_info = MpiExecInfo(
                nprocs=4,
                ppn=ppn,
                hostfile=self.hostfile,
                env=execution_environment,
                cwd=str(case_dir),
                timeout=1800,
                line_callback=preliminary,
            )
            self._require_success(
                Exec(f"{solver} -parallel", mpi_info).run(),
                f"OpenFOAM {angle}-degree solve",
            )
            self._require_success(
                Exec(f"{reconstruct} -latestTime", local_info).run(),
                f"OpenFOAM {angle}-degree reconstruction",
            )

        result_path = run_dir / RESULT_NAME
        document = write_incidence_result(run_dir, result_path, bundle)
        validate_result_document(run_dir, result_path, bundle)
        summary = result_summary_line(document)
        terminal = DeferredRuntimeCallback(delegate, terminal=True)
        final_info = LocalExecInfo(
            env=execution_environment,
            cwd=str(run_dir),
            timeout=30,
            line_callback=terminal,
        )
        self._require_success(
            Exec(f"printf '%s\\n' {shlex.quote(summary)}", final_info).run(),
            "OpenFOAM result finalization",
        )

    def stop(self) -> None:
        """Do nothing because all OpenFOAM cases are bounded batch processes."""

    def clean(self) -> None:
        """Leave immutable inputs and validated outputs for artifact finalization."""
