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
| `name`             | (selects backend)       | `slurm`, or `auto` (also `detect`, and the default when `name` is omitted) to use the scheduler whose client is on `PATH` (`sbatch`, `qsub`, `bsub`, `flux`). The record keeps the resolved provider. No client gives `scheduler_missing`; a provider JARVIS lacks gives `scheduler_unsupported` (exit 5, `field: scheduler.name`) |
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

For a submitted nonterminal record, `Pipeline.get_execution()` (and so
`jarvis execution get|status +json`) also asks the selected scheduler
provider for current state. The SLURM provider runs `squeue --states=all`,
which keeps ended jobs visible for `MinJobAge` (it reads the exit code of an
ended job from `scontrol show job`), and then `sacct` when accounting is
available. A
provider-reported terminal state is committed into the same durable
execution record, with `metadata.scheduler_observation`
(`jarvis.scheduler.observation.v1`: `state`, `provider_state`, `basis`,
`accounting`, `diagnostic`, and `reconciliation` when JARVIS had to infer
the outcome). Generic query errors never terminalize the execution.

### Reconciliation without accounting

Sites without slurmdbd (no `sacct`) forget a job as soon as it leaves the
queue. A job that ended normally finalized its own record from the batch
script, so a record that is still open when the scheduler no longer knows
the job tells JARVIS what happened. After the startup grace period, JARVIS
commits one of these outcomes:

| Situation | Committed state | `reconciliation.basis` |
|-----------|-----------------|------------------------|
| a `cancel_request` or `stop_request` was recorded | `canceled` (rc 143) | `cancel_request` / `stop_request` |
| the record never left `submitted` and the job wrote no batch output | `canceled` (rc 143): removed while pending, for example by an external `scancel` | `vanished_pending` |
| the runtime started but never finalized | `failed` (rc 1): "never finalized the record (lost); scheduler accounting is unavailable" | `vanished_running` |

Within the grace period the record stays `submitted`.

### Queue facts

While a job is pending, `execution get|status +json` carries
`metadata.queue` (`jarvis.scheduler.queue.v1`):

| Field | Meaning |
|-------|---------|
| `partition`, `nodes`, `time_limit`, `time_limit_s` | what the job asked for |
| `reason` | Slurm's pending reason (e.g. `Resources`, `Priority`, `QOSMaxJobsPerUserLimit`) |
| `reason_kind` | `resources`, `limit`, `job_hold` or `other` |
| `estimated_start`, `starts_in_s` | the scheduler's start estimate, when it has one |
| `partitions[]` | every other partition that is `up`: `partition`, `default`, `available`, `idle_nodes`, `allocated_nodes`, `other_nodes`, `total_nodes`, `max_time`, `max_time_s`, `cpus_per_node`, `physical_cores_per_node`, `threads_per_core`, `memory_mb_per_node`, `gres`, and `fits` (enough idle nodes now and a long enough time limit), sorted with fitting partitions first |
| `move_supported` | whether `execution move` works for this provider |

When the job leaves the queue, the block stays with `state: left_queue`.

### Moving a pending job (`execution move`)

```bash
jarvis execution move <execution-id> --queue <partition> [--pipeline-id P] +json
```

This runs `scontrol update JobId=<job> Partition=<partition>` (adding
`--clusters` only when sbatch reported a cluster) and prints the record.
`metadata.move_request` (`jarvis.execution.control.v1`, `action: move`)
records `from_queue`, `to_queue`, the exact `command`, `status` and the
scheduler's `result`. A move is refused with exit **5** and a
`jarvis.refusal.v1` document whose issue has kind `execution_move_refused`,
`field: scheduler.queue` and a `details.reason`:

| `reason` | When |
|----------|------|
| `not_pending` | the job is already running or has ended |
| `invalid_queue` | no such partition |
| `does_not_fit` | the partition's node or time limits reject the job |
| `not_permitted` | the account may not use the partition |
| `not_scheduler` / `unsupported_provider` | a direct execution, or a provider without a move verb |
| `scheduler_unavailable` / `scheduler_rejected` | the query failed, or another rejection |

### Site facts before submit (`sched info`)

`jarvis sched info +json` reports the same partition facts for every
partition, before any job exists: node counts, time and node limits from
`scontrol show partition -o`, allowed accounts and QoS, the MPI candidates
an install would offer, the resource graph's network, the site skills, and
a `spack_missing` issue when JARVIS has no Spack. It runs only light reads,
so it is safe on a login node. See [sched.md](sched.md).

## Validation before submit (`ppl check`)

