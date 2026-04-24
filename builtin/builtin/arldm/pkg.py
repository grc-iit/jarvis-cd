"""
ARLDM (Auto-Regressive Latent Diffusion Models) workflow. Supports bare-metal
and container deployments. Container mode clones the upstream ARLDM source
into /opt/ARLDM and pip-installs a CPU-only PyTorch + Lightning + Transformers
stack in build.sh. start() runs a tiny autoencoder training loop
(scripts/mini_train.py) that exercises the same imports ARLDM's main.py uses
— no GPU or multi-GB BLIP/Stable-Diffusion weights required.
"""
import os
import shutil

from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo, MpiExecInfo


_RUNSCRIPTS = ['flintstones', 'pororo', 'vistsis', 'vistdii']
_ARLDM_PATH_CONTAINER = '/opt/ARLDM'
_MINI_TRAIN_BASENAME = 'mini_train.py'


class Arldm(Application):
    """
    ARLDM driver.
    """

    def _configure_menu(self):
        return [
            {'name': 'arldm_path',
             'msg': ('Absolute path to the ARLDM source tree. In container '
                     'mode this is baked in at /opt/ARLDM and this option '
                     'is ignored.'),
             'type': str, 'default': None},
            {'name': 'runscript', 'msg': 'ARLDM dataset/script identifier',
             'type': str, 'default': 'vistsis', 'choices': _RUNSCRIPTS},
            {'name': 'mode', 'msg': 'Run mode: train or sample',
             'type': str, 'default': 'train', 'choices': ['train', 'sample']},
            {'name': 'nprocs', 'msg': 'Total number of ARLDM processes (mpi mode)',
             'type': int, 'default': 2},
            {'name': 'ppn', 'msg': 'Processes per node',
             'type': int, 'default': 2},
            {'name': 'exec_mode', 'msg': 'Multi-node mode: mpi or pssh',
             'type': str, 'default': 'mpi', 'choices': ['mpi', 'pssh']},
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
            arldm_path = _ARLDM_PATH_CONTAINER
        else:
            arldm_path = (
                self.config.get('arldm_path')
                or self.env.get('ARLDM_PATH')
                or os.environ.get('ARLDM_PATH')
            )
            if not arldm_path:
                raise RuntimeError(
                    "ARLDM_PATH is not set. Set arldm_path or export "
                    "ARLDM_PATH for bare-metal, or use deploy_mode=container."
                )
            if not os.path.isdir(arldm_path):
                raise RuntimeError(
                    f"arldm_path does not exist on the local node: {arldm_path}"
                )
        self.config['arldm_path'] = arldm_path
        self.setenv('ARLDM_PATH', arldm_path)

        # Stage mini_train.py into shared_dir so it is visible inside the
        # container (shared_dir is bind-mounted at the same path). The script
        # ships with the package under scripts/.
        src = os.path.join(self.pkg_dir, 'scripts', _MINI_TRAIN_BASENAME)
        dst = os.path.join(self.shared_dir, _MINI_TRAIN_BASENAME)
        shutil.copyfile(src, dst)
        self.config['mini_train'] = dst

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

    def _exec_info(self):
        nprocs = self.config['nprocs']
        ppn = self.config['ppn']
        exec_mode = self.config.get('exec_mode', 'mpi')
        # Don't pass cwd here: it would be applied host-side by subprocess.Popen,
        # but self.config['arldm_path'] only exists inside the container. We cd
        # inside the bash -c wrapper in start() instead.
        kwargs = dict(env=self.mod_env, **self._container_kwargs())

        if exec_mode == 'mpi':
            hostfile = self.hostfile if self._use_remote() else None
            return MpiExecInfo(
                nprocs=nprocs, ppn=ppn, hostfile=hostfile,
                port=self.ssh_port, **kwargs,
            )
        if exec_mode == 'pssh' and self._use_remote():
            return PsshExecInfo(hostfile=self.hostfile, **kwargs)
        return LocalExecInfo(**kwargs)

    def start(self):
        # The container wrapper doesn't honor exec_info.cwd, and mpirun
        # takes the first token as the executable — wrap in bash -c '...'
        # so mpirun launches `bash -c "cd X && python3 ..."` per rank.
        cwd = self.config['arldm_path']
        inner = f'cd {cwd} && python3 {self.config["mini_train"]}'
        Exec(f'bash -c "{inner}"', self._exec_info()).run()

    def stop(self):
        pass

    def clean(self):
        pass

    def _get_stat(self, stat_dict):
        stat_dict[f'{self.pkg_id}.runtime'] = self.start_time
