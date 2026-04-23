"""
This module provides classes and methods to launch the LAMMPS application.
LAMMPS (Large-scale Atomic/Molecular Massively Parallel Simulator) is a
classical molecular-dynamics code from Sandia National Laboratories.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm


class Lammps(Application):
    """
    Merged LAMMPS class supporting both default (bare-metal) and container deployment.

    Set deploy_mode='container' to build and run LAMMPS inside a Docker/Podman/Apptainer
    container.  Set deploy_mode='default' to use a system-installed lmp
    binary via MPI.

    gpu_backend selects the Kokkos backend inside the container:
      'cuda' — NVIDIA CUDA (Kokkos_ENABLE_CUDA, Kokkos_ARCH_AMPERE<arch>)
      'sycl' — Intel GPU via SYCL/Level Zero JIT (Kokkos_ENABLE_SYCL, icpx)
      'none' — CPU only (Kokkos serial; no Kokkos_ENABLE_* GPU flags)
    The legacy kokkos_gpu=True flag is equivalent to gpu_backend='cuda'.
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
                'name': 'script',
                'msg': 'Path to LAMMPS input script (e.g., in.lj)',
                'type': str,
                'default': None,
            },
            {
                'name': 'lmp_bin',
                'msg': 'Path to LAMMPS binary (default: lmp in PATH)',
                'type': str,
                'default': 'lmp',
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
                'name': 'out',
                'msg': 'Output directory for dump/trajectory files (default: <shared_dir>/lammps_out, which is on Lustre and visible to all nodes; set explicitly to /tmp/... only for single-node node-local scratch)',
                'type': str,
                'default': None,
            },
            {
                'name': 'kokkos_gpu',
                'msg': 'Enable Kokkos GPU (CUDA) acceleration',
                'type': bool,
                'default': True,
            },
            {
                'name': 'num_gpus',
                'msg': 'Number of GPUs per node',
                'type': int,
                'default': 1,
            },
            {
                'name': 'io_dump_interval',
                'msg': 'If >0, auto-generate LJ input with dump every N steps',
                'type': int,
                'default': 0,
            },
            {
                'name': 'io_lattice_size',
                'msg': 'FCC lattice size per dim (4*N^3 atoms) for auto-generated IO input',
                'type': int,
                'default': 80,
            },
            {
                'name': 'io_run_steps',
                'msg': 'Total steps for auto-generated IO input',
                'type': int,
                'default': 5000,
            },
            {
                'name': 'gpu_backend',
                'msg': 'GPU backend: cuda, sycl, or none (overrides kokkos_gpu when set). Leave unset to fall back to kokkos_gpu.',
                'type': str,
                'default': None,
            },
            {
                'name': 'gpu_aware_mpi',
                'msg': 'Enable Kokkos GPU-aware MPI (LAMMPS `-pk kokkos gpu/aware on`). Requires an MPI stack built with the matching GPU transport (e.g., CUDA-aware OpenMPI/UCX for cuda, Level-Zero-aware libfabric for sycl). Leave False when running inside the default apptainer container whose Ubuntu-apt OpenMPI has no GPU transport — LAMMPS will stage GPU data through host memory instead.',
                'type': bool,
                'default': False,
            },
        ]

    # ------------------------------------------------------------------
    # Backend resolution
    # ------------------------------------------------------------------

    def _resolve_gpu_backend(self):
        """Return the canonical gpu_backend string: 'cuda', 'sycl', or 'none'."""
        explicit = self.config.get('gpu_backend')
        if explicit:
            return explicit
        use_gpu = self.config.get('kokkos_gpu', False)
        return 'cuda' if use_gpu else 'none'

    def _gpu_passthrough(self):
        """Return the gpu value for MpiExecInfo: 'intel', True (nvidia), or False."""
        backend = self._resolve_gpu_backend()
        if backend == 'sycl':
            return 'intel'
        if backend == 'cuda':
            return True
        return False

    # ------------------------------------------------------------------
    # Container Dockerfile generators
    # ------------------------------------------------------------------

    def _build_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        base = self.config.get('base_image', 'sci-hpc-base')
        gpu_backend = self._resolve_gpu_backend()
        cuda_arch = self.config.get('cuda_arch', 80)

        if gpu_backend == 'cuda':
            cmake_extra = (
                f'-DPKG_KOKKOS=ON '
                f'-DKokkos_ENABLE_CUDA=ON '
                f'"-DKokkos_ARCH_AMPERE{cuda_arch}=ON" '
            )
            suffix = f'kokkos-cuda-{cuda_arch}'
            content = self._read_build_script('build.sh', {
                'BASE_IMAGE': base,
                'CMAKE_EXTRA': cmake_extra,
            })
        elif gpu_backend == 'sycl':
            # Dedicated SYCL build script: installs Intel GPU runtime,
            # scrubs Intel MPI from CPATH, sets up /.singularity.d/env
            # and sshd SetEnv for Level Zero / Kokkos SYCL JIT on PVC.
            content = self._read_build_script('sycl/build.sh', {
                'BASE_IMAGE': base,
            })
            suffix = 'kokkos-sycl-jit'
        else:
            cmake_extra = ''
            suffix = 'cpu'
            content = self._read_build_script('build.sh', {
                'BASE_IMAGE': base,
                'CMAKE_EXTRA': cmake_extra,
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
            # Use the full hpckit image at runtime: Level Zero runtime
            # and libur_adapter_level_zero must be present for Kokkos
            # SYCL JIT to dispatch to PVC.
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

    def _configure(self, **kwargs):
        """
        Configure LAMMPS.

        Calls super()._configure() which updates self.config and (when
        deploy_mode == 'container') triggers build_phase / build_deploy_phase.

        In default mode, also creates the output directory on all nodes.
        """
        super()._configure(**kwargs)

        if self.config.get('deploy_mode') == 'default':
            if self.config['out']:
                Mkdir(self.config['out'],
                      PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """
        Launch LAMMPS.

        Branches on deploy_mode: uses MpiExecInfo with container engine for
        container mode, MpiExecInfo with hostfile for default mode.
        """
        if self.config.get('deploy_mode') == 'container':
            gpu_backend = self._resolve_gpu_backend()
            # Default output dir: pipeline shared_dir (Lustre-backed,
            # visible to all nodes). /tmp inside an apptainer instance
            # is per-node writable-tmpfs, so any dump/trajectory writes
            # on remote ranks fail "No such file or directory". Users
            # can override via `out:` in YAML.
            out_dir = self.config.get('out') or f'{self.shared_dir}/lammps_out'
            script_path = self.config.get('script')
            if self.config.get('io_dump_interval', 0) > 0:
                import os
                n = self.config.get('io_lattice_size', 20)
                steps = self.config.get('io_run_steps', 100)
                interval = self.config['io_dump_interval']
                script_path = os.path.join(
                    str(self.shared_dir), 'generated_io_input.lmp')
                with open(script_path, 'w') as f:
                    f.write(
                        f"shell mkdir -p {out_dir}\n"
                        f"units lj\natom_style atomic\n"
                        f"lattice fcc 0.8442\n"
                        f"region box block 0 {n} 0 {n} 0 {n}\n"
                        f"create_box 1 box\ncreate_atoms 1 box\n"
                        f"mass 1 1.0\n"
                        f"velocity all create 1.44 87287 loop geom\n"
                        f"pair_style lj/cut 2.5\npair_coeff 1 1 1.0 1.0 2.5\n"
                        f"neighbor 0.3 bin\n"
                        f"neigh_modify every 10 delay 0 check no\n"
                        f"fix 1 all nve\n"
                        f"dump d1 all custom {interval} "
                        f"{out_dir}/dump.*.lammpstrj "
                        f"id type x y z vx vy vz\n"
                        f"dump_modify d1 sort id\n"
                        f"thermo {interval}\n"
                        f"timestep 0.005\nrun {steps}\n"
                    )
            cmd = ['/usr/local/bin/lmp']
            if script_path:
                cmd.append(f"-in {script_path}")
            if gpu_backend in ('cuda', 'sycl'):
                # Kokkos runtime flags:
                #   -k on g N      : enable Kokkos on N GPUs per rank
                #   -sf kk         : prefer kk-suffixed (Kokkos) pair/fix
                #                    variants over plain CPU ones
                #   -pk kokkos gpu/aware on|off :
                #        controls whether LAMMPS passes GPU-resident
                #        buffers directly to MPI (on) or stages them
                #        through host memory first (off). "on" requires
                #        the MPI stack to be GPU-aware for the matching
                #        backend — CUDA-aware OpenMPI/UCX for CUDA,
                #        Level-Zero-aware libfabric for SYCL. The
                #        container's apt-OpenMPI has neither, so the
                #        default is `off` (safe, stages via host
                #        memory); flip gpu_aware_mpi: true in YAML on
                #        systems where the MPI you link against knows
                #        how to handle GPU pointers.
                n_gpus = self.config.get('num_gpus', 1)
                aware = 'on' if self.config.get('gpu_aware_mpi', False) else 'off'
                cmd += [f'-k on g {n_gpus}', '-sf kk',
                        f'-pk kokkos gpu/aware {aware}']

            Exec(' '.join(cmd), MpiExecInfo(
                nprocs=self.config['nprocs'],
                ppn=self.config['ppn'],
                hostfile=self.hostfile,
                port=self.ssh_port,
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
                gpu=self._gpu_passthrough(),
                env=self.mod_env,
            )).run()
        else:
            cmd = [self.config['lmp_bin']]
            if self.config['script']:
                cmd.append(f"-in {self.config['script']}")
            if self.config.get('kokkos_gpu'):
                n_gpus = self.config.get('num_gpus', 1)
                cmd += [f'-k on g {n_gpus}', '-sf kk', '-pk kokkos cuda/aware on']

            Exec(' '.join(cmd),
                 MpiExecInfo(nprocs=self.config['nprocs'],
                             ppn=self.config['ppn'],
                             hostfile=self.hostfile,
                             env=self.mod_env,
                             cwd=self.config.get('out'))).run()

    def stop(self):
        """Stop LAMMPS (no-op — LAMMPS runs to completion)."""
        pass

    def clean(self):
        """Remove LAMMPS output directory."""
        if self.config['out']:
            Rm(self.config['out'] + '*',
               PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()
