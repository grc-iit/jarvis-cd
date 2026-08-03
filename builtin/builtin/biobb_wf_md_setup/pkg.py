"""Launch the BioBB five-stage molecular-dynamics setup workflow."""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path
from typing import Any

from jarvis_cd.core.pkg import Application
from jarvis_cd.deployment import (
    ConfigurationCondition,
    ConfigurationInputBinding,
    ExecutionProfile,
    PackageDeploymentContract,
    ProviderResolution,
    ReadinessContract,
    RuntimeRequirement,
    probe_program,
)
from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm

_MAX_PDB_BYTES = 32 * 1024 * 1024
_NATIVE_INPUT_NAME = "input.pdb"
_FORCE_FIELDS = frozenset(
    {
        "amber03",
        "amber94",
        "amber96",
        "amber99",
        "amber99sb",
        "amber99sb-ildn",
        "amberGS",
        "charmm27",
        "gromos43a1",
        "gromos43a2",
        "gromos45a3",
        "gromos53a5",
        "gromos53a6",
        "gromos54a7",
        "oplsaa",
    }
)
_WATER_TYPES = frozenset({"spc", "spce", "tip3p", "tip4p", "tip5p", "tips3p"})
_BOX_TYPES = frozenset({"cubic", "triclinic", "dodecahedron", "octahedron"})


