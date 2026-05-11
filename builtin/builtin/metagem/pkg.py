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

        if self.config.get('deploy_mode') == 'container':
            # /opt/run_metagem.sh runs fastp by reading inputs from
            # $WORKDIR/dataset on the CTE FUSE mount. wrp_cte_fuse can
            # round-trip large .gz files for whole-file consumers
            # (md5sum, wc -c, plain cat) but igzip's seek+chunked read
            # pattern returns short data and fastp dies with
            # "ERROR: igzip: unexpected eof" before any sample is
            # processed. We bypass /opt/run_metagem.sh entirely:
            # download into /tmp scratch, run fastp with /tmp inputs
            # (so igzip stays on a working FS), and stage the small
            # fastp outputs (.json, .html, filtered .gz) into FUSE
            # one file at a time -- still exercises wrp_cte_libfuse on
            # the write path, which is what metagem cares about.
            cmd = (
                'set -euo pipefail; '
                'STAGE=/tmp/metagem-prestage; '
                'mkdir -p "$STAGE"; '
                'if [ ! -f "$STAGE/.staged" ]; then '
                '  rm -rf "$STAGE"/*; '
                '  ( cd "$STAGE"; '
                '    while IFS= read -r url; do '
                '      [ -z "$url" ] && continue; '
                '      fname=$(basename "$url" | sed -e "s/?download=1//g" -e "s/_/_R/g"); '
                '      [ -f "$fname" ] || wget -q -O "$fname" "$url" || exit 1; '
                '    done < /opt/metaGEM/workflow/scripts/download_toydata.txt; '
                '    for f in *.gz; do '
                '      [ -e "$f" ] || continue; '
                '      sid="${f%%_R*}"; '
                '      mkdir -p "$sid"; '
                '      mv "$f" "$sid/"; '
                '    done; '
                '  ); '
                '  touch "$STAGE/.staged"; '
                'fi; '
                'FASTP=/opt/conda/envs/metagem/bin/fastp; '
                '[ -x "$FASTP" ] || { echo "FAIL: fastp not found at $FASTP" >&2; exit 1; }; '
                'TMP_OUT=/tmp/metagem-qfiltered; '
                'rm -rf "$TMP_OUT"; '
                'mkdir -p "$TMP_OUT"; '
                'ran_any=0; '
                'for sample_dir in "$STAGE"/*/; do '
                '  [ -d "$sample_dir" ] || continue; '
                '  sid="$(basename "$sample_dir")"; '
                '  in_r1="$sample_dir${sid}_R1.fastq.gz"; '
                '  in_r2="$sample_dir${sid}_R2.fastq.gz"; '
                '  if [ ! -f "$in_r1" ] || [ ! -f "$in_r2" ]; then '
                '    echo "SKIP $sid: missing R1/R2 under $sample_dir" >&2; '
                '    continue; '
                '  fi; '
                '  tmp_sd="$TMP_OUT/$sid"; '
                '  mkdir -p "$tmp_sd"; '
                '  echo "=== fastp $sid (reads /tmp, writes /tmp) ==="; '
                '  "$FASTP" --thread "${CORES:-4}" '
                '    -i "$in_r1" -I "$in_r2" '
                '    -o "$tmp_sd/${sid}_R1.fastq.gz" '
                '    -O "$tmp_sd/${sid}_R2.fastq.gz" '
                '    -j "$tmp_sd/${sid}.json" '
                '    -h "$tmp_sd/${sid}.html"; '
                '  ran_any=1; '
                'done; '
                'if [ "$ran_any" -eq 0 ]; then '
                '  echo "FAIL: no sample subdirs with R1+R2 under $STAGE" >&2; '
                '  exit 1; '
                'fi; '
                f'mkdir -p {out}/qfiltered; '
                'fail=0; '
                'for tmp_sd in "$TMP_OUT"/*/; do '
                '  [ -d "$tmp_sd" ] || continue; '
                '  sid="$(basename "$tmp_sd")"; '
                f'  dst_sd={out}/qfiltered/$sid; '
                '  mkdir -p "$dst_sd"; '
                '  for f in "$tmp_sd"*; do '
                '    [ -f "$f" ] || continue; '
                '    cp "$f" "$dst_sd/$(basename "$f")" || fail=1; '
                '    sync; '
                '  done; '
                'done; '
                '[ "$fail" -eq 0 ] || { echo "FAIL: staging fastp outputs into FUSE" >&2; exit 1; }; '
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
