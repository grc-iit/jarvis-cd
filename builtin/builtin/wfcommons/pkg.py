"""
WfCommons (https://wfcommons.org/) jarvis package.

Generates a synthetic scientific workflow from a wfcommons recipe
(Montage, Genome, Cycles, etc.), translates it into a runnable WfBench
benchmark workflow, and executes the resulting tasks locally in
topological order via a thread pool.

Supports both bare-metal (default) and container deployment modes.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm


RECIPES = [
    'montage', 'genome', 'cycles', 'blast', 'bwa',
    'srasearch', 'epigenomics', 'seismology', 'soykb', 'rnaseq',
]


class Wfcommons(Application):
    """
    Wfcommons workflow benchmark runner.

    Generates a WfFormat workflow from a recipe, translates it with
    WfBench, and executes the tasks. The translator is invoked locally
    on a single node; task parallelism is controlled by `max_workers`.
    """

    def _init(self):
        pass

    def _configure_menu(self):
        return [
            {
                'name': 'recipe',
                'msg': 'WfCommons recipe to generate',
                'type': str,
                'choices': RECIPES,
                'default': 'montage',
            },
            {
                'name': 'num_tasks',
                'msg': 'Number of tasks in the generated workflow',
                'type': int,
                'default': 100,
            },
            {
                'name': 'cpu_work',
                'msg': ('CPU work units per wfbench task. MUST be > 0; '
                        'wfbench gates io_alternate on cpu-benchmark progress '
                        'updates, so cpu_work=0 silently disables ALL '
                        'reads/writes. Use 1 for minimal CPU + maximal I/O.'),
                'type': int,
                'default': 1,
            },
            {
                'name': 'data_footprint',
                'msg': 'Per-file data size (e.g. 100M, 1G); 0 = recipe defaults',
                'type': str,
                'default': '0',
            },
            {
                'name': 'percent_cpu',
                'msg': ('CPU vs memory thread split for the cpu-benchmark '
                        'process (cpu_threads = 10*percent_cpu, '
                        'mem_threads = 10 - cpu_threads). Does NOT directly '
                        'affect I/O volume. mem_threads spawn stress-ng — '
                        'NOT installed on every system. Default 1.0 (all '
                        'cpu, no mem threads) so the I/O path runs cleanly '
                        'on systems without stress-ng.'),
                'type': float,
                'default': 1.0,
            },
            {
                'name': 'drop_page_cache',
                'msg': ('After each read/write, call '
                        'posix_fadvise(POSIX_FADV_DONTNEED) so the kernel '
                        'drops the file from the page cache. Writes also '
                        'fsync first so dirty pages become drop-eligible. '
                        'Makes NFS-vs-CTE comparisons apples-to-apples: '
                        'the CTE adapter has no client-side cache, so '
                        'letting NFS warm-cache the workflow data '
                        'biases the comparison. Toggling this sets '
                        'WFBENCH_DROP_CACHE=1 in the wfbench env.'),
                'type': bool,
                'default': False,
            },
            {
                'name': 'out',
                'msg': 'Output directory (receives bench/ + bench/bash/)',
                'type': str,
                'default': '${HOME}/wfcommons_out',
            },
            {
                'name': 'clio_prefix',
                'msg': 'Rewrite wfbench task paths to begin with "clio::" '
                       '(opts every read/write into WRP CTE POSIX '
                       'interception when libwrp_cte_posix.so is LD_PRELOADed)',
                'type': bool,
                'default': False,
            },
            {
                'name': 'venv',
                'msg': 'Path to a wfcommons venv (bare-metal mode only)',
                'type': str,
                'default': '${HOME}/.jarvis-wfcommons-venv',
            },
            {
                'name': 'nprocs',
                'msg': 'MPI ranks (driver runs single-process; tasks use max_workers)',
                'type': int,
                'default': 1,
            },
            {
                'name': 'ppn',
                'msg': 'MPI processes per node',
                'type': int,
                'default': 1,
            },
            {
                'name': 'base_image',
                'msg': 'Base image for the build container',
                'type': str,
                'default': 'ubuntu:24.04',
            },
        ]

    # ------------------------------------------------------------------
    # Container build hooks
    # ------------------------------------------------------------------

    def _build_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        content = self._read_build_script('build.sh', {
            'BASE_IMAGE': self.config.get('base_image', 'ubuntu:24.04'),
        })
        return content, 'py311'

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'DEPLOY_BASE': 'ubuntu:24.04',
        })
        return content, 'py311'

    # ------------------------------------------------------------------
    # Configure
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        super()._configure(**kwargs)

        if self.config['recipe'] not in RECIPES:
            raise ValueError(
                f"recipe '{self.config['recipe']}' not in {RECIPES}"
            )

        # wfbench main() guards the cpu-benchmark spawn behind
        # `if args.cpu_work:` (wfbench script line 443). When that block
        # is skipped, nothing feeds the cpu_queue that io_alternate
        # blocks on, so the io subprocess hangs until wfbench main kills
        # it on shutdown — no reads, no writes, just process spawn
        # overhead. Coerce cpu_work to a minimum of 1 so this path is
        # always taken.
        if int(self.config.get('cpu_work', 0)) <= 0:
            self.log(
                "cpu_work was 0; coercing to 1 so wfbench actually "
                "performs I/O (cpu_work=0 silently disables it)"
            )
            self.config['cpu_work'] = 1

        # Propagate the drop-page-cache toggle to the wfbench subprocess
        # (wfbench reads WFBENCH_DROP_CACHE in _drop_page_cache helper).
        if self.config.get('drop_page_cache', False):
            self.setenv('WFBENCH_DROP_CACHE', '1')

        if self.config.get('deploy_mode') == 'default':
            # Output dir on every node so MPI/PSSH topo-execution works.
            Mkdir(self.config['out'],
                  PsshExecInfo(hostfile=self.hostfile,
                               env=self.env)).run()
            # Bare-metal venv with wfcommons. Idempotent — if the venv
            # exists and imports wfcommons, skip the (slow) pip install.
            venv = self.config['venv']
            ensure = (
                f"python3 -c 'import sys; sys.exit(0)' && "
                f"if [ ! -x '{venv}/bin/python3' ]; then "
                f"  python3 -m venv '{venv}'; "
                f"fi && "
                f"'{venv}/bin/pip' install --quiet --upgrade pip && "
                f"'{venv}/bin/python3' -c 'import wfcommons' 2>/dev/null || "
                f"'{venv}/bin/pip' install --quiet 'wfcommons[bench]'"
            )
            Exec(ensure, LocalExecInfo(env=self.env)).run()
            self.setenv('WFCOMMONS_PYTHON', f"{venv}/bin/python3")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def _data_bytes(s):
        """Parse '100M', '1G', '500k', or a plain integer to bytes."""
        if s is None:
            return 0
        s = str(s).strip()
        if not s:
            return 0
        units = {'k': 1024, 'm': 1024**2, 'g': 1024**3, 't': 1024**4}
        suffix = s[-1].lower()
        if suffix in units:
            return int(float(s[:-1]) * units[suffix])
        return int(s)

    def _driver_args(self):
        cfg = self.config
        args = [
            f"--recipe {cfg['recipe']}",
            f"--num-tasks {cfg['num_tasks']}",
            f"--cpu-work {cfg['cpu_work']}",
            f"--data {self._data_bytes(cfg['data_footprint'])}",
            f"--percent-cpu {cfg['percent_cpu']}",
            f"--out '{cfg['out']}'",
        ]
        if cfg.get('clio_prefix'):
            args.append('--clio-prefix')
        return ' '.join(args)

    def start(self):
        if self.config.get('deploy_mode') == 'container':
            cmd = (
                '/opt/wfcommons-env/bin/python3 '
                '/opt/wfcommons-driver/run_wfbench.py '
                + self._driver_args()
            )
            Exec(cmd, LocalExecInfo(
                env=self.mod_env,
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
            )).run()
        else:
            driver = f"{self.pkg_dir}/run_wfbench.py"
            cmd = (
                f"{self.config['venv']}/bin/python3 {driver} "
                + self._driver_args()
            )
            Exec(cmd, LocalExecInfo(env=self.mod_env)).run()

    def stop(self):
        pass

    def clean(self):
        if self.config.get('out'):
            Rm(self.config['out'],
               PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

    def _get_stat(self, stat_dict):
        stat_dict[f'{self.pkg_id}.recipe'] = self.config['recipe']
        stat_dict[f'{self.pkg_id}.num_tasks'] = self.config['num_tasks']
        stat_dict[f'{self.pkg_id}.runtime'] = self.start_time
