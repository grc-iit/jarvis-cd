"""
Gadget2 cosmological N-body/SPH simulation. Supports bare-metal and
container deployments. Container mode builds FFTW 2.1.5 from source (Gadget2
needs the single-precision, type-prefixed, MPI variant — FFTW3 in the Ubuntu
archive is ABI-incompatible) and clones lukemartinlogan/gadget2 into
/opt/gadget2 in build.sh.
"""
import os

from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo, MpiExecInfo, Mkdir


_TEST_CASES = ['gassphere']
_GADGET2_PATH_CONTAINER = '/opt/gadget2'
_BINARY_REL_PATH = 'build/bin/Gadget2'


class Gadget2(Application):
    """
    Gadget2 driver.
    """

    def _configure_menu(self):
        return [
            {'name': 'gadget2_path',
             'msg': ('Absolute path to the Gadget2 source tree (containing '
                     'ICs/, Gadget2/, build/). In container mode this is '
                     'baked into the image at /opt/gadget2 and this option '
                     'is ignored.'),
             'type': str, 'default': None},
            {'name': 'test_case', 'msg': 'Predefined test case (paramfile basename)',
             'type': str, 'default': 'gassphere', 'choices': _TEST_CASES},
            {'name': 'output', 'msg': 'Output directory (None = under shared_dir)',
             'type': str, 'default': None},
            {'name': 'time_max', 'msg': 'Maximum simulation time (internal units)',
             'type': float, 'default': 0.05},
            {'name': 'buffer_size', 'msg': 'Communication buffer size in MB',
             'type': float, 'default': 15},
            {'name': 'part_alloc_factor',
             'msg': 'Per-rank particle allocation factor (1.0 - 3.0)',
             'type': float, 'default': 1.5},
            {'name': 'tree_alloc_factor', 'msg': 'BH-tree allocation factor',
             'type': float, 'default': 0.9},
            {'name': 'nprocs', 'msg': 'Total number of MPI ranks',
             'type': int, 'default': 2},
            {'name': 'ppn', 'msg': 'Processes per node',
             'type': int, 'default': 2},
            {'name': 'exec_mode', 'msg': 'Multi-node mode: mpi or pssh',
             'type': str, 'default': 'mpi', 'choices': ['mpi', 'pssh']},
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

        if self.config.get('deploy_mode') == 'container':
            gadget2_path = _GADGET2_PATH_CONTAINER
        else:
            gadget2_path = (
                self.config.get('gadget2_path')
                or self.env.get('GADGET2_PATH')
                or os.environ.get('GADGET2_PATH')
            )
            if not gadget2_path:
                raise RuntimeError(
                    "GADGET2_PATH is not set. Set gadget2_path or export "
                    "GADGET2_PATH for bare-metal, or use deploy_mode=container."
                )
            if not os.path.isdir(gadget2_path):
                raise RuntimeError(
                    f"gadget2_path does not exist on the local node: {gadget2_path}"
                )
        self.config['gadget2_path'] = gadget2_path
        self.setenv('GADGET2_PATH', gadget2_path)

        if self.config.get('output') is None:
            self.config['output'] = f'{self.shared_dir}/gadget2_out'
        Mkdir([self.config['output']], LocalExecInfo()).run()

        test_case = self.config.get('test_case', 'gassphere')
        paramfile_in = os.path.join(self.pkg_dir, 'paramfiles',
                                    f'{test_case}.param')
        paramfile_out = os.path.join(self.config['output'],
                                     f'{test_case}.param')
        # MaxSizeTimestep is hard-coded in the stock paramfile to 0.02; with
        # TimeMax much smaller we'd never take a step. The template only
        # exposes the params Jarvis users tend to tune.
        self.copy_template_file(paramfile_in, paramfile_out, replacements={
            'REPO_DIR': gadget2_path,
            'OUTPUT_DIR': self.config['output'],
            'TIME_MAX': self.config['time_max'],
            'BUFFER_SIZE': self.config['buffer_size'],
            'PART_ALLOC_FACTOR': self.config['part_alloc_factor'],
            'TREE_ALLOC_FACTOR': self.config['tree_alloc_factor'],
        })
        self.config['paramfile'] = paramfile_out

        binary = os.path.join(gadget2_path, _BINARY_REL_PATH)
        if self.config.get('deploy_mode') != 'container' and not os.path.exists(binary):
            raise RuntimeError(
                f"Gadget2 binary not found at {binary}. Build it first via "
                f"`cmake -S {gadget2_path} -B {gadget2_path}/build && "
                f"cmake --build {gadget2_path}/build`."
            )
        self.config['binary'] = binary

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _use_remote(self):
        return self.hostfile is not None and not self.hostfile.is_local()

    def _container_kwargs(self):
        if self.config.get('deploy_mode') != 'container':
            return {}
        return dict(
            container=self._container_engine,
            container_image=self.deploy_image_name(),
            shared_dir=self.shared_dir,
            private_dir=self.private_dir,
        )

    def _exec_info(self, cwd):
        nprocs = self.config['nprocs']
        ppn = self.config['ppn']
        exec_mode = self.config.get('exec_mode', 'mpi')
        kwargs = dict(env=self.mod_env, cwd=cwd, **self._container_kwargs())

        if exec_mode == 'mpi':
            hostfile = self.hostfile if self._use_remote() else None
            return MpiExecInfo(
                nprocs=nprocs, ppn=ppn, hostfile=hostfile,
                port=self.ssh_port, **kwargs,
            )
        if exec_mode == 'pssh' and self._use_remote():
            return PsshExecInfo(hostfile=self.hostfile, **kwargs)
        return LocalExecInfo(**kwargs)

    def start(self):
        # Gadget2 reads / writes paths relative to cwd. Run from the output
        # dir so EnergyFile / InfoFile / snapshots land alongside the
        # paramfile. Gadget2's MAXLEN_FILENAME is 100 chars — passing the
        # full absolute path (often >100 under /tmp/...) overflows an early
        # strcpy on startup ("buffer overflow detected"). We cd into the
        # output dir and pass just the basename to stay well under the cap.
        # Also needs a bash -c wrapper: the container exec wrapper doesn't
        # honor exec_info.cwd, and mpirun takes the first token as the
        # executable — wrap in `bash -c '...'` so mpirun launches it per-rank.
        cwd = self.config['output']
        paramfile_base = os.path.basename(self.config['paramfile'])
        inner = f'cd {cwd} && {self.config["binary"]} {paramfile_base}'
        cmd = f"bash -c \"{inner}\""
        Exec(cmd, self._exec_info(cwd)).run()

    def stop(self):
        pass

    def clean(self):
        pass

    def _get_stat(self, stat_dict):
        stat_dict[f'{self.pkg_id}.runtime'] = self.start_time
