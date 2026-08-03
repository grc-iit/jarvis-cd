# WfCommons

`builtin.wfcommons` generates and executes one bounded synthetic workflow cell
using WfCommons and WfBench. Multiple package aliases compose a parameter grid;
the package does not hide a benchmark-specific matrix inside one step.

JARVIS owns the process lifecycle, output root, pinned WfFormat schema, and
artifact finalization. The scheduled workload never installs Python packages or
downloads a schema. Operators prepare a WfCommons 1.4 runtime before execution,
while agents select only scientific and workload dimensions.

## Configuration menu

| Key | Default | Responsibility |
| --- | ---: | --- |
| `recipe` | `montage` | WfCommons scientific workflow family. |
| `num_tasks` | `100` | Requested generated task count. Recipe generators may produce a nearby realizable count, which the result records separately. |
| `data_footprint_mb` | `0` | Total workflow data footprint in MB. Zero retains recipe defaults. |
| `seed` | `424200` | Deterministic workflow topology seed. Reuse a seed to compare footprints at fixed topology. |
| `cpu_work` | `1` | Positive WfBench CPU work units. Zero is rejected because WfBench would not exercise its intended I/O path. |
| `percent_cpu` | `1.0` | Fraction of WfBench work threads assigned to CPU work. |
| `drop_page_cache` | `false` | Enables WfBench's per-file `POSIX_FADV_DONTNEED` behavior. It does not claim a privileged system-wide cold cache. |
| `clio_prefix` | `false` | Prefixes manifest-declared data paths with `clio::` for an explicitly attached storage interceptor. |
| `out` | `run` | Package-owned result directory under the JARVIS shared root. |
| `timeout_seconds` | `3600` | Hard cell timeout, bounded to one day. |
| `nprocs`, `ppn` | `1`, `1` | The workflow driver is single-process. Generated tasks are controlled by WfBench. |
| `runtime_python` | empty | Hidden operator setting. Empty uses `WFCOMMONS_PYTHON`, then the JARVIS Python executable. |

## Runtime contract

Native execution requires Bash and a prepared Python runtime whose imported
`wfcommons.__version__` is exactly `1.4`. The package's deployment description
probes that contract. Configuration never creates a venv, upgrades pip, or
installs from the network.

The optional container build creates that runtime before workload execution,
pins WfCommons 1.4, and verifies the WfFormat schema against repository digest
`716e7b625a37a144674afbf8e6a008c21bbd0fd467ccbb7be39deab9fb8f6aab`.
The built image digest is the deployable runtime identity.

## Results and artifacts

Every successful step closes these package-owned artifacts:

- `wfcommons-result.json`, schema `jarvis.wfcommons-result.v1`;
- the generated WfFormat workflow manifest;
- the WfBench workflow log;
- a sorted Python distribution lock from the prepared runtime;
- the exact staged WfFormat schema.

The result binds the requested and observed task counts, MB footprint, seed,
elapsed time, DAG edge count, topology hash, runtime versions, return code, and
SHA-256 digest of every member. Nonzero execution leaves present products
`incomplete`; it never finalizes them as successful outputs.

## Four-cell example

The following shape compares two task counts and two footprints without a
benchmark-specific adapter:

```yaml
name: wfcommons_epigenomics_grid
pkgs:
  - pkg_type: builtin.wfcommons
    pkg_name: tasks_100_data_8
    recipe: epigenomics
    num_tasks: 100
    data_footprint_mb: 8
    seed: 424300
  - pkg_type: builtin.wfcommons
    pkg_name: tasks_100_data_32
    recipe: epigenomics
    num_tasks: 100
    data_footprint_mb: 32
    seed: 424300
  - pkg_type: builtin.wfcommons
    pkg_name: tasks_500_data_8
    recipe: epigenomics
    num_tasks: 500
    data_footprint_mb: 8
    seed: 424700
  - pkg_type: builtin.wfcommons
    pkg_name: tasks_500_data_32
    recipe: epigenomics
    num_tasks: 500
    data_footprint_mb: 32
    seed: 424700
```

WfBench fabricates the CPU and I/O behavior of a workflow family; it does not
execute the scientific kernels represented by recipe task names.
