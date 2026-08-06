"""Run a bounded WRF tropical-cyclone surface-exchange comparison."""

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
from builtin.wrf.contract import (
    FORMULATIONS,
    RESULT_NAME,
    materialize_run,
    prepare_case,
    resolve_runtime_program,
    result_summary_line,
    validate_result_document,
    validate_wrf_bundle,
    validate_wrf_prefix,
    write_result,
)
from builtin.wrf.runtime import (
    DeferredRuntimeCallback,
    read_wrf_diagnostic,
)


class WrfTropicalCyclone(Application):
    """Compare WRF constant-Z0q and Garratt tropical-cyclone surface exchange."""

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
                "msg": "Immutable WRF v4.6.1 ideal tropical-cyclone inputs",
                "type": str,
                "default": "",
                "input_binding": ConfigurationInputBinding(
                    kind="local_file", structure="regular_file"
                ).to_dict(),
            },
            {
                "name": "wrf_prefix",
                "msg": "Resolved native WRF installation prefix",
                "type": str,
                "default": "",
            },
        ]

    def _deployment_contract(self) -> PackageDeploymentContract:
        environment = self._deployment_environment()
        probe = probe_program("ideal.exe", environment=environment, arguments=())
        ncdump_probe = probe_program("ncdump", environment=environment, arguments=())
        capabilities = (
            ("wrf", "netcdf_tools")
            if probe.status.usable is True and ncdump_probe.status.usable is True
            else ()
        )
        runtime = RuntimeRequirement(
            requirement_id="wrf",
            description="Native WRF 4.6.1 ideal-case runtime and NetCDF tools",
            required_capabilities=("wrf", "netcdf_tools"),
            available_capabilities=capabilities,
            status=probe.status,
            provider_resolutions=(
                ProviderResolution(
                    provider="spack",
                    query_kind="spec",
                    query_value="wrf@4.6.1 compile_type=em_tropical_cyclone",
                ),
            ),
        )
        return PackageDeploymentContract(
            package="builtin.wrf",
            execution_profiles=(
                ExecutionProfile(
                    name="supplied_surface_exchange_comparison",
                    execution_kind="batch",
                    when=(
                        ConfigurationCondition("input_bundle", "is_not_empty"),
                        ConfigurationCondition("wrf_prefix", "is_not_empty"),
                    ),
                    runtime_requirements=("wrf",),
                    readiness=ReadinessContract(
                        mechanism="process_exit", condition="successful_exit"
                    ),
                    description=(
                        "Initialize and run the same bounded idealized tropical cyclone with "
                        "constant-Z0q and Garratt surface-exchange formulations."
                    ),
                ),
                ExecutionProfile(
                    name="legacy_wrf_location",
                    execution_kind="batch",
                    when=(
                        ConfigurationCondition("input_bundle", "is_empty"),
                        ConfigurationCondition("wrf_location", "is_not_empty"),
                    ),
                    runtime_requirements=("wrf",),
                    readiness=ReadinessContract(
                        mechanism="process_exit", condition="successful_exit"
                    ),
                    description="Run wrf.exe from an operator-provided WRF directory.",
                ),
            ),
            runtime_requirements=(runtime,),
            configuration_rules=(
                ConfigurationRule(
                    when=(ConfigurationCondition("input_bundle", "is_empty"),),
                    requires=(ConfigurationCondition("wrf_location", "is_not_empty"),),
                    description=(
                        "Without a supplied study bundle, the legacy launcher requires "
                        "an operator-provided WRF directory."
                    ),
                ),
                ConfigurationRule(
                    when=(ConfigurationCondition("input_bundle", "is_not_empty"),),
                    requires=(ConfigurationCondition("wrf_prefix", "is_not_empty"),),
                    description="The comparison requires a resolved native WRF prefix.",
                ),
            ),
        )

    def _configure(self, **kwargs: Any) -> None:
        Application._configure(self, **kwargs)
        config = cast(dict[str, Any], self.config)
        if config.get("deploy_mode", "default") != "default":
            raise ValueError(
                "builtin.wrf supplied-input profile supports native execution only"
            )
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
        validate_wrf_bundle(bundle)
        prefix = config.get("wrf_prefix")
        if not isinstance(prefix, str) or not prefix:
            raise ValueError("wrf_prefix is required")
        validate_wrf_prefix(Path(prefix))

    @staticmethod
    def _require_success(
        result: Any,
        label: str,
        *,
        diagnostic_root: Path | None = None,
    ) -> None:
        failures = {host: code for host, code in result.exit_code.items() if code != 0}
        if failures:
            diagnostic = (
                read_wrf_diagnostic(diagnostic_root)
                if diagnostic_root is not None
                else ""
            )
            if diagnostic:
                print(f"WRF_RUNTIME_DIAGNOSTIC\n{diagnostic}", flush=True)
                bounded = " | ".join(diagnostic.splitlines())[-4096:]
                raise RuntimeError(f"{label} failed: {failures}; {bounded}")
            raise RuntimeError(
                f"{label} failed: {failures}; no WRF rank-zero log was produced"
            )

    def start(self) -> None:
        """Initialize, execute, and validate both bounded WRF cases."""

        config = cast(dict[str, Any], self.config)
        configured = config.get("input_bundle")
        prefix_setting = config.get("wrf_prefix")
        if not isinstance(configured, str) or not configured:
            raise RuntimeError("WRF input bundle was not persisted")
        if not isinstance(prefix_setting, str) or not prefix_setting:
            raise RuntimeError("WRF prefix was not persisted")
        nprocs = config.get("nprocs")
        ppn = config.get("ppn")
        if not isinstance(nprocs, int) or isinstance(nprocs, bool) or nprocs <= 0:
            raise RuntimeError("WRF nprocs was not persisted")
        if not isinstance(ppn, int) or isinstance(ppn, bool) or ppn <= 0:
            raise RuntimeError("WRF ppn was not persisted")
        bundle = extract_input_bundle(
            Path(configured),
            self.resolve_shared_path("input-bundles", field="input bundle root"),
        )
        prefix = validate_wrf_prefix(Path(prefix_setting))
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
        ncdump = resolve_runtime_program("ncdump", self.mod_env)
        for name, option in FORMULATIONS:
            case_root = prepare_case(
                run_dir, bundle, prefix, name=name, isftcflx=option
            )
            local_info = LocalExecInfo(
                env=self.mod_env,
                cwd=str(case_root),
                timeout=1800,
                line_callback=preliminary,
            )
            self._require_success(
                Exec(shlex.quote(str(prefix / "main" / "ideal.exe")), local_info).run(),
                f"WRF {name} initialization",
                diagnostic_root=case_root,
            )
            mpi_info = MpiExecInfo(
                nprocs=nprocs,
                ppn=ppn,
                hostfile=self.hostfile,
                env=self.mod_env,
                cwd=str(case_root),
                timeout=7200,
                line_callback=preliminary,
            )
            self._require_success(
                Exec(shlex.quote(str(prefix / "main" / "wrf.exe")), mpi_info).run(),
                f"WRF {name} forecast",
                diagnostic_root=case_root,
            )
            outputs = sorted(case_root.glob("wrfout_d01_*"))
            if len(outputs) != 1:
                raise RuntimeError(f"WRF {name} did not produce one history file")
            command = (
                f"{shlex.quote(str(ncdump))} -p 15,7 -v U10,V10,PSFC "
                f"{shlex.quote(outputs[0].name)} > surface-diagnostics.cdl"
            )
            self._require_success(
                Exec(command, local_info).run(), f"WRF {name} diagnostics"
            )

        result_path = run_dir / RESULT_NAME
        document = write_result(run_dir, result_path, bundle)
        validate_result_document(run_dir, result_path, bundle)
        final_info = LocalExecInfo(
            env=self.mod_env, cwd=str(run_dir), timeout=30, line_callback=terminal
        )
        self._require_success(
            Exec(
                f"printf '%s\\n' {shlex.quote(result_summary_line(document))}",
                final_info,
            ).run(),
            "WRF result finalization",
        )

    def stop(self) -> None:
        """Do nothing because the comparison is a bounded batch process."""

    def clean(self) -> None:
        """Leave immutable inputs and validated outputs for artifact finalization."""
