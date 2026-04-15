"""
This module provides classes and methods to launch the Redis benchmark tool.
"""
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
        ]

    def _build_deploy_phase(self) -> str:
        if self.config.get('deploy_mode') != 'container':
            return None
        base = getattr(self.pipeline, 'container_base', 'ubuntu:24.04')
        return f"""FROM {base}

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \\
    redis-server redis-tools \\
    gdb gdbserver \\
    && rm -rf /var/lib/apt/lists/*

CMD ["/bin/bash"]
"""

    def _configure(self, **kwargs):
        super()._configure(**kwargs)

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

        Exec(' '.join(cmd), LocalExecInfo(
            env=self.mod_env,
            container=self._container_engine,
            container_image=self.deploy_image_name,
            private_dir=self.private_dir,
        )).run()

    def stop(self):
        pass

    def clean(self):
        pass
