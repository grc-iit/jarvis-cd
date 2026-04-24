"""
Filebench package. Supports default (bare-metal) and container deployment.
Filebench was dropped from Ubuntu's universe repo after 20.04 so the
container path builds it from the upstream GitHub tree in build.sh.
"""
import os
import subprocess

from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo, MpiExecInfo, Mkdir
from jarvis_cd.shell.process import Kill, Rm


_WORKLOADS = ['fileserver', 'varmail', 'videoserver', 'webproxy', 'webserver']


class Filebench(Application):
    """
    Filebench driver.
    """

    def _configure_menu(self):
        return [
            {'name': 'workload', 'msg': 'Filebench workload personality',
             'type': str, 'default': 'fileserver', 'choices': _WORKLOADS},
            {'name': 'dir', 'msg': 'Target directory for fileset',
             'type': str, 'default': '/tmp/filebench'},
            {'name': 'run', 'msg': 'Total runtime (seconds)',
             'type': int, 'default': 15},
            {'name': 'nfiles', 'msg': 'Number of files in the fileset',
             'type': int, 'default': 10000},
            {'name': 'filesize', 'msg': 'Mean file size (e.g. 128k, 1m)',
             'type': str, 'default': '128k'},
            {'name': 'nthreads', 'msg': 'Worker threads per process',
             'type': int, 'default': 50},
            {'name': 'nprocs', 'msg': 'Total filebench processes',
             'type': int, 'default': 1},
            {'name': 'ppn', 'msg': 'Processes per node',
             'type': int, 'default': 1},
            {'name': 'exec_mode', 'msg': 'Multi-node mode: pssh or mpi',
             'type': str, 'default': 'pssh', 'choices': ['pssh', 'mpi']},
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
        workload = self.config['workload']
        dir_path = os.path.expandvars(self.config['dir'])
        self.config['dir'] = dir_path
        self.copy_template_file(
            f'{self.pkg_dir}/config/{workload}.f',
            f'{self.shared_dir}/{workload}.f',
            replacements={
                'DIR': dir_path,
                'RUN': self.config['run'],
                'NFILES': self.config['nfiles'],
                'FILESIZE': self.config['filesize'],
                'NTHREADS': self.config['nthreads'],
            },
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

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
        workload = self.config['workload']
        Mkdir(self.config['dir'], self._exec_info()).run()
        workload_file = f'{self.shared_dir}/{workload}.f'
        # Filebench requires ASLR off or workers die with "Unexpected Process
        # termination Code 3". Prefer sysctl (needs root + writable /proc —
        # privileged container) then setarch, then bare filebench + warning.
        if self._disable_aslr_globally():
            cmd = f'filebench -f {workload_file}'
        elif self._setarch_available():
            cmd = f'setarch `arch` --addr-no-randomize filebench -f {workload_file}'
        else:
            print('[Filebench] WARNING: cannot disable ASLR; '
                  'filebench workers may die with Code 3.')
            cmd = f'filebench -f {workload_file}'
        Exec(cmd, self._exec_info()).run()

    @staticmethod
    def _disable_aslr_globally() -> bool:
        try:
            with open('/proc/sys/kernel/randomize_va_space', 'w') as f:
                f.write('0')
            return True
        except (PermissionError, OSError):
            return False

    @staticmethod
    def _setarch_available() -> bool:
        try:
            probe = subprocess.run(
                ['setarch', 'x86_64', '--addr-no-randomize', 'true'],
                capture_output=True, timeout=5,
            )
            return probe.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def stop(self):
        Kill('filebench', self._exec_info()).run()

    def clean(self):
        Rm(self.config['dir'], self._exec_info()).run()

    def _get_stat(self, stat_dict):
        stat_dict[f'{self.pkg_id}.runtime'] = self.start_time