class BiobbWfMdSetup(Application):
    """Prepare one caller-supplied molecular structure with BioBB and GROMACS.

    Native mode copies an immutable PDB into package-owned storage and runs the
    package-owned five-stage driver. Legacy container benchmarking remains
    available but is not exposed as an agent-facing scientific profile.
    """

    def _init(self) -> None:
        self.python_bin: str | None = None

    def _configure_menu(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "nprocs",
                "msg": "Legacy process count; native MD setup is serial",
                "type": int,
                "default": 1,
                "agent_visible": False,
            },
            {
                "name": "ppn",
                "msg": "Legacy processes per node; native MD setup is serial",
                "type": int,
                "default": 1,
                "agent_visible": False,
            },
            {
                "name": "pdb_file",
                "msg": (
                    "Caller-supplied PDB structure. JARVIS copies it into the "
                    "execution-owned output directory before launch."
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
                "name": "force_field",
                "msg": "GROMACS force field used to construct the topology",
                "type": str,
                "default": "amber99sb-ildn",
            },
            {
                "name": "water_type",
                "msg": "GROMACS water model used by pdb2gmx and solvation",
                "type": str,
                "default": "tip3p",
            },
            {
                "name": "box_type",
                "msg": "Periodic simulation-box geometry",
                "type": str,
                "default": "cubic",
            },
            {
                "name": "distance_to_molecule",
                "msg": "Minimum solute-to-box distance in nanometers",
                "type": float,
                "default": 1.0,
            },
            {
                "name": "ignore_input_hydrogens",
                "msg": "Regenerate hydrogens while constructing the topology",
                "type": bool,
                "default": True,
            },
            {
                "name": "merge_chains",
                "msg": "Merge all input chains into one molecule definition",
                "type": bool,
                "default": False,
            },
            {
                "name": "replicates",
                "msg": (
                    "Number of times to run the MD-setup pipeline "
                    "back-to-back inside the same container exec. "
                    "Used to scale I/O for benchmarking when the "
                    "bundled lysozyme PDB is too small to dominate "
                    "wall time on its own."
                ),
                "type": int,
                "default": 1,
                "agent_visible": False,
            },
            {
                "name": "parallel_scratch_root",
                "msg": (
                    "Root dir for per-rep BIOBB_SCRATCH_DIR when "
                    "parallel_reps > 1. Each rep gets "
                    "`<root>/biobb-scratch-<rep>`. Default /dev/shm "
                    "(node-local tmpfs). Point at a FUSE mount "
                    "to route every GROMACS/biobb intermediate "
                    "through that adapter."
                ),
                "type": str,
                "default": "/dev/shm",
                "agent_visible": False,
            },
            {
                "name": "parallel_reps",
                "msg": (
                    "Per-host replicate concurrency. When > 1, the "
                    "replicates loop fans across hosts via "
                    "PsshExecInfo and each host runs this many in "
                    "parallel using `wait -n` batching. Default 1 "
                    "preserves the original host[0]-only sequential "
                    "loop."
                ),
                "type": int,
                "default": 1,
                "agent_visible": False,
            },
            {
                "name": "omp_threads",
                "msg": (
                    "OMP_NUM_THREADS for each parallel replicate. "
                    "Set to roughly cores_per_node / parallel_reps so "
                    "concurrent GROMACS instances on the same host "
                    "don't oversubscribe. 0 = leave unset (GROMACS "
                    "auto-detects which over-grabs when run in "
                    "parallel)."
                ),
                "type": int,
                "default": 0,
                "agent_visible": False,
            },
            {
                "name": "md_steps",
                "msg": (
                    "Production MD step count for the biobb_md_extend "
                    "post-step. Each step is 2 fs by default, so 5000 "
                    "steps ≈ 10 ps simulated time. 0 = skip the MD "
                    "extension entirely (legacy setup-only behavior)."
                ),
                "type": int,
                "default": 0,
                "agent_visible": False,
            },
            {
                "name": "md_nstxout",
                "msg": (
                    "XTC frame stride for the production MD "
                    "(`nstxout-compressed` in the .mdp). Lower = more "
                    "frames written = more I/O without changing "
                    "compute. The per-rep trajectory file size is "
                    "roughly md_steps / md_nstxout * frame_size."
                ),
                "type": int,
                "default": 100,
                "agent_visible": False,
            },
            {
                "name": "md_extend_script",
                "msg": (
                    "In-container path to the md-extend helper "
                    "script. Bind the host script at this path via "
                    "`container_binds` (e.g. "
                    "${HOME}/jarvis-bench-scripts/biobb_md_extend.sh:"
                    "/opt/biobb_md_extend.sh) and set md_steps > 0 "
                    "to enable."
                ),
                "type": str,
                "default": "/opt/biobb_md_extend.sh",
                "agent_visible": False,
            },
            {
                "name": "base_image",
                "msg": "Base Docker image for build container",
                "type": str,
                "default": "sci-hpc-base",
                "agent_visible": False,
            },
        ]

    def _deployment_contract(self) -> PackageDeploymentContract:
        """Describe the native BioBB and GROMACS runtime requirements."""

        environment = self._deployment_environment()
        biobb_probe = probe_program(
            "python3",
            environment=environment,
            arguments=("-c", "import biobb_gromacs, biobb_model"),
            timeout_seconds=10,
        )
        gromacs_probe = probe_program(
            "gmx",
            environment=environment,
            arguments=("--version",),
            timeout_seconds=10,
        )
        requirements = (
            RuntimeRequirement(
                requirement_id="biobb_python",
                description="Python runtime with BioBB model and GROMACS blocks",
                required_capabilities=("biobb_md_setup",),
                available_capabilities=(
                    ("biobb_md_setup",) if biobb_probe.status.usable is True else ()
                ),
                status=biobb_probe.status,
                provider_resolutions=(
                    ProviderResolution("python", "distribution", "biobb-gromacs"),
                    ProviderResolution("python", "distribution", "biobb-model"),
                ),
            ),
            RuntimeRequirement(
                requirement_id="gromacs",
                description="GROMACS command-line runtime available through PATH",
                required_capabilities=("molecular_dynamics_preparation",),
                available_capabilities=(
                    ("molecular_dynamics_preparation",)
                    if gromacs_probe.status.usable is True
                    else ()
                ),
                status=gromacs_probe.status,
                provider_resolutions=(ProviderResolution("spack", "spec", "gromacs"),),
            ),
        )
        return PackageDeploymentContract(
            package="builtin.biobb_wf_md_setup",
            execution_profiles=(
                ExecutionProfile(
                    name="native_md_setup",
                    execution_kind="batch",
                    when=(
                        ConfigurationCondition("deploy_mode", "equals", "default"),
                        ConfigurationCondition("pdb_file", "is_not_empty"),
                    ),
                    runtime_requirements=("biobb_python", "gromacs"),
                    readiness=ReadinessContract(
                        mechanism="process_exit", condition="successful_exit"
                    ),
                    description=(
                        "Prepare one caller-supplied PDB through side-chain repair, "
                        "topology generation, box construction, and solvation."
                    ),
                ),
            ),
            runtime_requirements=requirements,
        )

    # ------------------------------------------------------------------
    # Container Dockerfile generators
    # ------------------------------------------------------------------

    def _build_phase(self) -> tuple[str, str] | None:  # pyright: ignore[reportIncompatibleMethodOverride]
        if self.config.get("deploy_mode") != "container":
            return None
        content = self._read_build_script(
            "build.sh",
            {
                "BASE_IMAGE": self.config.get("base_image", "sci-hpc-base"),
            },
        )
        return content, "gromacs2026"

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
        return content, "gromacs2026"

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs: Any) -> None:
        """Validate native settings and prepare package-owned output storage."""

        super()._configure(**kwargs)
        if self.config.get("deploy_mode") == "default":
            self._validate_native_configuration()
            self.python_bin = self._discover_python(self._deployment_environment())
            self._ensure_output_dir()

    @staticmethod
    def _discover_python(environment: dict[str, str]) -> str | None:
        """Return a Python command from the activated package environment."""

        for candidate in ("python3", "python"):
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
                f"Failed to create BioBB output directory {output_dir}: {failures}"
            )

    def _validate_native_configuration(self) -> None:
        """Reject missing input and unsupported scientific settings locally."""

        pdb_file = self.config.get("pdb_file")
        if not isinstance(pdb_file, str) or not pdb_file:
            raise ValueError("native BioBB MD setup requires pdb_file")
        for name, supported in (
            ("force_field", _FORCE_FIELDS),
            ("water_type", _WATER_TYPES),
            ("box_type", _BOX_TYPES),
        ):
            value = self.config.get(name)
            if not isinstance(value, str) or value not in supported:
                raise ValueError(f"unsupported {name}: {value!r}")
        distance = self.config.get("distance_to_molecule")
        if (
            isinstance(distance, bool)
            or not isinstance(distance, (int, float))
            or not 0 < float(distance) <= 10
        ):
            raise ValueError(
                "distance_to_molecule must be greater than 0 and at most 10"
            )
        for name in ("ignore_input_hydrogens", "merge_chains"):
            if not isinstance(self.config.get(name), bool):
                raise ValueError(f"{name} must be a boolean")

    def _stage_native_input(self) -> Path:
        """Copy one bounded caller PDB into execution-owned storage."""

        self._validate_native_configuration()
        self._ensure_output_dir()
        configured = self.config.get("pdb_file")
        if not isinstance(configured, str):
            raise ValueError("native BioBB MD setup requires pdb_file")
        source = Path(
            os.path.abspath(os.path.expandvars(os.path.expanduser(configured)))
        )
        try:
            status = source.lstat()
        except OSError as exc:
            raise ValueError("pdb_file is not a readable bounded regular PDB") from exc
        if (
            source.is_symlink()
            or not source.is_file()
            or source.suffix.casefold() != ".pdb"
            or status.st_size <= 0
            or status.st_size > _MAX_PDB_BYTES
        ):
            raise ValueError("pdb_file is not a bounded regular PDB")
        destination = self._output_dir() / _NATIVE_INPUT_NAME
        if destination.exists() or destination.is_symlink():
            raise ValueError("BioBB staged input already exists")
        shutil.copyfile(source, destination, follow_symlinks=False)
        os.chmod(destination, 0o600)
        return destination

    def _start_native(self) -> None:
        """Execute the package-owned BioBB workflow and propagate failure."""

        staged = self._stage_native_input()
        python_bin = self.python_bin or self._discover_python(self.mod_env)
        if python_bin is None:
            raise RuntimeError("BioBB Python runtime is unavailable through PATH")
        script = Path(__file__).with_name("run_md_setup.py").resolve()
        force_field = self.config.get("force_field")
        water_type = self.config.get("water_type")
        box_type = self.config.get("box_type")
        distance = self.config.get("distance_to_molecule")
        if (
            not isinstance(force_field, str)
            or not isinstance(water_type, str)
            or not isinstance(box_type, str)
            or isinstance(distance, bool)
            or not isinstance(distance, (int, float))
        ):
            raise RuntimeError("validated BioBB scientific configuration was lost")
        args = [
            python_bin,
            str(script),
            str(staged),
            str(staged.parent),
            "--force-field",
            force_field,
            "--water-type",
            water_type,
            "--box-type",
            box_type,
            "--distance-to-molecule",
            str(float(distance)),
            "--gmx",
            "gmx",
        ]
        if self.config.get("ignore_input_hydrogens") is True:
            args.append("--ignore-input-hydrogens")
        if self.config.get("merge_chains") is True:
            args.append("--merge-chains")
        command = " ".join(shlex.quote(argument) for argument in args)
        result = Exec(
            command,
            LocalExecInfo(
                env=self.mod_env,
                cwd=str(staged.parent),
                line_callback=self.runtime_line_callback(),
            ),
        ).run()
        failures = {host: code for host, code in result.exit_code.items() if code != 0}
        if failures:
            raise RuntimeError(f"BioBB MD setup failed: {failures}")

    def _legacy_integer(self, name: str, default: int) -> int:
        """Read one legacy container integer without permissive coercion."""

        value = self.config.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        return value

    def _legacy_string(self, name: str, default: str) -> str:
        """Read one legacy container string without implicit serialization."""

        value = self.config.get(name, default)
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        return value

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Launch biobb_wf_md_setup.

        Container mode delegates to /opt/run_batch.sh, which sets PATH,
        runs the MD setup pipeline in a /tmp scratch dir (GROMACS/biobb
        use atomic write-then-rename, which wrp_cte_fuse does not
        support), and stages the finished outputs into the configured
        output directory via plain cp — which exercises the CTE adapter
        when ``out`` is on the FUSE mountpoint.

        run_md_setup.py takes POSITIONAL args (pdb, workdir), not
        --pdb/--out flags; the wrapper script handles that and lets a
        pdb_file config point at either a single PDB or a directory of
        PDBs.
        """
        if self.config.get("deploy_mode") == "container":
            replicates = max(self._legacy_integer("replicates", 1), 1)
            parallel = max(self._legacy_integer("parallel_reps", 1), 1)
            omp = self._legacy_integer("omp_threads", 0)
            pdb_in = self._legacy_string("pdb_file", "")
            out_root = self._legacy_string("out", "run")

            if parallel <= 1:
                # Original single-host sequential path. Preserved for
                # back-compat / small-replicates runs where cross-node
                # fan-out's apptainer-exec startup cost would dominate.
                if replicates == 1:
                    cmd = f"/opt/run_batch.sh '{pdb_in}' '{out_root}'"
                else:
                    cmd = (
                        f"set -e; "
                        f"for i in $(seq 1 {replicates}); do "
                        f"  rep=$(printf 'rep_%03d' \"$i\"); "
                        f'  echo "=== biobb replicate $rep ($i/{replicates}) ==="; '
                        f"  /opt/run_batch.sh '{pdb_in}' '{out_root}/'$rep || exit 1; "
                        f"done"
                    )
                Exec(
                    cmd,
                    LocalExecInfo(
                        container=self._container_engine,
                        container_image=self.deploy_image_name(),
                        shared_dir=self.shared_dir,
                        private_dir=self.private_dir,
                        env=self.mod_env,
                    ),
                ).run()
                return

            # parallel_reps > 1: fan replicates across every host in the
            # SLURM allocation via PsshExecInfo, with each host running
            # `parallel` reps concurrently in `wait -n`-bounded batches.
            # Apptainer instances are now started on every host by the
            # pipeline pre-start (see pipeline.py — start uses PsshExecInfo
            # symmetric with stop/kill), so `apptainer exec instance://...`
            # works on every remote.
            #
            # Per-rep BIOBB_SCRATCH_DIR override goes to /dev/shm (per-host
            # tmpfs auto-mounted by apptainer). /tmp would land in the
            # shared NFS overlay (pipeline.py uses --no-mount tmp + a
            # single overlay dir bound into every host's instance), where
            # 8 concurrent reps across 4 hosts race on mkdir and most
            # fail with "File exists / Invalid argument".
            nhosts = max(len(self.hostfile.hosts), 1) if self.hostfile else 1
            local_reps = (replicates + nhosts - 1) // nhosts  # ceil
            scratch_root = self._legacy_string(
                "parallel_scratch_root", "/dev/shm"
            ).rstrip("/")
            omp_export = f"export OMP_NUM_THREADS={omp}; " if omp > 0 else ""
            # MD extension: when md_steps > 0, chain the in-container
            # helper script after /opt/run_batch.sh in each rep. Adds
            # an EM + production-NVT pass on top of the setup-only biobb
            # pipeline so the workflow generates real trajectory data
            # decoupled from compute step count via nstxout-compressed.
            md_steps = self._legacy_integer("md_steps", 0)
            md_nstxout = self._legacy_integer("md_nstxout", 100)
            md_extend_script = self._legacy_string(
                "md_extend_script", "/opt/biobb_md_extend.sh"
            )
            md_export = ""
            md_chain = ""
            if md_steps > 0:
                md_export = (
                    f"export MD_STEPS={md_steps}; export MD_NSTXOUT={md_nstxout}; "
                )
                md_chain = f'      && {md_extend_script} "$BIOBB_SCRATCH_DIR" "$out" '
            # Build command. Bash $$, $!, ${{}}, ${{#PIDS[@]}}, ${{PIDS[@]:1}}
            # all need doubled braces / escape so f-string passes them
            # through untouched.
            cmd = (
                f"set -e; "
                f"{omp_export}"
                f"{md_export}"
                f"NHOSTS={nhosts}; "
                f"LOCAL={local_reps}; "
                f"PAR={parallel}; "
                f"PDB='{pdb_in}'; "
                f"OUT='{out_root}'; "
                f"H=$(hostname -s); "
                f'echo "[biobb-parallel] host=$H reps=$LOCAL parallel=$PAR omp=${{OMP_NUM_THREADS:-default}} md_steps=${{MD_STEPS:-0}} md_nstxout=${{MD_NSTXOUT:-0}}"; '
                f"PIDS=(); "
                f"for i in $(seq 1 $LOCAL); do "
                f"  ( "
                f'    rep=$(printf \'rep_%03d-%s\' "$i" "$H"); '
                f'    out="$OUT/$rep"; '
                f'    export BIOBB_SCRATCH_DIR="{scratch_root}/biobb-scratch-$rep"; '
                f'    /opt/run_batch.sh "$PDB" "$out" '
                f"{md_chain}"
                f'      && echo "[biobb-parallel] host=$H $rep DONE" '
                f'      || {{ echo "[biobb-parallel] host=$H $rep FAILED" >&2; exit 1; }} '
                f"  ) & "
                f"  PIDS+=($!); "
                f'  if [ "${{#PIDS[@]}}" -ge $PAR ]; then '
                f"    wait -n; "
                f'    PIDS=("${{PIDS[@]:1}}"); '
                f"  fi; "
                f"done; "
                f"wait"
            )
            Exec(
                cmd,
                PsshExecInfo(
                    hostfile=self.hostfile,
                    container=self._container_engine,
                    container_image=self.deploy_image_name(),
                    shared_dir=self.shared_dir,
                    private_dir=self.private_dir,
                    env=self.mod_env,
                ),
            ).run()
        else:
            self._start_native()

    def stop(self) -> None:
        """Stop biobb_wf_md_setup (no-op -- runs to completion)."""
        pass

    def clean(self) -> None:
        """Remove only the exact configured BioBB output directory."""

        output_dir = self._output_dir()
        if output_dir == Path(output_dir.anchor):
            raise ValueError("refusing to clean a filesystem root as BioBB output")
        result = Rm(
            str(output_dir),
            self._node_exec_info(env=self.env),
            recursive=True,
        ).run()
        failures = {host: code for host, code in result.exit_code.items() if code != 0}
        if failures:
            raise RuntimeError(f"Failed to clean BioBB output {output_dir}: {failures}")
