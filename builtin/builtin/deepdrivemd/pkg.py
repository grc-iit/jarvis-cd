"""
This module provides classes and methods to launch the DeepDriveMD
application.  DeepDriveMD is an adaptive biomolecular simulation framework
that interleaves MD simulation, representation learning, model selection,
and an agent that steers new rounds of sampling.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, LocalExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm


class Deepdrivemd(Application):
    """
    Deepdrivemd class supporting both default (bare-metal) and container
    deployment.

    Set deploy_mode='container' to build and run DeepDriveMD inside a
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
                'name': 'ppn',
                'msg': 'Processes per node',
                'type': int,
                'default': 1,
            },
            {
                'name': 'iterations',
                'msg': 'Number of DDMD iterations',
                'type': int,
                'default': 2,
            },
            {
                'name': 'num_tasks',
                'msg': ('Number of MD tasks per iteration (drives the '
                        'NUM_TASKS template placeholder; each task writes '
                        '~4 MB of /dev/urandom from ddmd_io_task.sh). '
                        'Total MD bytes per run = iterations * num_tasks '
                        '* 4 MiB. Defaults to nprocs for backwards '
                        'compatibility.'),
                'type': int,
                'default': None,
            },
            {
                'name': 'replicates',
                'msg': ('Run the entire DDMD pipeline this many times '
                        'back-to-back in one container exec, each into '
                        'rep_NNN/ under `out`. Use this to amplify total '
                        'I/O when iterations * num_tasks is capped by '
                        'RADICAL\'s walltime.'),
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
                'name': 'omp_threads',
                'msg': ('OMP_NUM_THREADS for each parallel replicate. '
                        '0 = unset (RADICAL/OpenMM defaults).'),
                'type': int,
                'default': 0,
            },
            {
                'name': 'out',
                'msg': 'Output directory for results',
                'type': str,
                'default': '/tmp/ddmd_out',
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
        return content, 'openmm'

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'DEPLOY_BASE': 'ubuntu:24.04',
        })
        return content, 'openmm'

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        """
        Configure DeepDriveMD.

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
        Launch DeepDriveMD.

        Branches on deploy_mode: uses LocalExecInfo with container engine for
        container mode, LocalExecInfo for default mode. With parallel_reps > 1
        and deploy_mode='container', fans replicates across hosts via
        PsshExecInfo and runs parallel_reps in parallel per host.
        """
        out = self.config['out']
        iterations = self.config['iterations']
        nprocs = self.config['nprocs']
        num_tasks = self.config.get('num_tasks') or nprocs
        replicates = max(int(self.config.get('replicates', 1) or 1), 1)
        parallel = max(int(self.config.get('parallel_reps', 1) or 1), 1)
        omp = int(self.config.get('omp_threads', 0) or 0)

        if parallel <= 1:
            # Single-host sequential path (original).
            if replicates == 1:
                cmd = f'/opt/run_ddmd.sh {out} {iterations} {num_tasks}'
            else:
                cmd = (
                    "set -e; "
                    f"for i in $(seq 1 {replicates}); do "
                    f"  rep=$(printf 'rep_%03d' \"$i\"); "
                    f"  echo \"=== ddmd replicate $rep ($i/{replicates}) ===\"; "
                    f"  /opt/run_ddmd.sh '{out}/'$rep {iterations} {num_tasks} || exit 1; "
                    f"done"
                )
            if self.config.get('deploy_mode') == 'container':
                Mkdir(out).run()
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

        # parallel_reps > 1, container mode only — fan replicates across
        # hosts via PsshExecInfo + per-host wait -n batching. Each host
        # runs ceil(replicates/nhosts) reps; rep tags include hostname so
        # output paths don't collide on shared storage.
        nhosts = max(len(self.hostfile.hosts), 1) if self.hostfile else 1
        local_reps = (replicates + nhosts - 1) // nhosts
        omp_export = (
            f"export OMP_NUM_THREADS={omp}; " if omp > 0 else ""
        )
        cmd = (
            f"set -e; "
            f"{omp_export}"
            f"LOCAL={local_reps}; "
            f"PAR={parallel}; "
            f"OUT='{out}'; "
            f"H=$(hostname -s); "
            f"echo \"[ddmd-parallel] host=$H reps=$LOCAL parallel=$PAR omp=${{OMP_NUM_THREADS:-default}}\"; "
            f"PIDS=(); "
            f"for i in $(seq 1 $LOCAL); do "
            f"  ( "
            f"    rep=$(printf 'rep_%03d-%s' \"$i\" \"$H\"); "
            f"    /opt/run_ddmd.sh \"$OUT/$rep\" {iterations} {num_tasks} "
            f"      && echo \"[ddmd-parallel] host=$H $rep DONE\" "
            f"      || {{ echo \"[ddmd-parallel] host=$H $rep FAILED\" >&2; exit 1; }} "
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
            Mkdir(out).run()
            Exec(cmd, PsshExecInfo(
                hostfile=self.hostfile,
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
                env=self.mod_env,
            )).run()
        else:
            # Bare-metal multi-host parallel — same shape but no container.
            Exec(cmd, PsshExecInfo(
                hostfile=self.hostfile, env=self.mod_env)).run()

    def stop(self):
        """Stop DeepDriveMD (no-op -- runs to completion)."""
        pass

    def clean(self):
        """Remove DeepDriveMD output directory."""
        if self.config['out']:
            Rm(self.config['out'] + '*',
               PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()
