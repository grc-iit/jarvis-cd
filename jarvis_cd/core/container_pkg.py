"""
Base classes for containerized application deployment.
"""
from .pkg import Application
from ..shell import Exec, LocalExecInfo
from ..shell.container_compose_exec import ContainerComposeExec, ContainerBuildExec
from ..shell.container_exec import ContainerExec
import os
import yaml
from pathlib import Path


class ContainerApplication(Application):
    """
    Base class for containerized application deployment using Docker/Podman/Apptainer.

    Implements a two-phase container build strategy:

    1. Build phase: A stable, cached container that compiles the application.
       Named automatically as 'jarvis-build-{pkg_name}'.
       Built once and reused across pipelines via Docker layer cache.

    2. Deploy phase: A lightweight container that copies binaries from the build
       container. Named after the pipeline. For apptainer, the deploy container
       is additionally converted to a SIF file.

    Subclasses must implement:
      - _build_phase(): Return Dockerfile content for the build container
      - _build_deploy_phase(): Return Dockerfile content for the deploy container
      - augment_container(): Return Dockerfile RUN commands for legacy pipeline builds
    """

    def _init(self):
        """Initialize paths"""
        pass

    @property
    def build_image_name(self) -> str:
        """
        Stable name for the BUILD container image.
        Independent of pipeline name so it can be reused across pipelines.
        """
        pkg_name = self.pkg_type.split('.')[-1].replace('_', '-')
        return f'jarvis-build-{pkg_name}'

    @property
    def deploy_image_name(self) -> str:
        """Name for the DEPLOY container image (pipeline-specific)."""
        if hasattr(self, 'pipeline') and self.pipeline:
            return self.pipeline.name
        return 'jarvis-deploy'

    @property
    def _container_engine(self) -> str:
        """Get the container engine from pipeline config."""
        return getattr(self.pipeline, 'container_engine', 'docker').lower()

    @property
    def _build_engine(self) -> str:
        """Get the engine to use for building (apptainer needs docker/podman as intermediate)."""
        engine = self._container_engine
        if engine == 'apptainer':
            import shutil
            if shutil.which('docker'):
                return 'docker'
            elif shutil.which('podman'):
                return 'podman'
            else:
                raise RuntimeError(
                    "Apptainer requires docker or podman for the build phase. "
                    "Neither was found in PATH."
                )
        return engine

    def _build_phase(self) -> str:
        """
        Return the Dockerfile content for the BUILD container.

        The build container is a heavy container that compiles the application
        from source. It is built once with a stable name and cached between
        pipeline runs via Docker layer cache.

        Override this method in subclasses to provide application-specific
        build steps. Return None to skip the build phase.

        :return: Dockerfile content string, or None to skip
        """
        return None

    def build_phase(self):
        """
        Build the BUILD container image and save Dockerfile to private_dir.

        Uses the container engine's layer cache so repeated builds are fast
        when the Dockerfile content hasn't changed. The image is named
        '{build_image_name}' (stable, pipeline-independent).

        :return: None
        """
        dockerfile_content = self._build_phase()
        if not dockerfile_content:
            return

        # Save Dockerfile to private_dir for audit/reference
        private_path = Path(self.private_dir)
        private_path.mkdir(parents=True, exist_ok=True)
        dockerfile_path = private_path / 'build.Dockerfile'

        with open(dockerfile_path, 'w') as f:
            f.write(dockerfile_content)

        print(f"Building build container: {self.build_image_name}")
        engine = self._build_engine
        build_cmd = (
            f"{engine} build -t {self.build_image_name} "
            f"-f {dockerfile_path} {private_path}"
        )
        Exec(build_cmd, LocalExecInfo()).run()
        print(f"Build container ready: {self.build_image_name}")

    def _build_deploy_phase(self) -> str:
        """
        Return the Dockerfile content for the DEPLOY container.

        The deploy container copies compiled binaries from the build container
        into a leaner runtime image. It is named after the pipeline.

        For apptainer, this docker image is additionally converted to a SIF file.

        Override this method in subclasses. Return None to skip.

        :return: Dockerfile content string, or None to skip
        """
        return None

    def build_deploy_phase(self):
        """
        Build the DEPLOY container image and save Dockerfile to private_dir.

        For docker/podman: builds image named after the pipeline.
        For apptainer: builds docker image then converts to SIF file stored in
        private_dir/{deploy_image_name}.sif.

        :return: None
        """
        dockerfile_content = self._build_deploy_phase()
        if not dockerfile_content:
            return

        private_path = Path(self.private_dir)
        private_path.mkdir(parents=True, exist_ok=True)
        dockerfile_path = private_path / 'deploy.Dockerfile'

        with open(dockerfile_path, 'w') as f:
            f.write(dockerfile_content)

        engine = self._container_engine
        build_engine = self._build_engine

        print(f"Building deploy container: {self.deploy_image_name}")
        build_cmd = (
            f"{build_engine} build -t {self.deploy_image_name} "
            f"-f {dockerfile_path} {private_path}"
        )
        Exec(build_cmd, LocalExecInfo()).run()

        if engine == 'apptainer':
            sif_path = private_path / f'{self.deploy_image_name}.sif'
            print(f"Converting to Apptainer SIF: {sif_path}")
            from ..shell.container_compose_exec import ApptainerBuildExec
            apptainer_exec = ApptainerBuildExec(
                self.deploy_image_name,
                str(sif_path),
                LocalExecInfo(),
                source='docker-daemon'
            )
            apptainer_exec.run()
            print(f"Apptainer SIF ready: {sif_path}")
        else:
            print(f"Deploy container ready: {self.deploy_image_name}")

    def _configure(self, **kwargs):
        """
        Configure container deployment.
        Triggers build and deploy phase container builds.

        :param kwargs: Configuration parameters for this pkg.
        :return: None
        """
        super()._configure(**kwargs)
        self.build_phase()
        self.build_deploy_phase()

    def augment_container(self) -> str:
        """
        Return Dockerfile RUN commands to install this package in a container.

        Used by the pipeline-level Dockerfile generation (legacy mechanism).
        Override in subclasses to provide package-specific installation steps.

        :return: Dockerfile commands as a string
        """
        return ""

    def _generate_container_ppl_yaml(self):
        """
        Generate pipeline YAML file containing just this package.
        Used to load the pipeline configuration inside the container.
        """
        pkg_config = self.config.copy()
        pkg_entry = {'pkg_type': self.pkg_type}
        for key, value in pkg_config.items():
            if key != 'deploy':
                pkg_entry[key] = value

        pipeline_config = {
            'name': f'{self.pipeline.name}_container',
            'pkgs': [pkg_entry]
        }

        interceptors_list = pkg_config.get('interceptors', [])
        if interceptors_list:
            pipeline_config['interceptors'] = []
            for interceptor_name in interceptors_list:
                if interceptor_name in self.pipeline.interceptors:
                    interceptor_def = self.pipeline.interceptors[interceptor_name]
                    interceptor_entry = {'pkg_type': interceptor_def['pkg_type']}
                    for key, value in interceptor_def.get('config', {}).items():
                        interceptor_entry[key] = value
                    pipeline_config['interceptors'].append(interceptor_entry)

        pipeline_file = Path(self.shared_dir) / 'pkg.yaml'
        with open(pipeline_file, 'w') as f:
            yaml.dump(pipeline_config, f, default_flow_style=False)

        print(f"Generated pipeline YAML: {pipeline_file}")

    def _generate_dockerfile(self):
        """
        Generate Dockerfile for the container.
        Deprecated: use _build_phase()/_build_deploy_phase() instead.
        Subclasses may override for custom Dockerfile generation.

        :return: None
        """
        pass

    def _get_container_command(self):
        """
        Get the command to run in the container.
        """
        ssh_port = getattr(self.pipeline, 'container_ssh_port', 2222)
        return [
            f'cp -r /root/.ssh_host /root/.ssh && '
            f'chmod 700 /root/.ssh && '
            f'chmod 600 /root/.ssh/* 2>/dev/null || true && '
            f'cat /root/.ssh/*.pub > /root/.ssh/authorized_keys 2>/dev/null && '
            f'chmod 600 /root/.ssh/authorized_keys 2>/dev/null || true && '
            f'echo "Host *" > /root/.ssh/config && '
            f'echo "    Port {ssh_port}" >> /root/.ssh/config && '
            f'echo "    StrictHostKeyChecking no" >> /root/.ssh/config && '
            f'chmod 600 /root/.ssh/config && '
            f'sed -i "s/^#*Port .*/Port {ssh_port}/" /etc/ssh/sshd_config && '
            f'/usr/sbin/sshd && '
            f'jarvis ppl run yaml /root/.ppi-jarvis/shared/pkg.yaml && '
            f'tail -f /dev/null'
        ]

    def _get_service_name(self):
        """Get the service name for the compose file."""
        if self.pkg_type and '.' in self.pkg_type:
            return self.pkg_type.split('.')[-1]
        return 'app'

    def _generate_compose_file(self):
        """Generate docker/podman compose file."""
        container_name = f"{self.pipeline.name}_{self.pkg_id}"
        service_name = self._get_service_name()
        shm_size = self.config.get('shm_size', 0)
        ssh_dir = os.path.expanduser('~/.ssh')

        compose_config = {
            'services': {
                service_name: {
                    'container_name': container_name,
                    'entrypoint': ['/bin/bash', '-c'],
                    'command': self._get_container_command(),
                    'volumes': [
                        f"{self.private_dir}:/root/.ppi-jarvis/private",
                        f"{self.shared_dir}:/root/.ppi-jarvis/shared",
                        f"{ssh_dir}:/root/.ssh_host:ro"
                    ]
                }
            }
        }

        if hasattr(self.pipeline, 'get_container_image') and self.pipeline.get_container_image():
            compose_config['services'][service_name]['image'] = self.pipeline.get_container_image()
        else:
            compose_config['services'][service_name]['build'] = str(self.shared_dir)

        compose_config['services'][service_name]['network_mode'] = 'host'

        if shm_size > 0:
            compose_config['services'][service_name]['shm_size'] = f'{shm_size}m'
            self.pipeline.shm_container = container_name
        elif hasattr(self.pipeline, 'shm_container') and self.pipeline.shm_container:
            compose_config['services'][service_name]['ipc'] = f"container:{self.pipeline.shm_container}"

        compose_file = Path(self.shared_dir) / 'compose.yaml'
        with open(compose_file, 'w') as f:
            yaml.dump(compose_config, f, default_flow_style=False)

        print(f"Generated compose file: {compose_file}")

    def _build_image(self):
        """Build the container image. Deprecated: use build_phase() instead."""
        pass

    def start(self):
        """Start is handled at the pipeline level for containerized applications."""
        pass

    def stop(self):
        """Stop is handled at the pipeline level for containerized applications."""
        pass

    def clean(self):
        """Clean is handled at the pipeline level for containerized applications."""
        pass

    def kill(self):
        """Kill is handled at the pipeline level for containerized applications."""
        pass


class ContainerService(ContainerApplication):
    """
    Alias for ContainerApplication following service naming conventions.
    """
    pass
