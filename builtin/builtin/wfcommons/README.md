# WfCommons

Generate a synthetic scientific workflow from a [WfCommons](https://wfcommons.org/)
recipe, translate it into a runnable [WfBench](https://github.com/wfcommons/wfbench)
benchmark, and execute the tasks locally in topological order.

## What it runs

1. **Generate** — picks the named recipe (`montage`, `genome`, `cycles`, `blast`,
   `bwa`, `srasearch`, `epigenomics`, `seismology`, `soykb`) and instantiates a
   `WorkflowGenerator` with `num_tasks` tasks. Writes the WfFormat JSON to
   `<out>/workflow.json`.
2. **Translate** — uses `WfBenchTranslator` to produce an executable benchmark
   workflow under `<out>/bench/` (per-task `wfbench` commands + I/O stubs).
3. **Execute** — walks the DAG in topological order with a thread pool of
   `max_workers` workers, shelling out to each task's `wfbench` command.

## Config

| Key | Default | Notes |
| --- | --- | --- |
| `recipe` | `montage` | One of the 9 wfcommons recipes. |
| `num_tasks` | `100` | Workflow size. |
| `cpu_work` | `100` | CPU work units per `wfbench` task. |
| `data_footprint` | `100M` | Data size per task; passed to translator. |
| `max_workers` | `4` | Concurrency for the local task pool. |
| `out` | `$HOME/wfcommons_out` | Output directory (json + bench/ + logs/). |
| `keep_outputs` | `false` | Retain bulky `bench/data/` after the run. |
| `venv` | `$HOME/.jarvis-wfcommons-venv` | Bare-metal venv path (default mode only). |

## Deployment modes

- **`base_deploy_mode: container`** — builds a python venv with `wfcommons[bench]`
  inside the build container and ships it via the deploy image.
- **`base_deploy_mode: default`** — creates a venv at `venv:` on the local host
  and `pip install`s wfcommons there (idempotent).

## Example

```yaml
name: wfcommons_container_test
base_deploy_mode: container
container_engine: docker
container_base: ubuntu:24.04

pkgs:
  - pkg_type: builtin.wfcommons
    pkg_name: wfcommons_container
    recipe: montage
    num_tasks: 25
    cpu_work: 100
    data_footprint: 100M
    out: /tmp/wfcommons_out
```

Run with:

```
jarvis ppl load yaml builtin/pipelines/examples/wfcommons_container_test.yaml
jarvis ppl run
```

## Notes

- The runner is single-process; task-level parallelism is governed by
  `max_workers`, not by MPI. Set `nprocs: 1`, `ppn: 1`.
- The WfBench tasks fabricate CPU + I/O load; they don't carry the actual
  scientific kernel of (say) Montage. Use it for scheduler / storage / system
  benchmarking, not for science.
