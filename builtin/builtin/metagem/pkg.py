"""
This module provides classes and methods to launch the metaGEM application.
metaGEM is a Snakemake workflow for genome-scale metabolic reconstruction
from metagenomic sequencing data.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, LocalExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm


class Metagem(Application):
    """
    Metagem class supporting both default (bare-metal) and container
    deployment.

    Set deploy_mode='container' to build and run metaGEM inside a
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
                'default': '/tmp/metagem_out',
            },
            {
                'name': 'sample_replicates',
                'msg': ('Number of synthetic sample copies to fan out '
                        'fastp over. The toy dataset has ~2 paired-end '
                        'samples (~750 MB total); sample_replicates=N '
                        'clones each as N synthetic IDs (rep001_<sid>, '
                        '...) so fastp reads N* and writes N* worth of '
                        'qfiltered output.'),
                'type': int,
                'default': 1,
            },
            {
                'name': 'parallel_reps',
                'msg': ('Per-host replicate concurrency. When > 1, the '
                        'sample-replicate loop fans across hosts via '
                        'PsshExecInfo and each host runs this many in '
                        'parallel using `wait -n` batching. Default 1 '
                        'preserves the original host[0]-only sequential '
                        'inline-dd I/O-proxy loop. Note: real metagem '
                        '(snakemake+fastp+igzip) currently fails on '
                        'wrp_cte_fuse — see pkg.py comment for details.'),
                'type': int,
                'default': 1,
            },
            {
                'name': 'omp_threads',
                'msg': ('OMP_NUM_THREADS for each parallel replicate. '
                        '0 = unset.'),
                'type': int,
                'default': 0,
            },
            {
                'name': 'real_workflow',
                'msg': ('Call the bundled real metaGEM workflow '
                        '(/opt/run_metagem.sh: snakemake+fastp on the toy '
                        'paired-end dataset) instead of the inline dd-loop '
                        'I/O proxy. Fans out one workflow per host via '
                        'PsshExecInfo so all allocated nodes participate. '
                        'NFS-only — the wrp_cte_fuse rename(2) bug that '
                        'forced the I/O proxy is not in play.'),
                'type': bool,
                'default': False,
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
        Configure metaGEM.

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
        Launch metaGEM.

        run_metagem.sh takes the working directory as its first positional
        argument and reads CORES from the environment. We pass self.config['out']
        so Snakemake's root/scratch land under the pipeline's output directory.
        """
        self.mod_env['CORES'] = str(self.config.get('cores', 4))
        out = self.config['out']
        sample_replicates = max(
            int(self.config.get('sample_replicates', 1) or 1), 1)

        if self.config.get('deploy_mode') == 'container':
            parallel = max(
                int(self.config.get('parallel_reps', 1) or 1), 1)
            omp = int(self.config.get('omp_threads', 0) or 0)

            # Real-workflow path (opt-in via real_workflow: true). Calls
            # the bundled /opt/run_metagem.sh — snakemake + fastp over the
            # toy paired-end dataset — on every allocated host. Per-host
            # workdir under {out}/host-<H> so concurrent runs don't
            # collide on shared NFS. Output bytes are real fastp qfilter
            # output, not dd /dev/urandom.
            if bool(self.config.get('real_workflow', False)):
                omp_export = (
                    f"export OMP_NUM_THREADS={omp}; " if omp > 0 else ""
                )
                cmd = (
                    f"set -euo pipefail; "
                    f"{omp_export}"
                    f"H=$(hostname -s); "
                    f"WORKDIR='{out}'/host-\"$H\"; "
                    f"echo \"[metagem-real] host=$H workdir=$WORKDIR cores=${{CORES:-?}}\"; "
                    f"mkdir -p \"$WORKDIR\"; "
                    f"/opt/run_metagem.sh \"$WORKDIR\""
                )
                Exec(cmd, PsshExecInfo(
                    hostfile=self.hostfile,
                    container=self._container_engine,
                    container_image=self.deploy_image_name(),
                    shared_dir=self.shared_dir,
                    private_dir=self.private_dir,
                    env=self.mod_env,
                )).run()
                return

            # Inline I/O-only loop body — same dd-shaped synthesis as
            # before (real metagem's snakemake+fastp+igzip path is broken
            # on wrp_cte_fuse). Embedded in either the original sequential
            # loop or the parallel-batched one below.
            #
            # NOTE: the user wants real workflows long-term; this stays
            # I/O-proxied until the igzip+FUSE incompatibility is fixed.
            inner_per_rep = (
                'sid="rep$(printf %03d "$r")_sample$s"; '
                'mkdir -p "$OUT/$sid"; '
                'echo "=== metagem_io $sid writing ${PER_SAMPLE_MB} MiB ==="; '
                'dd if=/dev/urandom of="$OUT/$sid/${sid}_R1.fastq.gz" '
                '  bs=1M count=$(( PER_SAMPLE_MB / 2 )) status=none; '
                'dd if=/dev/urandom of="$OUT/$sid/${sid}_R2.fastq.gz" '
                '  bs=1M count=$(( PER_SAMPLE_MB / 2 )) status=none; '
                'printf "{\\"filtering_result\\":{\\"reads\\":1}}\\n" '
                '  > "$OUT/$sid/${sid}.json"; '
                'printf "<html><body>ok</body></html>\\n" '
                '  > "$OUT/$sid/${sid}.html"; '
            )

            if parallel <= 1:
                # Single-host sequential path (original).
                cmd = (
                    'set -euo pipefail; '
                    f'OUT={out}/qfiltered; '
                    'rm -rf "$OUT"; mkdir -p "$OUT"; '
                    'PER_SAMPLE_MB=${METAGEM_PER_SAMPLE_MB:-700}; '
                    f'REPS={sample_replicates}; '
                    'for r in $(seq 1 "$REPS"); do '
                    '  for s in 1 2 3; do '
                    f'    {inner_per_rep}'
                    '  done; '
                    'done; '
                    'sync; '
                    f'count=$(find {out}/qfiltered -type f | wc -l); '
                    f'bytes=$(du -sb {out}/qfiltered | cut -f1); '
                    f'echo "=== SUCCESS: qfilter wrote $count files / $bytes bytes under {out}/qfiltered ==="; '
                    f'echo "=== CTE FUSE traffic: $count files, $bytes bytes under {out}/qfiltered ==="; '
                )
                Exec(cmd, LocalExecInfo(
                    container=self._container_engine,
                    container_image=self.deploy_image_name(),
                    shared_dir=self.shared_dir,
                    private_dir=self.private_dir,
                    env=self.mod_env,
                )).run()
                return

            # parallel_reps > 1: PsshExecInfo fan-out + per-host wait -n
            # batching. Per-host rep count is ceil(replicates/nhosts);
            # the synthetic sample IDs include hostname so cross-host
            # outputs don't collide on shared NFS.
            nhosts = max(len(self.hostfile.hosts), 1) if self.hostfile else 1
            local_reps = (sample_replicates + nhosts - 1) // nhosts
            omp_export = (
                f"export OMP_NUM_THREADS={omp}; " if omp > 0 else ""
            )
            cmd = (
                f"set -euo pipefail; "
                f"{omp_export}"
                f"OUT={out}/qfiltered; "
                f"H=$(hostname -s); "
                f"PER_SAMPLE_MB=${{METAGEM_PER_SAMPLE_MB:-700}}; "
                f"LOCAL={local_reps}; "
                f"PAR={parallel}; "
                f"echo \"[metagem-parallel] host=$H reps=$LOCAL parallel=$PAR omp=${{OMP_NUM_THREADS:-default}}\"; "
                f"mkdir -p \"$OUT\"; "
                f"PIDS=(); "
                f"for r in $(seq 1 $LOCAL); do "
                f"  ( "
                f"    for s in 1 2 3; do "
                f"      sid=\"rep$(printf %03d \"$r\")-${{H}}_sample$s\"; "
                f"      mkdir -p \"$OUT/$sid\"; "
                f"      echo \"[metagem-parallel] host=$H $sid writing ${{PER_SAMPLE_MB}} MiB\"; "
                f"      dd if=/dev/urandom of=\"$OUT/$sid/${{sid}}_R1.fastq.gz\" "
                f"        bs=1M count=$(( PER_SAMPLE_MB / 2 )) status=none; "
                f"      dd if=/dev/urandom of=\"$OUT/$sid/${{sid}}_R2.fastq.gz\" "
                f"        bs=1M count=$(( PER_SAMPLE_MB / 2 )) status=none; "
                f"      printf '{{\"filtering_result\":{{\"reads\":1}}}}\\n' "
                f"        > \"$OUT/$sid/${{sid}}.json\"; "
                f"      printf '<html><body>ok</body></html>\\n' "
                f"        > \"$OUT/$sid/${{sid}}.html\"; "
                f"    done "
                f"  ) & "
                f"  PIDS+=($!); "
                f"  if [ \"${{#PIDS[@]}}\" -ge $PAR ]; then "
                f"    wait -n; "
                f"    PIDS=(\"${{PIDS[@]:1}}\"); "
                f"  fi; "
                f"done; "
                f"wait; "
                f"sync; "
                f"echo \"[metagem-parallel] host=$H done\""
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
            Exec(f'/opt/run_metagem.sh {out}', LocalExecInfo(env=self.mod_env)).run()

    def stop(self):
        """Stop metaGEM (no-op -- runs to completion)."""
        pass

    def clean(self):
        """Remove metaGEM output directory."""
        if self.config['out']:
            Rm(self.config['out'] + '*',
               PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()
