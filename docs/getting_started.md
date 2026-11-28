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

### Site setup with uv and a git package repository

A site that runs JARVIS with its own package repository (for example
`clio_jarvis`) instead of the builtin one needs three steps. jarvis-cd is not
published on PyPI (the PyPI package named `jarvis` is unrelated), so install
it from git as a uv tool:

```bash
uv tool install git+https://github.com/grc-iit/jarvis-cd@relay_ready
jarvis repo rm builtin
jarvis repo add https://github.com/iowarp/clio-jarvis
```

- `uv tool install` puts the `jarvis` command on `PATH` (in uv's tool bin
  directory; run `uv tool update-shell` once if it is not on `PATH`).
- `jarvis repo rm builtin` records `no_builtin: true` in
  `<JARVIS_ROOT>/jarvis_config.yaml` (initializing the root with the
  `jarvis init` default directories if it was not initialized yet).
- `jarvis repo add <git url>` clones the repository into
  `<JARVIS_ROOT>/repos/<name>`, where `<name>` is the URL basename without
  `.git` with `-` and `.` replaced by `_` (`clio-jarvis` becomes
  `clio_jarvis`, which must be the package directory inside it). It checks out
  `--ref` (a branch, tag or commit; default: the remote's default branch),
  checks the `<name>/<name>/<pkg>/pkg.py` layout, and registers the clone.
  The URL and ref are recorded under `sources:` in `<JARVIS_ROOT>/repos.yaml`.

Later, refresh the clone or move it to another ref (`clio-jarvis` is
accepted too):

```bash
jarvis repo update clio_jarvis              # fetch, then the recorded ref
jarvis repo update clio_jarvis --ref v1.2   # move to (and record) a new ref
```

See [Repository commands](package_dev_guide.md#adding-repositories) for the
full command list.

### Leaving the builtin package repository out

By default JARVIS includes the `builtin` package repository it ships with.
A deployment that must not use those packages (for example an automated
relay that brings its own repositories) can leave builtin out completely:

```bash
jarvis init ~/.jarvis/config ~/.jarvis/private ~/.jarvis/shared +no_builtin
# or, on a root that is already set up:
jarvis repo rm builtin
# or, for every root a process touches:
export JARVIS_NO_BUILTIN=1
```

| Switch | Where | Effect |
|---|---|---|
| `JARVIS_NO_BUILTIN=1` (`true`/`yes`/`on`; `0`/`false`/`no`/`off` forces builtin back on) | environment | Wins over the config key for this process. |
| `no_builtin: true` | `<JARVIS_ROOT>/jarvis_config.yaml` | Recorded by `jarvis init +no_builtin`, by `jarvis repo rm builtin`, or by `jarvis init` while `JARVIS_NO_BUILTIN` is truthy. A later `jarvis init` keeps the root's setting unless `+no_builtin`/`-no_builtin` is given. `jarvis repo add <path to a builtin repository>` clears it. |

With builtin left out:

- `jarvis init` registers no repositories, and strips `builtin` entries from
  an existing `repos.yaml` it preserves.
- The repository list (`jarvis repo list`, package search and resolution)
  ignores every repository named `builtin`. `jarvis repo add` of a
  repository named `builtin` brings builtin back (it clears `no_builtin`),
  except while `JARVIS_NO_BUILTIN` is truthy, where it is refused.
- No builtin copy is located: not the distribution's `builtin`, the legacy
  `<JARVIS_ROOT>/builtin`, nor a `builtin` package in site-packages.
  `builtin.<pkg>` package types fail with an error naming the switch.
- `jarvis rg builtins` lists no profiles, and `jarvis rg load-builtin` finds
  none, because those profiles live in the builtin repository.

The default (neither switch set) is unchanged.

## Your First Pipeline

The only prerequisite is a container engine (Docker, Podman, or Apptainer). Jarvis builds all scientific software from source inside the container.

Create a file called `my_ior_test.yaml`:

```yaml
name: my_ior_test
base_deploy_mode: container

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
1. **Build** — Start a build container from `ubuntu:24.04`, compile IOR and Darshan inside it
2. **Deploy** — Create a lean runtime image with only the compiled binaries, MPI, and SSH
3. **Start** — Launch the deploy container with SSH and MPI configured
4. **Run** — Execute `ior` inside the container with 2 MPI processes
5. **Print** — Report write and read throughput results
6. **Stop** — Clean up the container

The first run takes a few minutes to compile. Subsequent runs skip the build entirely — the deploy image is cached locally. Set `container_cache: false` on a package to force a rebuild.

## Choosing a Container Engine

Jarvis supports three container engines. Set `container_engine` in the pipeline YAML to choose one.

### Docker

The most common option for workstations and cloud environments.

```yaml
name: ior_docker_test
base_deploy_mode: container

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
base_deploy_mode: container

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
base_deploy_mode: container

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
base_deploy_mode: container

# Container settings (when base_deploy_mode: container)
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
| `base_deploy_mode` | `container` (Docker/Podman/Apptainer), `spack`, or omit for bare-metal |
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
base_deploy_mode: container

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
2. **Build** — Check if deploy image exists (skip if cached); otherwise start a build container, run each package's `build.sh`, commit, build deploy image. For spack: run `spack install` instead.
3. **Configure** — Set up each package (create config files, directories)
4. **Start** — Launch each package in order (for containers: `docker compose up`, then `docker exec`)
5. **Stop** — Stop each package in reverse order (for containers: `docker compose down`)

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
