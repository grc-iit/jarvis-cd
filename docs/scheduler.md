# Jarvis-CD Scheduler Integration

Jarvis pipelines and pipeline tests can be submitted directly to a batch
resource manager (SLURM today; PBS / others can be added by subclassing
`Scheduler`). The integration lives in `jarvis_cd/core/scheduler.py` and
is driven by a top-level `scheduler:` key in the YAML.

When `scheduler:` is set, `jarvis ppl submit` creates a unique execution
directory under `<pipeline_shared_dir>/executions/`, seals the pipeline and
environment that were current at submission time, writes an execution-scoped
job script, and (by default) hands it off to the scheduler's submit command
(`sbatch` for SLURM). Inside the allocation the script:

1. Builds a hostfile from the scheduler's nodelist
   (`scontrol show hostnames "$SLURM_JOB_NODELIST"` for SLURM)
2. Writes it to that execution's private `hostfile.txt`
3. Runs the execution's isolated `runtime/pipeline.yaml` working copy

Changing or deleting the named pipeline after submission cannot change an
already queued execution. Each execution directory contains:

- `input/`: a read-only copy of the submitted `pipeline.yaml` and
  `environment.yaml`, used as durable provenance
- `runtime/`: the private working copy loaded inside the allocation; package
  state written during the run stays here rather than mutating the named
  pipeline
- `shared/`: package-visible shared runtime files for this execution only
- `private/`: package-visible machine-private runtime files for this execution
  only
- `submit.slurm`: the exact script handed to SLURM
- `hostfile.txt`: the allocation hostfile created when the job starts
- `.jarvis-execution.json`: the ownership and terminal-state marker used by
  explicit cleanup

The pipeline's structured `last_submission` record includes the execution ID,
the execution root, the sealed input and runtime paths, the script and hostfile
paths, and a SHA-256 digest of the two sealed input documents.

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
| `hostfile`         | (used by Jarvis)        | Direct scheduler use defaults to `${SHARED_DIR}/hostfile.txt`; `Pipeline.submit` isolates it per execution |
| `suffix`           | (used by Jarvis)        | Appended to every hostname pulled from the allocation, e.g. `-40g` to target a 40GbE NIC |
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

### Hostname suffix

`suffix:` is appended to every hostname `scontrol show hostnames`
emits before the hostfile is written. Use this when SLURM resolves
nodes by their management hostname but you want the pipeline to
reach them over a different NIC.

```yaml
scheduler:
  name: slurm
  nodes: 4
  partition: compute
  suffix: "-40g"        # ares-comp-3 -> ares-comp-3-40g
```

## Pipeline Test YAML

A pipeline test can drive the scheduler in two modes; which one
applies is decided by whether `vars:` contains any `scheduler.*`
entries.

### Mode A — single-job wrapper

No `scheduler.*` vars. A `scheduler:` block at the **top level** of
the test wraps the whole test (every variable combination + repeat)
in a single batch allocation:

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

Submit with `jarvis ppl submit path/to/test.yaml`.

### Mode B — per-iteration template (`scheduler.*` vars)

Any `scheduler.<key>` entry in `vars:` switches the test into
per-iteration submission. The top-level `scheduler:` block becomes a
template; each iteration's effective scheduler is

```
top-level scheduler  ⊕  config.scheduler (if any)  ⊕  scheduler.* vars
```

Each iteration is submitted on its own via `sbatch --wait`, so the
test runner blocks on each job before moving on. This is the natural
way to sweep node counts (or partitions, time limits, ...) for
scaling experiments.

```yaml
scheduler:
  name: slurm
  partition: compute
  time: "00:30:00"
  ntasks_per_node: 16

config:
  name: ior_scaling
  pkgs:
    - pkg_type: builtin.ior
      pkg_name: ior
      block: 1G

vars:
  scheduler.nodes: [1, 2, 4, 8, 16]
  ior.nprocs:     [16, 32, 64, 128, 256]
loop:
  - [scheduler.nodes, ior.nprocs]
```

Run with `jarvis ppl run yaml path/to/test.yaml`. `jarvis ppl submit`
is rejected in this mode, since wrapping a per-iteration template in
a single job would freeze the swept values.

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

The generated script is written to
`<pipeline_shared_dir>/executions/<execution-id>/submit.slurm`. A fresh
execution ID is generated for every CLI submission; API callers may provide a
bounded path-safe ID for end-to-end correlation.

## Execution cleanup and pipeline destruction

Scheduler execution roots are never removed by age, count, or "keep latest"
retention. An old root may still belong to a queued or running job. Cleanup is
therefore an explicit, bounded API operation over exact execution IDs:

```python
pipeline.cleanup_executions(["run-2026-07-11"])
```

JARVIS accepts an ID without an override only when its owned execution marker
is terminal. For a nonterminal marker, the operator must first check the
scheduler's authoritative state and then name that same ID in
`terminal_verified`:

```python
pipeline.cleanup_executions(
    ["run-2026-07-11"],
    terminal_verified=["run-2026-07-11"],
)
```

`force=True` is the explicit emergency override; it does not broaden the set
of IDs being removed. Cleanup validates the ownership marker and quarantines
each exact root before deletion. `Pipeline.destroy()` refuses to destroy a
named pipeline while *any* execution-root entries remain, including queued
jobs and incomplete or unrecognized entries. A runtime snapshot cannot clean
execution roots or destroy its named source pipeline.

## Multi-node SSH inside an allocation

Once the job is running, packages reach the other allocated nodes over
SSH/PSSH (e.g. apptainer instance start is fanned to every host, MPI
bootstraps remote ranks). Inside a SLURM allocation those ssh hops to
allocated peers are adopted automatically by `pam_slurm_adopt`, so no
extra SSH keys are needed.

If the workload runs from a conda environment, pair the scheduler with
a pipeline-level `ssh_cmd` launcher override so the host `ssh` does not
inherit the conda `LD_LIBRARY_PATH` (an OpenSSL ABI mismatch otherwise
makes `ssh` exit 255):

```yaml
ssh_cmd:  "env -u LD_LIBRARY_PATH ssh"
pssh_cmd: "env -u LD_LIBRARY_PATH ssh"

scheduler:
  name: slurm
  nodes: 4
  partition: compute
  time: "00:30:00"

pkgs:
  - pkg_type: builtin.ior
    nprocs: 96
    ppn: 24
```

`ssh_cmd` is also forwarded to the MPI bootstrap agent (OpenMPI
`--mca plm_rsh_agent`, MPICH/Hydra `-bootstrap-exec`) so multi-node
MPI ranks spawn through the wrapped ssh too. See
[shell.md → Launcher Overrides](shell.md#launcher-overrides) for the
full mechanism. In a pipeline test, put these keys inside `config:`.

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
