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

Create a file called `my_ior_test.yaml`:

```yaml
name: my_ior_test

pkgs:
  - pkg_type: builtin.ior
    pkg_name: ior_bench
    nprocs: 1
    ppn: 1
    block: 32m
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

This runs the IOR I/O benchmark on your local machine using the system-installed `ior` binary. It writes 32 MB then reads it back, reporting throughput.

> **Prerequisite:** IOR must be installed and in your PATH. On Ubuntu: `sudo apt install ior`. If you don't have IOR installed, skip ahead to the [Container Deployment](#container-deployment) section which installs it automatically.

## Pipeline YAML Format

Every pipeline YAML has this structure:

```yaml
# Pipeline name (required)
name: my_pipeline

# Install manager (optional): 'container', 'spack', or omitted
install_manager: container

# Packages to run (required)
pkgs:
  - pkg_type: builtin.package_name    # Which package to use
    pkg_name: my_instance              # A name for this instance
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
| `install_manager` | How to install software: `container` (Docker/Podman), `spack`, or omit for bare-metal |
| `pkgs` | List of packages to run, each with `pkg_type` and config parameters |
| `interceptors` | Optional list of interceptors (profilers, I/O tracers, etc.) |
| `container_engine` | `docker` or `podman` (when `install_manager: container`) |
| `container_base` | Base Docker image for builds (when `install_manager: container`) |
| `hostfile` | Path to MPI hostfile for multi-node runs |

## Container Deployment

If you have Docker installed, Jarvis can build and run packages inside containers automatically. No need to install any scientific software on your host.

Create `ior_docker_test.yaml`:

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

Run it:

```bash
jarvis ppl run yaml ior_docker_test.yaml
```

Jarvis will:
1. Build a container image with IOR compiled from source
2. Start the container with SSH access
3. Run `ior` inside the container via MPI (2 processes)
4. Print throughput results
5. Stop and clean up the container

The first run takes a few minutes to build the image. Subsequent runs reuse the cached image.

## Spack Deployment

If you have [Spack](https://spack.io/) installed, Jarvis can install packages through it:

```yaml
name: ior_spack_test
install_manager: spack

pkgs:
  - pkg_type: builtin.ior
    pkg_name: ior_bench
    install: ior
    nprocs: 2
    ppn: 2
    block: 64m
    xfer: 1m
    api: posix
    out: /tmp/ior_test_file
    write: true
    read: true
```

The `install` key on each package specifies the Spack spec. Jarvis runs `spack install ior`, then `spack load ior` to put the binary in PATH, then runs the pipeline bare-metal.

## IOR Configuration Reference

IOR is a good starting point because it runs on any system. Here are the key parameters:

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

For running across multiple nodes, add a hostfile:

```yaml
name: ior_cluster_test
install_manager: container

container_engine: docker
container_base: ubuntu:24.04
hostfile: /path/to/hostfile.txt

container_env:
  OMPI_MCA_btl_tcp_if_include: eth0

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

Jarvis starts a container on each node via Docker Compose, then runs IOR across all of them via MPI over SSH.

## Pipeline Lifecycle

When you run `jarvis ppl run yaml pipeline.yaml`, Jarvis executes these phases:

1. **Load** — Parse the YAML, capture the environment
2. **Install** — Build containers (`install_manager: container`) or run spack install (`install_manager: spack`)
3. **Configure** — Run each package's configure method (create config files, set up directories)
4. **Start** — Launch each package in order
5. **Stop** — Stop each package in reverse order

You can also run phases individually:

```bash
# Load and configure only (useful for debugging)
jarvis ppl load yaml my_pipeline.yaml
jarvis ppl configure

# Start/stop manually
jarvis ppl start
jarvis ppl stop

# Clean up all generated data
jarvis ppl clean

# Destroy the pipeline entirely
jarvis ppl destroy my_pipeline
```

## Available Packages

Jarvis ships with packages for many scientific applications:

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

- [Pipeline Configuration Reference](pipelines.md) — Full YAML format, container settings, environment management
- [Package Development Guide](package_dev_guide.md) — How to create your own packages
- [Pipeline Tests](pipeline_tests.md) — Automated testing with grid search
- [Hostfile Configuration](hostfile.md) — Multi-node setup and pattern expansion
