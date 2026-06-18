# Pipeline Tests

Pipeline tests are used to run experiment sets using a grid search. They allow you to systematically explore parameter spaces by varying package configurations across multiple runs.

## Table of Contents

1. [Overview](#overview)
2. [Multiple Pipelines in a Single YAML File](#multiple-pipelines-in-a-single-yaml-file)
3. [Multiple Experiments (Suite)](#multiple-experiments-suite)
4. [YAML Format](#yaml-format)
5. [Installers](#installers)
6. [Example Files](#example-files)
7. [Variables and Loop](#variables-and-loop)
   - [Scheduler Variables](#scheduler-variables)
8. [Launcher Overrides](#launcher-overrides)
9. [Output and Statistics](#output-and-statistics)
10. [CLI Commands](#cli-commands)
11. [Resume and Progress](#resume-and-progress)
12. [Custom Statistics](#custom-statistics)
13. [Scheduling](#scheduling)
14. [Combined Example: SLURM + Spack + Multiple Experiments](#combined-example-slurm--spack--multiple-experiments)

## Overview

A pipeline test consists of:
- A **config** section containing the base pipeline definition
- A **vars** section defining variables to sweep
- A **loop** section defining how variables are iterated
- A **repeat** count for running each configuration multiple times
- An **output** directory for storing results

## Multiple Pipelines in a Single YAML File

A pipeline test **is** the mechanism for expressing many pipelines in
one file. A single test YAML contains one `config:` skeleton plus a
`vars:`/`loop:` grid; loading it expands that grid into one distinct
pipeline per `(combination × repeat)`.

Each generated pipeline is materialized independently at run time:

- It is renamed `"{test_name}_run{N}"` (`N` is the global run index,
  starting at 1), so every iteration is a separate Jarvis pipeline
  with its own name and config/shared/private directories — they do
  not collide.
- Its config is `config:` with that combination's variable values
  patched in (`pkg_name.var_name` onto the matching package;
  `scheduler.var_name` onto the scheduler block — see
  [Scheduler Variables](#scheduler-variables)).
- It is written to a temporary YAML, loaded as an ordinary pipeline,
  then either run in-process (`start()` → `stop()`) or, if it has a
  scheduler block, submitted as its own batch job and waited on.

So a 5×4 grid with `repeat: 3` is "60 pipelines in one YAML file":

```yaml
config:
  name: ior_sweep            # pipelines: ior_sweep_run1 .. ior_sweep_run60
  pkgs:
    - pkg_type: builtin.ior
      pkg_name: ior
vars:
  ior.nprocs: [1, 2, 4, 8, 16]
  ior.block:  [512M, 1G, 2G, 4G]
loop:
  - [ior.nprocs]
  - [ior.block]
repeat: 3
output: "${HOME}/ior_results"
```

Detection is automatic (see [Auto-Detection](#auto-detection)): a file
with a `config:` section plus any of `vars`/`loop`/`repeat`/`output`
is a multi-pipeline test; a file with top-level `name`/`pkgs` is a
single regular pipeline. The same `jarvis ppl load|run|submit yaml
<file>` commands work for both.

## Multiple Experiments (Suite)

The `vars:`/`loop:` grid above expands **one** `config:` skeleton into
many runs — every run is the *same* pipeline with different parameter
values. When you instead want several **distinct** configurations in one
file — different packages, different sweeps, even different installers or
schedulers — list them under a top-level `experiments:` key. This makes
the file a **suite**: one `jarvis ppl run yaml <file>` runs each
experiment's full grid in order.

```yaml
name: storage_sweeps          # suite name (optional)

experiments:
  - config:                   # experiment 1 — its own packages + sweep
      name: ior_sweep
      pkgs:
        - pkg_type: builtin.ior
          pkg_name: ior
    vars:
      ior.xfer:   ["256k", "1m", "4m"]
      ior.nprocs: [1, 2, 4]
    loop:
      - [ior.xfer]
      - [ior.nprocs]
    output: ${HOME}/ior_test/results

  - config:                   # experiment 2 — a completely different pipeline
      name: redis_sweep
      pkgs:
        - pkg_type: builtin.redis
          pkg_name: redis
        - pkg_type: builtin.redis-benchmark
          pkg_name: redis_bench
    vars:
      redis_bench.req_size: [64, 1024, 4096]
      redis_bench.nthreads: [1, 2, 4]
    loop:
      - [redis_bench.req_size]
      - [redis_bench.nthreads]
    output: ${HOME}/redis_test/results
```

Key points:

- **Each entry is a full pipeline test.** An experiment accepts every
  key a standalone test does — `config`, `vars`, `loop`, `repeat`,
  `output`, and a nested `scheduler:` (see
  [Scheduling](#scheduling)). They run sequentially in listed order.
- **Independent outputs.** Give each experiment its own `output:`
  directory so their `results.csv` files don't overwrite each other.
- **Detection.** A file containing `experiments:` is always a suite. As
  with single tests, `jarvis ppl load|run yaml <file>` auto-detects it
  (`jarvis ppl load` prints the experiment count and total runs).
- **Grid vs. suite.** Use `vars`/`loop` to sweep one experiment; use
  `experiments:` to combine several experiments. The two compose — every
  experiment in a suite has its own grid.

A runnable example lives at
[`builtin/pipelines/examples/storage_sweep_test.yaml`](../builtin/pipelines/examples/storage_sweep_test.yaml)
(IOR + Redis sweeps in Docker). For a suite that also adds Spack and
SLURM, see
[Combined Example](#combined-example-slurm--spack--multiple-experiments).

## YAML Format

Pipeline tests use a specific YAML format that differs from regular pipelines:

```yaml
config:
  name: my_experiment
  env: my_environment
  pkgs:
    - pkg_type: my_package
      pkg_name: my_pkg
      param1: default_value
      param2: default_value

vars:
  my_pkg.param1: [value1, value2, value3]
  my_pkg.param2: [10, 20, 30]

loop:
  - [my_pkg.param1, my_pkg.param2]

repeat: 3

output: "${HOME}/experiment_results"
```

### config

This section contains the skeleton of a pipeline. It has the same exact parameters as a regular pipeline script, including:
- `name`: Pipeline name
- `env`: Environment reference (optional)
- `pkgs`: List of packages with their configurations
- `interceptors`: List of interceptors (optional)
- Container configuration (optional)

### vars

Defines the variables to vary during the experiment. Each variable follows the format:

```
pkg_name.var_name: [value1, value2, ...]
```

Where:
- `pkg_name` is the name of the package (from `pkg_name` field or derived from `pkg_type`)
- `var_name` is any configuration parameter the package accepts

### loop

Defines how variables should be iterated. The loop is a list of groups:
- Variables in the **same group** vary together (zip)
- Variables in **different groups** are independent (cartesian product)

### repeat

The number of times each experiment configuration should be run. This is useful for:
- Calculating averages across runs
- Understanding variability and noise in experiments
- Statistical significance testing

### output

The directory where results are stored. You can use environment variables:
- `${SHARED_DIR}` - Pipeline's shared directory
- `${PRIVATE_DIR}` - Pipeline's private directory
- `${CONFIG_DIR}` - Pipeline's config directory
- `${HOME}` - User's home directory

### scheduler (optional)

A top-level `scheduler:` block plays one of two roles depending on
whether the `vars:` section contains any `scheduler.*` entries. See
[scheduler.md](scheduler.md) for the full scheduler key reference.

**Mode A — single-job wrapper (no `scheduler.*` vars):**
the whole test runs inside one allocation; every variable combination
and repeat executes sequentially in that one job.

```yaml
scheduler:
  name: slurm
  nodes: 2
  ntasks_per_node: 4
  partition: cpu
  time: "01:00:00"

config:
  name: my_test
  pkgs: [...]

vars: {...}
loop: [...]
```

Submit with `jarvis ppl submit path/to/test.yaml`.

**Mode B — per-iteration template (`scheduler.*` vars present):**
the top-level scheduler becomes a set of *defaults* that every
iteration's scheduler inherits, and each iteration is submitted as its
own job via `sbatch --wait` (so the test runner blocks on each job
before moving on). See [Scheduler Variables](#scheduler-variables).

In Mode B, `jarvis ppl submit` is rejected; use `jarvis ppl run` so
each iteration submits independently.

## Installers

Before a pipeline runs, Jarvis can **install** the software each package
needs. Because the `config:` block is an ordinary pipeline definition,
the same installer keys apply inside a pipeline test, and they are picked
up for every iteration the test generates.

An installer is selected per package, in this order:

1. The package's explicit **`install_method`** — one of `pip`, `conda`,
   `spack`, or `container`.
2. Otherwise the pipeline-level **`base_deploy_mode`** — when it is
   `container` or `spack`, it doubles as the default install method for
   every package that doesn't set its own.
3. Otherwise no installer runs (the binary is assumed already on `PATH`).

The thing to install is named by **`install_query`** (the legacy alias
`install` also works). Its meaning depends on the method:

| `install_method` | `install_query` is …                    | Action |
|------------------|------------------------------------------|--------|
| `spack`          | a Spack spec, e.g. `ior@3.3.0 +hdf5`     | `spack install <spec>`, then load its env into the run |
| `pip`            | one or more pip requirement specs        | `python3 -m pip install <specs>` |
| `conda`          | conda package specs                      | `conda install <specs>` |
| `container`      | (n/a — built from the package Dockerfiles) | builds/pulls the deploy image |

Packages sharing an installer are batched into a single command, so a
suite that spack-installs `ior` in several experiments builds it once
(Spack is idempotent on subsequent specs).

### Spack example

```yaml
config:
  name: ior_spack
  base_deploy_mode: spack          # default installer for the whole pipeline
  pkgs:
    - pkg_type: builtin.ior
      pkg_name: ior
      install_method: spack        # explicit; redundant with base_deploy_mode
      install_query: "ior +hdf5"   # the Spack spec to build + load
      api: posix
      block: 32m
      xfer: 1m

vars:
  ior.xfer: ["256k", "1m", "4m"]
loop:
  - [ior.xfer]
output: ${HOME}/ior_spack_results
```

Spack is discovered via `SPACK_ROOT` (its `setup-env.sh` is sourced
before `spack install`). After installation, the environment produced by
`spack load` is merged into the run so the freshly built binaries are on
`PATH`/`LD_LIBRARY_PATH`.

## Example Files

### Basic Example

```yaml
config:
  name: ior_scaling_test
  env: hpc_env
  pkgs:
    - pkg_type: builtin.ior
      pkg_name: ior
      nprocs: 4
      block: 1G
      xfer: 1M

vars:
  ior.nprocs: [1, 2, 4, 8, 16]
  ior.block: [512M, 1G, 2G, 4G]

loop:
  - [ior.nprocs]
  - [ior.block]

repeat: 3

output: "${HOME}/ior_results"
```

This example:
- Varies `nprocs` independently (5 values)
- Varies `block` independently (4 values)
- Creates 5 x 4 = 20 unique configurations
- Runs each configuration 3 times
- Total: 60 runs

### Spark KMeans Example

```yaml
config:
  name: mm_kmeans_spark
  env: mega_mmap
  pkgs:
    - pkg_type: spark_cluster
      pkg_name: spark_cluster
      num_nodes: 1
    - pkg_type: mm_kmeans_df
      pkg_name: mm_kmeans_df
      path: ${HOME}/mm_data/parquet/kmeans.parquet
      window_size: 4g
      df_size: 4g
      nprocs: 1
      ppn: 16
      type: parquet
      k: 1000
    - pkg_type: mm_kmeans
      pkg_name: mm_kmeans
      path: ${HOME}/mm_data/parquet/*
      window_size: 30g
      api: spark
      max_iter: 4
      k: 8
      do_dbg: False
      dbg_port: 4001

vars:
  mm_kmeans_df.window_size: [16m, 64m, 128m, 1g, 2g, 4g]
  mm_kmeans_df.df_size: [16m, 64m, 128m, 1g, 2g, 4g]
  spark_cluster.num_nodes: [4]

loop:
  - [mm_kmeans_df.window_size, mm_kmeans_df.df_size]
  - [spark_cluster.num_nodes]

repeat: 1

output: "${SHARED_DIR}/output_multi"
```

This example:
- Varies `window_size` and `df_size` **together** (they change in lockstep)
- Varies `num_nodes` independently
- Creates 6 configurations (from the zipped window_size/df_size pairs)
- Runs each configuration 1 time
- Total: 6 runs

The resulting test cases are:
| window_size | df_size | num_nodes |
|-------------|---------|-----------|
| 16m         | 16m     | 4         |
| 64m         | 64m     | 4         |
| 128m        | 128m    | 4         |
| 1g          | 1g      | 4         |
| 2g          | 2g      | 4         |
| 4g          | 4g      | 4         |

## Variables and Loop

### Variable Naming

Variables use one of two prefixes:

- `pkg_name.var_name` — set `var_name` on the matching package.
  - `pkg_name` must match the `pkg_name` field of a package in the config
  - `var_name` is any configuration parameter that package accepts
- `scheduler.var_name` — set `var_name` on this iteration's scheduler
  block. See [Scheduler Variables](#scheduler-variables) below.

### Loop Groups

Loop groups define iteration patterns:

```yaml
loop:
  - [var_a, var_b]     # Group 1: var_a and var_b change together
  - [var_c]            # Group 2: var_c changes independently
  - [var_d, var_e]     # Group 3: var_d and var_e change together
```

**Rules:**
1. Variables in the same group must have the same number of values
2. Groups are combined using cartesian product
3. Within a group, variables are zipped (paired by index)

### Example: Complex Loop

```yaml
vars:
  pkg_a.x: [1, 2, 3]
  pkg_a.y: [10, 20, 30]
  pkg_b.z: [100, 200]
  pkg_c.w: [a, b]

loop:
  - [pkg_a.x, pkg_a.y]  # 3 combinations (zipped)
  - [pkg_b.z]           # 2 combinations
  - [pkg_c.w]           # 2 combinations
```

Total combinations: 3 x 2 x 2 = 12

### Scheduler Variables

Variables prefixed with `scheduler.` are applied to this iteration's
scheduler block instead of a package. This is the right way to sweep
node counts, partitions, time limits, or anything else the scheduler
understands (see [scheduler.md](scheduler.md) for the SLURM key list).

The effective scheduler for each iteration is built by merging, in
order:

1. The test's top-level `scheduler:` block (defaults / template).
2. Any `scheduler:` block nested inside `config:` (per-test override).
3. The `scheduler.*` values from this combination's `vars:`.

When the merged block exists, the iteration is submitted as its own
job via `sbatch --wait`; the test runner blocks on each job before
collecting stats and moving on.

#### Example: Scaling Sweep

```yaml
scheduler:
  name: slurm
  partition: compute
  time: "00:30:00"
  ntasks_per_node: 16
  output: ${HOME}/logs/scaling.%j.out
  error:  ${HOME}/logs/scaling.%j.err

config:
  name: ior_scaling
  pkgs:
    - pkg_type: builtin.ior
      pkg_name: ior
      block: 1G
      xfer: 1M

vars:
  scheduler.nodes: [1, 2, 4, 8, 16]
  ior.nprocs:     [16, 32, 64, 128, 256]

loop:
  - [scheduler.nodes, ior.nprocs]   # zipped: nodes * 16 procs

repeat: 3
output: "${HOME}/ior_scaling_results"
```

This sweeps a 5-point scaling curve. Each iteration submits its own
SLURM job with the matching `--nodes=...`, inheriting `partition`,
`time`, `ntasks_per_node`, etc. from the top-level template.

## Launcher Overrides

The `config:` block is loaded as an ordinary pipeline, so it accepts
the same top-level launcher-override keys a regular pipeline does —
`ssh_cmd`, `pssh_cmd`, and `mpi_cmd`. Place them **inside `config:`**
(not at the test's top level), and every iteration generated from the
test inherits them:

```yaml
config:
  name: ior_sweep
  ssh_cmd:  "env -u LD_LIBRARY_PATH ssh"   # host openssh, not conda's
  pssh_cmd: "env -u LD_LIBRARY_PATH ssh"
  mpi_cmd:  "mpiexec"
  pkgs:
    - pkg_type: builtin.ior
      pkg_name: ior

vars:
  ior.nprocs: [1, 2, 4, 8, 16]
loop:
  - [ior.nprocs]
repeat: 3
output: "${HOME}/ior_results"
```

These swap the SSH / parallel-SSH / MPI launchers without modifying
any package. The canonical use is `env -u LD_LIBRARY_PATH ssh`, which
keeps a conda environment's `libcrypto` out of the host `ssh` (an ABI
mismatch otherwise makes `ssh` exit 255 before forwarding the remote
command). See [shell.md → Launcher Overrides](shell.md#launcher-overrides)
for the full mechanism and the per-MPI-backend bootstrap forwarding.

## Output and Statistics

### Output Directory

Results are written to the specified output directory:
- `results.csv` - CSV file with all results (easy to import into Excel/pandas)
- `results.yaml` - YAML file with full result details

### CSV Format

The CSV file contains columns for:
1. `run_idx` - Sequential run number
2. `combination_idx` - Index of the parameter combination
3. `repeat_idx` - Repeat index (0 to repeat-1)
4. `status` - Success or failed
5. `runtime` - Execution time in seconds
6. All variable values (one column per variable)
7. All collected statistics (from `_get_stat()`)
8. `error` - Error message if failed

### YAML Format

The YAML file contains the complete test configuration and results:

```yaml
test_name: my_experiment
total_runs: 60
variables:
  ior.nprocs: [1, 2, 4, 8, 16]
  ior.block: [512M, 1G, 2G, 4G]
loop:
  - [ior.nprocs]
  - [ior.block]
repeat: 3
results:
  - combination_idx: 0
    repeat_idx: 0
    variables:
      ior.nprocs: 1
      ior.block: 512M
    status: success
    runtime: 45.2
    start_time: "2024-01-15T10:30:00"
    end_time: "2024-01-15T10:30:45"
    stats:
      ior.throughput: 1200.5
      ior.latency: 0.5
  # ... more results
```

## CLI Commands

### Loading a Pipeline Test

```bash
# Load a pipeline test (auto-detected from YAML structure)
jarvis ppl load yaml /path/to/test.yaml

# This outputs:
# Loaded pipeline test: my_experiment
#   Total combinations: 20
#   Repeat count: 3
#   Total runs: 60
# Run with 'jarvis ppl run' to execute the test
```

### Running a Pipeline Test

```bash
# Run a previously loaded test
jarvis ppl run

# Or load and run in one command
jarvis ppl run yaml /path/to/test.yaml
```

### Auto-Detection

The system automatically detects whether a YAML file is a pipeline test or a regular pipeline:

- **Pipeline Test**: Has a `config` section plus `vars`, `loop`, `repeat`, or `output`
- **Regular Pipeline**: Has `name` or `pkgs` at the top level

This means you can use the same commands for both:

```bash
# Both work the same way:
jarvis ppl load yaml regular_pipeline.yaml
jarvis ppl load yaml pipeline_test.yaml
```

## Resume and Progress

### Incremental CSV Logging

Pipeline test results are written to CSV incrementally after each run completes. This means that if a long-running test crashes or is interrupted mid-way, all completed results are preserved in `results.csv`.

### Resuming a Test

To resume an interrupted test, simply re-run the same command:

```bash
jarvis ppl run yaml /path/to/test.yaml
```

The test runner will:
1. Check for an existing `results.csv` in the output directory
2. Load any previously completed results
3. Skip runs that are already done
4. Continue from the next incomplete run

When resuming, you'll see output like:

```
Resuming pipeline test: my_experiment
  Found 15/60 completed runs, resuming from run 16
```

### Progress Output

During execution, the test runner prints progress information for each run:

```
Run 16/60: Combination 6, Repeat 1 (44 remaining)
  Parameters: ior.nprocs=4, ior.block=2G
  Status: success, Runtime: 45.20s
```

This includes:
- Current run number and total
- Combination and repeat indices
- Number of remaining runs
- Parameter values for the current run
- Result status and runtime after completion

### Notes

- The YAML results file (`results.yaml`) is written once at the end of all runs
- If the CSV has more rows than the test's total runs (e.g., the test configuration changed), the test starts fresh
- Resume works by matching run count, so the test configuration (vars, loop, repeat) should remain the same between runs

## Custom Statistics

Packages can define custom statistics by implementing the `_get_stat()` method:

```python
class MyBenchmark(Application):
    def _get_stat(self, stat_dict):
        """
        Get statistics from the application.

        :param stat_dict: A dictionary to populate with statistics.
        :return: None
        """
        # Parse output for results
        output = self.exec.stdout.get('localhost', '')

        # Extract throughput
        if 'throughput' in output:
            throughput = self._parse_throughput(output)
            stat_dict[f'{self.pkg_id}.throughput'] = throughput

        # Record runtime
        stat_dict[f'{self.pkg_id}.runtime'] = self.runtime
```

### YCSB Example

```python
class Ycsb(Application):
    def _get_stat(self, stat_dict):
        """
        Get statistics from the YCSB benchmark.

        :param stat_dict: A dictionary of statistics.
        :return: None
        """
        output = self.exec.stdout['localhost']

        # Extract throughput from YCSB output
        if 'throughput(ops/sec)' in output:
            match = re.search(r'throughput\(ops\/sec\): ([0-9.]+)', output)
            if match:
                throughput = match.group(1)
                stat_dict[f'{self.pkg_id}.throughput'] = throughput

        # Record runtime
        stat_dict[f'{self.pkg_id}.runtime'] = self.start_time
```

### Best Practices for Statistics

1. **Prefix with package ID**: Use `f'{self.pkg_id}.stat_name'` to avoid conflicts
2. **Convert to appropriate types**: Store numbers as numbers, not strings
3. **Handle missing data**: Check for existence before parsing
4. **Store timing information**: Include start/end times and runtime
5. **Parse structured output**: Use regex or structured parsing for reliability

### Common Statistics to Collect

| Statistic Type | Example Key | Description |
|---------------|-------------|-------------|
| Throughput | `pkg.throughput` | Operations per second |
| Bandwidth | `pkg.bandwidth` | Data transfer rate (MB/s) |
| Latency | `pkg.latency_avg` | Average latency (ms) |
| IOPS | `pkg.iops` | I/O operations per second |
| Runtime | `pkg.runtime` | Execution time (seconds) |
| Memory | `pkg.memory_peak` | Peak memory usage (MB) |
| Error Rate | `pkg.error_rate` | Percentage of failures |

## Scheduling

Pipeline tests integrate with the batch scheduler (SLURM today) in two
mutually exclusive modes, selected automatically by whether `vars:`
contains any `scheduler.*` entries. Both are introduced under
[scheduler (optional)](#scheduler-optional) above; this section
summarizes them and points at the full reference.

### Mode A — one job wraps the whole test

No `scheduler.*` vars. A top-level `scheduler:` block wraps **every**
combination and repeat in a single allocation. The generated job
script builds a hostfile from the allocation, then runs
`jarvis ppl run yaml <test>` once so all iterations reuse the same
nodes sequentially.

```bash
jarvis ppl submit path/to/test.yaml          # writes + sbatch
jarvis ppl submit path/to/test.yaml +no_submit   # script only
```

The script is written to `<test_shared_dir>/submit.slurm`.

### Mode B — one job per iteration

Any `scheduler.<key>` entry in `vars:` flips the test into
per-iteration submission. The effective scheduler for each iteration is
merged in order:

```
top-level scheduler  ⊕  config.scheduler (if any)  ⊕  scheduler.* vars
```

Each iteration is submitted on its own via `sbatch --wait`, so the
test runner **blocks** on each job (capturing its runtime/stats)
before moving to the next. Run these with `jarvis ppl run yaml
<test>`; `jarvis ppl submit` is rejected in this mode because wrapping
a per-iteration template in one job would freeze the swept values.

This is the natural way to drive scaling sweeps (node count,
partition, time limit, ...) — see
[Scheduler Variables](#scheduler-variables) for a worked scaling
example.

### Reference

See **[scheduler.md](scheduler.md)** for: the full SLURM key table
(`nodes`, `ntasks_per_node`, `partition`, `time`, `gres`, ...),
pass-through of arbitrary `--flag=value` directives, `pre_cmds` /
`post_cmds` hooks, the hostname `suffix:` (secondary-NIC) feature, and
how the allocation-built hostfile flows back into every package via
`self.hostfile`.

## Combined Example: SLURM + Spack + Multiple Experiments

The three features above compose. This suite runs **two experiments**
([Multiple Experiments](#multiple-experiments-suite)), each installing
its software with **Spack** ([Installers](#installers)) and submitting
every iteration as its own **SLURM** job ([Scheduling](#scheduling)).
The full file is
[`builtin/pipelines/examples/ior_spack_slurm_suite.yaml`](../builtin/pipelines/examples/ior_spack_slurm_suite.yaml).

```yaml
name: ior_spack_slurm_suite

experiments:
  # Experiment 1: IOR over the POSIX API
  - config:
      name: ior_posix
      base_deploy_mode: spack            # Spack installer for all pkgs
      scheduler:                         # nested -> one SLURM job per iteration
        name: slurm
        job_name: ior_posix
        nodes: 2
        ntasks_per_node: 8
        partition: cpu
        time: "00:30:00"
      pkgs:
        - pkg_type: builtin.ior
          pkg_name: ior
          install_method: spack
          install_query: ior             # the Spack spec
          api: posix
          ppn: 8
          block: 512m
          out: ${HOME}/ior_spack_test/posix.bin
          log: ${HOME}/ior_spack_test/posix.log
          write: true
          read: true
    vars:
      ior.xfer:   ["1m", "4m"]           # I/O size
      ior.nprocs: [8, 16]                # number of MPI processes
    loop:
      - [ior.xfer]
      - [ior.nprocs]
    output: ${HOME}/ior_spack_test/posix_results

  # Experiment 2: IOR over the MPI-IO API (same spack-built binary)
  - config:
      name: ior_mpiio
      base_deploy_mode: spack
      scheduler:
        name: slurm
        job_name: ior_mpiio
        nodes: 2
        ntasks_per_node: 8
        partition: cpu
        time: "00:30:00"
      pkgs:
        - pkg_type: builtin.ior
          pkg_name: ior
          install_method: spack
          install_query: ior
          api: mpiio
          ppn: 8
          block: 512m
          out: ${HOME}/ior_spack_test/mpiio.bin
          log: ${HOME}/ior_spack_test/mpiio.log
          write: true
          read: true
    vars:
      ior.xfer:   ["1m", "4m"]
      ior.nprocs: [8, 16]
    loop:
      - [ior.xfer]
      - [ior.nprocs]
    output: ${HOME}/ior_spack_test/mpiio_results
```

```bash
jarvis ppl run yaml builtin/pipelines/examples/ior_spack_slurm_suite.yaml
```

How the pieces interact:

- **Suite** → the two experiments run in order; each writes its own
  `results.csv` under its `output:` directory.
- **Spack** → before each experiment's pipeline deploys, Jarvis runs
  `spack install ior` and loads its environment. The install is
  idempotent, so the second experiment reuses the first's build.
- **SLURM** → because the `scheduler:` block is **nested inside
  `config:`**, every iteration is submitted as its own job with
  `sbatch --wait`; the runner blocks on each job, records its
  runtime/stats, then continues. (A nested `config.scheduler` is the
  per-iteration form — see [Scheduling](#scheduling). A suite does not
  wrap all experiments in one allocation; scheduling is expressed
  per experiment.)
