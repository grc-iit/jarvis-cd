# Getting Started with Jarvis-CD

Jarvis-CD deploys and runs scientific applications through **pipeline configuration files**. A pipeline is a YAML file that declares what software to run and how to run it. Jarvis handles the rest: building containers, setting up environments, configuring MPI, and orchestrating execution.

## Installation

```bash
git clone https://github.com/grc-iit/jarvis-cd.git
cd jarvis-cd
pip install -e .
```

Initialize Jarvis (once per machine):

```bash
jarvis init ~/.jarvis/config ~/.jarvis/private ~/.jarvis/shared
```

On a personal machine all three directories can be the same. On a cluster, `shared` should be on a shared filesystem visible to all nodes.

## Your First Pipeline

The only prerequisite is a container engine (Docker, Podman, or Apptainer). Jarvis builds all scientific software from source inside the container.

Create a file called `my_ior_test.yaml`:

```yaml
name: my_ior_test
install_manager: container

container_engine: docker
container_base: ubuntu:24.04

pkgs:
  - pkg_type: builtin.ior
    pkg_name: ior_bench
    nprocs: 2
    ppn: 2
    block: 64m
    xfer: 1m
    api: posix
    out: /tmp/ior_test_file
    write: true
    read: true
```

Run it:

```bash
jarvis ppl run yaml my_ior_test.yaml
```

Jarvis will:
1. **Build** a container image with IOR and Darshan compiled from `ubuntu:24.04`
2. **Start** the container with SSH and MPI configured
3. **Run** `ior` inside the container with 2 MPI processes
4. **Print** write and read throughput results
5. **Stop** and clean up the container

The first run takes a few minutes to compile IOR inside the container. Subsequent runs reuse the cached image (`jarvis-build-ior-mpi`).

## Choosing a Container Engine

Jarvis supports three container engines. Set `container_engine` in the pipeline YAML to choose one.

### Docker

The most common option for workstations and cloud environments.

```yaml
name: ior_docker_test
install_manager: container

container_engine: docker
container_base: ubuntu:24.04

pkgs:
  - pkg_type: builtin.ior
    pkg_name: ior_bench
    nprocs: 2
    ppn: 2
    block: 64m
    xfer: 1m
    api: posix
    out: /tmp/ior_test_file
    write: true
    read: true
```

**Requirements:** Docker Engine installed, user in the `docker` group (or running as root).

### Podman

A rootless, daemonless alternative to Docker. Preferred on systems where users don't have root access.

```yaml
name: ior_podman_test
install_manager: container

container_engine: podman
container_base: docker.io/ubuntu:24.04

pkgs:
  - pkg_type: builtin.ior
    pkg_name: ior_bench
    nprocs: 2
    ppn: 2
    block: 64m
    xfer: 1m
    api: posix
    out: /tmp/ior_test_file
    write: true
    read: true
```

**Requirements:** Podman installed. Note the `docker.io/` prefix on the base image — Podman requires fully qualified image names.

**Differences from Docker:**
- No daemon process — containers run as child processes
- Rootless by default — no `docker` group needed
- Uses `podman build` and `podman run` instead of `docker build`/`docker run`
- Same Dockerfile format, same image layer caching

### Apptainer (Singularity)

The standard on HPC clusters where Docker is not available. Apptainer converts Docker images to `.sif` files that run without a daemon or root access.

```yaml
name: ior_apptainer_test
install_manager: container

container_engine: apptainer
container_base: ubuntu:24.04

pkgs:
  - pkg_type: builtin.ior
    pkg_name: ior_bench
    nprocs: 2
    ppn: 2
    block: 64m
    xfer: 1m
    api: posix
    out: /tmp/ior_test_file
    write: true
    read: true
```

**Requirements:** Apptainer installed, plus either Docker or Podman (used as an intermediate build step — Apptainer cannot build Dockerfiles directly).

**How it works:**
1. Jarvis builds the image using Docker or Podman (whichever is available)
2. Converts the image to a `.sif` file in the pipeline's shared directory
3. Runs the `.sif` via `apptainer exec`

This makes Apptainer pipelines portable to compute nodes that only have Apptainer, as long as the `.sif` is on a shared filesystem.

### Engine Comparison

| | Docker | Podman | Apptainer |
|---|---|---|---|
| **Root required** | Build: yes, Run: no (with group) | No | No |
| **Daemon** | Yes (`dockerd`) | No | No |
| **HPC clusters** | Rare | Sometimes | Standard |
| **Build tool** | `docker build` | `podman build` | Docker/Podman (intermediate) |
| **Run tool** | `docker run` | `podman run` | `apptainer exec` |
| **Image format** | OCI layers | OCI layers | `.sif` file |
| **GPU passthrough** | `--gpus all` | `--gpus all` | `--nv` |
| **Base image prefix** | Optional | `docker.io/` required | Optional |

## Pipeline YAML Format

Every pipeline YAML has this structure:

```yaml
# Pipeline name (required)
name: my_pipeline

# Install manager (required for containerized): 'container' or 'spack'
install_manager: container

# Container settings (when install_manager: container)
container_engine: docker          # docker, podman, or apptainer
container_base: ubuntu:24.04     # base image for the build phase

# Packages to run (required)
pkgs:
  - pkg_type: builtin.package_name
    pkg_name: my_instance
    # ... package-specific config ...

# Interceptors (optional): modify environment for packages
interceptors:
  - pkg_type: builtin.interceptor_name
    pkg_name: my_interceptor
```

