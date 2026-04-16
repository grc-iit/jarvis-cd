"""
This module provides classes and methods to launch the Ior application.
Ior is a benchmark tool for measuring the performance of I/O systems.
It is a simple tool that can be used to measure the performance of a file system.
It is mainly targeted for HPC systems and parallel I/O.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, PsshExecInfo, Rm, Mkdir
from jarvis_cd.shell.process import GdbServer
import os
import pathlib


class Ior(Application):
    """
    Merged IOR class supporting both default (bare-metal) and container deployment.

    Set deploy_mode='container' to build and run IOR inside a Docker/Podman/Apptainer
    container.  Set deploy_mode='default' (the default) to use a system-installed ior
    binary via MPI.
    """

    def _configure_menu(self):
        """
        Create a CLI menu for the configurator method.

        :return: List(dict)
        """
        return [
            {
                'name': 'write',
                'msg': 'Perform a write workload',
                'type': bool,
                'default': True,
                'choices': [],
                'args': [],
            },
            {
                'name': 'read',
                'msg': 'Perform a read workload',
                'type': bool,
                'default': False,
            },
            {
                'name': 'xfer',
                'msg': 'The size of data transfer',
                'type': str,
                'default': '1m',
            },
            {
                'name': 'block',
                'msg': 'Amount of data to generate per-process',
                'type': str,
                'default': '32m',
                'aliases': ['block_size']
            },
            {
                'name': 'api',
                'msg': 'The I/O api to use',
                'type': str,
                'choices': ['posix', 'mpiio', 'hdf5'],
                'default': 'posix',
            },
            {
                'name': 'fpp',
                'msg': 'Use file-per-process',
                'type': bool,
                'default': False,
            },
            {
                'name': 'reps',
                'msg': 'Number of times to repeat',
                'type': int,
                'default': 1,
            },
            {
                'name': 'nprocs',
                'msg': 'Number of processes',
                'type': int,
                'default': 1,
            },
            {
                'name': 'ppn',
                'msg': 'The number of processes per node',
                'type': int,
                'default': 16,
            },
            {
                'name': 'out',
                'msg': 'Path to the output file',
                'type': str,
                'default': '/tmp/ior.bin',
                'aliases': ['output']
            },
            {
                'name': 'log',
                'msg': 'Path to IOR output log',
                'type': str,
                'default': '',
            },
            {
                'name': 'direct',
                'msg': 'Use direct I/O (O_DIRECT) for POSIX API, bypassing I/O buffers',
                'type': bool,
                'default': False,
            }
        ]

    # ------------------------------------------------------------------
    # Container Dockerfile generators
    # ------------------------------------------------------------------

    def _build_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        base = getattr(self.pipeline, 'container_base', 'ubuntu:24.04')
        content = self._read_dockerfile('Dockerfile.build', {
            'BASE_IMAGE': base,
        })
        return content, 'mpi'

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        base = getattr(self.pipeline, 'container_base', 'ubuntu:24.04')
        suffix = getattr(self, '_build_suffix', '')
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'BASE_IMAGE': base,
        })
        return content, suffix

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        """
        Configure IOR.

        Calls super()._configure() which updates self.config and (when
        deploy_mode == 'container') triggers build_phase / build_deploy_phase.

        In default mode, also uppercases the API name and creates the output
        directory on all nodes.
        """
        super()._configure(**kwargs)

        if self.config.get('deploy_mode') == 'default':
            self.config['api'] = self.config['api'].upper()

            # Create parent directory of output file on all nodes
            out = os.path.expandvars(self.config['out'])
            parent_dir = str(pathlib.Path(out).parent)
            Mkdir(parent_dir,
                  PsshExecInfo(env=self.mod_env,
                               hostfile=self.hostfile)).run()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Launch IOR via MpiExecInfo; Exec handles container wrapping transparently."""
        cfg = self.config

        cmd = [
            'ior',
            '-k',
            f'-b {cfg["block"]}',
            f'-t {cfg["xfer"]}',
            f'-a {cfg["api"].upper()}',
            f'-o {cfg["out"]}',
        ]
        if cfg.get('write', True):
            cmd.append('-w')
        if cfg.get('read'):
            cmd.append('-r')
        if cfg.get('fpp'):
            cmd.append('-F')
        if cfg.get('reps', 1) > 1:
            cmd.append(f'-i {cfg["reps"]}')
        if cfg.get('direct'):
            cmd.append('-O useO_DIRECT=1')

        ior_cmd = ' '.join(cmd)
        if cfg.get('log'):
            ior_cmd += f' 2>&1 | tee {cfg["log"]}'

        gdb_server = GdbServer(ior_cmd, cfg.get('dbg_port', 4000))
        cmd_list = [
            {'cmd': gdb_server.get_cmd(), 'nprocs': 1 if cfg.get('do_dbg') else 0, 'disable_preload': True},
            {'cmd': ior_cmd, 'nprocs': None},
        ]
        Exec(cmd_list, MpiExecInfo(
            nprocs=cfg['nprocs'],
            ppn=cfg['ppn'],
            hostfile=self.hostfile,
            port=self.ssh_port,
            container=self._container_engine,
            container_image=self.deploy_image_name(),
            shared_dir=self.shared_dir,
            private_dir=self.private_dir,
            env=self.mod_env,
        )).run()

    def stop(self):
        """Stop IOR (no-op — IOR runs to completion)."""
        pass

    def clean(self):
        """Remove IOR output files."""
        Rm(self.config['out'] + '*',
           PsshExecInfo(env=self.env,
                        hostfile=self.hostfile)).run()

    def _get_stat(self, stat_dict):
        """
        Get statistics from the application.

        :param stat_dict: A dictionary of statistics.
        :return: None
        """
        stat_dict[f'{self.pkg_id}.runtime'] = self.start_time

    def log(self, message):
        """Simple logging method."""
        print(f"[IOR:{self.pkg_id}] {message}")
