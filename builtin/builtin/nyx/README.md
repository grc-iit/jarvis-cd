# Nyx (jarvis package)

Nyx is an adaptive-mesh, massively-parallel cosmological hydrodynamics
simulation code built on [AMReX](https://github.com/AMReX-Codes/amrex).
Upstream: <https://github.com/AMReX-Astro/Nyx>,
<https://amrex-astro.github.io/Nyx/>.

This jarvis package builds Nyx inside an Apptainer/Docker/Podman
container and launches it via MPI. The same package supports multiple
GPU backends — select the one that matches your machine with
`gpu_backend` in the pipeline YAML.

| Backend | Typical machine | YAML `gpu_backend` | AMReX flag |
|---|---|---|---|
| NVIDIA CUDA | Delta, Polaris, Perlmutter | `cuda` | `Nyx_GPU_BACKEND=CUDA` |
| Intel SYCL | Aurora (PVC) | `sycl` | `Nyx_GPU_BACKEND=SYCL` |
| CPU only | Ares, laptop, cloud | `none` | `Nyx_GPU_BACKEND=NONE` |

---

## Which guide do I read?

Ready-made pipelines and platform-specific walkthroughs live next to
their pipeline YAMLs:

- **Aurora (Intel PVC, SYCL)** — full step-by-step guide:
  [`builtin/pipelines/portability/aurora/NYX.md`](../../pipelines/portability/aurora/NYX.md)
  (pipelines: `aurora/nyx_single_node.yaml`, `aurora/nyx_multi_node.yaml`)
- **Delta (NVIDIA A100, CUDA)** — pipeline header has the steps:
  [`builtin/pipelines/portability/delta/nyx_single_node.yaml`](../../pipelines/portability/delta/nyx_single_node.yaml)
- **Ares / CPU** — pipeline header has the steps:
  [`builtin/pipelines/portability/ares/cpu/nyx_single_node.yaml`](../../pipelines/portability/ares/cpu/nyx_single_node.yaml)
- **Local CPU** (laptop / dev box):
  [`builtin/pipelines/portability/local/cpu/nyx_single_node.yaml`](../../pipelines/portability/local/cpu/nyx_single_node.yaml)

If you're writing a new pipeline from scratch, the common shape is:

```yaml
name: nyx_<machine>_<scale>
install_manager: container
container_engine: apptainer            # or docker/podman
container_base: <base image>
container_ssh_port: 2233
container_gpu: <true | intel | false>   # nvidia=true, intel=intel, cpu=false

hostfile: ~/hostfile_single.txt

pkgs:
  - pkg_type: builtin.nyx
    pkg_name: nyx_<machine>
    nprocs: <N × ppn>
    ppn: <ranks per node>
    gpu_backend: <cuda | sycl | none>
    base_image: <base image>
    max_step: 100
    n_cell: "128 128 128"
    max_level: 0
    plot_int: 10
    # `out` omitted — pkg.py defaults to the pipeline shared_dir (on
    # Lustre), which is visible to all nodes for multi-node plotfile
    # writes. Set explicitly only if you want node-local scratch on
    # single-node runs.
```

---

## Configuration reference

| YAML key (under `pkgs:`) | Type | Default | Description |
|---|---|---|---|
| `nprocs` | int | 4 | Total MPI ranks across all nodes |
| `ppn` | int | 4 | Ranks per node (≤ 6 on Aurora, ≤ 4 on Delta A100) |
| `max_step` | int | 100 | Number of coarse timesteps |
| `n_cell` | str | `"128 128 128"` | Base grid cells `"nx ny nz"` |
| `max_level` | int | 0 | AMR refinement levels |
| `out` | str | *(shared_dir)* | Plotfile directory — leave unset for the safe default (Lustre) |
| `plot_int` | int | 10 | Steps between plotfiles (`-1` disables) |
| `gpu_backend` | str | `None` | `cuda`, `sycl`, or `none`. Overrides `use_gpu`. |
| `use_gpu` | bool | `False` | Legacy flag: `True` is equivalent to `gpu_backend=cuda` |
| `cuda_arch` | int | 80 | CUDA arch code (80=A100, 90=H100). Used only with `gpu_backend=cuda` |
| `base_image` | str | `sci-hpc-base` | Base Docker/Apptainer image |

---

## What's inside the container

Per backend:

- **CUDA build**: `nvidia/cuda:*-devel` base, `nvcc`, AMReX CUDA
  backend. See [`build.sh`](build.sh) + [`Dockerfile.deploy`](Dockerfile.deploy).
- **SYCL build**: `intel/oneapi-hpckit:2025.0` base, `icpx`, AMReX SYCL
  (JIT spir64), Intel Level Zero runtime, OpenMPI 4, plus container-env
  tweaks needed to run under apptainer's rootless user namespace. See
  [`sycl/build.sh`](sycl/build.sh) — every non-obvious line has a
  comment explaining why. The SYCL build also patches Nyx's
  `cmake_dependent_option` that force-disables MPI under SYCL.
- **CPU build**: `ubuntu:24.04` base, system `g++`, OpenMPI, no AMReX
  GPU backend enabled.

All three share the same `pkg.py` — the only difference is which build
script/Dockerfile gets templated in (`_build_phase` / `_build_deploy_phase`
dispatch on `gpu_backend`).
