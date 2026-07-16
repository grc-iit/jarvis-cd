"""
FIO benchmark package. Supports default (bare-metal) and container
deployment via the two-phase build/deploy container architecture.
FIO itself is tiny — apt-installed in the deploy image, no build.sh needed.
"""
import json
import os
import pathlib

from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo, MpiExecInfo, Mkdir
from jarvis_cd.shell.process import Rm
from jarvis_cd.util.hostfile import Hostfile


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
            {'name': 'mode', 'msg': 'fio --rw mode; overrides read/write '
             'when set (adds random and time-based patterns); one of '
             'write/read/randwrite/randread/readwrite. No choices= here: '
             'jarvis enforces choices against the None default and rejects '
             'it; _configure validates a set value instead.',
             'type': str, 'default': None},
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
            {'name': 'target_dir', 'msg': 'Directory to run fio in '
             '(--directory mode, e.g. a FUSE mountpoint); overrides out',
             'type': str, 'default': None},
            {'name': 'direct', 'msg': 'Use direct I/O',
             'type': bool, 'default': False},
            {'name': 'random', 'msg': 'Use random access pattern',
             'type': bool, 'default': False},
            {'name': 'engine', 'msg': 'FIO I/O engine',
             'type': str, 'default': 'psync'},
            {'name': 'fio_bin', 'msg': 'Path to the fio binary',
             'type': str, 'default': 'fio'},
            {'name': 'runtime', 'msg': 'Seconds to run '
             '(fio --runtime --time_based); 0 = size-bound run',
             'type': int, 'default': 0},
            {'name': 'use_thread', 'msg': 'Spawn jobs as threads sharing '
             'one address space (fio --thread)',
             'type': bool, 'default': False},
            {'name': 'fallocate', 'msg': 'fio --fallocate mode. Use "none" '
             'for FUSE mounts that reject fallocate; "native" keeps fio\'s '
             'default (flag omitted)',
             'type': str, 'default': 'native',
             'choices': ['native', 'none', 'posix', 'keep']},
            {'name': 'log', 'msg': 'Path to FIO output log',
             'type': str, 'default': None},
            {'name': 'output_file', 'msg': 'fio JSON report filename '
             '(under shared_dir); enables JSON metrics in _get_stat',
             'type': str, 'default': None},
            {'name': 'single_instance', 'msg': 'Pin fio to the FIRST host '
             'even when the pipeline hostfile has >1 host. Use for '
             'single-client baselines (e.g. one client against NFS or a '
             'head-node-only FUSE mount) so N nodes do not clobber one '
             'shared JSON report or hit a mount that only exists on the '
             'head node',
             'type': bool, 'default': False},
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
        if self.config.get('nprocs', 1) <= 0:
            raise ValueError('fio: nprocs must be > 0')
        if int(self.config.get('runtime') or 0) < 0:
            raise ValueError('fio: runtime must be >= 0')
        mode = self.config.get('mode')
        if mode and mode not in ('write', 'read', 'randwrite', 'randread',
                                 'readwrite'):
            raise ValueError(f'fio: invalid mode {mode}')

    def _eff_hostfile(self):
        """The hostfile fio actually fans out over. With single_instance set
        and a multi-host pipeline hostfile, collapse to just the FIRST host;
        otherwise the full hostfile (single-node pipelines and genuinely
        distributed runs are unchanged)."""
        hf = self.hostfile
        if self.config.get('single_instance') and hf is not None \
                and len(hf.hosts) > 1:
            return Hostfile(hosts=hf.hosts[:1],
                            hosts_ip=hf.hosts_ip[:1] if hf.hosts_ip else None)
        return hf

    def _exec_info(self):
        exec_mode = self.config.get('exec_mode', 'pssh')
        nprocs = self.config.get('nprocs', 1)
        ppn = self.config.get('ppn', 1)
        hostfile = self._eff_hostfile()
        use_remote = hostfile is not None and not hostfile.is_local()

        kwargs = dict(env=self.mod_env)
        if self.config.get('deploy_mode') == 'container':
            kwargs.update(
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
            )

        if exec_mode == 'mpi' and use_remote:
            return MpiExecInfo(hostfile=hostfile, nprocs=nprocs, ppn=ppn,
                               port=self.ssh_port, **kwargs)
        if exec_mode == 'pssh' and use_remote:
            return PsshExecInfo(hostfile=hostfile, **kwargs)
        return LocalExecInfo(**kwargs)

    def _rw_mode(self):
        if self.config.get('mode'):
            return self.config['mode']
        if self.config['read'] and self.config['write']:
            return 'readwrite'
        if self.config['read']:
            return 'read'
        return 'write'

    def _json_path(self):
        return os.path.join(self.shared_dir, self.config['output_file'])

    def start(self):
        mode = self._rw_mode()
        target_dir = self.config.get('target_dir')
        exec_info = self._exec_info()

        if target_dir:
            # Directory mode: fio manages its own files under target_dir
            # (e.g. a FUSE mountpoint). Created via the wrapped exec_info —
            # for a container deploy target_dir may be an in-container path
            # (a mount that only exists in the instance's namespace), so a
            # host-side mkdir would create the wrong directory.
            target_dir = os.path.expanduser(
                os.path.expandvars(str(target_dir)))
            Mkdir(target_dir, exec_info).run()
        else:
            out = self.config['out']
            out_dir = str(pathlib.Path(out).parent) \
                if '.' in os.path.basename(out) else out
            Mkdir(out_dir, exec_info).run()

        runtime = int(self.config.get('runtime') or 0)
        cmd = [
            self.config.get('fio_bin') or 'fio',
            f'--rw={mode}',
            f'--size={self.config["total_size"]}',
            f'--bs={self.config["xfer"]}',
            f'--iodepth={self.config["iodepth"]}',
            f'--numjobs={self.config.get("nprocs", 1)}',
        ]
        if self.config.get('use_thread'):
            cmd.append('--thread')
        if runtime > 0:
            cmd += [f'--runtime={runtime}', '--time_based']
        cmd += [
            f'--direct={1 if self.config["direct"] else 0}',
            f'--randrepeat={1 if self.config["random"] else 0}',
        ]
        if target_dir:
            cmd.append(f'--directory={target_dir}')
        else:
            cmd.append(f'--filename={self.config["out"]}')
        cmd.append(f'--ioengine={self.config["engine"]}')
        if self.config.get('fallocate', 'native') != 'native':
            cmd.append(f'--fallocate={self.config["fallocate"]}')
        if self.config.get('output_file'):
            # JSON report to shared_dir: written where fio runs, read back
            # host-side by _get_stat (the start->_get_stat contract).
            cmd += ['--group_reporting', '--output-format=json',
                    f'--output={self._json_path()}']
        elif self.config.get('log'):
            cmd.append(f'--output={self.config["log"]}')
        cmd.append('--name=job')

        cmd = ' '.join(cmd)
        self.log(f'Executing: {cmd}')
        Exec(cmd, exec_info).run()

    def stop(self):
        pass

    def clean(self):
        if not self.config.get('target_dir'):
            Rm(self.config['out'] + '*', self._exec_info()).run()
        if self.config.get('output_file'):
            json_path = self._json_path()
            if os.path.exists(json_path):
                try:
                    os.remove(json_path)
                except OSError as e:
                    self.log(f'Error removing {json_path}: {e}')

    # ------------------------------------------------------------------
    # Statistics collection
    # ------------------------------------------------------------------

    def _get_stat(self, stat_dict):
        # start_time is in-memory run state, absent on the freshly-loaded
        # instance the sweep runner uses for stat collection (and never set by
        # jarvis core at all — dev builtin.fio has the same latent bug). Guard
        # it so the AttributeError does not sink the JSON metrics below.
        start_time = getattr(self, 'start_time', None)
        if start_time is not None:
            stat_dict[f'{self.pkg_id}.runtime'] = start_time
        if not self.config.get('output_file'):
            return
        # Called by the sweep runner on a FRESHLY-LOADED package instance,
        # so metrics must be re-read from disk, not from memory.
        json_path = self._json_path()
        if not os.path.exists(json_path):
            self.log(f'No fio report found at {json_path}')
            return
        with open(json_path, 'r') as f:
            raw = f.read()
        if not raw.strip():
            self.log(f'fio report is empty: {json_path}')
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            self.log(f'Could not parse fio JSON {json_path}: {e}')
            return
        self._parse_output(data, stat_dict)

    def _parse_output(self, data, stat_dict):
        """
        Extract bandwidth/IOPS/latency from a parsed fio JSON document into
        ``<pkg_id>.<op>.<metric>`` results.csv columns, for each op (read,
        write) that actually transferred bytes.
        """
        jobs = data.get('jobs') or []
        if not jobs:
            self.log('fio JSON has no jobs array; no metrics extracted')
            return
        extracted = 0
        for op in ('read', 'write'):
            section = jobs[0].get(op, {})
            if not section.get('io_bytes'):
                continue
            prefix = f'{self.pkg_id}.{op}'
            # bw is reported in KiB/s.
            if 'bw' in section:
                stat_dict[f'{prefix}.agg_bw_mbps'] = section['bw'] / 1024.0
            if 'iops' in section:
                stat_dict[f'{prefix}.iops'] = float(section['iops'])
            lat_ns = section.get('lat_ns', {})
            if 'mean' in lat_ns:
                stat_dict[f'{prefix}.lat_mean_us'] = lat_ns['mean'] / 1000.0
            clat_pct = section.get('clat_ns', {}).get('percentile', {})
            if '99.000000' in clat_pct:
                stat_dict[f'{prefix}.lat_p99_us'] = \
                    clat_pct['99.000000'] / 1000.0
            stat_dict[f'{prefix}.total_io_mb'] = \
                section['io_bytes'] / (1024.0 * 1024.0)
            extracted += 1
        if not extracted:
            self.log('Warning: no fio metrics extracted; check the fio '
                     f'JSON at {self._json_path()}')
