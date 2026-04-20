"""
SparkCluster package. Runs a Spark master + worker, bare-metal or in a
container. Container mode downloads a prebuilt Spark tarball inside
build.sh (no source compilation), then the deploy image copies
/opt/spark + a JRE for runtime.
"""
import os
import time

from jarvis_cd.core.pkg import Service
from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo, MpiExecInfo


class SparkCluster(Service):
    """
    Spark standalone cluster driver (master + worker).
    """

    def _configure_menu(self):
        return [
            {'name': 'master_port', 'msg': 'Spark master RPC port',
             'type': int, 'default': 7077},
            {'name': 'worker_port', 'msg': 'Spark worker RPC port',
             'type': int, 'default': 7078},
            {'name': 'num_workers', 'msg': 'Number of worker nodes',
             'type': int, 'default': 1},
            {'name': 'sleep', 'msg': 'Seconds to wait after starting workers',
             'type': int, 'default': 2},
            {'name': 'exec_mode', 'msg': 'Multi-node mode: pssh or mpi',
             'type': str, 'default': 'pssh', 'choices': ['pssh', 'mpi']},
        ]

    # ------------------------------------------------------------------
    # Container build/deploy
    # ------------------------------------------------------------------

    def _build_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        return self._read_build_script('build.sh', {}), 'default'

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        base = getattr(self.pipeline, 'container_base', 'ubuntu:22.04')
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'DEPLOY_BASE': base,
        })
        return content, ''

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        super()._configure(**kwargs)

        if self.config.get('deploy_mode') == 'container':
            spark_home = '/opt/spark'
        else:
            spark_home = (
                self.env.get('SPARK_HOME')
                or self.env.get('SPARK_SCRIPTS')
                or os.environ.get('SPARK_HOME')
                or os.environ.get('SPARK_SCRIPTS')
            )
            if not spark_home:
                raise RuntimeError(
                    "SPARK_HOME is not set. Export SPARK_HOME or use "
                    "deploy_mode=container."
                )
        self.config['SPARK_HOME'] = spark_home
        master_host = self.hostfile.hosts[0] if self.hostfile else 'localhost'
        self.env['SPARK_MASTER_HOST'] = master_host
        self.env['SPARK_MASTER_PORT'] = str(self.config['master_port'])
        self.env['SPARK_WORKER_PORT'] = str(self.config['worker_port'])

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _use_remote(self):
        return self.hostfile is not None and not self.hostfile.is_local()

    def _container_kwargs(self):
        if self.config.get('deploy_mode') != 'container':
            return {}
        return dict(
            container=self._container_engine,
            container_image=self.deploy_image_name(),
            shared_dir=self.shared_dir,
            private_dir=self.private_dir,
        )

    def _master_exec_info(self):
        kwargs = dict(env=self.mod_env, **self._container_kwargs())
        if self._use_remote():
            return PsshExecInfo(hosts=self.hostfile.subset(1), **kwargs)
        return LocalExecInfo(**kwargs)

    def _workers_exec_info(self):
        num_workers = self.config.get('num_workers', 1)
        exec_mode = self.config.get('exec_mode', 'pssh')
        kwargs = dict(env=self.mod_env, **self._container_kwargs())
        if exec_mode == 'mpi' and self._use_remote():
            return MpiExecInfo(hostfile=self.hostfile.subset(num_workers),
                               nprocs=num_workers, ppn=1,
                               port=self.ssh_port, **kwargs)
        if self._use_remote():
            return PsshExecInfo(hosts=self.hostfile.subset(num_workers), **kwargs)
        return LocalExecInfo(**kwargs)

    def start(self):
        spark_home = self.config['SPARK_HOME']
        Exec(f'{spark_home}/sbin/start-master.sh',
             self._master_exec_info()).run()
        time.sleep(1)
        master_host = self.env['SPARK_MASTER_HOST']
        master_port = self.env['SPARK_MASTER_PORT']
        Exec(
            f'{spark_home}/sbin/start-worker.sh spark://{master_host}:{master_port}',
            self._workers_exec_info(),
        ).run()
        time.sleep(self.config.get('sleep', 2))

    def stop(self):
        spark_home = self.config['SPARK_HOME']
        Exec(f'{spark_home}/sbin/stop-worker.sh',
             self._workers_exec_info()).run()
        Exec(f'{spark_home}/sbin/stop-master.sh',
             self._master_exec_info()).run()

    def clean(self):
        pass

    def status(self):
        return True
