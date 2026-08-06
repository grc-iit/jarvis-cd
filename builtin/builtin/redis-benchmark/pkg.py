"""
This module provides classes and methods to launch the Redis benchmark tool.
"""
import csv
import io
import os
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, LocalExecInfo
from jarvis_cd.util.container_utils import (
    container_kwargs, eff_hostfile, single_instance_menu_opt)


class RedisBenchmark(Application):
    """
    Redis benchmark — supports default (host) and container deployment modes.

    Output is captured in ``--csv`` mode (one row per test with rps and
    latency percentiles) and teed to ``<shared_dir>/<pkg_id>_redis_bench.csv``
    so ``_get_stat`` can populate results.csv columns from disk.
    """

    def _configure_menu(self):
        return [
            {
                'name': 'port',
                'msg': 'The port to use',
                'type': int,
                'default': 6379,
            },
            {
                'name': 'count',
                'msg': 'Number of requests to generate (-n)',
                'type': int,
                'default': 100000,
            },
            {
                'name': 'write',
                'msg': 'Perform writes (SET)',
                'type': bool,
                'default': True,
            },
            {
                'name': 'read',
                'msg': 'Perform reads (GET)',
                'type': bool,
                'default': True,
            },
            {
                'name': 'nthreads',
                'msg': 'Number of I/O-issuing threads (--threads)',
                'type': int,
                'default': 1,
            },
            {
                'name': 'clients',
                'msg': 'Number of parallel client connections (-c). 50 is '
                       'the tool default. For a "threads" sweep, zip this '
                       'with nthreads so a row means N connections driven '
                       'by N threads.',
                'type': int,
                'default': 50,
            },
            {
                'name': 'pipeline',
                'msg': 'Number of requests to pipeline (-P)',
                'type': int,
                'default': 1,
            },
            {
                'name': 'req_size',
                'msg': 'Size of requests in bytes (-d, integer bytes)',
                'type': int,
                'default': 3,
            },
            {
                'name': 'node',
                'msg': 'The node index to use for cluster benchmarking',
                'type': int,
                'default': 0,
            },
            single_instance_menu_opt(
                msg='Run the benchmark on the FIRST host (next to a '
                    'single_instance redis) instead of the driver node, '
                    'and skip cluster addressing. For multi-node '
                    'pipelines with a head-pinned standalone redis.'),
        ]

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        base = getattr(self.pipeline, 'container_base', 'ubuntu:24.04')
        content = self._read_dockerfile('Dockerfile.deploy', {
            'DEPLOY_BASE': base,
        })
        return content, 'default'

    def _configure(self, **kwargs):
        super()._configure(**kwargs)

    def _csv_path(self):
        """Where the raw ``--csv`` output lands (shared_dir is bound at an
        identical path inside the container, so host-side reads work)."""
        return os.path.join(self.shared_dir,
                            f'{self.pkg_id}_redis_bench.csv')

    def start(self):
        # Stale-output guard: a failed run must not report the previous
        # combo's numbers.
        csv_path = self._csv_path()
        if os.path.isfile(csv_path):
            try:
                os.remove(csv_path)
            except OSError:
                pass

        bench_type = ','.join(filter(None, [
            'set' if self.config.get('write', True) else '',
            'get' if self.config.get('read', True) else '',
        ]))
        cmd = [
            'redis-benchmark',
            f'-n {self.config["count"]}',
            f'-t {bench_type}',
            f'-P {self.config["pipeline"]}',
            f'--threads {self.config["nthreads"]}',
            f'-c {self.config["clients"]}',
            f'-d {self.config["req_size"]}',
            f'-p {self.config["port"]}',
        ]

        exec_kwargs = dict(env=self.mod_env, **container_kwargs(self))
        if self.config.get('single_instance'):
            # Run next to the head-pinned standalone redis (LocalExecInfo
            # with a remote hostfile promotes to SSH on host[0]); loopback
            # addressing, no cluster flags.
            exec_kwargs['hostfile'] = eff_hostfile(self)
        else:
            # Legacy behavior: run on the driver node; address a cluster
            # node explicitly when the pipeline spans hosts.
            hostfile = self.hostfile
            if len(hostfile) > 1:
                cmd += [f'-h {hostfile.hosts[self.config["node"]]}',
                        '--cluster']

        cmd += ['--csv', f'2>&1 | tee {csv_path}']

        Exec(' '.join(cmd), LocalExecInfo(**exec_kwargs)).run()

    def stop(self):
        pass

    def clean(self):
        csv_path = self._csv_path()
        if os.path.isfile(csv_path):
            try:
                os.remove(csv_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Output parsing
    # ------------------------------------------------------------------

    def _parse_output(self, text):
        """Parse ``redis-benchmark --csv`` output into stat entries.

        Handles the redis 6/7 8-column form
        (``"test","rps","avg_latency_ms",...,"p50_latency_ms",...``) and
        falls back to the legacy 2-column ``"test","rps"`` form. Never
        raises; unparseable text yields an empty dict.
        """
        stats = {}
        prefix = self.pkg_id
        try:
            rows = list(csv.reader(io.StringIO(text)))
        except csv.Error:
            return stats
        if not rows:
            return stats

        header = [h.strip().lower() for h in rows[0]]
        col = {name: i for i, name in enumerate(header)}
        # Legacy 2-column output has no header row; detect by first cell.
        has_header = 'test' in col
        data_rows = rows[1:] if has_header else rows

        def field(row, name, legacy_idx=None):
            idx = col.get(name, legacy_idx if not has_header else None)
            if idx is None or idx >= len(row):
                return None
            try:
                return float(row[idx])
            except (TypeError, ValueError):
                return None

        for row in data_rows:
            if not row:
                continue
            test = row[0].strip().strip('"').lower()
            if test not in ('set', 'get'):
                continue
            rps = field(row, 'rps', legacy_idx=1)
            if rps is not None:
                stats[f'{prefix}.{test}_rps'] = rps
            p50 = field(row, 'p50_latency_ms')
            if p50 is not None:
                stats[f'{prefix}.{test}_p50_ms'] = p50
            p99 = field(row, 'p99_latency_ms')
            if p99 is not None:
                stats[f'{prefix}.{test}_p99_ms'] = p99
        return stats

    def _get_stat(self, stat_dict):
        """Populate ``stat_dict`` from the teed ``--csv`` file on disk.

        Called on a freshly-loaded instance after the run; missing or
        unparseable file → only runtime is recorded.
        """
        stat_dict[f'{self.pkg_id}.runtime'] = self.runtime

        csv_path = self._csv_path()
        if not os.path.isfile(csv_path):
            return
        try:
            with open(csv_path, 'r') as f:
                text = f.read()
        except OSError:
            return
        stat_dict.update(self._parse_output(text))