`ppl submit` and `ppl run` first run the same static check as
`jarvis ppl check`. On errors they refuse with exit 5 before writing a script
or calling `sbatch`. A blocking site-fact question (for example MPICH with
interface names that differ between nodes) refuses with exit 3, also before
any scheduler call. Advisory questions are submitted with the job and kept
in the record's `metadata.site_facts` (see [Site facts](site-facts.md)). See
[pipelines.md → Coupling and configuration checks](pipelines.md#coupling-and-configuration-checks-ppl-check).

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

The job uses the pipeline's `scheduler:` block, sized by JARVIS for a build.
It keeps `account`, `partition`, `time` and the other site keys. It drops
`ntasks_per_node`, `gpus`, `gpus_per_node`, `gres` and `array`, and sets
`nodes: 1` and `ntasks: 1`. A build (not `+check`) also gets `exclusive:
true`, because `spack install -j` defaults to all of the node's cores. A
`+check` stays a light single task. An optional `scheduler.install` mapping
overrides any directive for install jobs only. For example, it can send
builds to another partition with a longer limit. `nodes` there must be 1:

```yaml
scheduler:
  name: slurm
  nodes: 16
  partition: compute
  time: "01:00:00"
  install:                 # only for `ppl install +submit`
    partition: debug
    time: "00:45:00"
    mem: 0
```

The record's `metadata.install_job` shows the shape JARVIS submitted:
`nodes`, `ntasks`, `cpus_per_task`, `mem`, `exclusive`, `time`,
`partition`, `account`, `qos` (when set), and `overrides`, the keys taken
from `scheduler.install`. Run jobs ignore `scheduler.install`. The answers
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
| `issues` | `<execution>/install/issues.jsonl`: questions when `phase` is `needs_decision`, typed failures when it is `failed` |
| `open_issues`, `issue_kinds` | on `phase: failed`, the failure issues' `issue_id`s and kinds (for example `compiler_unavailable`, `build_failed`) |
| `record` | `<execution>/install/install-record.json`: final per-package result, with `build_compilers` (the compilers verified on the job's node) |

Because an install job's files live under its execution, the job also writes
`<pipeline shared>/install/last-install.json` (`jarvis.install-pointer.v1`)
with its `execution_id`, `phase` and the record, log and issues paths. A later
`ppl install +check +json` outside a job reports it as `last_install` (see
[Install records](pipeline_tests.md#install-records)). Decisions stay in the
pipeline's `<pipeline shared>/install/decisions.yaml` (or
`JARVIS_SPACK_DECISIONS`) for both, so an answer given to an install job is
replayed by the next check.

The job exits `3` when it needs a decision, or when `+check` finds packages
that still need building. The record is then `failed` with `return_code: 3`
and `metadata.install.phase: needs_decision`. Answer the questions, then
resubmit with `--answers`. When Spack itself fails (a compiler missing on the
node, a fetch, a build), the job exits `6`: the record is `failed` with
`return_code: 6`, `metadata.install.phase: failed`, and the typed issue in
`issues.jsonl` (see
[Install failures](pipeline_tests.md#install-failures)). Before it builds,
the job checks the registered compilers on its own node and keeps
login-only compilers, and the upstream specs built with them, out of the
build (see [spack.md → Compilers on the build host](spack.md#compilers-on-the-build-host)).
A job (or a local `+check`) on a host where
JARVIS finds no Spack at all asks a `spack_missing` question whose
`site_spacks[]` lists the Spack checkouts it found (see
[Missing Spack](pipeline_tests.md#missing-spack-spack_missing)). To chain a
JARVIS Spack to the site's upstreams, compilers and externals first, run
[`jarvis spack prepare`](spack.md); it never builds.

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
| `sched info` | `sinfo`, `scontrol show partition`, `spack find`: allowed |
| `spack prepare` | config writes, `spack compiler find` / `external find`, a shallow clone when the root is missing; never a build: allowed (run it in a job where even that is too much) |
| `ppl install +submit` (with or without `+check`) | writes the snapshot and calls `sbatch`: allowed; the build runs in the job |
| `ppl submit <file>` with `JARVIS_INSTALL_ON_LOAD=check` | `spack spec -I` / `spack find`, then `sbatch`: allowed; exits 3 without submitting when something is missing |
| `ppl install` (no `+submit`) | refused with exit **4** if anything must be built or the env must be concretized |
| `ppl load` / `ppl run yaml` / `ppl submit <file>` with the default `install` policy | refused with exit **4** under the same condition, when the pipeline has a `scheduler:` block or `sbatch`/`qsub`/`bsub`/`flux` is on `PATH` |

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
