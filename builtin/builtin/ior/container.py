"""
Container-based IOR deployment using Docker/Podman/Apptainer.
"""
from jarvis_cd.core.container_pkg import ContainerApplication
from jarvis_cd.shell import Exec


class IorContainer(ContainerApplication):
    """
    Container-based IOR deployment.

    Build phase: Installs IOR from apt + OpenMPI (cached).
    Deploy phase: Copies the ior binary into a lean runtime image.
    """

    def _build_phase(self) -> str:
        """
        Generate the BUILD container Dockerfile.

        Downloads an IOR release tarball (includes pre-generated configure script)
        and builds against system OpenMPI. Each RUN layer maximizes cache reuse:
          apt deps → download tarball → configure + make
        """
        base = getattr(self.pipeline, 'container_base', 'ubuntu:24.04')
        return f"""FROM {base}

ARG DEBIAN_FRONTEND=noninteractive

# Build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl \\
    build-essential \\
    openmpi-bin libopenmpi-dev \\
    && rm -rf /var/lib/apt/lists/*

# Download IOR release tarball (includes pre-generated configure)
RUN curl -sL https://github.com/hpc/ior/releases/download/3.3.0/ior-3.3.0.tar.gz \\
    | tar -xz -C /opt \\
    && mv /opt/ior-3.3.0 /opt/ior

# Configure and build
RUN cd /opt/ior \\
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

    def start(self):
        cfg = self.config
        ior_args = [
            '-k',
            f'-b {cfg["block"]}',
            f'-t {cfg["xfer"]}',
            f'-a {cfg["api"].upper()}',
            f'-o {cfg["out"]}',
        ]
        if cfg.get('write'):
            ior_args.append('-w')
        if cfg.get('read'):
            ior_args.append('-r')
        if cfg.get('fpp'):
            ior_args.append('-F')
        if cfg.get('reps', 1) > 1:
            ior_args.append(f'-i {cfg["reps"]}')
        if cfg.get('direct'):
            ior_args.append('-O useO_DIRECT=1')

        nprocs = cfg.get('nprocs', 1)
        inner = f'mpirun --allow-run-as-root -n {nprocs} ior {" ".join(ior_args)}'
        if cfg.get('log'):
            inner += f' 2>&1 | tee {cfg["log"]}'

        Exec(inner, self.container_exec_info()).run()
