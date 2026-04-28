"""
Nyx — AMReX-based cosmological simulation (HydroTests).
Adaptive mesh, massively parallel simulation code from AMReX-Astro.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm


class Nyx(Application):
    """
    Merged Nyx class supporting both default (bare-metal) and container deployment.

    Set deploy_mode='container' to build and run Nyx inside a Docker/Podman/Apptainer
    container.  Set deploy_mode='default' to use a system-installed nyx_HydroTests
    binary via MPI.

    gpu_backend controls the AMReX GPU backend:
      'cuda' — NVIDIA CUDA (Nyx_GPU_BACKEND=CUDA)
      'sycl' — Intel GPU via SYCL/Level Zero (Nyx_GPU_BACKEND=SYCL, icpx)
      'none' — CPU only (Nyx_GPU_BACKEND=NONE)
    The legacy use_gpu=True flag is equivalent to gpu_backend='cuda'.
    """

    def _init(self):
        pass

    def _configure_menu(self):
        return [
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
                'name': 'max_step',
                'msg': 'Number of coarse time steps',
                'type': int,
                'default': 100,
            },
            {
                'name': 'n_cell',
                'msg': 'Base grid cells as "nx ny nz"',
                'type': str,
                'default': '128 128 128',
            },
            {
                'name': 'max_level',
                'msg': 'Maximum AMR refinement level',
                'type': int,
                'default': 0,
            },
            {
                'name': 'out',
                'msg': 'Output directory for plot files (default: <shared_dir>/nyx_out, which is on Lustre and visible to all nodes; set explicitly to /tmp/... only for single-node node-local scratch)',
                'type': str,
                'default': None,
            },
            {
                'name': 'plot_int',
                'msg': 'Plot file interval (-1 to disable)',
                'type': int,
                'default': 10,
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
            {
                'name': 'use_gpu',
                'msg': 'Build with CUDA/GPU backend (legacy; prefer gpu_backend)',
                'type': bool,
                'default': False,
            },
            {
                'name': 'gpu_backend',
                'msg': 'GPU backend: cuda, sycl, or none (overrides use_gpu when set). Leave unset to fall back to use_gpu.',
                'type': str,
                'default': None,
            },
            {
                'name': 'do_hydro',
                'msg': 'Run hydro update each step (1=normal, 0=skip hydro for I/O-benchmark mode where simulation cost is dwarfed by plotfile writes)',
                'type': int,
                'default': 1,
            },
            {
                'name': 'check_int',
                'msg': 'Checkpoint write interval in steps. <=0 disables. Checkpoints store full state in double-precision (~2x plotfile size); set check_int=1 to amplify I/O for I/O-heavy studies',
                'type': int,
                'default': -1,
            },
            {
                'name': 'derive_plot_vars',
                'msg': 'Space-separated derived plot variables (e.g. "pressure x_velocity y_velocity z_velocity magvel divv magvort"). Each adds ~4 bytes/cell to plotfiles. Empty=use inputs file default.',
                'type': str,
                'default': None,
            },
            {
                'name': 'small_plot_int',
                'msg': 'Small plotfile interval in steps. <=0 disables. When >0, Nyx writes a SECOND plotfile stream per step (full state vars) to amplify I/O without changing compute.',
                'type': int,
                'default': -1,
            },
        ]

    # ------------------------------------------------------------------
    # Container Dockerfile generators
    # ------------------------------------------------------------------

    def _resolve_gpu_backend(self):
        """Return the canonical gpu_backend string: 'cuda', 'sycl', or 'none'."""
        explicit = self.config.get('gpu_backend')
        if explicit:
            return explicit
        use_gpu = self.config.get('use_gpu', False) or 'sci-hpc' in self.config.get('base_image', '')
        return 'cuda' if use_gpu else 'none'

    def _build_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        base = self.config.get('base_image', 'sci-hpc-base')
        gpu_backend = self._resolve_gpu_backend()
        cuda_arch = self.config.get('cuda_arch', 80)

        if gpu_backend == 'cuda':
            gpu_flags = (
                f'-DNyx_GPU_BACKEND=CUDA '
                f'"-DAMReX_CUDA_ARCH={cuda_arch}" '
                f'-DCMAKE_CUDA_HOST_COMPILER="$(which g++)" '
            )
            hdf5_flags = '-DAMReX_HDF5=YES -DHDF5_ROOT=/usr/local '
            suffix = f'cuda-{cuda_arch}'
            content = self._read_build_script('build.sh', {
                'BASE_IMAGE': base,
                'HDF5_FLAGS': hdf5_flags,
                'GPU_FLAGS': gpu_flags,
            })
        elif gpu_backend == 'sycl':
            content = self._read_build_script('sycl/build.sh', {
                'BASE_IMAGE': base,
            })
            suffix = 'sycl-jit'
        else:
            gpu_flags = '-DNyx_GPU_BACKEND=NONE '
            hdf5_flags = ''
            suffix = 'cpu'
            content = self._read_build_script('build.sh', {
                'BASE_IMAGE': base,
                'HDF5_FLAGS': hdf5_flags,
                'GPU_FLAGS': gpu_flags,
            })

        return content, suffix

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        gpu_backend = self._resolve_gpu_backend()
        suffix = getattr(self, '_build_suffix', '')

        if gpu_backend == 'cuda':
            deploy_base = 'nvidia/cuda:12.6.0-runtime-ubuntu24.04'
            content = self._read_dockerfile('Dockerfile.deploy', {
                'BUILD_IMAGE': self.build_image_name(),
                'DEPLOY_BASE': deploy_base,
            })
        elif gpu_backend == 'sycl':
            # Full hpckit image at runtime: Level Zero runtime + icpx needed
            # to JIT-compile SYCL kernels on first launch.
            deploy_base = 'intel/oneapi-hpckit:2025.0.0-0-devel-ubuntu24.04'
            content = self._read_dockerfile('sycl/Dockerfile.deploy', {
                'BUILD_IMAGE': self.build_image_name(),
                'DEPLOY_BASE': deploy_base,
            })
        else:
            deploy_base = 'ubuntu:24.04'
            content = self._read_dockerfile('Dockerfile.deploy', {
                'BUILD_IMAGE': self.build_image_name(),
                'DEPLOY_BASE': deploy_base,
            })

        return content, suffix

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _gpu_passthrough(self):
        """Return the gpu value for MpiExecInfo: 'intel', True (nvidia), or False."""
        backend = self._resolve_gpu_backend()
        if backend == 'sycl':
            return 'intel'
        if backend == 'cuda':
            return True
        return False

    def _configure(self, **kwargs):
        """
        Configure Nyx.

        Calls super()._configure() which updates self.config and (when
        deploy_mode == 'container') triggers build_phase / build_deploy_phase.

        In default mode, also creates the output directory on all nodes.
        """
        super()._configure(**kwargs)

        if self.config.get('deploy_mode') == 'default':
            Mkdir(self.config['out'],
                  PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """
        Launch Nyx.

        Branches on deploy_mode: uses MpiExecInfo with container engine for
        container mode, MpiExecInfo with hostfile for default mode.
        """
        if self.config.get('deploy_mode') == 'container':
            # Default plotfile dir: the pipeline's shared_dir (on Lustre,
            # auto-bound into the container by jarvis). /tmp inside an
            # apptainer instance is a per-node writable-tmpfs, so a
            # multi-node collective plotfile write fails on remote ranks
            # with "Couldn't open file" — their /tmp doesn't have the
            # directory rank 0 created. Shared storage avoids this.
            # Users can override via `out:` in the YAML.
            outdir = self.config.get('out') or f'{self.shared_dir}/nyx_out'
            Mkdir(outdir).run()

            nprocs = self.config.get('nprocs', 4)
            inputs_file = self.config.get(
                'inputs_file',
                '/opt/Nyx/Exec/HydroTests/inputs.regtest.sedov',
            )
            check_int = int(self.config.get('check_int', -1) or -1)
            cmd_parts = [
                '/opt/Nyx/build/Exec/HydroTests/nyx_HydroTests',
                inputs_file,
                f'max_step={self.config["max_step"]}',
                f'"amr.n_cell={self.config["n_cell"]}"',
                f'amr.max_level={self.config["max_level"]}',
                f'amr.plot_file={outdir}/plt',
                f'amr.plot_int={self.config["plot_int"]}',
                f'nyx.do_hydro={self.config.get("do_hydro", 1)}',
            ]
            if check_int > 0:
                cmd_parts.extend([
                    f'amr.check_int={check_int}',
                    f'amr.checkpoint_files_output=1',
                    f'amr.check_file={outdir}/chk',
                ])
            dpv = self.config.get('derive_plot_vars')
            if dpv:
                cmd_parts.append(f'"amr.derive_plot_vars={dpv}"')
            small_plot_int = int(self.config.get('small_plot_int', -1) or -1)
            if small_plot_int > 0:
                cmd_parts.extend([
                    f'amr.small_plot_int={small_plot_int}',
                    f'amr.small_plot_file={outdir}/smallplt',
                    f'"amr.small_plot_vars=density xmom ymom rho_E rho_e Temp"',
                ])
            inner = ' '.join(cmd_parts)
            Exec(inner, MpiExecInfo(
                nprocs=nprocs,
                ppn=self.config.get('ppn'),
                hostfile=self.hostfile,
                port=self.ssh_port,
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
                env=self.mod_env,
                gpu=self._gpu_passthrough(),
            )).run()
        else:
            inputs_file = self.config.get(
                'inputs_file',
                '/opt/Nyx/Exec/HydroTests/inputs.regtest.sedov',
            )
            check_int = int(self.config.get('check_int', -1) or -1)
            cmd = [
                'nyx_HydroTests',
                inputs_file,
                f'max_step={self.config["max_step"]}',
                f'"amr.n_cell={self.config["n_cell"]}"',
                f'amr.max_level={self.config["max_level"]}',
                f'amr.plot_file={self.config["out"]}/plt',
                f'amr.plot_int={self.config["plot_int"]}',
                f'nyx.do_hydro={self.config.get("do_hydro", 1)}',
            ]
            if check_int > 0:
                cmd.extend([
                    f'amr.check_int={check_int}',
                    f'amr.checkpoint_files_output=1',
                    f'amr.check_file={self.config["out"]}/chk',
                ])
            dpv = self.config.get('derive_plot_vars')
            if dpv:
                cmd.append(f'"amr.derive_plot_vars={dpv}"')
            small_plot_int = int(self.config.get('small_plot_int', -1) or -1)
            if small_plot_int > 0:
                cmd.extend([
                    f'amr.small_plot_int={small_plot_int}',
                    f'amr.small_plot_file={self.config["out"]}/smallplt',
                    f'"amr.small_plot_vars=density xmom ymom rho_E rho_e Temp"',
                ])
            Exec(' '.join(cmd),
                 MpiExecInfo(nprocs=self.config['nprocs'],
                             ppn=self.config['ppn'],
                             hostfile=self.hostfile,
                             env=self.mod_env)).run()

    def stop(self):
        """Stop Nyx (no-op — Nyx runs to completion)."""
        pass

    def clean(self):
        """Remove Nyx output directory."""
        Rm(self.config['out'] + '*',
           PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()
