"""
FIO benchmark package. Supports default (bare-metal) and container
deployment via the two-phase build/deploy container architecture.
FIO itself is tiny — apt-installed in the deploy image, no build.sh needed.
"""
import os
import pathlib

from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo, MpiExecInfo, Mkdir
from jarvis_cd.shell.process import Rm


class Fio(Application):
    """
    FIO benchmark driver.
    """

    def _configure_menu(self):
        return [
            {'name': 'write', 'msg': 'Perform a write workload',
             'type': bool, 'default': True},
            {'name': 'read', 'msg': 'Perform a read workload',
             'type': bool, 'default': False},
            {'name': 'xfer', 'msg': 'Block size for each I/O transfer',
             'type': str, 'default': '1m'},
            {'name': 'total_size', 'msg': 'Total data per job',
             'type': str, 'default': '32m'},
            {'name': 'iodepth', 'msg': 'I/O ops in flight',
             'type': int, 'default': 1},
            {'name': 'reps', 'msg': 'Number of repetitions',
             'type': int, 'default': 1},
            {'name': 'nprocs', 'msg': 'Number of FIO jobs',
             'type': int, 'default': 1},
            {'name': 'ppn', 'msg': 'FIO jobs per node',
             'type': int, 'default': 1},
            {'name': 'out', 'msg': 'Output test file path',
             'type': str, 'default': '/tmp/fio_test.bin'},
            {'name': 'direct', 'msg': 'Use direct I/O',
             'type': bool, 'default': False},
            {'name': 'random', 'msg': 'Use random access pattern',
             'type': bool, 'default': False},
            {'name': 'engine', 'msg': 'FIO I/O engine',
             'type': str, 'default': 'psync'},
            {'name': 'log', 'msg': 'Path to FIO output log',
             'type': str, 'default': None},
            {'name': 'exec_mode', 'msg': 'Multi-node mode: pssh or mpi',
             'type': str, 'default': 'pssh', 'choices': ['pssh', 'mpi']},
        ]

    # ------------------------------------------------------------------
    # Container build/deploy
    # ------------------------------------------------------------------

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        base = getattr(self.pipeline, 'container_base', 'ubuntu:22.04')
        content = self._read_dockerfile('Dockerfile.deploy', {
            'DEPLOY_BASE': base,
        })
        return content, ''

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        super()._configure(**kwargs)

    def _exec_info(self):
        exec_mode = self.config.get('exec_mode', 'pssh')
        nprocs = self.config.get('nprocs', 1)
        ppn = self.config.get('ppn', 1)
        use_remote = self.hostfile is not None and not self.hostfile.is_local()

        kwargs = dict(env=self.mod_env)
        if self.config.get('deploy_mode') == 'container':
            kwargs.update(
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
            )

        if exec_mode == 'mpi' and use_remote:
            return MpiExecInfo(hostfile=self.hostfile, nprocs=nprocs, ppn=ppn,
                               port=self.ssh_port, **kwargs)
        if exec_mode == 'pssh' and use_remote:
            return PsshExecInfo(hostfile=self.hostfile, **kwargs)
        return LocalExecInfo(**kwargs)

    def start(self):
        if self.config['read'] and self.config['write']:
            mode = 'readwrite'
        elif self.config['read']:
            mode = 'read'
        else:
            mode = 'write'

        out = self.config['out']
        out_dir = str(pathlib.Path(out).parent) \
            if '.' in os.path.basename(out) else out

        exec_info = self._exec_info()
        Mkdir(out_dir, exec_info).run()

        cmd = ' '.join([
            'fio',
            f'--rw={mode}',
            f'--size={self.config["total_size"]}',
            f'--bs={self.config["xfer"]}',
            f'--iodepth={self.config["iodepth"]}',
            f'--numjobs={self.config.get("nprocs", 1)}',
            f'--direct={1 if self.config["direct"] else 0}',
            f'--randrepeat={1 if self.config["random"] else 0}',
            f'--filename={out}',
            f'--ioengine={self.config["engine"]}',
            '--name=job',
        ])
        Exec(cmd, exec_info).run()

    def stop(self):
        pass

    def clean(self):
        Rm(self.config['out'] + '*', self._exec_info()).run()

    def _get_stat(self, stat_dict):
        stat_dict[f'{self.pkg_id}.runtime'] = self.start_time
