# LAMMPS (jarvis package)

LAMMPS (Large-scale Atomic/Molecular Massively Parallel Simulator) is an
open-source classical molecular-dynamics code from Sandia National
Laboratories, distributed under the GPL v2. Upstream docs:
<https://docs.lammps.org/>.

This jarvis package builds LAMMPS inside an Apptainer/Docker/Podman
container and launches it via MPI. The same package supports multiple
GPU backends — select the one that matches your machine with
`gpu_backend` in the pipeline YAML.

| Backend | Typical machine | YAML `gpu_backend` | Kokkos flag |
|---|---|---|---|
| NVIDIA CUDA | Delta, Polaris, Perlmutter | `cuda` | `Kokkos_ENABLE_CUDA=ON` |
| Intel SYCL | Aurora (PVC) | `sycl` | `Kokkos_ENABLE_SYCL=ON` |
| CPU only | Ares, laptop, cloud | `none` | (none; serial/OpenMP) |

---

## Which guide do I read?

Ready-made pipelines and platform-specific walkthroughs live next to
their pipeline YAMLs:

- **Aurora (Intel PVC, SYCL)** — full step-by-step guide:
  [`builtin/pipelines/portability/aurora/LAMMPS.md`](../../pipelines/portability/aurora/LAMMPS.md)
  (pipelines: `aurora/lammps_single_node.yaml`, `aurora/lammps_multi_node.yaml`)
- **Delta (NVIDIA A100, CUDA)** — pipeline header has the steps:
  [`builtin/pipelines/portability/delta/lammps_single_node.yaml`](../../pipelines/portability/delta/lammps_single_node.yaml)
- **Ares / CPU** — pipeline header has the steps:
  [`builtin/pipelines/portability/ares/cpu/lammps_single_node.yaml`](../../pipelines/portability/ares/cpu/lammps_single_node.yaml)
- **Local CPU** (laptop / dev box):
  [`builtin/pipelines/portability/local/cpu/lammps_single_node.yaml`](../../pipelines/portability/local/cpu/lammps_single_node.yaml)

If you're writing a new pipeline from scratch, the common shape is:

```yaml
name: lammps_<machine>_<scale>
install_manager: container
container_engine: apptainer            # or docker/podman
container_base: <base image>
container_ssh_port: 2233
container_gpu: <true | intel | false>   # nvidia=true, intel=intel, cpu=false

hostfile: ~/hostfile_single.txt

pkgs:
  - pkg_type: builtin.lammps
    pkg_name: lammps_<machine>
    nprocs: <N × ppn>
    ppn: <ranks per node>
    gpu_backend: <cuda | sycl | none>
    num_gpus: 1                          # GPUs per rank (one per tile on Aurora, one per card on Delta)
    gpu_aware_mpi: false                 # flip to true only when your MPI stack is GPU-aware
    base_image: <base image>
    # EITHER point at a LAMMPS input deck inside the container ...
    script: /opt/lammps/bench/in.lj
    io_dump_interval: 0                  # 0 = use `script:` verbatim
    # ... OR auto-generate an LJ benchmark:
    # io_dump_interval: 100
    # io_lattice_size: 40                # 4 × N³ atoms (40 → 256k, 80 → 2M)
    # io_run_steps: 1000
```

---

## Configuration reference

| YAML key (under `pkgs:`) | Type | Default | Description |
|---|---|---|---|
| `nprocs` | int | 4 | Total MPI ranks across all nodes |
| `ppn` | int | 4 | Ranks per node (≤ 6 on Aurora, ≤ 4 on Delta A100) |
| `script` | str | `None` | Path to a LAMMPS input deck *inside* the container |
| `io_dump_interval` | int | 0 | If > 0, auto-generate an LJ input with dumps every N steps (overrides `script:`) |
| `io_lattice_size` | int | 80 | FCC lattice N; atom count = 4 × N³ (auto-gen only) |
| `io_run_steps` | int | 5000 | Total MD timesteps (auto-gen only) |
| `out` | str | *(shared_dir)* | Dump directory — leave unset for the safe default (Lustre) |
| `gpu_backend` | str | `None` | `cuda`, `sycl`, or `none`. Overrides `kokkos_gpu`. |
| `kokkos_gpu` | bool | `False` | Legacy flag: `True` is equivalent to `gpu_backend=cuda` |
| `cuda_arch` | int | 80 | CUDA arch code (80=A100, 90=H100). Used only with `gpu_backend=cuda` |
| `num_gpus` | int | 1 | GPUs exposed to each MPI rank |
| `gpu_aware_mpi` | bool | `False` | Pass GPU pointers straight to MPI (requires matching transport) |
| `base_image` | str | `sci-hpc-base` | Base Docker/Apptainer image |

---

## What's inside the container

Per backend:

- **CUDA build** (`sycl/` not used): `nvidia/cuda:*-devel` base, `nvcc`,
  Kokkos CUDA. See [`build.sh`](build.sh) + [`Dockerfile.deploy`](Dockerfile.deploy).
- **SYCL build**: `intel/oneapi-hpckit:2025.0` base, `icpx`, Kokkos SYCL
  (JIT spir64), Intel Level Zero runtime, OpenMPI 4, plus container-env
  tweaks needed to run under apptainer's rootless user namespace. See
  [`sycl/build.sh`](sycl/build.sh) — every non-obvious line has a
  comment explaining why.
- **CPU build**: `ubuntu:24.04` base, system `g++`, OpenMPI, no Kokkos
  GPU backend enabled.

All three share the same `pkg.py` — the only difference is which build
script/Dockerfile gets templated in (`_build_phase` / `_build_deploy_phase`
dispatch on `gpu_backend`).
