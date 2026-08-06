"""
This module provides classes and methods to launch the Ior application.
Ior is a benchmark tool for measuring the performance of I/O systems.
It is a simple tool that can be used to measure the performance of a file system.
It is mainly targeted for HPC systems and parallel I/O.
"""
import os
import pathlib
import re
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, PsshExecInfo, Rm, Mkdir
from jarvis_cd.shell.process import GdbServer
from jarvis_cd.util.container_utils import (
    container_kwargs, eff_hostfile, single_instance_menu_opt)


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
            },
            {
                'name': 'num_nodes',
                'msg': 'Number of nodes to launch on (first N hosts of the '
                       'pipeline hostfile). 0 means all hosts. Enables '
                       'node-count sweeps inside one allocation without '
                       'changing the pipeline hostfile.',
                'type': int,
                'default': 0,
            },
            {
                'name': 'stonewall',
                'msg': 'Stonewalling deadline in seconds (ior -D): cap each '
                       'write/read phase at this many seconds. 0 disables.',
                'type': int,
                'default': 0,
            },
            single_instance_menu_opt(
                msg='Pin ior to the FIRST host even when the pipeline '
                    'hostfile has >1 host - the single-client baseline '
                    '(e.g. NFS) on multi-node pipelines. Applied after '
                    'num_nodes subsetting.'),
        ]

    # ------------------------------------------------------------------
    # Container Dockerfile generators
    # ------------------------------------------------------------------

    def _build_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        base = getattr(self.pipeline, 'container_base', 'ubuntu:24.04')
        content = self._read_build_script('build.sh', {
            'BASE_IMAGE': base,
        })
        return content, 'mpi'

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        suffix = getattr(self, '_build_suffix', '')
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'DEPLOY_BASE': 'ubuntu:24.04',
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

        # Default the log path to <shared_dir>/ior.log so _get_stat has
        # something to parse even when the YAML omits `log:`. Users who
        # set `log:` explicitly keep their override.
        if not self.config.get('log'):
            self.config['log'] = str(pathlib.Path(self.shared_dir) / 'ior.log')

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

    def _eff_hostfile(self):
        """The hostfile this ior run actually launches on.

        Order matters: ``num_nodes`` subsets to the first N hosts (the
        node-count sweep axis), then the ``single_instance`` collapse pins
        to host[0] (the single-client baseline; it wins in the degenerate
        combination). In container mode the result is stripped of its
        backing file path so mpiexec (running inside the instance) gets an
        inline ``--host`` list instead of a path the instance may not see.
        """
        hf = self.hostfile
        if hf is not None and self.config.get('num_nodes', 0) > 0:
            hf = hf.subset(self.config['num_nodes'])
        hf = eff_hostfile(self, hostfile=hf)
        if (hf is not None and hf.path is not None
                and self._container_engine != 'none'):
            hf = hf.copy()
        return hf

    def start(self):
        """Launch IOR via MpiExecInfo; Exec handles container wrapping transparently."""
        cfg = self.config

        # Stale-log guard: a partial/failed run must never report the
        # previous combo's bandwidths. shared_dir is bound at an identical
        # path in the container, so a host-side remove is sufficient.
        log_path = cfg.get('log')
        if log_path and os.path.isfile(log_path):
            try:
                os.remove(log_path)
            except OSError:
                pass

        hostfile = self._eff_hostfile()

        # Ensure the output parent dir exists in the deployment context:
        # inside the container instance when containerized (the path may be
        # an in-container-only mount), a harmless mkdir -p bare-metal.
        out = os.path.expandvars(cfg['out'])
        parent_dir = str(pathlib.Path(out).parent)
        Mkdir(parent_dir,
              PsshExecInfo(env=self.mod_env, hostfile=hostfile,
                           **container_kwargs(self))).run()

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
        if cfg.get('stonewall', 0) > 0:
            cmd.append(f'-D {cfg["stonewall"]}')

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
            hostfile=hostfile,
            port=self.ssh_port,
            env=self.mod_env,
            **container_kwargs(self),
        )).run()

        # Fail loudly on a silent MPI/ior failure. A hard mpiexec abort
        # (e.g. "PRTE has lost communication with a remote daemon" when the
        # cross-node spawn fails) or an ior that never reached its results
        # block leaves no summary in the log, yet Exec does not always raise.
        # Without this gate the pipeline marks the combination success with
        # blank bandwidths -- a false green that would let a daily regression
        # report healthy while multi-node is broken.
        self._assert_ior_completed()

    def _assert_ior_completed(self):
        """Raise if the just-finished ior run produced no results summary.

        Reads the log (same file _get_stat parses) and requires a Max
        Write/Read line for each requested operation. A missing summary
        means ior aborted or never ran the measured I/O -- surface it as a
        failed combination instead of a success with empty stats.
        """
        wrote = self.config.get('write', True)
        read = self.config.get('read', False)
        if not wrote and not read:
            return  # no workload requested; nothing to validate

        log_path = self.config.get('log')
        text = ''
        if log_path and os.path.isfile(log_path):
            try:
                with open(log_path, 'r') as f:
                    text = f.read()
            except OSError:
                text = ''
        stats = self.parse_log(text)

        missing = []
        if wrote and f'{self.pkg_id}.write_max_mibs' not in stats:
            missing.append('write')
        if read and f'{self.pkg_id}.read_max_mibs' not in stats:
            missing.append('read')
        if missing:
            raise RuntimeError(
                f'ior[{self.pkg_id}]: no IOR {"/".join(missing)} summary in '
                f'{log_path!r} -- the run failed (mpiexec abort or no '
                f'cross-node spawn) and produced no bandwidth. Failing this '
                f'combination rather than reporting a false success; inspect '
                f'the log for the mpiexec/PRTE error.')

    def stop(self):
        """Stop IOR (no-op — IOR runs to completion)."""
        pass

    def clean(self):
        """Remove IOR output files."""
        Rm(self.config['out'] + '*',
           PsshExecInfo(env=self.env,
                        hostfile=self._eff_hostfile(),
                        **container_kwargs(self))).run()

    # ------------------------------------------------------------------
    # Output parsing
    # ------------------------------------------------------------------

    # Captures "Max Write: 269.97 MiB/sec (283.08 MB/sec)" — the trailing
    # MB/sec figure in parens is decimal-megabytes (1e6 bytes/sec); IOR
    # also prints binary MiB/sec (2^20 bytes/sec). We expose both.
    _MAX_RE = re.compile(
        r'^Max\s+(?P<op>Write|Read):\s+'
        r'(?P<mib>[0-9.]+)\s+MiB/sec\s+'
        r'\((?P<mb>[0-9.]+)\s+MB/sec\)',
        re.MULTILINE,
    )

    # Captures the per-operation summary row at the end of an IOR run.
    # The columns in IOR 3.3.0 are: Operation Max(MiB) Min(MiB) Mean(MiB)
    # StdDev Max(OPs) Min(OPs) Mean(OPs) StdDev Mean(s) ... — we keep
    # the MiB stats (first four numeric columns after the op name).
    _SUMMARY_RE = re.compile(
        r'^(?P<op>write|read)\s+'
        r'(?P<max>[0-9.]+)\s+'
        r'(?P<min>[0-9.]+)\s+'
        r'(?P<mean>[0-9.]+)\s+'
        r'(?P<stddev>[0-9.]+)\s+',
        re.MULTILINE,
    )

    def parse_log(self, text: str) -> dict:
        """Extract bandwidth stats from raw IOR log text.

        Returns a dict keyed by ``{pkg_id}.<op>_<stat>`` (e.g.
        ``ior_smoke.write_max_mibs``). Both the binary (MiB/sec) and
        decimal (MB/sec) maxes are recorded; the summary block fills
        in mean/min/stddev when present. The function never raises —
        unparseable text simply yields an empty dict.
        """
        stats: dict = {}
        prefix = self.pkg_id

        for m in self._MAX_RE.finditer(text):
            op = m.group('op').lower()
            stats[f'{prefix}.{op}_max_mibs'] = float(m.group('mib'))
            stats[f'{prefix}.{op}_max_mbs'] = float(m.group('mb'))

        for m in self._SUMMARY_RE.finditer(text):
            op = m.group('op')
            stats[f'{prefix}.{op}_max_mibs'] = float(m.group('max'))
            stats[f'{prefix}.{op}_min_mibs'] = float(m.group('min'))
            stats[f'{prefix}.{op}_mean_mibs'] = float(m.group('mean'))
            stats[f'{prefix}.{op}_stddev_mibs'] = float(m.group('stddev'))

        return stats

    def _get_stat(self, stat_dict):
        """Populate ``stat_dict`` with bandwidths parsed from the IOR log.

        Reads ``self.config['log']`` (defaulted to ``<shared_dir>/ior.log``
        by ``_configure``) and adds Max/Min/Mean/StdDev MiB/sec entries
        per operation. Missing or unparseable log → only runtime is set.
        """
        stat_dict[f'{self.pkg_id}.runtime'] = self.runtime

        log_path = self.config.get('log')
        if not log_path or not os.path.isfile(log_path):
            return

        try:
            with open(log_path, 'r') as f:
                text = f.read()
        except OSError:
            return

        stat_dict.update(self.parse_log(text))

    def log(self, message):
        """Simple logging method."""
        print(f"[IOR:{self.pkg_id}] {message}")
