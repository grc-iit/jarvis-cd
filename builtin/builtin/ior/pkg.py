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
                'name': 'deploy_mode',
                'msg': 'Deployment mode',
                'type': str,
                'choices': ['default', 'container'],
                'default': 'default',
            },
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

    def _build_phase(self) -> str:
        """
        Return the BUILD container Dockerfile, or None when not in container mode.
        """
        if self.config.get('deploy_mode') != 'container':
            return None
        base = getattr(self.pipeline, 'container_base', 'ubuntu:24.04')
        return f"""FROM {base}

ARG DEBIAN_FRONTEND=noninteractive

# Build dependencies (IOR + Darshan)
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl \\
    build-essential autoconf automake libtool \\
    zlib1g-dev \\
    openmpi-bin libopenmpi-dev \\
    && rm -rf /var/lib/apt/lists/*

# Download IOR release tarball (includes pre-generated configure)
RUN curl -sL https://github.com/hpc/ior/releases/download/3.3.0/ior-3.3.0.tar.gz \\
    | tar -xz -C /opt \\
    && mv /opt/ior-3.3.0 /opt/ior

# Configure and build IOR
RUN cd /opt/ior \\
    && ./configure --prefix=/opt/ior/install \\
    && make -j$(nproc) \\
    && make install

# Download and build Darshan runtime with MPI support
RUN curl -sL https://github.com/darshan-hpc/darshan/archive/refs/tags/darshan-3.4.4.tar.gz \\
    | tar -xz -C /opt \\
    && mv /opt/darshan-darshan-3.4.4 /opt/darshan

RUN cd /opt/darshan/darshan-runtime \\
    && autoreconf -ivf \\
    && ./configure --prefix=/opt/darshan/install \\
        --with-log-path-by-env=DARSHAN_LOG_DIR \\
        --with-jobid-env=PBS_JOBID \\
        CC=mpicc \\
    && make -j$(nproc) install
"""

    def _build_deploy_phase(self) -> str:
        """
        Return the DEPLOY container Dockerfile, or None when not in container mode.
        """
        if self.config.get('deploy_mode') != 'container':
            return None
        base = getattr(self.pipeline, 'container_base', 'ubuntu:24.04')
        return f"""FROM {self.build_image_name} AS builder
FROM {base}

ARG DEBIAN_FRONTEND=noninteractive

# MPI runtime only (no build tools)
RUN apt-get update && apt-get install -y --no-install-recommends \\
    openmpi-bin libopenmpi-dev \\
    openssh-server openssh-client \\
    && rm -rf /var/lib/apt/lists/* \\
    && mkdir -p /var/run/sshd \\
    && sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config \\
    && sed -i 's/#PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config

# Copy ior binary and darshan library from build container
COPY --from=builder /opt/ior/install/bin/ior /usr/bin/ior
COPY --from=builder /opt/darshan/install/lib/libdarshan.so /opt/darshan/lib/libdarshan.so

CMD ["/bin/bash"]
"""

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
        """
        Launch IOR.

        Branches on deploy_mode: uses container_exec_info() for container
        mode, MpiExecInfo with hostfile for default mode.
        """
        cfg = self.config

        if cfg.get('deploy_mode') == 'container':
            ior_args = [
                '-k',
                f'-b {cfg["block"]}',
                f'-t {cfg["xfer"]}',
                f'-a {cfg["api"].upper()}',
                f'-o {cfg["out"]}',
            ]
            if cfg.get('write'):
                ior_args.append('-w')
            if cfg.get('read'):
                ior_args.append('-r')
            if cfg.get('fpp'):
                ior_args.append('-F')
            if cfg.get('reps', 1) > 1:
                ior_args.append(f'-i {cfg["reps"]}')
            if cfg.get('direct'):
                ior_args.append('-O useO_DIRECT=1')

            nprocs = cfg.get('nprocs', 1)
            inner = f'mpirun --allow-run-as-root -n {nprocs} ior {" ".join(ior_args)}'
            if cfg.get('log'):
                inner += f' 2>&1 | tee {cfg["log"]}'

            Exec(inner, self.container_exec_info()).run()
        else:
            cmd = [
                'ior',
                '-k',
                f'-b {cfg["block"]}',
                f'-t {cfg["xfer"]}',
                f'-a {cfg["api"]}',
                f'-o {cfg["out"]}',
            ]
            if cfg['write']:
                cmd.append('-w')
            if cfg['read']:
                cmd.append('-r')
            if cfg['fpp']:
                cmd.append('-F')
            if cfg['reps'] > 1:
                cmd.append(f'-i {cfg["reps"]}')
            if cfg['direct']:
                cmd.append('-O useO_DIRECT=1')

            ior_cmd = ' '.join(cmd)

            # Use GdbServer to create gdbserver command if debugging is enabled
            gdb_server = GdbServer(ior_cmd, cfg.get('dbg_port', 4000))
            gdbserver_cmd = gdb_server.get_cmd()

            cmd_list = [
                {
                    'cmd': gdbserver_cmd,
                    'nprocs': 1 if cfg.get('do_dbg', False) else 0,
                    'disable_preload': True
                },
                {
                    'cmd': ior_cmd,
                    'nprocs': None  # Will be calculated from remainder
                }
            ]
            print(cmd_list)

            Exec(cmd_list,
                 MpiExecInfo(env=self.mod_env,
                             hostfile=self.hostfile,
                             nprocs=cfg['nprocs'],
                             ppn=cfg['ppn'])).run()

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
