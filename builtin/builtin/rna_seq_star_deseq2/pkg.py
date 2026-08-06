"""Launch a supplied STAR to DESeq2 differential-expression workflow."""

from __future__ import annotations

import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

from .result_contract import load_rnaseq_result
from jarvis_cd.core.pkg import Application
from jarvis_cd.deployment import (
    ConfigurationCondition,
    ConfigurationInputBinding,
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
from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm

_EXPECTED_ROLES = {
    "gene_annotation": 1,
    "reference_genome": 1,
    "sample_design": 1,
}


class RnaSeqStarDeseq2(Application):
    """Align supplied single-end RNA-seq reads and compare two conditions.

    The agent-facing native profile accepts one digest-verified bundle carrying
    the reads, sample design, reference genome, and gene annotation. It never
    installs software or accesses the network during scheduled execution. The
    historical container profile remains available for existing pipelines but
    is intentionally hidden from the scientific contract.
    """

    def _init(self) -> None:
        self.star_bin: str | None = None
        self.rscript_bin: str | None = None

    def _configure_menu(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "input_bundle",
                "msg": (
                    "Digest-verified bundle containing replicated single-end "
                    "FASTQ reads, one sample design, one reference genome, and "
                    "one matching gene annotation"
                ),
                "type": str,
                "default": "",
                "input_binding": ConfigurationInputBinding(
                    kind="local_file", structure="regular_file"
                ).to_dict(),
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
                "name": "cores",
                "msg": "CPU cores used by STAR for indexing and alignment",
                "type": int,
                "default": 4,
            },
            {
                "name": "nprocs",
                "msg": "Legacy process count; the native workflow is one batch process",
                "type": int,
                "default": 1,
                "agent_visible": False,
            },
            {
                "name": "ppn",
                "msg": "Legacy processes per node",
                "type": int,
                "default": 1,
                "agent_visible": False,
            },
            {
                "name": "replicates",
                "msg": "Legacy image-baked workflow repetition count",
                "type": int,
                "default": 1,
                "agent_visible": False,
            },
            {
                "name": "parallel_reps",
                "msg": "Legacy image-baked per-host repetition concurrency",
                "type": int,
                "default": 1,
                "agent_visible": False,
            },
            {
                "name": "parallel_scratch_root",
                "msg": "Legacy image-baked scratch root",
                "type": str,
                "default": "/tmp",
                "agent_visible": False,
            },
            {
                "name": "omp_threads",
                "msg": "Legacy image-baked OpenMP thread count",
                "type": int,
                "default": 0,
                "agent_visible": False,
            },
            {
                "name": "base_image",
                "msg": "Legacy container build base image",
                "type": str,
                "default": "sci-hpc-base",
                "agent_visible": False,
            },
        ]

    @staticmethod
    def _discover(program: str, environment: dict[str, str]) -> str | None:
        return program if shutil.which(program, path=environment.get("PATH")) else None

    def _deployment_contract(self) -> PackageDeploymentContract:
        """Describe the native STAR and DESeq2 runtime and completion contract."""

        environment = self._deployment_environment()
        star = self._discover("STAR", environment) or "STAR"
        rscript = self._discover("Rscript", environment) or "Rscript"
        star_probe = probe_program(
            star,
            environment=environment,
            arguments=("--version",),
            timeout_seconds=10,
        )
        deseq_probe = probe_program(
            rscript,
            environment=environment,
            arguments=(
                "-e",
                "suppressPackageStartupMessages(library(DESeq2));cat(as.character(packageVersion('DESeq2')))",
            ),
            timeout_seconds=20,
        )
        return PackageDeploymentContract(
            package="builtin.rna_seq_star_deseq2",
            execution_profiles=(
                ExecutionProfile(
                    name="native_supplied_reads",
                    execution_kind="batch",
                    when=(
                        ConfigurationCondition("deploy_mode", "equals", "default"),
                        ConfigurationCondition("input_bundle", "is_not_empty"),
                    ),
                    runtime_requirements=("star", "deseq2"),
                    readiness=ReadinessContract(
                        mechanism="process_exit",
                        condition="successful_exit_with_required_products",
                    ),
                    description=(
                        "Align one supplied replicated two-condition RNA-seq "
                        "study with STAR and calculate differential expression "
                        "with DESeq2."
                    ),
                ),
            ),
            runtime_requirements=(
                RuntimeRequirement(
                    requirement_id="star",
                    description="STAR aligner available through PATH",
                    required_capabilities=("rna_seq_alignment", "gene_counting"),
                    available_capabilities=(
                        ("rna_seq_alignment", "gene_counting")
                        if star_probe.status.usable is True
                        else ()
                    ),
                    status=star_probe.status,
                ),
                RuntimeRequirement(
                    requirement_id="deseq2",
                    description="R runtime with the DESeq2 package",
                    required_capabilities=("differential_expression",),
                    available_capabilities=(
                        ("differential_expression",)
                        if deseq_probe.status.usable is True
                        else ()
                    ),
                    status=deseq_probe.status,
                ),
            ),
        )

    def _build_phase(self) -> tuple[str, str] | None:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Build the retained legacy container image only when requested."""

        if self.config.get("deploy_mode") != "container":
            return None
        content = self._read_build_script(
            "build.sh", {"BASE_IMAGE": self.config.get("base_image", "sci-hpc-base")}
        )
        return content, "snakemake"

    def _build_deploy_phase(self) -> tuple[str, str] | None:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Create the retained legacy deployment image only when requested."""

        if self.config.get("deploy_mode") != "container":
            return None
        content = self._read_dockerfile(
            "Dockerfile.deploy",
            {"BUILD_IMAGE": self.build_image_name(), "DEPLOY_BASE": "ubuntu:24.04"},
        )
        return content, "snakemake"

    def _configure(self, **kwargs: Any) -> None:
        """Validate the selected profile and prepare package-owned storage."""

        super()._configure(**kwargs)
        if self.config.get("deploy_mode", "default") == "default":
            self._validate_native_configuration()
            environment = self._deployment_environment()
            self.star_bin = self._discover("STAR", environment)
            self.rscript_bin = self._discover("Rscript", environment)
            self._ensure_output_root()

    def _validate_native_configuration(self) -> None:
        bundle = self.config.get("input_bundle")
        if not isinstance(bundle, str) or not bundle:
            raise ValueError("native STAR to DESeq2 requires input_bundle")
        cores = self.config.get("cores")
        if (
            isinstance(cores, bool)
            or not isinstance(cores, int)
            or not 1 <= cores <= 64
        ):
            raise ValueError("cores must be an integer between 1 and 64")

    def _output_root(self) -> Path:
        return self.resolve_shared_path(
            self.config.get("out"), field="out", default="run"
        )

    def _node_exec_info(self, **kwargs: Any) -> LocalExecInfo | PsshExecInfo:
        hostfile = self.hostfile
        if hostfile is None or hostfile.is_local():
            return LocalExecInfo(**kwargs)
        return PsshExecInfo(hostfile=hostfile, **kwargs)

    def _ensure_output_root(self) -> None:
        output = str(self._output_root())
        result = Mkdir(output, self._node_exec_info(env=self.env)).run()
        failures = {host: code for host, code in result.exit_code.items() if code != 0}
        if failures:
            raise RuntimeError(
                f"Failed to create RNA-seq output directory {output}: {failures}"
            )

    @staticmethod
    def _bundle_members(
        bundle: MaterializedInputBundle,
    ) -> dict[str, Path | tuple[Path, ...]]:
        by_role: dict[str, list[Path]] = {}
        for item in bundle.manifest.files:
            by_role.setdefault(item.role, []).append(bundle.root / item.path)
        if set(by_role) != {*_EXPECTED_ROLES, "single_end_fastq"}:
            raise ValueError(
                "RNA-seq input bundle contains unsupported or missing roles"
            )
        for role, expected_count in _EXPECTED_ROLES.items():
            if len(by_role[role]) != expected_count:
                raise ValueError(f"RNA-seq input bundle requires exactly one {role}")
        fastqs = by_role["single_end_fastq"]
        if not 4 <= len(fastqs) <= 64:
            raise ValueError("RNA-seq input bundle requires 4 to 64 FASTQ files")
        design = by_role["sample_design"][0]
        if design != bundle.entrypoint:
            raise ValueError(
                "RNA-seq input bundle entrypoint must be its sample design"
            )
        return {
            "sample_design": design,
            "reference_genome": by_role["reference_genome"][0],
            "gene_annotation": by_role["gene_annotation"][0],
            "single_end_fastq": tuple(fastqs),
        }

    def _prepare_native_input(self) -> tuple[Path, Path, Path, Path, Path]:
        """Verify and stage one closed study without accepting stale products."""

        self._validate_native_configuration()
        self._ensure_output_root()
        root = self._output_root()
        for name in ("input", "results", "work"):
            target = root / name
            if target.exists() or target.is_symlink():
                raise ValueError(f"RNA-seq execution target already exists: {name}")
        configured = self.config.get("input_bundle")
        assert isinstance(configured, str) and configured
        if self.shared_dir is None:
            raise RuntimeError("RNA-seq input bundles require shared storage")
        bundle = extract_input_bundle(
            configured, Path(self.shared_dir) / "input-bundles"
        )
        members = self._bundle_members(bundle)
        stage_input_bundle(bundle, root / "input")

        def staged(role: str) -> Path:
            source = members[role]
            assert isinstance(source, Path)
            return root / "input" / source.relative_to(bundle.root)

        return (
            root / "input",
            staged("sample_design"),
            staged("reference_genome"),
            staged("gene_annotation"),
            root,
        )

    def _start_native(self) -> None:
        input_root, samples, genome, annotation, root = self._prepare_native_input()
        star = self.star_bin or self._discover("STAR", self.mod_env)
        rscript = self.rscript_bin or self._discover("Rscript", self.mod_env)
        if star is None:
            raise RuntimeError("STAR is unavailable through PATH")
        if rscript is None:
            raise RuntimeError("Rscript with DESeq2 is unavailable through PATH")
        runner = Path(__file__).with_name("native_runner.py").resolve()
        deseq = Path(__file__).with_name("deseq2_analysis.R").resolve()
        arguments = [
            sys.executable,
            str(runner),
            "--input-root",
            str(input_root),
            "--samples",
            str(samples),
            "--genome",
            str(genome),
            "--annotation",
            str(annotation),
            "--output-root",
            str(root / "results"),
            "--work-root",
            str(root / "work"),
            "--deseq-script",
            str(deseq),
            "--star",
            star,
            "--rscript",
            rscript,
            "--cores",
            str(self.config["cores"]),
        ]
        result = Exec(
            " ".join(shlex.quote(argument) for argument in arguments),
            LocalExecInfo(
                env=self.mod_env,
                cwd=str(root),
                line_callback=self.runtime_line_callback(),
            ),
        ).run()
        failures = {host: code for host, code in result.exit_code.items() if code != 0}
        if failures:
            raise RuntimeError(f"STAR to DESeq2 workflow failed: {failures}")
        load_rnaseq_result(root / "results")

    def _start_legacy_container(self) -> None:
        """Retain the historical image-baked workflow for explicit container users."""

        out_root = self.config.get("out")
        if not isinstance(out_root, str) or not out_root:
            raise ValueError("legacy RNA-seq output must be a path string")
        configured_replicates = self.config.get("replicates", 1)
        if isinstance(configured_replicates, bool) or not isinstance(
            configured_replicates, int
        ):
            raise ValueError("legacy RNA-seq replicates must be an integer")
        replicates = max(configured_replicates, 1)
        command = (
            f"/opt/run_rnaseq.sh {shlex.quote(out_root)}"
            if replicates == 1
            else "set -e; "
            + f"for i in $(seq 1 {replicates}); do "
            + 'rep=$(printf "rep_%03d" "$i"); '
            + f"/opt/run_rnaseq.sh {shlex.quote(out_root)}/$rep || exit 1; done"
        )
        result = Exec(
            command,
            LocalExecInfo(
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
                env=self.mod_env,
            ),
        ).run()
        failures = {host: code for host, code in result.exit_code.items() if code != 0}
        if failures:
            raise RuntimeError(f"Legacy STAR to DESeq2 workflow failed: {failures}")

    def start(self) -> None:
        """Run the selected native or explicit legacy profile."""

        if self.config.get("deploy_mode", "default") == "container":
            self._start_legacy_container()
        else:
            self._start_native()

    def stop(self) -> None:
        """Do nothing because this package is a bounded batch process."""

    def clean(self) -> None:
        """Remove the exact package-owned output directory."""

        output = str(self._output_root())
        Rm(output, self._node_exec_info(env=self.env), recursive=True).run()
