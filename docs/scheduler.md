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
3. Runs the execution's isolated `runtime/pipeline.yaml` working copy as a
   background child that the batch shell `wait`s on, with a `TERM` trap, so
   `jarvis execution stop` can run `jarvis ppl stop` before the job exits
   (see [Cancelling and stopping executions](#cancelling-and-stopping-executions))

The generated script invokes JARVIS through the same Python interpreter that
created the submission and binds the submitting `JARVIS_ROOT`. This preserves
the submitting configuration boundary on compute nodes; an unrelated `jarvis`
executable or a different user's default built-in checkout on the allocation's
`PATH` cannot replace it.

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
- `.jarvis-execution.json`: the typed durable execution record used by status
  queries and explicit cleanup

`Pipeline.submit()` returns a typed execution handle containing the JARVIS
execution ID plus the provider-native scheduler ID when submission succeeds.
The pipeline's compatibility `last_submission` record includes the execution ID,
the execution root, the sealed input and runtime paths, the script and hostfile
paths, and a SHA-256 digest of the two sealed input documents.

Before invoking the scheduler, JARVIS creates the concrete parent directories
for `scheduler.output` and `scheduler.error`. Scheduler replacement tokens such
as SLURM `%j` are supported in the filename, where the provider can resolve
them after submission, but not in a parent directory whose name is unknown
before the scheduler accepts the job. A directory preparation failure is a
terminal pre-submission error, so JARVIS never returns an accepted-looking
handle for a job that could not open its configured logs.

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
| `stop_timeout`     | (used by Jarvis)        | Seconds `jarvis ppl stop` may take after `jarvis execution stop` (default 60; needs `timeout(1)`) |
| `stop_grace`       | (used by Jarvis)        | Seconds the pipeline child gets after TERM before KILL (default 20, below Slurm's usual 30 s KillWait) |

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

# Submit asynchronously and print the handle as the final JSON line
jarvis ppl submit +json

# Block until the scheduler workload finishes
jarvis ppl submit +wait +json
```

The generated script is written to
`<pipeline_shared_dir>/executions/<execution-id>/submit.slurm`. A fresh
execution ID is generated for every CLI submission; API callers may provide a
bounded path-safe ID for end-to-end correlation.

For a submitted nonterminal record, `Pipeline.get_execution()` also asks the
selected scheduler provider for current state. The SLURM provider checks the
live queue and then accounting when available. A provider-reported terminal
state is committed into the same durable execution record. If SLURM no longer
recognizes an accepted job and no historical record is available, JARVIS waits
through a bounded startup grace period before recording failure; generic query
errors do not silently terminalize the execution.

## Cancelling and stopping executions

Callers never run `scancel` themselves; they ask JARVIS, which signals the job
recorded in `.jarvis-execution.json` and commits the outcome to the same
record:

```bash
jarvis execution cancel <execution-id> [--pipeline-id P] +json
jarvis execution stop   <execution-id> [--pipeline-id P] [+wait] [--timeout 120] +json
```

Both print the resulting `jarvis.execution.record.v1` document (the shape of
`jarvis execution get +json`). Both refuse direct (non-scheduler) executions,
and both return a terminal record unchanged, so they are idempotent.

**cancel** runs `scancel [--clusters=C] <job>`. `--clusters` is passed only
when `sbatch --parsable` itself reported a cluster (`jobid;cluster`, as on a
federated or multi-cluster site). The cluster that the job records from
`SLURM_CLUSTER_NAME` is never passed, because every Slurm job has one, and
`scancel --clusters` needs slurmdbd. Without slurmdbd it fails with "Problem
talking to database". The same rule applies to stop. Before signalling, cancel writes
`metadata.cancel_request` (`jarvis.execution.control.v1`: action, status,
provider, native id, the exact command, then the scheduler's `result`). An
accepted cancel commits state `canceled`. If Slurm reports that the job is
already gone (`Invalid job id`, `already completing or completed`), JARVIS
first reconciles with `squeue`/`sacct`. A real terminal state, such as
`completed`, is kept. Otherwise the record is committed `canceled` with a note
that the scheduler no longer had the job. A failed `scancel` marks the request
`failed`, leaves the record unchanged, and exits non-zero. If the job was
running, the batch shell still finalizes: its TERM trap reaps the pipeline
child and records the cancel (without running `ppl stop`).

**stop** is the graceful path for services such as pvserver. It writes
`metadata.stop_request` and sends `scancel --signal=TERM --batch <job>`, which
signals only the batch shell. A job that is still pending has no shell to stop,
so stop falls back to cancel (`stop_request.status: fallback_cancel`). With
`+wait`, the command polls the record until it is terminal or `--timeout`
expires.

The generated script runs the pipeline as a background child and blocks in
`wait`, because bash runs a trap only between commands, and a foreground child
would delay it until the child exits. On TERM, the trap does this, in order:

1. Ignores further TERM/INT and records `metadata.termination.reason`
   (`stop_request`, `cancel_request` or `scheduler`) with `received_at`.
2. Only for `stop_request`: runs `jarvis ppl stop yaml <runtime>/pipeline.yaml`
   (bounded by `stop_timeout`). This stops services in reverse order and
   finalizes their service runtimes.
3. TERMs the pipeline child and its descendants, waits up to `stop_grace`,
   KILLs whatever is left, then `wait`s on the child to reap it.
4. Records `stopped_at`, `stop_return_code` and `child_reaped`, then exits:
   `0` after a clean stop, `1` if `ppl stop` failed, `143` otherwise.
5. The `EXIT` trap finalizes the record. A live `cancel_request` commits
   `canceled`, a clean stop commits `completed`, and a failed stop commits
   `failed` with a "could not stop the pipeline cleanly" error.

The job log shows the order explicitly:

```
JARVIS: TERM received by the batch shell
JARVIS: termination reason=stop_request
JARVIS: ppl stop BEGIN
JARVIS: ppl stop END rc=0
JARVIS: pipeline child reaped
JARVIS: finalizing execution record (exit 0)
```

PBS and LSF have no backend in this release. `cancel`/`stop` against them
fail with a typed "not supported" error instead of guessing a command.

## Install jobs

`jarvis ppl install` checks or installs a pipeline's packages. Spack packages
are resolved find-first (see
[pipeline_tests.md → Installers](pipeline_tests.md#installers)). Run it as a
JARVIS-submitted one-node job so the build happens on a compute node under the
same sealed-execution safeguards as a run:

```bash
jarvis ppl install +submit [--execution-id ID] +json              # build missing
jarvis ppl install +check +submit +json                           # report only
jarvis ppl install +submit --answers answers.json +json           # re-run with answers
```

The job uses the pipeline's `scheduler:` block with `nodes: 1`. It keeps
`account`, `partition`, `time` and the other site keys, and drops `ntasks`,
`ntasks_per_node`, `gpus`, `gpus_per_node`, `gres` and `array`. The answers
file is sealed into `input/answers.json`. Inside the allocation the script
runs `jarvis ppl install yaml <runtime>/pipeline.yaml [+check] [--answers …]
+json` in place of `ppl run`. The execution record carries
`metadata.action: install` and a live `metadata.install` progress document
(`jarvis.install.progress.v1`), which `jarvis execution get +json` returns:

| Field | Meaning |
|-------|---------|
| `mode` | `install` or `check` |
| `phase` | `queued`, `starting`, `resolving`, `waiting_env_lock` (another operation holds a shared `JARVIS_SPACK_ENV_DIR`), `building`, `needs_decision`, `refused` (a build outside an allocation, see below), `failed`, `done` |
| `packages_total` / `packages_done` / `current_package` | position in the package list |
| `missing` | names Spack is building for the current package |
| `log` | `<execution>/install/install.log`: append-only, stream it while the job runs |
| `issues` | `<execution>/install/issues.jsonl`: questions, when `phase` is `needs_decision` |
| `record` | `<execution>/install/install-record.json`: final per-package result |

The job exits `3` when it needs a decision, or when `+check` finds packages
that still need building. The record is then `failed` with `return_code: 3`
and `metadata.install.phase: needs_decision`. Answer the questions, then
resubmit with `--answers`.

`ppl run` loads still run the install phase on load, as before. Set
`JARVIS_INSTALL_ON_LOAD=check` on the submitting process, or in the
scheduler's `pre_cmds`, to make a run job verify instead of building. It then
fails fast with exit 3 when something is missing. `skip` disables the phase.

Install and run jobs see `JARVIS_SPACK_ROOT`, `JARVIS_SPACK_CONFIG` and
`JARVIS_SPACK_ENV_DIR` through Slurm's default environment export
(`--export=ALL`). If the site exports nothing, set them in `pre_cmds`. The job
then calls the same
`<root>/bin/spack` with the same isolated configuration, and uses the
package-local `install_repos` (see
[pipeline_tests.md → Package Spack repos](pipeline_tests.md#package-spack-repos-install_repos)).
Each run job writes `<execution>/provenance.json` before it finalizes (see
[executions.md → Provenance record](executions.md#provenance-record)).

### Login nodes: no builds outside an allocation

A site login node may only do light, short work: submit, read records,
`spack find` and `spack spec`. Every `spack install`, environment concretize
and build has to run inside a JARVIS-submitted job. JARVIS enforces this at
the point where it would start heavy work, so a find-first run with nothing
to build is never refused:

| Command on the submitting host | What runs there |
|---|---|
| `ppl install +check` | `spack spec -I` / `spack find` only: allowed |
| `ppl install +submit` (with or without `+check`) | writes the snapshot and calls `sbatch`: allowed; the build runs in the job |
| `ppl submit yaml` with `JARVIS_INSTALL_ON_LOAD=check` | `spack spec -I` / `spack find`, then `sbatch`: allowed; exits 3 without submitting when something is missing |
| `ppl install` (no `+submit`) | refused with exit **4** if anything must be built or the env must be concretized |
| `ppl load` / `ppl run yaml` / `ppl submit yaml` with the default `install` policy | refused with exit **4** under the same condition, when the pipeline has a `scheduler:` block or `sbatch`/`qsub`/`bsub`/`flux` is on `PATH` |

A host is inside an allocation when `SLURM_JOB_ID`, `PBS_JOBID`, `LSB_JOBID`
or `FLUX_JOB_ID` is set. `jarvis ppl install +allow_local`, or
`JARVIS_ALLOW_LOCAL_BUILD=1` for install-on-load, opts in to building on the
current host (a workstation, or an interactive allocation the caller trusts).
The refusal happens before any `spack install`, `spack -e DIR concretize`,
container build, or pip/conda install. It is logged to `install.log`,
written to `install-record.json` as `refused`, and published as
`metadata.install.phase: refused` inside an execution. With `+json`, the
report has `refused: {kind: build_outside_allocation, what, packages,
exit_code: 4, message}`.

## Giving a package a subset of the allocation

Every package reads its hosts from `self.hostfile`. A package `host_slice:
"start:end"` narrows that list with Python slice semantics. The slice is
applied to the allocation hostfile when the package asks for it inside the job,
and it is also written to `<package shared_dir>/hostfile.slice_<start>_<end>`
so launchers can use `--hostfile`:

```yaml
scheduler:
  name: slurm
  nodes: 16
pkgs:
  - pkg_type: builtin.paraview
    mode: service             # start_mode auto -> background
    host_slice: "12:16"       # last 4 nodes
  - pkg_type: builtin.xcompact3d
    host_slice: "0:12"        # first 12 nodes
```

Packages start in pipeline order. A foreground package's `start()` blocks
until it returns. A package with `start_mode: background` runs `start()` in
a thread instead, and the next package starts once it is ready, so two
packages on disjoint slices run at the same time. `start_mode` is a common
option for every package:

| `start_mode` | Behaviour |
|--------------|-----------|
| `auto` (default) | background for `Service` packages, and for `builtin.paraview` with `mode: service`; foreground otherwise |
| `background` | always run `start()` in a thread and wait for readiness |
| `foreground` | always block on `start()` |

Ready means the package's `ready()` returns true. By default that's when its
service runtime (`JARVIS_SERVICE_RUNTIME_PATH`) reports `ready` or `degraded`.
A `start()` that returns also counts as ready. A `failed` or `stopped` report
before ready, a `start()` that raises, or `start_timeout` seconds (default
600, `0` for no limit) without readiness fails the pipeline start. After the
last package, `start()` waits until every background `start()` has returned.
A service therefore keeps the job alive until `jarvis execution stop` or the
time limit, even after the solver finishes.

For the viz-plus-solver demo above, the service comes first. pvserver starts and its supervisor reports `ready`. xcompact3d then runs on
its 12 nodes while pvserver serves on the other 4. `jarvis execution stop`
TERMs the batch shell. The trap runs `ppl stop`, then TERMs the pipeline child
and its whole process tree. That stops both the paraview supervisor (which
finalizes its service runtime as `stopped`) and the solver's `mpirun`, before
the record is finalized.

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
