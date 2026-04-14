"""
Container-based IOR deployment using Docker/Podman/Apptainer.
"""
from jarvis_cd.core.container_pkg import ContainerApplication
from pathlib import Path


class IorContainer(ContainerApplication):
    """
    Container-based IOR deployment.

    Build phase: Installs IOR via Spack on top of the iowarp base image.
    Deploy phase: Copies the IOR binary from the build container.
    """

    def _build_phase(self) -> str:
        """
        Generate the BUILD container Dockerfile.

        Installs IOR via Spack and copies binaries to /usr for easy access.
        The layer cache means this only rebuilds when the Dockerfile changes.
        """
        base = getattr(self.pipeline, 'container_base', 'iowarp/iowarp-build:latest')
        return f"""FROM {base}

ARG DEBIAN_FRONTEND=noninteractive

# Install IOR via Spack (cached after first build)
RUN . "${{SPACK_DIR}}/share/spack/setup-env.sh" && \\
    spack install -y ior

# Copy IOR and Jarvis dependencies to /usr for runtime access
RUN . "${{SPACK_DIR}}/share/spack/setup-env.sh" && \\
    spack load iowarp && \\
    cp -r $(spack location -i python)/bin/* /usr/bin || true && \\
    cp -r $(spack location -i py-pip)/bin/* /usr/bin || true && \\
    cp -r $(spack location -i python-venv)/bin/* /usr/bin || true && \\
    PYTHON_PATH=$(readlink -f /usr/bin/python3) && \\
    PYTHON_PREFIX=$(dirname $(dirname $PYTHON_PATH)) && \\
    cp -r $(spack location -i mpi)/bin/* /usr/bin || true && \\
    cp -r $(spack location -i ior)/bin/* /usr/bin || true && \\
    cp -r $(spack location -i iowarp-runtime)/bin/* /usr/bin || true && \\
    cp -r $(spack location -i iowarp-cte)/bin/* /usr/bin || true && \\
    cp -r $(spack location -i cte-hermes-shm)/bin/* /usr/bin || true && \\
    for pkg in $(spack find --format '{{name}}' | grep '^py-'); do \\
        cp -r $(spack location -i $pkg)/lib/* $PYTHON_PREFIX/lib/ 2>/dev/null || true; \\
        cp -r $(spack location -i $pkg)/bin/* /usr/bin 2>/dev/null || true; \\
    done && \\
    sed -i '1s|.*|#!/usr/bin/python3|' /usr/bin/jarvis && \\
    echo "IOR and dependencies installed"
"""

    def _build_deploy_phase(self) -> str:
        """
        Generate the DEPLOY container Dockerfile.

        Copies the pre-built IOR binary and Jarvis from the build container.
        The deploy container reuses the build image as its base since IOR
        depends on spack-managed MPI libraries.
        """
        return f"""FROM {self.build_image_name}

# Deploy container: IOR binary already available in /usr/bin from build phase
# SSH configuration for multi-node MPI runs
RUN apt-get update && apt-get install -y --no-install-recommends \\
    openssh-server openssh-client \\
    && rm -rf /var/lib/apt/lists/* \\
    && mkdir -p /var/run/sshd \\
    && sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config \\
    && sed -i 's/#PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config

CMD ["/bin/bash"]
"""

    def augment_container(self) -> str:
        """
        Legacy pipeline-level Dockerfile commands for IOR installation.
        Used when the pipeline assembles a single Dockerfile for all packages.
        """
        return """
# Install IOR
RUN . "${SPACK_DIR}/share/spack/setup-env.sh" && \\
    spack install -y ior

# Copy required spack executables and libraries to /usr
RUN . "${SPACK_DIR}/share/spack/setup-env.sh" && \\
    spack load iowarp && \\
    cp -r $(spack location -i python)/bin/* /usr/bin || true && \\
    cp -r $(spack location -i py-pip)/bin/* /usr/bin || true && \\
    cp -r $(spack location -i python-venv)/bin/* /usr/bin || true && \\
    PYTHON_PATH=$(readlink -f /usr/bin/python3) && \\
    PYTHON_PREFIX=$(dirname $(dirname $PYTHON_PATH)) && \\
    cp -r $(spack location -i mpi)/bin/* /usr/bin || true && \\
    cp -r $(spack location -i ior)/bin/* /usr/bin || true && \\
    cp -r $(spack location -i iowarp-runtime)/bin/* /usr/bin || true && \\
    cp -r $(spack location -i iowarp-cte)/bin/* /usr/bin || true && \\
    cp -r $(spack location -i cte-hermes-shm)/bin/* /usr/bin || true && \\
    for pkg in $(spack find --format '{name}' | grep '^py-'); do \\
        cp -r $(spack location -i $pkg)/lib/* $PYTHON_PREFIX/lib/ 2>/dev/null || true; \\
        cp -r $(spack location -i $pkg)/bin/* /usr/bin 2>/dev/null || true; \\
    done && \\
    sed -i '1s|.*|#!/usr/bin/python3|' /usr/bin/jarvis && \\
    echo "Spack packages copied to /usr directory"
"""

    def _configure(self, **kwargs):
        """Configure container deployment."""
        super()._configure(**kwargs)

    def _generate_dockerfile(self):
        """Generate Dockerfile for IOR container (legacy method)."""
        ssh_port = getattr(self.pipeline, 'container_ssh_port', 2222)
        if hasattr(self.pipeline, 'container_base') and self.pipeline.container_base:
            base_image = self.pipeline.container_base
        else:
            base_image = 'iowarp/iowarp-build:latest'

        sshd_port = ssh_port
        dockerfile_content = f"""FROM {base_image}

ARG DEBIAN_FRONTEND=noninteractive

RUN sed -i 's/^#*Port .*/Port {sshd_port}/' /etc/ssh/sshd_config
"""
        dockerfile_path = Path(self.shared_dir) / 'Dockerfile'
        with open(dockerfile_path, 'w') as f:
            f.write(dockerfile_content)

        print(f"Generated Dockerfile: {dockerfile_path}")
