# Jarvis-CD Scheduler Integration

Jarvis pipelines and pipeline tests can be submitted directly to a batch
resource manager (SLURM today; PBS / others can be added by subclassing
`Scheduler`). The integration lives in `jarvis_cd/core/scheduler.py` and
is driven by a top-level `scheduler:` key in the YAML.

When `scheduler:` is set, `jarvis ppl submit` writes a job script into
the pipeline's shared directory and (by default) hands it off to the
scheduler's submit command (`sbatch` for SLURM). Inside the allocation
the script:

1. Builds a hostfile from the scheduler's nodelist
   (`scontrol show hostnames "$SLURM_JOB_NODELIST"` for SLURM)
2. Writes it to the path declared by `scheduler.hostfile`
   (default: `${SHARED_DIR}/hostfile.txt`, where `${SHARED_DIR}` is the
   pipeline's shared directory)
3. Runs the pipeline (`jarvis ppl run yaml <file>` for a YAML-loaded
   pipeline; `jarvis cd <name> && jarvis ppl run` for a current
   pipeline)

The pipeline binds `self.hostfile` to that same path at load time, so
every package in the pipeline that consults `self.hostfile` reads the
allocation-derived host list with no extra wiring.

## Pipeline YAML

```yaml
name: my_pipeline

scheduler:
  name: slurm                 # required; currently: slurm
  # hostfile: ${SHARED_DIR}/hostfile.txt   # default
  job_name: my_pipeline
  nodes: 2
  ntasks_per_node: 4
  partition: cpu
  account: my_account
  time: "00:30:00"
  output: slurm-%j.out
  error: slurm-%j.err

pkgs:
  - pkg_type: builtin.ior
    nprocs: 8
    ppn: 4
```

### Recognised scheduler keys (SLURM)

| Key                | SBATCH flag             | Notes                               |
|--------------------|-------------------------|-------------------------------------|
| `name`             | (selects backend)       | Required: `slurm`                   |
| `hostfile`         | (used by Jarvis)        | Defaults to `${SHARED_DIR}/hostfile.txt` |
| `job_name`         | `--job-name`            |                                     |
| `nodes`            | `--nodes`               |                                     |
| `ntasks`           | `--ntasks`              |                                     |
| `ntasks_per_node`  | `--ntasks-per-node`     |                                     |
| `cpus_per_task`    | `--cpus-per-task`       |                                     |
| `mem`              | `--mem`                 |                                     |
| `time`             | `--time`                |                                     |
| `partition`        | `--partition`           |                                     |
| `account`          | `--account`             |                                     |
| `qos`              | `--qos`                 |                                     |
| `output`           | `--output`              |                                     |
| `error`            | `--error`               |                                     |
| `gres`             | `--gres`                |                                     |
| `gpus`             | `--gpus`                |                                     |
| `gpus_per_node`    | `--gpus-per-node`       |                                     |
| `constraint`       | `--constraint`          |                                     |
| `reservation`      | `--reservation`         |                                     |
| `exclusive`        | `--exclusive`           | Pass `true` to emit the flag        |
| `mail_user`        | `--mail-user`           |                                     |
| `mail_type`        | `--mail-type`           |                                     |
| `sbatch_args`      | (raw)                   | List of literal SBATCH lines        |
| `pre_cmds`         | (shell)                 | Lines run before hostfile build     |
| `post_cmds`        | (shell)                 | Lines run after the pipeline        |

Unknown keys are passed straight through as `--<key with underscores
replaced by dashes>=<value>`, so most SBATCH flags can be expressed
without an explicit mapping.

## Pipeline Test YAML

A `scheduler:` block at the **top level** of a pipeline test wraps the
whole test (every variable combination + repeat) in a single batch
allocation:

```yaml
scheduler:
  name: slurm
  nodes: 2
  ntasks_per_node: 4
  partition: cpu
  time: "01:00:00"

config:
  name: my_test
  pkgs:
    - pkg_type: builtin.ior
      ...

vars:
  ior.xfer: ["64K", "1M"]
loop:
  - [ior.xfer]
repeat: 2
```

To submit **one job per iteration** instead, move the `scheduler:`
block inside `config:` so each generated child pipeline owns its own
submission script.

## CLI

```bash
# Submit (writes script + runs sbatch)
jarvis ppl submit                                 # current pipeline
jarvis ppl submit path/to/pipeline.yaml           # specific YAML
jarvis ppl submit path/to/pipeline_test.yaml      # YAML test

# Generate script only, no sbatch
jarvis ppl submit +no_submit
jarvis ppl submit path/to/pipeline.yaml +no_submit
```

The generated script is written to `<pipeline_shared_dir>/submit.slurm`.

## How the hostfile flows

```
scheduler.hostfile  ──►  job script writes file inside allocation
       │                          │
       └────────────────►  self.hostfile (Pipeline + every package)
```

`Pipeline._apply_scheduler_hostfile()` binds the pipeline's `hostfile`
attribute to the scheduler-owned path with `load_path=False`, so the
file does not have to exist at submit-time. Inside the allocation, the
job script writes the file before `jarvis ppl run` executes, and every
subsequent package read sees the real node list.
