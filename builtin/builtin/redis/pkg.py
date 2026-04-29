"""
This module provides classes and methods to launch Redis.
Redis cluster is used if the hostfile has many hosts.
"""
from jarvis_cd.core.pkg import Service
from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Kill


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

    def start(self):
        hostfile = self.hostfile
        host_str = ' '.join(f'{h}:{self.config["port"]}' for h in hostfile.hosts)
        cluster_config_file = f'{self.private_dir}/nodes.conf'

        cmd = ['redis-server', f'{self.shared_dir}/redis.conf']
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
            container=self._container_engine,
            container_image=self.deploy_image_name(),
            shared_dir=self.shared_dir,
            private_dir=self.private_dir,
            bind_mounts=self.container_mounts,
        )).run()

        self.sleep()

        if len(hostfile) > 1:
            for host in hostfile.hosts:
                Exec(f'redis-cli -p {self.config["port"]} -h {host} flushall',
                     LocalExecInfo(env=self.mod_env,
                                   container=self._container_engine,
                                   container_image=self.deploy_image_name(),
                                   shared_dir=self.shared_dir,
                                   private_dir=self.private_dir)).run()
                Exec(f'redis-cli -p {self.config["port"]} -h {host} cluster reset',
                     LocalExecInfo(env=self.mod_env,
                                   container=self._container_engine,
                                   container_image=self.deploy_image_name(),
                                   shared_dir=self.shared_dir,
                                   private_dir=self.private_dir)).run()

            cmd = ' '.join([
                'redis-cli',
                f'--cluster create {host_str}',
                '--cluster-replicas 0',
                '--cluster-yes',
            ])
            Exec(cmd, LocalExecInfo(
                env=self.mod_env,
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
            )).run()
            self.sleep()

    def stop(self):
        Kill('redis-server', PsshExecInfo(env=self.env, hostfile=self.hostfile)).run()

    def clean(self):
        pass
