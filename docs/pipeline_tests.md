# Pipeline Tests

Pipeline tests are used to run experiment sets using a grid search. They allow you to systematically explore parameter spaces by varying package configurations across multiple runs.

## Table of Contents

1. [Overview](#overview)
2. [YAML Format](#yaml-format)
3. [Example Files](#example-files)
4. [Variables and Loop](#variables-and-loop)
5. [Output and Statistics](#output-and-statistics)
6. [CLI Commands](#cli-commands)
7. [Resume and Progress](#resume-and-progress)
8. [Custom Statistics](#custom-statistics)

## Overview

A pipeline test consists of:
- A **config** section containing the base pipeline definition
- A **vars** section defining variables to sweep
- A **loop** section defining how variables are iterated
- A **repeat** count for running each configuration multiple times
- An **output** directory for storing results

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

Variables use the format `pkg_name.var_name`:
- `pkg_name` must match the `pkg_name` field of a package in the config
- `var_name` is any configuration parameter that package accepts

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