### Key Fields

| Field | Description |
|---|---|
| `name` | Pipeline identifier |
| `install_manager` | `container` (Docker/Podman/Apptainer), `spack`, or omit for bare-metal |
| `container_engine` | `docker`, `podman`, or `apptainer` |
| `container_base` | Base image for the build phase (e.g., `ubuntu:24.04`, `sci-hpc-base`) |
| `pkgs` | List of packages to run |
| `interceptors` | Optional list of interceptors (profilers, tracers) |
| `hostfile` | Path to MPI hostfile for multi-node runs |
| `container_env` | Environment variables injected into containers |

## IOR Configuration Reference

| Parameter | Type | Default | Description |
|---|---|---|---|
| `nprocs` | int | 1 | Total MPI processes |
| `ppn` | int | 16 | Processes per node |
| `block` | str | `32m` | Data per process (e.g., `32m`, `1G`) |
| `xfer` | str | `1m` | Transfer size per I/O operation |
| `api` | str | `posix` | I/O API: `posix`, `mpiio`, or `hdf5` |
| `out` | str | `/tmp/ior.bin` | Output file path |
| `write` | bool | true | Perform write benchmark |
| `read` | bool | false | Perform read benchmark |
| `fpp` | bool | false | File-per-process mode |
| `reps` | int | 1 | Number of repetitions |
| `direct` | bool | false | Use O_DIRECT (bypass OS cache) |

## Multi-Node Deployment

For running across multiple nodes, add a hostfile and set `container_env` to configure MPI networking:

```yaml
name: ior_cluster_test
install_manager: container

container_engine: docker
container_base: ubuntu:24.04
hostfile: /path/to/hostfile.txt

container_env:
  OMPI_MCA_btl_tcp_if_include: eth0
  OMPI_MCA_oob_tcp_if_include: eth0

pkgs:
  - pkg_type: builtin.ior
    pkg_name: ior_bench
    nprocs: 4
    ppn: 1
    block: 1G
    xfer: 1m
    api: posix
    out: /tmp/ior_cluster_test
    write: true
    read: true
```

The hostfile is a plain text file with one hostname per line:

```text
node-01
node-02
node-03
node-04
```

Jarvis starts a container on each node via Docker Compose, then runs IOR across all of them via MPI over SSH. The `container_env` settings tell OpenMPI which network interface to use — this is important because containers see all host interfaces including Docker bridges that MPI cannot use.

## Pipeline Lifecycle

When you run `jarvis ppl run yaml pipeline.yaml`, Jarvis executes these phases:

1. **Load** — Parse the YAML, capture the environment
2. **Build** — Build containers or run `spack install`
3. **Configure** — Set up each package (create config files, directories)
4. **Start** — Launch each package in order
5. **Stop** — Stop each package in reverse order

You can also run phases individually:

```bash
# Load a pipeline (build containers, save config)
jarvis ppl load yaml my_pipeline.yaml

# Configure all packages
jarvis ppl configure

# Start/stop manually
jarvis ppl start
jarvis ppl stop

# Clean up generated data
jarvis ppl clean

# Destroy the pipeline entirely
jarvis ppl destroy my_pipeline
```

## Available Packages

Jarvis ships with containerized packages for many scientific applications:

| Package | Description | GPU |
|---|---|---|
| `builtin.ior` | I/O benchmark (POSIX, MPI-IO, HDF5) | |
| `builtin.lammps` | Molecular dynamics (Kokkos CUDA) | Yes |
| `builtin.gray_scott` | Reaction-diffusion simulation (CUDA + HDF5) | Yes |
| `builtin.warpx` | Particle-in-cell plasma simulation (AMReX CUDA) | Yes |
| `builtin.vpic` | Vector PIC plasma physics (Kokkos CUDA) | Yes |
| `builtin.nyx` | Cosmological simulation (AMReX CUDA + HDF5) | Yes |
| `builtin.ai_training` | PyTorch distributed training | Yes |
| `builtin.wrf_container` | Weather Research and Forecasting v4.6.0 | |
| `builtin.xcompact3d` | CFD with ADIOS2 I/O | |
| `builtin.gray_scott_paraview` | ParaView visualization with ADIOS2 | Yes |
| `builtin.montage` | Astronomical image mosaic | |
| `builtin.biobb_wf_md_setup` | Molecular dynamics (GROMACS/BioExcel) | |
| `builtin.deepdrivemd` | Adaptive molecular dynamics (OpenMM) | |
| `builtin.metagem` | Metagenomics (Snakemake) | |
| `builtin.rna_seq_star_deseq2` | RNA-seq differential expression | |
| `builtin.vpipe` | Viral sequence analysis | |
| `builtin.pyflextrkr` | Atmospheric feature tracking | |
| `builtin.redis` | Redis key-value store (Service) | |

## Next Steps

- [Pipeline Configuration Reference](pipelines.md) — Full YAML format, container settings, environment management, devcontainers
- [Package Development Guide](package_dev_guide.md) — How to create your own packages
- [Pipeline Tests](pipeline_tests.md) — Automated testing with grid search
- [Hostfile Configuration](hostfile.md) — Multi-node setup and pattern expansion
