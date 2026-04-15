"""
VPIC-Kokkos — Vector Particle-In-Cell plasma physics simulation.
GPU-accelerated, relativistic, kinetic PIC code from Los Alamos National Lab.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, LocalExecInfo, MpiExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm
import os


class Vpic(Application):
    """
    Merged VPIC class supporting both default (bare-metal) and container deployment.

    Set deploy_mode='container' to build and run VPIC inside a Docker/Podman/Apptainer
    container with Kokkos CUDA backend.  Set deploy_mode='default' to use a
    system-installed vpic binary via MPI.
    """

    def _init(self):
        self.deck_binary = None

    def _configure_menu(self):
        return [
            {
                'name': 'deploy_mode',
                'msg': 'Deployment mode',
                'type': str,
                'choices': ['default', 'container'],
                'default': 'default',
            },
            {
                'name': 'nprocs',
                'msg': 'Number of MPI processes',
                'type': int,
                'default': 4,
            },
            {
                'name': 'ppn',
                'msg': 'Processes per node',
                'type': int,
                'default': 4,
            },
            {
                'name': 'deck',
                'msg': 'Path to VPIC input deck (.cxx file)',
                'type': str,
                'default': None,
            },
            {
                'name': 'sample_deck',
                'msg': 'Built-in sample deck to use (harris, lpi, langmuir_wave)',
                'type': str,
                'choices': ['harris', 'lpi', 'langmuir_wave', 'custom'],
                'default': 'harris',
            },
            {
                'name': 'run_dir',
                'msg': 'Working directory for VPIC run',
                'type': str,
                'default': '/tmp/vpic_run',
            },
            {
                'name': 'cuda_arch',
                'msg': 'CUDA architecture code (80=A100, 90=H100, 70=V100)',
                'type': int,
                'default': 80,
            },
            {
                'name': 'base_image',
                'msg': 'Base Docker image for build container',
                'type': str,
                'default': 'sci-hpc-base',
            },
        ]

    # ------------------------------------------------------------------
    # Container Dockerfile generators
    # ------------------------------------------------------------------

    def _build_phase(self) -> str:
        """
        Return the BUILD container Dockerfile, or None when not in container mode.
        Builds with Kokkos CUDA when base_image contains 'sci-hpc',
        otherwise builds CPU-only (Kokkos Serial/OpenMP).
        """
        if self.config.get('deploy_mode') != 'container':
            return None
        base = self.config.get('base_image', 'sci-hpc-base')
        use_gpu = 'sci-hpc' in base

        if use_gpu:
            cuda_arch = self.config.get('cuda_arch', 80)
            cmake_flags = (
                f'-DENABLE_KOKKOS_CUDA=ON '
                f'-DBUILD_INTERNAL_KOKKOS=ON '
                f'"-DKokkos_ARCH_AMPERE{cuda_arch}=ON" '
                f'-DCMAKE_CXX_COMPILER="$(pwd)/kokkos/bin/nvcc_wrapper"'
            )
            post_build = (
                '# Patch deck-compiler to link against CUDA stub library\n'
                'RUN sed -i \\\n'
                "    's|-lkokkossimd|-lkokkossimd -L/usr/local/cuda/lib64/stubs -lcuda|' \\\n"
                '    /opt/vpic-kokkos/build/bin/vpic\n'
                '\n'
                'ENV NVCC_WRAPPER_DEFAULT_COMPILER=g++\n'
            )
        else:
            cmake_flags = (
                '-DENABLE_KOKKOS_CUDA=OFF '
                '-DBUILD_INTERNAL_KOKKOS=ON'
            )
            post_build = ''

        return f"""FROM {base}

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates git cmake build-essential \\
    openmpi-bin libopenmpi-dev \\
    && rm -rf /var/lib/apt/lists/*

# Clone VPIC-Kokkos with bundled Kokkos (cached until repo changes)
RUN git clone --recursive https://github.com/lanl/vpic-kokkos.git /opt/vpic-kokkos

# Build VPIC core library
RUN cd /opt/vpic-kokkos \\
    && cmake -S . -B build \\
        -DCMAKE_BUILD_TYPE=Release \\
        {cmake_flags} \\
    && cmake --build build -j$(nproc)

{post_build}ENV PATH=/opt/vpic-kokkos/build/bin:${{PATH}}
"""

    def _build_deploy_phase(self) -> str:
        """
        Return the DEPLOY container Dockerfile, or None when not in container mode.
        """
        if self.config.get('deploy_mode') != 'container':
            return None
        base = self.config.get('base_image', 'sci-hpc-base')
        use_gpu = 'sci-hpc' in base

        nvcc_env = 'ENV NVCC_WRAPPER_DEFAULT_COMPILER=g++\n' if use_gpu else ''
        # Only copy nvcc_wrapper if GPU build
        nvcc_copy = (
            'COPY --from=builder /opt/vpic-kokkos/kokkos/bin/nvcc_wrapper '
            '/opt/vpic-kokkos/kokkos/bin/nvcc_wrapper\n'
        ) if use_gpu else ''

        return f"""FROM {self.build_image_name} AS builder
FROM {base}

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    openmpi-bin libopenmpi-dev \\
    openssh-server openssh-client \\
    gdb gdbserver \\
    && rm -rf /var/lib/apt/lists/* \\
    && mkdir -p /var/run/sshd \\
    && sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config \\
    && sed -i 's/#PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config

# Copy full VPIC tree (deck compiler needs source + kokkos headers)
COPY --from=builder /opt/vpic-kokkos /opt/vpic-kokkos
{nvcc_copy}
{nvcc_env}ENV PATH=/opt/vpic-kokkos/build/bin:${{PATH}}

CMD ["/bin/bash"]
"""

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        """
        Configure VPIC.

        Calls super()._configure() which updates self.config and (when
        deploy_mode == 'container') triggers build_phase / build_deploy_phase.

        In default mode, also creates the run directory on all nodes.
        """
        super()._configure(**kwargs)

        if self.config.get('deploy_mode') == 'default':
            Mkdir(self.config['run_dir'],
                  PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """
        Launch VPIC.

        Branches on deploy_mode: compiles and runs inside the container for
        container mode, or uses system-installed vpic binary for default mode.
        """
        # Use shared_dir for container mode so compiled binary is visible on all nodes
        if self.config.get('deploy_mode') == 'container':
            run_dir = f'{self.shared_dir}/vpic_run'
        else:
            run_dir = self.config['run_dir']

        if self.config.get('deploy_mode') == 'container':
            pass  # run_dir created by the compile command's bash -c

            if self.config.get('deck'):
                deck_file = self.config['deck']
            else:
                sample = self.config.get('sample_deck', 'harris')
                deck_file = f'/opt/vpic-kokkos/sample/{sample}'

            deck_name = os.path.basename(deck_file).replace('.cxx', '')
            nprocs = self.config.get('nprocs', 4)

            # Step 1: Compile deck inside container (single node)
            compile_cmd = (
                f'bash -c "mkdir -p {run_dir} && cp {deck_file} {run_dir}/{deck_name}.cxx '
                f'&& cd {run_dir} && /opt/vpic-kokkos/build/bin/vpic {deck_name}.cxx"'
            )
            Exec(compile_cmd, LocalExecInfo(
                container=self._container_engine,
                container_image=self.deploy_image_name,
                hostfile=self.hostfile,
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
                env=self.mod_env,
            )).run()

            # Step 2: Run compiled binary via MPI across all nodes
            Exec(f'{run_dir}/{deck_name}.Linux', MpiExecInfo(
                nprocs=nprocs,
                ppn=self.config.get('ppn', 1),
                hostfile=self.hostfile,
                port=self.ssh_port,
                container=self._container_engine,
                container_image=self.deploy_image_name,
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
                env=self.mod_env,
                cwd=run_dir,
            )).run()
        else:
            if self.config.get('deck'):
                deck_file = self.config['deck']
            else:
                sample = self.config.get('sample_deck', 'harris')
                deck_file = f'/opt/vpic-kokkos/sample/{sample}/{sample}.cxx'

            # Step 1: Compile the deck
            deck_name = os.path.basename(deck_file).replace('.cxx', '')
            Exec(
                f'cp {deck_file} {run_dir}/ && cd {run_dir} && vpic {deck_name}.cxx',
                MpiExecInfo(nprocs=1, hostfile=self.hostfile, env=self.mod_env,
                            cwd=run_dir)
            ).run()

            # Step 2: Run compiled binary
            binary = f'{run_dir}/{deck_name}.Linux'
            Exec(
                binary,
                MpiExecInfo(nprocs=self.config['nprocs'],
                            ppn=self.config['ppn'],
                            hostfile=self.hostfile,
                            env=self.mod_env,
                            cwd=run_dir)
            ).run()

    def stop(self):
        """Stop VPIC (no-op — VPIC runs to completion)."""
        pass

    def clean(self):
        """Remove VPIC run directory."""
        Rm(self.config['run_dir'] + '*',
           PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()
