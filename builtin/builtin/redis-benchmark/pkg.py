"""
This module provides classes and methods to launch the Redis benchmark tool.
"""
import os
import pathlib
import re
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, LocalExecInfo


class RedisBenchmark(Application):
    """
    Redis benchmark — supports default (host) and container deployment modes.
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
                'msg': 'Number of requests to generate',
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
                'msg': 'Number of threads',
                'type': int,
                'default': 1,
            },
            {
                'name': 'pipeline',
                'msg': 'Number of requests to pipeline',
                'type': int,
                'default': 1,
            },
            {
                'name': 'req_size',
                'msg': 'Size of requests in bytes',
                'type': int,
                'default': 3,
            },
            {
                'name': 'node',
                'msg': 'The node index to use for cluster benchmarking',
                'type': int,
                'default': 0,
            },
            {
                'name': 'log',
                'msg': 'Path to redis-benchmark output log (parsed for throughput)',
                'type': str,
                'default': '',
            },
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

        # Default the log path to <shared_dir>/redis_benchmark.log so
        # _get_stat has something to parse even when the YAML omits `log:`.
        # shared_dir is bind-mounted into the container, so the host can
        # read it after the run. Users who set `log:` keep their override.
        if not self.config.get('log'):
            self.config['log'] = str(
                pathlib.Path(self.shared_dir) / 'redis_benchmark.log')

    def start(self):
        hostfile = self.hostfile
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
            f'-d {self.config["req_size"]}',
            f'-p {self.config["port"]}',
        ]
        if len(hostfile) > 1:
            cmd += [f'-h {hostfile.hosts[self.config["node"]]}', '--cluster']

        bench_cmd = ' '.join(cmd)
        if self.config.get('log'):
            bench_cmd += f' 2>&1 | tee {self.config["log"]}'

        Exec(bench_cmd, LocalExecInfo(
            env=self.mod_env,
            container=self._container_engine,
            container_image=self.deploy_image_name(),
            shared_dir=self.shared_dir,
                private_dir=self.private_dir,
        )).run()

    def stop(self):
        pass

    def clean(self):
        pass

    # ------------------------------------------------------------------
    # Output parsing
    # ------------------------------------------------------------------

    # redis-benchmark groups output per operation under a header line like
    # "====== SET ======" and reports throughput as either
    # "throughput summary: 71994.24 requests per second" (redis >= 6) or a
    # bare "71994.24 requests per second" line (older redis). We track the
    # current section header and capture the rps figure for it.
    _OP_MAP = {'SET': 'write', 'GET': 'read'}
    _HDR_RE = re.compile(r'^\s*=+\s*(?P<op>[A-Za-z]+)')
    _RPS_RE = re.compile(r'(?P<rps>[0-9][0-9.]*)\s+requests per second',
                         re.IGNORECASE)

    def parse_log(self, text: str) -> dict:
        """Extract throughput (requests/sec) from raw redis-benchmark output.

        Returns a dict keyed by ``{pkg_id}.<op>_rps`` (e.g.
        ``redis_bench.write_rps``). Never raises — unparseable text simply
        yields an empty dict.
        """
        stats: dict = {}
        prefix = self.pkg_id
        current = None
        for line in text.splitlines():
            hdr = self._HDR_RE.match(line)
            if hdr:
                current = hdr.group('op').upper()
                continue
            m = self._RPS_RE.search(line)
            if m and current:
                op = self._OP_MAP.get(current, current.lower())
                stats[f'{prefix}.{op}_rps'] = float(m.group('rps'))
        return stats

    def _get_stat(self, stat_dict):
        """Populate ``stat_dict`` with throughput parsed from the log.

        Reads ``self.config['log']`` (defaulted to
        ``<shared_dir>/redis_benchmark.log`` by ``_configure``) and adds a
        requests/sec entry per operation. Missing or unparseable log →
        only runtime is set.
        """
        stat_dict[f'{self.pkg_id}.runtime'] = getattr(self, 'start_time', None)

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
        print(f"[RedisBenchmark:{self.pkg_id}] {message}")
