"""
This module provides classes and methods to launch the biobb_wf_md_setup
application.  BioExcel Building Blocks MD setup pipeline: fetch PDB,
fix side chains, topology, box, solvation via GROMACS.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, LocalExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm


class BiobbWfMdSetup(Application):
    """
    BiobbWfMdSetup class supporting both default (bare-metal) and container
    deployment.

    Set deploy_mode='container' to build and run the biobb MD setup pipeline
    inside a Docker/Podman/Apptainer container with GROMACS 2026.
    Set deploy_mode='default' to use a system-installed environment.
    """

    def _init(self):
        pass

    def _configure_menu(self):
        return [
            {
                'name': 'nprocs',
                'msg': 'Number of MPI processes',
                'type': int,
                'default': 1,
            },
            {
                'name': 'ppn',
                'msg': 'Processes per node',
                'type': int,
                'default': 1,
            },
            {
                'name': 'pdb_file',
                'msg': 'Path to input PDB file',
                'type': str,
                'default': '/opt/biobb-bench/1AKI.pdb',
            },
            {
                'name': 'out',
                'msg': 'Output directory for results',
                'type': str,
                'default': '/tmp/biobb_out',
            },
            {
                'name': 'replicates',
                'msg': ('Number of times to run the MD-setup pipeline '
                        'back-to-back inside the same container exec. '
                        'Used to scale I/O for benchmarking when the '
                        'bundled lysozyme PDB is too small to dominate '
                        'wall time on its own.'),
                'type': int,
                'default': 1,
            },
            {
                'name': 'parallel_scratch_root',
                'msg': ('Root dir for per-rep BIOBB_SCRATCH_DIR when '
                        'parallel_reps > 1. Each rep gets '
                        '`<root>/biobb-scratch-<rep>`. Default /dev/shm '
                        '(node-local tmpfs). Point at a FUSE mount '
                        'to route every GROMACS/biobb intermediate '
                        'through that adapter.'),
                'type': str,
                'default': '/dev/shm',
            },
            {
                'name': 'parallel_reps',
                'msg': ('Per-host replicate concurrency. When > 1, the '
                        'replicates loop fans across hosts via '
                        'PsshExecInfo and each host runs this many in '
                        'parallel using `wait -n` batching. Default 1 '
                        'preserves the original host[0]-only sequential '
                        'loop.'),
                'type': int,
                'default': 1,
            },
            {
                'name': 'omp_threads',
                'msg': ('OMP_NUM_THREADS for each parallel replicate. '
                        'Set to roughly cores_per_node / parallel_reps so '
                        'concurrent GROMACS instances on the same host '
                        'don\'t oversubscribe. 0 = leave unset (GROMACS '
                        'auto-detects which over-grabs when run in '
                        'parallel).'),
                'type': int,
                'default': 0,
            },
            {
                'name': 'md_steps',
                'msg': ('Production MD step count for the biobb_md_extend '
                        'post-step. Each step is 2 fs by default, so 5000 '
                        'steps ≈ 10 ps simulated time. 0 = skip the MD '
                        'extension entirely (legacy setup-only behavior).'),
                'type': int,
                'default': 0,
            },
            {
                'name': 'md_nstxout',
                'msg': ('XTC frame stride for the production MD '
                        '(`nstxout-compressed` in the .mdp). Lower = more '
                        'frames written = more I/O without changing '
                        'compute. The per-rep trajectory file size is '
                        'roughly md_steps / md_nstxout * frame_size.'),
                'type': int,
                'default': 100,
            },
            {
                'name': 'md_extend_script',
                'msg': ('In-container path to the md-extend helper '
                        'script. Bind the host script at this path via '
                        '`container_binds` (e.g. '
                        '${HOME}/jarvis-bench-scripts/biobb_md_extend.sh:'
                        '/opt/biobb_md_extend.sh) and set md_steps > 0 '
                        'to enable.'),
                'type': str,
                'default': '/opt/biobb_md_extend.sh',
            },
            {
                'name': 'base_image',
                'msg': 'Base Docker image for build container',
                'type': str,
                'default': 'sci-hpc-base',
            },
        ]

    # ------------------------------------------------------------------
    # Container Dockerfile generators
    # ------------------------------------------------------------------

    def _build_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        content = self._read_build_script('build.sh', {
            'BASE_IMAGE': self.config.get('base_image', 'sci-hpc-base'),
        })
        return content, 'gromacs2026'

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'DEPLOY_BASE': 'ubuntu:24.04',
        })
        return content, 'gromacs2026'

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        """
        Configure biobb_wf_md_setup.

        Calls super()._configure() which updates self.config and (when
        deploy_mode == 'container') triggers build_phase / build_deploy_phase.

        In default mode, also creates the output directory on all nodes.
        """
        super()._configure(**kwargs)

        if self.config.get('deploy_mode') == 'default':
            if self.config['out']:
                Mkdir(self.config['out'],
                      PsshExecInfo(hostfile=self.hostfile,
                                   env=self.env)).run()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
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
        if self.config.get('deploy_mode') == 'container':
            replicates = max(int(self.config.get('replicates', 1) or 1), 1)
            parallel = max(int(self.config.get('parallel_reps', 1) or 1), 1)
            omp = int(self.config.get('omp_threads', 0) or 0)
            pdb_in = self.config['pdb_file']
            out_root = self.config['out']

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
                        f"  echo \"=== biobb replicate $rep ($i/{replicates}) ===\"; "
                        f"  /opt/run_batch.sh '{pdb_in}' '{out_root}/'$rep || exit 1; "
                        f"done"
                    )
                Exec(cmd, LocalExecInfo(
                    container=self._container_engine,
                    container_image=self.deploy_image_name(),
                    shared_dir=self.shared_dir,
                    private_dir=self.private_dir,
                    env=self.mod_env,
                )).run()
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
            scratch_root = (self.config.get('parallel_scratch_root')
                            or '/dev/shm').rstrip('/')
            omp_export = (
                f"export OMP_NUM_THREADS={omp}; " if omp > 0 else ""
            )
            # MD extension: when md_steps > 0, chain the in-container
            # helper script after /opt/run_batch.sh in each rep. Adds
            # an EM + production-NVT pass on top of the setup-only biobb
            # pipeline so the workflow generates real trajectory data
            # decoupled from compute step count via nstxout-compressed.
            md_steps = int(self.config.get('md_steps', 0) or 0)
            md_nstxout = int(self.config.get('md_nstxout', 100) or 100)
            md_extend_script = self.config.get('md_extend_script') or \
                '/opt/biobb_md_extend.sh'
            md_export = ""
            md_chain = ""
            if md_steps > 0:
                md_export = (
                    f"export MD_STEPS={md_steps}; "
                    f"export MD_NSTXOUT={md_nstxout}; "
                )
                md_chain = (
                    f"      && {md_extend_script} "
                    f"\"$BIOBB_SCRATCH_DIR\" \"$out\" "
                )
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
                f"echo \"[biobb-parallel] host=$H reps=$LOCAL parallel=$PAR omp=${{OMP_NUM_THREADS:-default}} md_steps=${{MD_STEPS:-0}} md_nstxout=${{MD_NSTXOUT:-0}}\"; "
                f"PIDS=(); "
                f"for i in $(seq 1 $LOCAL); do "
                f"  ( "
                f"    rep=$(printf 'rep_%03d-%s' \"$i\" \"$H\"); "
                f"    out=\"$OUT/$rep\"; "
                f"    export BIOBB_SCRATCH_DIR=\"{scratch_root}/biobb-scratch-$rep\"; "
                f"    /opt/run_batch.sh \"$PDB\" \"$out\" "
                f"{md_chain}"
                f"      && echo \"[biobb-parallel] host=$H $rep DONE\" "
                f"      || {{ echo \"[biobb-parallel] host=$H $rep FAILED\" >&2; exit 1; }} "
                f"  ) & "
                f"  PIDS+=($!); "
                f"  if [ \"${{#PIDS[@]}}\" -ge $PAR ]; then "
                f"    wait -n; "
                f"    PIDS=(\"${{PIDS[@]:1}}\"); "
                f"  fi; "
                f"done; "
                f"wait"
            )
            Exec(cmd, PsshExecInfo(
                hostfile=self.hostfile,
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
                env=self.mod_env,
            )).run()
        else:
            cmd = ' '.join([
                'python3', '/opt/run_md_setup.py',
                self.config['pdb_file'],
                self.config['out'],
            ])
            Exec(cmd, LocalExecInfo(env=self.mod_env)).run()

    def stop(self):
        """Stop biobb_wf_md_setup (no-op -- runs to completion)."""
        pass

    def clean(self):
        """Remove biobb_wf_md_setup output directory."""
        if self.config['out']:
            Rm(self.config['out'] + '*',
               PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()
