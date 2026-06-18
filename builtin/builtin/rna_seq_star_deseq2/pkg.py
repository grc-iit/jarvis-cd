"""
This module provides classes and methods to launch the rna_seq_star_deseq2
application.  Snakemake workflow for RNA-seq differential expression analysis
with STAR aligner and DESeq2.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, LocalExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm


class RnaSeqStarDeseq2(Application):
    """
    RnaSeqStarDeseq2 class supporting both default (bare-metal) and container
    deployment.

    Set deploy_mode='container' to build and run the RNA-seq pipeline inside a
    Docker/Podman/Apptainer container.  Set deploy_mode='default' to use a
    system-installed environment.
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
                'name': 'cores',
                'msg': 'Number of Snakemake cores',
                'type': int,
                'default': 4,
            },
            {
                'name': 'out',
                'msg': 'Output directory for results',
                'type': str,
                'default': '/tmp/rnaseq_out',
            },
            {
                'name': 'replicates',
                'msg': ('Run the full snakemake RNA-seq workflow this '
                        'many times back-to-back in one container exec, '
                        'each into rep_NNN/ under `out`. Each replicate '
                        'rebuilds STAR index, aligns FASTQs, and writes '
                        'BAM + DESeq2 results, so I/O scales linearly.'),
                'type': int,
                'default': 1,
            },
            {
                'name': 'parallel_reps',
                'msg': ('Per-host replicate concurrency. When > 1, the '
                        'replicates loop fans across hosts via PsshExecInfo '
                        'and each host runs this many in parallel using '
                        '`wait -n` batching. Default 1 preserves the '
                        'original host[0]-only sequential loop.'),
                'type': int,
                'default': 1,
            },
            {
                'name': 'parallel_scratch_root',
                'msg': ('Root dir for per-rep SNAKEMAKE_SCRATCH_DIR when '
                        'parallel_reps > 1. Each rep gets '
                        '`<root>/rnaseq-scratch-<rep>`. Default /tmp keeps '
                        'snakemake/STAR intermediates on node-local '
                        'disk; point at a FUSE mount (e.g. '
                        '/mnt/dumbwarp/scratch) to route every '
                        'STAR-index / BAM / DESeq2 R/W through that '
                        'adapter.'),
                'type': str,
                'default': '/tmp',
            },
            {
                'name': 'omp_threads',
                'msg': ('OMP_NUM_THREADS for each parallel replicate. '
                        'Snakemake threading is governed separately by '
                        '`cores`. 0 = unset.'),
                'type': int,
                'default': 0,
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
        return content, 'snakemake'

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'DEPLOY_BASE': 'ubuntu:24.04',
        })
        return content, 'snakemake'

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        """
        Configure rna_seq_star_deseq2.

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
        Launch rna_seq_star_deseq2.

        run_rnaseq.sh takes the output directory as its first positional
        argument. Snakemake runs in a /tmp scratch workdir (snakemake
        uses atomic write-then-rename, which wrp_cte_fuse does not
        support) and the final results tree is staged into ``out`` via
        plain cp — which exercises the CTE adapter when ``out`` is on
        the FUSE mountpoint.
        """
        out_root = self.config['out']
        replicates = max(int(self.config.get('replicates', 1) or 1), 1)
        parallel = max(int(self.config.get('parallel_reps', 1) or 1), 1)
        omp = int(self.config.get('omp_threads', 0) or 0)

        if parallel <= 1:
            # Single-host sequential path (original).
            if replicates == 1:
                cmd = f"/opt/run_rnaseq.sh '{out_root}'"
            else:
                cmd = (
                    "set -e; "
                    f"for i in $(seq 1 {replicates}); do "
                    f"  rep=$(printf 'rep_%03d' \"$i\"); "
                    f"  echo \"=== rna_seq replicate $rep ($i/{replicates}) ===\"; "
                    f"  /opt/run_rnaseq.sh '{out_root}/'$rep || exit 1; "
                    f"done"
                )
            if self.config.get('deploy_mode') == 'container':
                Exec(cmd, LocalExecInfo(
                    container=self._container_engine,
                    container_image=self.deploy_image_name(),
                    shared_dir=self.shared_dir,
                    private_dir=self.private_dir,
                    env=self.mod_env,
                )).run()
            else:
                Exec(cmd, LocalExecInfo(env=self.mod_env)).run()
            return

        # parallel_reps > 1: PsshExecInfo fan-out + per-host wait -n
        # batching. Snakemake's /tmp/rnaseq-scratch is wiped at start of
        # each invocation; concurrent runs on the same host need a per-
        # rep scratch dir, which the runner script honors via
        # SNAKEMAKE_SCRATCH_DIR (mirroring biobb/montage's pattern).
        nhosts = max(len(self.hostfile.hosts), 1) if self.hostfile else 1
        local_reps = (replicates + nhosts - 1) // nhosts
        scratch_root = (self.config.get('parallel_scratch_root')
                        or '/tmp').rstrip('/')
        omp_export = (
            f"export OMP_NUM_THREADS={omp}; " if omp > 0 else ""
        )
        cmd = (
            f"set -e; "
            f"{omp_export}"
            f"LOCAL={local_reps}; "
            f"PAR={parallel}; "
            f"OUT='{out_root}'; "
            f"H=$(hostname -s); "
            f"echo \"[rnaseq-parallel] host=$H reps=$LOCAL parallel=$PAR omp=${{OMP_NUM_THREADS:-default}}\"; "
            f"PIDS=(); "
            f"for i in $(seq 1 $LOCAL); do "
            f"  ( "
            f"    rep=$(printf 'rep_%03d-%s' \"$i\" \"$H\"); "
            f"    RNASEQ_SCRATCH_DIR=\"{scratch_root}/rnaseq-scratch-$rep\" "
            f"    /opt/run_rnaseq.sh \"$OUT/$rep\" "
            f"      && echo \"[rnaseq-parallel] host=$H $rep DONE\" "
            f"      || {{ echo \"[rnaseq-parallel] host=$H $rep FAILED\" >&2; exit 1; }} "
            f"  ) & "
            f"  PIDS+=($!); "
            f"  if [ \"${{#PIDS[@]}}\" -ge $PAR ]; then "
            f"    wait -n; "
            f"    PIDS=(\"${{PIDS[@]:1}}\"); "
            f"  fi; "
            f"done; "
            f"wait"
        )
        if self.config.get('deploy_mode') == 'container':
            Exec(cmd, PsshExecInfo(
                hostfile=self.hostfile,
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
                env=self.mod_env,
            )).run()
        else:
            Exec(cmd, PsshExecInfo(
                hostfile=self.hostfile, env=self.mod_env)).run()

    def stop(self):
        """Stop rna_seq_star_deseq2 (no-op -- runs to completion)."""
        pass

    def clean(self):
        """Remove rna_seq_star_deseq2 output directory."""
        if self.config['out']:
            Rm(self.config['out'] + '*',
               PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()
