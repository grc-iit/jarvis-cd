"""
This module provides classes and methods to launch Redis.
Redis cluster is used if the hostfile has many hosts.
"""
import time

from jarvis_cd.core.pkg import Service
from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Kill
from jarvis_cd.util.container_utils import (
    all_hosts_ok, container_kwargs, eff_hostfile, single_instance_menu_opt)
from jarvis_cd.util.logger import Color


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
            single_instance_menu_opt(
                msg='Force ONE redis server on the first host even when '
                    'the hostfile has >1 host (skip the cluster branch). '
                    'Use when redis is a metadata singleton — e.g. '
                    'JuiceFS meta over redis://.../1, which needs SELECT '
                    'and so cannot use DB0-only cluster mode. Default '
                    'keeps the legacy behaviour (cluster iff >1 host).'),
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
                                hostfile=eff_hostfile(self),
                                collect_output=True,
                                **container_kwargs(self))).run()
        return all_hosts_ok(res, expect)

    def start(self):
        # single_instance pins to the first host (see eff_hostfile), keeping
        # redis a plain non-cluster server: cluster mode is DB0-only and
        # breaks clients that SELECT a non-zero DB (e.g. a JuiceFS meta_url
        # of redis://.../1), and N servers sharing one nodes.conf on a
        # shared filesystem collide anyway.
        hostfile = eff_hostfile(self)
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
                          **container_kwargs(self))).run()

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
            **container_kwargs(self),
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
                                   **container_kwargs(self))).run()
                Exec(f'redis-cli -p {port} -h {host} cluster reset',
                     LocalExecInfo(env=self.mod_env,
                                   **container_kwargs(self))).run()

            cmd = ' '.join([
                'redis-cli',
                f'--cluster create {host_str}',
                '--cluster-replicas 0',
                '--cluster-yes',
            ])
            Exec(cmd, LocalExecInfo(
                env=self.mod_env,
                **container_kwargs(self),
            )).run()
            self.sleep()

    def stop(self):
        port = self.config['port']
        hostfile = eff_hostfile(self)
        # Graceful shutdown via redis-cli on the SAME hosts redis was started
        # on (single_instance => first host only), else stop tries to reach
        # servers that were never launched.
        Exec(f'redis-cli -p {port} shutdown nosave',
             PsshExecInfo(env=self.mod_env,
                          hostfile=hostfile,
                          **container_kwargs(self))).run()
        # Fallback: force-kill any remaining redis-server processes.
        Kill('redis-server',
             PsshExecInfo(env=self.mod_env,
                          hostfile=hostfile,
                          **container_kwargs(self))).run()
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
