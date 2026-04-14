"""
Container-based Gray-Scott reaction-diffusion simulation.
Builds the standalone CUDA+MPI+HDF5 version from awesome-scienctific-applications.
"""
from jarvis_cd.core.container_pkg import ContainerApplication
from jarvis_cd.shell import Exec, MpiExecInfo
from jarvis_cd.shell.process import Mkdir


class GrayScottContainer(ContainerApplication):
    """
    Container-based Gray-Scott using the CUDA+MPI+HDF5 standalone version.

    Build phase: Clones source and builds with CMake+CUDA.
    Deploy phase: Copies binary to a leaner runtime image.

    Based on: awesome-scienctific-applications/grayscott/Dockerfile
    """

    def _build_phase(self) -> str:
        cuda_arch = self.config.get('cuda_arch', 80)
        base = self.config.get('base_image', 'sci-hpc-base')
        return f"""FROM {base}

ARG CUDA_ARCH={cuda_arch}

# Download Gray-Scott source from awesome-scienctific-applications
# Cached until repo URL changes
RUN git clone --depth 1 \\
    https://github.com/grc-iit/awesome-scienctific-applications.git \\
    /tmp/awesome-sci

WORKDIR /opt/gray_scott
RUN cp /tmp/awesome-sci/grayscott/CMakeLists.txt . && \\
    cp /tmp/awesome-sci/grayscott/gray_scott.cu . && \\
    rm -rf /tmp/awesome-sci

# Build (expensive — cached when source and CMakeLists unchanged)
RUN cmake -S . -B build \\
        -DCMAKE_BUILD_TYPE=Release \\
        -DCMAKE_CUDA_ARCHITECTURES=${{CUDA_ARCH}} \\
        -DHDF5_ROOT=/opt/hdf5 \\
    && cmake --build build -j$(nproc)

ENV PATH=/opt/gray_scott/build:${{PATH}}
"""

    def _build_deploy_phase(self) -> str:
        base = self.config.get('base_image', 'sci-hpc-base')
        return f"""FROM {self.build_image_name} AS builder
FROM {base}

# Copy compiled binary from build container
COPY --from=builder /opt/gray_scott/build/gray_scott /usr/bin/gray_scott

# HDF5 runtime libraries are already in sci-hpc-base
ENV PATH=/usr/bin:${{PATH}}

CMD ["/bin/bash"]
"""

    def augment_container(self) -> str:
        cuda_arch = self.config.get('cuda_arch', 80)
        return f"""
# Build Gray-Scott CUDA+HDF5 simulation
RUN git clone --depth 1 \\
    https://github.com/grc-iit/awesome-scienctific-applications.git /tmp/awesome-sci && \\
    mkdir -p /opt/gray_scott && \\
    cp /tmp/awesome-sci/grayscott/CMakeLists.txt /opt/gray_scott/ && \\
    cp /tmp/awesome-sci/grayscott/gray_scott.cu /opt/gray_scott/ && \\
    rm -rf /tmp/awesome-sci && \\
    cmake -S /opt/gray_scott -B /opt/gray_scott/build \\
        -DCMAKE_BUILD_TYPE=Release \\
        -DCMAKE_CUDA_ARCHITECTURES={cuda_arch} \\
        -DHDF5_ROOT=/opt/hdf5 \\
    && cmake --build /opt/gray_scott/build -j$(nproc) && \\
    cp /opt/gray_scott/build/gray_scott /usr/bin/gray_scott

ENV PATH=/usr/bin:${{PATH}}
"""

    def start(self):
        """Run the Gray-Scott simulation."""
        outdir = self.config.get('outdir', '/tmp/gray_scott_out')
        Mkdir(outdir).run()
        cmd = [
            '/usr/bin/gray_scott',
            f'--width {self.config["width"]}',
            f'--height {self.config["height"]}',
            f'--steps {self.config["steps"]}',
            f'--out-every {self.config["out_every"]}',
            f'--outdir {outdir}',
            f'--F {self.config["F"]}',
            f'--k {self.config["k"]}',
            f'--Du {self.config["Du"]}',
            f'--Dv {self.config["Dv"]}',
        ]
        Exec(' '.join(cmd),
             MpiExecInfo(nprocs=self.config['nprocs'],
                         ppn=self.config['ppn'],
                         hostfile=self.hostfile,
                         env=self.mod_env)).run()

    def clean(self):
        from jarvis_cd.shell.process import Rm
        outdir = self.config.get('outdir', '')
        if outdir:
            Rm(outdir + '*').run()
