# Jarvis-CD

Jarvis-CD is a unified platform for deploying various applications, including storage systems and benchmarks. Many applications have complex configuration spaces and are difficult to deploy across different machines.

Jarvis is built around **pipeline tests**: declarative YAML experiments that deploy a pipeline of packages and sweep their parameters in a grid, collecting results automatically.

## Installation

```bash
cd /path/to/jarvis-cd
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
```

## Configuration (Build your Jarvis setup)

```bash
jarvis init [CONFIG_DIR] [PRIVATE_DIR] [SHARED_DIR]
```
- CONFIG_DIR: Stores Jarvis metadata for pkgs/pipelines (any path you can access)
- PRIVATE_DIR: Per-machine local data (e.g., OrangeFS state)
- SHARED_DIR: Shared across machines with the same view of data

On a personal machine, these can point to the same directory.

## Pipeline Tests

A pipeline test is a YAML file with a `config:` section (the pipeline of
packages to deploy) plus a parameter sweep. Run one with:

```bash
jarvis ppl run yaml <path/to/test.yaml>
```

The anatomy of a single pipeline test:

- **`config:`** — the pipeline definition: a `name` and a list of `pkgs` to
  deploy, each with its default parameters.
- **`vars:`** — parameters to sweep, keyed by `pkg_name.param`, each mapping
  to a list of values.
- **`loop:`** — how the variables combine. Variables in the same list vary
  together (zipped); separate lists form a cartesian product.
- **`repeat:`** — how many times to run each combination.
- **`output:`** — directory for results. Each run appends a row to
  `results.csv` (resumable if interrupted), and a full `results.yaml` is
  written at the end.

To run several experiments back-to-back from one file, list them under
**`experiments:`** — each entry is a full pipeline test as above. One
`jarvis ppl run yaml` command runs them all in order.

### Example

[`builtin/pipelines/examples/storage_sweep_test.yaml`](builtin/pipelines/examples/storage_sweep_test.yaml)
is a suite that runs two experiments with a single command:

1. **IOR sweep** over I/O size (transfer size) × number of processes,
   writing under `$HOME/ior_test`. The largest run generates 256 MiB.
2. **Redis sweep** over request value size × number of client threads,
   driving `redis-benchmark` against a Redis server. The largest run moves
   ~400 MB.

Both run in **Docker containers** (`base_deploy_mode: container`), so no
host install of IOR or Redis is needed — Jarvis builds the images. Both
stay well under 1 GB of I/O, so the suite is cheap to run as a demo.
Requirements: Docker installed and running.

```bash
jarvis ppl run yaml builtin/pipelines/examples/storage_sweep_test.yaml
```

```yaml
name: storage_sweeps

experiments:
  # Experiment 1: IOR sweep over I/O size x number of processes
  - config:
      name: ior_sweep
      base_deploy_mode: container
      container_engine: docker
      container_base: ubuntu:24.04
      container_binds:            # mount the output dir into the container
        - ${HOME}/ior_test
      pkgs:
        - pkg_type: builtin.ior
          pkg_name: ior
          api: posix
          block: 64m
          write: true
          out: ${HOME}/ior_test/data.bin
          log: ${HOME}/ior_test/ior.log
    vars:
      ior.xfer: ["256k", "1m", "4m"]   # I/O size
      ior.nprocs: [1, 2, 4]            # number of processes
    loop:
      - [ior.xfer]
      - [ior.nprocs]
    output: ${HOME}/ior_test/results

  # Experiment 2: Redis sweep over request size x number of threads
  - config:
      name: redis_sweep
      base_deploy_mode: container
      container_engine: docker
      container_base: ubuntu:24.04
      pkgs:
        - pkg_type: builtin.redis
          pkg_name: redis
          port: 6379
          sleep: 2
        - pkg_type: builtin.redis-benchmark
          pkg_name: redis_bench
          port: 6379
          count: 100000
    vars:
      redis_bench.req_size: [64, 1024, 4096]   # I/O size (bytes)
      redis_bench.nthreads: [1, 2, 4]          # number of threads
    loop:
      - [redis_bench.req_size]
      - [redis_bench.nthreads]
    output: ${HOME}/redis_test/results
```

To run on bare metal instead, drop the three `*container*` lines from an
experiment's `config:`; that experiment then uses host-installed binaries
(`ior`, `redis-server`, `redis-benchmark`).

### Results

Each experiment writes a `results.csv` under its `output:` directory with
one row per (combination, repeat). Columns include the swept variable
values, the runtime, and per-package stats: IOR contributes write/read
bandwidth (`ior.write_max_mibs`, …) and redis-benchmark contributes
throughput (`redis_bench.write_rps`, `redis_bench.read_rps`). Re-running a
test resumes from the last completed row.

## License

BSD-3-Clause License - see [LICENSE](LICENSE) file for details.

**Copyright (c) 2024, Gnosis Research Center, Illinois Institute of Technology**
