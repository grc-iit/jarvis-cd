"""
Container-based IOR deployment using Docker/Podman/Apptainer.
"""
from jarvis_cd.core.container_pkg import ContainerApplication
from pathlib import Path


class IorContainer(ContainerApplication):
    """
    Container-based IOR deployment.

    Build phase: Builds IOR from source using autoconf + OpenMPI.
    Deploy phase: Copies the ior binary into a lean runtime image.
    """

    def _build_phase(self) -> str:
        """
        Generate the BUILD container Dockerfile.

        Clones IOR from GitHub and builds with autoconf against system OpenMPI.
        Each RUN layer is ordered to maximize Docker cache reuse:
          apt deps → clone → configure → make
        """
        base = getattr(self.pipeline, 'container_base', 'ubuntu:24.04')
        return f"""FROM {base}

ARG DEBIAN_FRONTEND=noninteractive

# System build dependencies (cached after first pull)
RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential autoconf automake libtool git \\
    openmpi-bin libopenmpi-dev \\
    && rm -rf /var/lib/apt/lists/*

# Clone IOR (cached until repo/branch changes)
RUN git clone --depth 1 https://github.com/hpc/ior.git /opt/ior

# Configure and build (cached when source is unchanged)
RUN cd /opt/ior \\
    && ./bootstrap \\
    && ./configure --prefix=/opt/ior/install \\
    && make -j$(nproc) \\
    && make install
"""

    def _build_deploy_phase(self) -> str:
        """
        Generate the DEPLOY container Dockerfile.

        Copies the ior binary and required MPI runtime from the build container.
        Much smaller than the full build image.
        """
        base = getattr(self.pipeline, 'container_base', 'ubuntu:24.04')
        return f"""FROM {self.build_image_name} AS builder
FROM {base}

ARG DEBIAN_FRONTEND=noninteractive

# MPI runtime only (no build tools)
RUN apt-get update && apt-get install -y --no-install-recommends \\
    openmpi-bin libopenmpi-dev \\
    openssh-server openssh-client \\
    && rm -rf /var/lib/apt/lists/* \\
    && mkdir -p /var/run/sshd \\
    && sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config \\
    && sed -i 's/#PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config

# Copy ior binary from build container
COPY --from=builder /opt/ior/install/bin/ior /usr/bin/ior

CMD ["/bin/bash"]
"""

    def augment_container(self) -> str:
        """
        Legacy pipeline-level Dockerfile commands for IOR installation.
        Used when the pipeline assembles a single Dockerfile for all packages.
        """
        return """
# Build IOR from source
RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential autoconf automake libtool git \\
    openmpi-bin libopenmpi-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/hpc/ior.git /opt/ior \\
    && cd /opt/ior \\
    && ./bootstrap \\
    && ./configure --prefix=/usr \\
    && make -j$(nproc) \\
    && make install
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
