"""
This module provides classes and methods to launch Redis.
Redis cluster is used if the hostfile has many hosts.
"""
import time

from jarvis_cd.core.pkg import Service
from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Kill
from jarvis_cd.util.hostfile import Hostfile
from jarvis_cd.util.logger import Color


def _all_hosts_ok(exec_result, expect=None):
    """True iff every host exited 0 and, when ``expect`` is given, every
    host's stdout contains it. Handles dict-or-scalar exit_code/stdout."""
    codes = getattr(exec_result, 'exit_code', 1)
    if isinstance(codes, dict):
        if any(c != 0 for c in codes.values()):
            return False
    elif codes != 0:
        return False
    if expect is None:
        return True
    out = getattr(exec_result, 'stdout', '')
    if isinstance(out, dict):
        return all(expect in str(v) for v in out.values())
    return expect in str(out)


class Redis(Service):
    """
    Redis server — supports default (host) and container deployment modes.
    """

    def _configure_menu(self):
        return [
            {
                'name': 'port',
                'msg': 'The port to use for the cluster',
                'type': int,
                'default': 6379,
            },
            {
                'name': 'single_instance',
                'msg': 'Force ONE redis server on the first host even when '
                       'the hostfile has >1 host (skip the cluster branch). '
                       'Use when redis is a metadata singleton — e.g. '
                       'JuiceFS meta over redis://.../1, which needs SELECT '
                       'and so cannot use DB0-only cluster mode. Default '
                       'keeps the legacy behaviour (cluster iff >1 host).',
                'type': bool,
                'default': False,
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
        self.copy_template_file(
            f'{self.pkg_dir}/config/redis.conf',
            f'{self.shared_dir}/redis.conf',
            {'PORT': self.config['port']},
        )

    def _eff_hostfile(self):
        """The hostfile the redis server(s) actually run on.

        With single_instance set and a multi-host pipeline hostfile, collapse
        to just the FIRST host so redis stays a plain (non-cluster) server:
        cluster mode is DB0-only and would break clients that SELECT a
        non-zero DB (e.g. a JuiceFS meta_url of redis://.../1), and N servers
        sharing one nodes.conf on a shared filesystem collide anyway.
        Otherwise return the full hostfile (>1 host still takes the cluster
        branch)."""
        hf = self.hostfile
        if self.config.get('single_instance') and len(hf.hosts) > 1:
            return Hostfile(
                hosts=hf.hosts[:1],
                hosts_ip=hf.hosts_ip[:1] if hf.hosts_ip else None)
        return hf

    def _container_kwargs(self):
        """ExecInfo kwargs that route a command into the deployment context
        (a no-op for the default deploy mode: ``_container_engine`` is
        ``'none'`` and the container wrap is skipped)."""
        return dict(
            container=self._container_engine,
            container_image=self.deploy_image_name(),
            shared_dir=self.shared_dir,
            private_dir=self.private_dir,
        )

    def _redis_cli(self, args, expect=None, timeout_s=2):
        """Run ``redis-cli <args>`` in the deployment context (inside the
        container instance for a container deploy — redis-cli need not exist
        host-side). ``timeout N`` bounds each attempt; callers keep their
        own retry loops.

        :param args: redis-cli argument string (e.g. ``-p 6379 ping``)
        :param expect: optional substring required in every host's stdout
        :param timeout_s: per-attempt timeout in seconds
        :return: True iff every host exited 0 (and printed ``expect``)
        """
        res = Exec(f'timeout {timeout_s} redis-cli {args}',
                   PsshExecInfo(env=self.mod_env,
                                hostfile=self._eff_hostfile(),
                                collect_output=True,
                                **self._container_kwargs())).run()
        return _all_hosts_ok(res, expect)

    def start(self):
        hostfile = self._eff_hostfile()
        port = self.config['port']
        host_str = ' '.join(f'{h}:{port}' for h in hostfile.hosts)
        cluster_config_file = f'{self.private_dir}/nodes.conf'

        # Redis loads ./dump.rdb from its working directory at startup. The
        # conf sets `dir ./`, which resolves to whatever CWD redis starts in —
        # and a stale dump.rdb left there by a prior run or a newer redis
        # crashes startup ("Can't handle RDB format version N / Fatal error
        # loading the DB. Exiting."). Pin --dir to THIS run's private dir and
        # wipe any leftover dump so startup is clean.
        Exec(f'rm -f {self.private_dir}/dump.rdb',
             PsshExecInfo(env=self.mod_env, hostfile=hostfile,
                          **self._container_kwargs())).run()

        cmd = [
            'redis-server',
            f'{self.shared_dir}/redis.conf',
            f'--dir {self.private_dir}',
        ]
        if len(hostfile) > 1:
            cmd += [
                '--cluster-enabled yes',
                f'--cluster-config-file {cluster_config_file}',
                '--cluster-node-timeout 5000',
            ]

        Exec(' '.join(cmd), PsshExecInfo(
            env=self.mod_env,
            hostfile=hostfile,
            exec_async=True,
            bind_mounts=self.container_mounts,
            **self._container_kwargs(),
        )).run()

        self.sleep()

        # Wait for redis to actually accept connections before dependents
        # (benchmarks, JuiceFS format) connect. Warn-only: a slow-but-alive
        # server still comes up; a dead one fails loudly downstream.
        self.log(f'Waiting for Redis to accept connections on port {port}',
                 color=Color.YELLOW)
        for _ in range(30):
            if self._redis_cli(f'-p {port} ping', expect='PONG'):
                break
            time.sleep(1)
        else:
            self.log('WARNING: Redis did not respond to PING after 30s',
                     color=Color.RED)

        # Standalone hygiene — wipe ALL DBs so each run/sweep combo starts
        # from an empty server, regardless of any dump.rdb a prior run left
        # behind. The cluster branch below already flushall's per host.
        if len(hostfile) <= 1:
            self.log('Flushing all DBs (fresh slate for this run)',
                     color=Color.YELLOW)
            self._redis_cli(f'-p {port} flushall', timeout_s=5)

        if len(hostfile) > 1:
            for host in hostfile.hosts:
                Exec(f'redis-cli -p {port} -h {host} flushall',
                     LocalExecInfo(env=self.mod_env,
                                   **self._container_kwargs())).run()
                Exec(f'redis-cli -p {port} -h {host} cluster reset',
                     LocalExecInfo(env=self.mod_env,
                                   **self._container_kwargs())).run()

            cmd = ' '.join([
                'redis-cli',
                f'--cluster create {host_str}',
                '--cluster-replicas 0',
                '--cluster-yes',
            ])
            Exec(cmd, LocalExecInfo(
                env=self.mod_env,
                **self._container_kwargs(),
            )).run()
            self.sleep()

    def stop(self):
        port = self.config['port']
        hostfile = self._eff_hostfile()
        # Graceful shutdown via redis-cli on the SAME hosts redis was started
        # on (single_instance => first host only), else stop tries to reach
        # servers that were never launched.
        Exec(f'redis-cli -p {port} shutdown nosave',
             PsshExecInfo(env=self.mod_env,
                          hostfile=hostfile,
                          **self._container_kwargs())).run()
        # Fallback: force-kill any remaining redis-server processes.
        Kill('redis-server',
             PsshExecInfo(env=self.mod_env,
                          hostfile=hostfile,
                          **self._container_kwargs())).run()
        # Wait for the port to be free before returning so the next combo's
        # server doesn't race a dying one. If the deployment context is
        # already gone the probe fails -> treated as "port free", the right
        # best-effort answer at teardown.
        for _ in range(10):
            if not self._redis_cli(f'-p {port} ping', expect='PONG'):
                break
            time.sleep(1)
        time.sleep(1)

    def clean(self):
        pass
