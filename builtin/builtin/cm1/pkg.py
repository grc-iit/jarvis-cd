"""
CM1 numerical weather model. Supports bare-metal and container deployments.
Container mode builds CM1 from the upstream UCAR tarball with gfortran +
OpenMPI + NetCDF in build.sh; deploy image copies the built tree and pulls
in only the runtime libs.
"""
import os
import pathlib

from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo, MpiExecInfo, Mkdir


_FILE_FORMAT_MAP = {'grads': 1, 'netcdf': 2, 'lofs': 5}
_FILE_COUNT_MAP = {'shared': 1, 'fpo': 2, 'fpp': 3, 'lofs': 4}


class Cm1(Application):
    """
    CM1 driver.
    """

    def _configure_menu(self):
        return [
            {'name': 'nx', 'msg': 'x dimension of 3-D grid',
             'type': int, 'default': 16},
            {'name': 'ny', 'msg': 'y dimension of 3-D grid',
             'type': int, 'default': 16},
            {'name': 'nz', 'msg': 'z dimension of 3-D grid',
             'type': int, 'default': 16},
            {'name': 'corex', 'msg': 'Number of MPI ranks along x',
             'type': int, 'default': 2},
            {'name': 'corey', 'msg': 'Number of MPI ranks along y',
             'type': int, 'default': 2},
            {'name': 'file_format', 'msg': 'Output file format',
             'type': str, 'default': 'netcdf',
             'choices': ['grads', 'netcdf', 'lofs']},
            {'name': 'file_count', 'msg': 'Output file layout',
             'type': str, 'default': 'shared',
             'choices': ['shared', 'fpo', 'fpp', 'lofs']},
            {'name': 'test_case', 'msg': 'Predefined CM1 test case',
             'type': str, 'default': 'nssl3', 'choices': ['nssl3']},
            {'name': 'ppn', 'msg': 'Processes per node',
             'type': int, 'default': 4},
            {'name': 'output', 'msg': 'Output directory (None = under shared_dir)',
             'type': str, 'default': None},
            {'name': 'exec_mode', 'msg': 'Multi-node mode: mpi or pssh',
             'type': str, 'default': 'mpi', 'choices': ['pssh', 'mpi']},
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

        if self.config.get('output') is None:
            self.config['output'] = f'{self.shared_dir}/cm1_out'
        out_parent = str(pathlib.Path(self.config['output']).parent)
        self.config['restart'] = os.path.join(out_parent, 'restart_dir')
        Mkdir([self.config['output'], self.config['restart']],
              LocalExecInfo()).run()

        if self.config.get('deploy_mode') == 'container':
            cm1_path = '/opt/cm1'
        else:
            cm1_path = self.env.get('CM1_PATH') or os.environ.get('CM1_PATH')
            if not cm1_path:
                raise RuntimeError(
                    "CM1_PATH is not set. Export CM1_PATH or use "
                    "deploy_mode=container."
                )
        self.config['CM1_PATH'] = cm1_path

        file_format = _FILE_FORMAT_MAP.get(self.config['file_format'])
        if file_format is None:
            raise ValueError(f"Invalid file_format: {self.config['file_format']}")
        file_count = _FILE_COUNT_MAP.get(self.config['file_count'])
        if file_count is None:
            raise ValueError(f"Invalid file_count: {self.config['file_count']}")

        test_case = self.config.get('test_case', 'nssl3')
        namelist_in = os.path.join(self.pkg_dir, 'config',
                                   f'namelist.input.{test_case}')
        # CM1 reads the namelist from the file literally named
        # 'namelist.input' in the process's cwd; emit it under the output
        # directory so cm1.exe can be launched from there.
        namelist_out = os.path.join(self.config['output'], 'namelist.input')
        self.copy_template_file(namelist_in, namelist_out, replacements={
            'file_format': file_format,
            'file_count': file_count,
            'nx': self.config['nx'],
            'ny': self.config['ny'],
            'nz': self.config['nz'],
            'ppn': self.config['ppn'],
        })
        self.config['namelist'] = namelist_out

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

    def start(self):
        # cm1.exe reads ./namelist.input and writes output to cwd. The
        # container wrapper (`docker exec`) doesn't honor exec_info.cwd, and
        # mpirun takes the first token as the executable — so we wrap in an
        # explicit `bash -c 'cd X && cm1.exe'` that mpirun launches per-rank.
        run_cwd = self.config['output']
        inner = f'cd {run_cwd} && {self.config["CM1_PATH"]}/run/cm1.exe'
        cmd = f"bash -c \"{inner}\""
        nprocs = self.config['corex'] * self.config['corey']
        exec_mode = self.config.get('exec_mode', 'mpi')

        kwargs = dict(env=self.mod_env, cwd=run_cwd, **self._container_kwargs())

        if exec_mode == 'mpi':
            hostfile = self.hostfile if self._use_remote() else None
            exec_info = MpiExecInfo(
                nprocs=nprocs,
                ppn=self.config['ppn'],
                hostfile=hostfile,
                port=self.ssh_port,
                **kwargs,
            )
        elif self._use_remote():
            exec_info = PsshExecInfo(hostfile=self.hostfile, **kwargs)
        else:
            exec_info = LocalExecInfo(**kwargs)

        Exec(cmd, exec_info).run()

    def stop(self):
        pass

    def clean(self):
        pass

    def _get_stat(self, stat_dict):
        stat_dict[f'{self.pkg_id}.runtime'] = self.start_time
