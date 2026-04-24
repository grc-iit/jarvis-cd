"""
YCSB-cpp (Yahoo! Cloud Serving Benchmark) package.

Supports default (bare-metal) and container deployment via the two-phase
build/deploy container architecture. In container mode, build.sh compiles
YCSB-cpp with LevelDB and Redis bindings inside a build container; the
deploy image copies only the ycsb binary + workloads from the build image.
"""
import os
import re

from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo, MpiExecInfo


class Ycsbc(Application):
    """
    YCSB-cpp benchmark driver.

    deploy_mode='container' runs ycsb in a Docker/Podman/Apptainer container
    built from the package's build.sh + Dockerfile.deploy. deploy_mode='default'
    uses a system ycsb binary discoverable via YCSBC_ROOT.
    """

    def _configure_menu(self):
        return [
            {
                'name': 'db_name',
                'msg': 'The database backend to benchmark',
                'type': str,
                'default': 'leveldb',
                'choices': ['leveldb', 'redis'],
            },
            {
                'name': 'workload',
                'msg': 'The YCSB workload to run (a-f)',
                'type': str,
                'default': 'a',
                'choices': ['a', 'b', 'c', 'd', 'e', 'f'],
            },
            {
                'name': 'status',
                'msg': 'Print status updates every 10 seconds',
                'type': bool,
                'default': True,
            },
            {
                'name': 'nprocs',
                'msg': 'Total number of YCSB client processes',
                'type': int,
                'default': 1,
            },
            {
                'name': 'ppn',
                'msg': 'YCSB client processes per node',
                'type': int,
                'default': 1,
            },
            {
                'name': 'exec_mode',
                'msg': 'Multi-node execution mode: pssh or mpi',
                'type': str,
                'default': 'pssh',
                'choices': ['pssh', 'mpi'],
            },
        ]

    # ------------------------------------------------------------------
    # Container build/deploy
    # ------------------------------------------------------------------

    def _build_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        return self._read_build_script('build.sh', {}), 'default'

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        base = getattr(self.pipeline, 'container_base', 'ubuntu:22.04')
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'DEPLOY_BASE': base,
        })
        return content, ''

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        super()._configure(**kwargs)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _ycsb_root(self) -> str:
        if self.config.get('deploy_mode') == 'container':
            return '/opt/ycsb-cpp'
        root = self.env.get('YCSBC_ROOT') or os.environ.get('YCSBC_ROOT')
        if not root:
            raise RuntimeError(
                "YCSBC_ROOT is not set. Set it in your jarvis env or export "
                "it before running the pipeline, or use deploy_mode=container."
            )
        return root

    def _exec_info_kwargs(self):
        """Kwargs common to all exec_info variants, including container wrap."""
        kwargs = dict(env=self.mod_env, collect_output=True)
        if self.config.get('deploy_mode') == 'container':
            kwargs.update(
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
            )
        return kwargs

    def _exec_info(self):
        exec_mode = self.config.get('exec_mode', 'pssh')
        nprocs = self.config.get('nprocs', 1)
        ppn = self.config.get('ppn', 1)
        use_remote = self.hostfile is not None and not self.hostfile.is_local()
        kwargs = self._exec_info_kwargs()

        if exec_mode == 'mpi' and use_remote:
            return MpiExecInfo(hostfile=self.hostfile, nprocs=nprocs, ppn=ppn,
                               port=self.ssh_port, **kwargs)
        if exec_mode == 'pssh' and use_remote:
            return PsshExecInfo(hostfile=self.hostfile, **kwargs)
        return LocalExecInfo(**kwargs)

    def start(self):
        root = self._ycsb_root()
        db_name = self.config['db_name']
        workload = f'workload{self.config["workload"]}'
        props = f'{root}/{db_name}/{db_name}.properties'
        status_arg = '-s' if self.config['status'] else ''

        db_path_args = []
        if db_name == 'leveldb':
            db_dir = self.config.get('db_dir') or f'{self.shared_dir}/leveldb_db'
            db_path_args = [f'-p leveldb.dbname={db_dir}']

        def _cmd(phase):
            return ' '.join(filter(None, [
                f'ycsb -{phase}',
                f'-db {db_name}',
                f'-P {root}/workloads/{workload}',
                f'-P {props}' if os.path.exists(props) else '',
                *db_path_args,
                status_arg,
            ]))

        exec_info = self._exec_info()
        Exec(_cmd('load'), exec_info).run()
        self.exec = Exec(_cmd('run'), exec_info).run()

    def stop(self):
        pass

    def clean(self):
        pass

    def _get_stat(self, stat_dict):
        if not hasattr(self, 'exec') or not getattr(self.exec, 'stdout', None):
            return
        total = 0.0
        n = 0
        for _, output in self.exec.stdout.items():
            m = re.search(r'throughput\(ops/sec\): ([0-9.]+)', output)
            if m:
                total += float(m.group(1))
                n += 1
        if n:
            stat_dict[f'{self.pkg_id}.throughput'] = total
            stat_dict[f'{self.pkg_id}.throughput_per_node'] = total / n
        stat_dict[f'{self.pkg_id}.runtime'] = self.start_time
