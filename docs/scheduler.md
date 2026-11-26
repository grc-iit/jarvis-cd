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

### Planning a submission (`+plan`)

```bash
jarvis ppl submit pipeline.yaml --execution-id E +plan +json   # one jarvis.submit-plan.v1 document
jarvis ppl submit pipeline.yaml --execution-id E --plan_sha256 <plan_sha256> +json
jarvis ppl install [+check] +submit --execution-id E +plan +json   # the install job
jarvis ppl install [+check] +submit --execution-id E --plan_sha256 <plan_sha256> +json
```

`+plan` resolves everything a real `+submit` would and prints it, without
submitting. It writes no execution record or script, makes no scheduler call,
and runs no Spack, even where a consented `login_limited` check would run at
submit (the plan's `install.at_submit` says so instead). That keeps it light
enough for a login node, so a caller can show the job and ask before
submitting. A resource-graph collection job is a pipeline with
`builtin.resource_graph`, so it plans the same way.

| Field | Content |
|-------|---------|
| `schema_version` | `jarvis.submit-plan.v1` |
| `pipeline_id`, `execution_id`, `action` | `action` is `run`, `install` or `install_check` |
| `scheduler`, `partition`, `account`, `qos`, `time`, `time_limit_s`, `job_name`, `nodes` | the directives the script carries, and the total nodes |
| `sources` | where each directive came from: `pipeline.scheduler`, `pipeline.scheduler.install` (an install job's override), `jarvis` (set by JARVIS, such as an install job's `nodes: 1`), `jarvis_estimate` (an install job's `time` derived from the build estimate, see [Install job wall time](#install-job-wall-time)), or `unset`. JARVIS has no site-level directive defaults, so these come only from the pipeline's `scheduler:` block |
| `components[]` | one per package: `id`, `package`, `label`, `nodes`, `ranks`, `host_slice`, `node_range` `{first, end}`, `role` (`service`/`batch`), `starts` (`background`/`foreground`), `after_steps`, `ports`, `readiness`. An install job has one component `install` (package `jarvis.install`, 1 node, `packages[]`) |
| `couplings[]` | `{from, to, kind, label, source}`: the pipeline's [`couplings:`](pipelines.md#couplings-between-packages) (`source: pipeline`) plus interceptor edges (`source: derived`) |
| `script` | `{path, text, sha256}`: the exact batch script the submit will send |
| `input_sha256` | the digest of the sealed execution input (the pipeline snapshot, environment and install answers) |
| `plan_sha256` | sha256 of `{schema, script_sha256, input_sha256}`; the value to submit with |
| `issues`, `exit_code`, `would_submit` | what the submit would refuse with: `ppl check` errors (5), blocking site-fact questions (3), install decisions such as a placement question (3), or a build outside an allocation (4) |
| `install` | `policy`, `runs_in`, `status`, the recorded `check` (when one matched), `placement`, and `at_submit`/`at_submit_limits` for a consented login check |
| `install_job` | for an install job, the 1-node shape (`nodes`, `ntasks`, `exclusive`, `partition`, `account`, `time`, `overrides`) and `time_estimate`, how the wall time was chosen (see [Install job wall time](#install-job-wall-time)) |
| `execution_root`, `hostfile`, `component_states` | where the record, the hostfile and [`components.jsonl`](executions.md#component-states-componentsjsonl) will be |
| `warnings` | the `ppl check` warnings, plus the `cores.meaning` sentence when the ranks claim more cores per node than the partition has |
| `cores` | for a Slurm run, what the request claims per node in physical cores (`jarvis.submit-cores.v1`, see [Whole cores per rank](#whole-cores-per-rank)) |
| `submit_with`, `binding` | the bound submit command line, and how the binding works |

The command exits with `exit_code`, so a caller handles a plan's exit 3 the
way it handles a submit's.

**Binding.** A submit with `--plan_sha256` re-renders the submission and
refuses with exit **5** (`jarvis.refusal.v1`, issue kind
`submission_plan_changed`, with the expected and actual shas in `details`)
unless the script and the sealed input match. It checks before it creates
the execution record, and again once the script is written. Then it records
`metadata.submission_plan` (`jarvis.submit-plan.binding.v1`: `script_sha256`,
`input_sha256`, `plan_sha256`, `expected_plan_sha256`, `bound`). Plan and
submit with the same `--execution-id` and from the same working directory,
since the sealed input records it. The submit can name the YAML or use the
current pipeline: directive order is canonical (sorted), so it doesn't depend
on how the pipeline was loaded. A changed pipeline, time limit, answers file
or environment between the plan and the submit refuses, so a person's yes
covers only the job they were shown.

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
in the record's `metadata.site_facts` (see [Site facts](site-facts.md)).
`site_facts.net_plan` says what the MPI launch will do about the network. A
multi-node job on a site whose graph has no net facts for its nodes (for
example right after a site clean) gets the advisory `net_not_collected`: the
launch [collects the facts](resource-graph-network.md#collected-at-launch)
inside the allocation before it starts MPI, and refuses rather than launching
blind if a host returns none. See
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
   finalizes their service runtimes. Each package it stops is listed in
   `<runtime>/stopped-packages.json`, and the pipeline child's own stop (a
   `ppl run` stops its packages once `start` returns) skips those, logging
   `already stopped by this execution's stop request`, so no package's
   `stop()` runs twice. The stops can also overlap: once the trap stops a
   service, the service's `start()` returns and the child begins its own
   teardown while the trap's `stop()` is still running. So before calling a
   package's `stop()`, every stop in the execution (the trap's `ppl stop`,
   the child's stop and the after_steps stop) claims the package
   with an exclusive create of `<runtime>/stop-claims/<pkg_id>`. A stop that
   finds the claim taken skips the package, logging `already being stopped by
   another stop in this execution`. A failed `stop()` keeps its claim and is
   reported through `ppl stop`'s return code; it is not retried.
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

Compilers can differ between compute nodes, and an install checks only
the node its job lands on (see
[spack.md → Compilers on the build host](spack.md#compilers-on-the-build-host)).
To build on nodes known to have the compiler a build needs, pin the job
with `scheduler.install.nodelist` (or `constraint`), and add
`exclusive: false` if those nodes are shared.

#### Install job wall time

An explicit `scheduler.install.time` is used as given. `scheduler.time` is
the run job's limit: an install job does not inherit it. Otherwise JARVIS
always sets the install job's `time` from its own build estimate, never
leaving it to the partition's default, which can be too short, or `MaxTime`
when `DefaultTime=NONE`:

```
time = ceil((startup + estimate) × factor / 5 min) × 5 min, capped at MaxTime
```

- `estimate` is the time for what is still missing. For Spack packages it
  is the install check's estimate plus the build: a package the recorded
  install check (`install-check.json`) found installed counts as nothing;
  a missing one uses Spack's measured per-node times for the nodes still
  missing (`node_build_seconds` in `install-costs.json`), else the measured
  `build_seconds` of a previous build, scaled to the nodes still missing,
  or else the per-node estimate the placement question uses. Nodes a
  previous attempt already built are subtracted. A check record whose only
  changed inputs are the recorded decisions (answers given after the check,
  often to its own questions) or `spack.lock` still counts, and
  `estimate.check_record.stale` names them. pip, conda and container installs get a
  fixed allowance (15, 30 and 20 min; 1 min for a `+check`). When a
  package has neither a usable check record nor a measured build, and is
  not a known-heavy package, it gets a default build allowance of 1 h
  (`JARVIS_INSTALL_UNKNOWN_BUILD_SECONDS`); it is listed in
  `estimate.allowance`, and the plan's `why` says to run
  `ppl install +check +submit` first so JARVIS counts the missing nodes.
- `startup` is 10 min (`JARVIS_INSTALL_STARTUP_SECONDS`) and `factor` is
  1.5 (`JARVIS_INSTALL_TIME_FACTOR`, at least 1).
- `MaxTime` is the partition's limit, read with `scontrol show partition`
  (the job's `partition`, or the default partition). When only the factor
  pushes the time past it, the time is set to `MaxTime` and
  `capped_at_max_time` is recorded.
- When `startup + estimate` alone is past `MaxTime`, JARVIS does not cut the
  build short. The plan and the submit stop with exit 3 and an
  `install_time_exceeds_partition` issue (`field: scheduler.install`). Its
  candidates are the partitions whose `MaxTime` fits, setting
  `scheduler.install.time` to `MaxTime` to build in parts, and re-running
  the install check.

When an earlier install job of the pipeline ended without finishing (for
example it timed out, which kills it before it can update its record), the
resubmit reports it on stderr and in `time_estimate.previous_attempts`
(execution, state, phase, the nodes it built) and `already_built`. Spack
keeps what was built, so those nodes are not rebuilt and are not counted in
the estimate. The document (`jarvis.install.walltime.v1`) is in the submit
plan's `install_job.time_estimate` and the record's
`metadata.install_job.time_estimate`, with `source` (`explicit` or
`jarvis_estimate`), `time`, `formula`, `why`, `estimate`,
`partition_limit`, `previous_attempts`, `already_built` and `issue_kinds`.

The record's `metadata.install_job` shows the shape JARVIS submitted:
`nodes`, `ntasks`, `cpus_per_task`, `mem`, `exclusive`, `time`,
`partition`, `account`, `qos` (when set), `overrides`, the keys taken
from `scheduler.install`, and `time_estimate`. Run jobs ignore `scheduler.install`. The answers
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
| `issues` | `<execution>/install/issues.jsonl`: questions when `phase` is `needs_decision`, `failure` records when it is `failed` |
| `failures`, `open_issues`, `issue_kinds` | on `phase: failed`, the failure issues' `issue_id`s (both lists) and kinds (`failure` for anything JARVIS ran that failed, or `compiler_unavailable`) |
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
resubmit with `--answers`. When something the job runs fails (a concretize,
a build, the compiler probe, a Spack check), the job exits `6`: the record is
`failed` with `return_code: 6`, `metadata.install.phase: failed`, and the
`failure` record (the command, node, exit code and full output) in
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
`ppl submit` seals the submitter's `JARVIS_INSTALL_ON_LOAD` into the run
command (`env JARVIS_PIPELINE_SNAPSHOT_DIR=… JARVIS_INSTALL_ON_LOAD=check …
ppl run yaml …`), so the job applies the same policy even where the scheduler
exports no environment. For a scheduled pipeline the phase does its Spack work
only inside the job; on the submitting host it reads the recorded check (see
[Light checks on the submitting host](#light-checks-on-the-submitting-host)).

Inside a run job the load's install phase mirrors its progress into the
execution record's `metadata.install` (`jarvis.install.progress.v1`, with
`trigger: install_on_load`, `mode` = the policy, `phase` `starting` →
`resolving`/… → `done`, `needs_decision`, `refused` or `failed`, plus
`open_issues`, `issue_kinds`, `failures` on `failed`, `log`, `issues` and
`check_record`). A run job
that stops on an install question (exit 3) or a failure (exit 6)
therefore reports it the way an install job does.

### Light checks on the submitting host

Concretizing (`spack spec -I`) can hold a login node's CPU for minutes. So
for a pipeline with a `scheduler:` block, outside an allocation, JARVIS runs
**no Spack command** in the install phase unless the site's
[install placement](#install-placement-where-checks-and-builds-run) says so:
not for `ppl install +check`, not for `ppl install`, and not for the
install-on-load of `ppl load` / `ppl submit`. It answers from the record of
the last full check instead. Without a matching record, the install
placement decides whether the full check runs here (niced and bounded) or in
a job, and asks the person when the site has not chosen yet.
Spack otherwise runs only inside an allocation (`SLURM_JOB_ID`, `PBS_JOBID`,
`LSB_JOBID` or `FLUX_JOB_ID`). `+allow_local` or `JARVIS_ALLOW_LOCAL_BUILD=1`
opts back in to an unlimited full check on the current host. A pipeline
without a scheduler block is unchanged.

Every full check or install (in an install job, in a run job's
install-on-load, a `login_limited` check on the submitting host, or a local
`+allow_local` run) ends by writing
`install-check.json` (`jarvis.install-check.v1`) next to the env capture:
`<pipeline shared>/install/install-check.json`, or
`$JARVIS_SPACK_ENV_DIR/install-check.json` when that is set. It holds:

- `checked_at` and `where` (`host`, `allocation`, `execution_id`);
- `mode` and `outcome` (`satisfied`, `missing`, `needs_decision` or
  `failed`);
- the full `issues[]` and `issue_kinds`, and the per-package summary
  (`status`, `spec`, `root`, `missing`, `mpi`) with `mpi_env`;
- the `inputs` digests the env capture uses (packages' `install*` settings
  as written, `decisions.yaml`, the Spack root and config, `spack.lock`), and
  `watched_paths` (the root prefixes it saw);
- the install `log` and `record` paths, and `placement` (the
  `login_limited` choice, host measurements, limits and usage) when the
  submitting host ran it.

A light check applies the record only while its inputs match and its watched
paths exist. A `failed` outcome is never replayed: it counts as unchecked. An
answer recorded in `decisions.yaml` changes the `decisions` digest, so the
next light check is unchecked until a job checks again. Install placement
answers are left out of that digest: they decide where a check runs, not
what it finds. A site-copy answer that the check applied to a package's
config (`install_site_choice`, and for a site copy `install_method: site`
and `install_site`) is undone in the `packages` digest, and a method choice
counts by its method only (not its host-specific `why`): the answer itself
is the `decisions` input, so a `+plan` or wall-time estimate on the
submitting host matches the record the job wrote. The report's `check`
block (also in `phase_timings.env_activation.check`) says what happened:
`{where: recorded, light: true, host_reason, record, source (install_check |
env_capture | null), outcome, checked_at, checked_where, reason, missing,
pending_answers}`; after a check on the submitting host it is `{where:
login_limited, light: false, limits, login_host, usage, record}`.

The rows below are for a site whose check placement is `compute_job` (or a
login host too busy now). With no placement chosen yet, the "no matching
record" and `--answers` rows ask an `install_placement` question first
(exit 3, nothing submitted). With `login_limited`, they run the full check
here under limits instead, and continue from its outcome as in the
"record …" rows.

| Recorded state | `ppl install +check` | `ppl install` (no `+submit`) | install-on-load at `ppl load` / `ppl submit` |
|---|---|---|---|
| record (or fresh capture) satisfied | exit 0, `satisfied` | exit 0 | proceeds; `activation.method: deferred` |
| record with open questions | the same issues again, exit **3** | exit **3** | exit **3** before `sbatch` (ask before submit) |
| record with only `not_installed` | the `not_installed` issues (plus a build placement question while none is chosen), exit **3** | build placement `login_limited`: builds here under limits; `compute_job`: refused, exit **4**; unchosen: exit **3** | `check`: exit **3**; `install`: proceeds, the job builds |
| no matching record | `install_unchecked`, exit **3** | refused, exit **4** | proceeds; the job checks at load |
| `--answers` for recorded questions | `install_unchecked` with `reason: answers_pending`, exit **3** | the same | the same |

When the install phase stops `ppl submit <file> +json` before `sbatch` (exit 3
or 4), stdout carries the same `jarvis.install` report that `ppl install
+check +json` prints, with `submitted: false`; load logs go to stderr.

`install_unchecked` (`scope: site`, `subject: install_check`, not answerable)
carries `packages`, `reason` (`no_record`, `inputs_changed`,
`installed_path_gone`, `last_check_failed`, `unreadable_record` or
`answers_pending`), `record`, and `remedy[]`:

- `jarvis ppl install yaml FILE +check +submit`: a full check in a 1-node job,
  which writes the record;
- `jarvis ppl install yaml FILE +submit`: check and build what is missing;
- `jarvis ppl submit yaml FILE`: the run job checks (and under `install`
  builds) at load.

Answers are validated against the recorded candidates on the submitting host
but applied and recorded only by a check that runs Spack, so pass them to the
job: `jarvis ppl install yaml FILE +check +submit --answers answers.json`.

When `ppl submit` defers the check, it prints an advisory and records it in
the execution's `metadata.install_on_load`: `{policy, runs_in: allocation,
submit_host: {source, outcome, reason, record, missing}, advisory}`. The job's
own result then appears in `metadata.install`.

### Install placement: where checks and builds run

Where a full check (`spack spec -I`) or a build may run is a per-cluster,
and sometimes per-package, decision. Some sites' documentation recommends
the login node, others compute nodes, and a 20-minute build differs from a
4-hour one. JARVIS hard-codes no site. It offers the choice to the person as
a typed `install_placement` question, with candidates, measured evidence and
a recommendation, and records the answer in the site's decisions store
(`JARVIS_SPACK_DECISIONS`, see [spack.md](spack.md)).

The placement applies only on the submitting host of a scheduled pipeline
(outside an allocation). Inside a job the check and build simply run there.

**Choices** (candidate `id` = `constraint`):

| Choice | What it does |
|---|---|
| `login_limited` | runs the full check (or the build) on the submitting host now, niced and bounded (see limits below), so questions such as the MPI come back without a queue wait |
| `compute_job` | today's path: the run job checks at load, or `jarvis ppl install yaml FILE +check +submit`; builds go to `jarvis ppl install yaml FILE +submit` |
| `ask_each_time` | keeps no standing choice: every time, an `…occasion` question (only `login_limited` / `compute_job`) is asked, and its answer applies to that run only |

**Keys.** A site answer has the subject `install_placement.<phase>`
(`phase`: `check` or `build`). It is one decision per site and phase, and it
covers every package, including packages of later pipelines. So one exit-3
round asks at most one check question and one build question per site, and
the question lists every pending package with its estimate. The cost class
(`short` / `long`) and the per-package estimates shape the recommendation and
the evidence (`cost_class` is the costliest package's class, `cost_classes`
all of them), not the key. To place one package otherwise, answer its
`package_overrides` issue: a package override has the subject
`install_placement.<phase>` under the package's Spack name (`scope:
package`), and it beats the site answer for that package. Resolution order:
package decision → package answer → site decision → site answer →
`JARVIS_INSTALL_PLACEMENT_CHECK` / `JARVIS_INSTALL_PLACEMENT_BUILD` (a
caller's default, `login_limited` or `compute_job`) → question. One check
covers every package in the pipeline. So it runs here only when every
package resolves to `login_limited`; any `compute_job` sends the check to a
job. Under `ask_each_time` the per-run question is
`install_placement.<phase>.occasion`.

When a site's `login_limited` build answer covers a `long` build, the
package's placement carries a `note` (also logged) naming its override
issue, so the person can send that one build to a job.

**Answers recorded before the per-phase key.** jarvis-cd 58cd5b1a and
earlier recorded one site answer per cost class
(`install_placement.<phase>.short` / `.long`). They still count:

- when they agree (or only one exists), they cover every package, the same
  as a per-phase answer;
- when they differ, each still covers its own class, so nothing the person
  answered is asked again;
- an answers file keyed by the old per-class `issue_id`s still applies.
  Agreeing answers are recorded under `install_placement.<phase>` with
  `rekeyed_from` (the old id);
- `--forget install_placement.<phase>` also forgets the per-class entries.

**Cost estimate** per package (shown in the question and its evidence):

- a prior measurement at this site, from `install-costs.json`
  (`jarvis.install-costs.v1`) next to the decisions store. Every full check
  records `check_seconds` (with `check_host`, `checked_at`), and every build
  records `build_seconds` (with `build_host`, `built_at`, `built_nodes`) and,
  for each Spack node it built, `node_build_seconds` (Spack's own
  `<prefix>/.spack/install_times.json`; binary-cache installs are skipped).
  A build through the pipeline's Spack environment records `build_seconds`
  as the install's wall clock when it built one package, else each
  package's measured nodes;
- for a build with known missing nodes, the nodes built here before count
  at their measured `node_build_seconds` and the rest at the per-node
  heuristic below; with none measured, a prior `build_seconds` of the
  package is used whole;
- otherwise, for a build: the check's missing Spack nodes × 60 s, with
  known-heavy nodes (`paraview`, `vtk`, `qt`, `llvm`, `gcc`, `trilinos`, …)
  at 1800 s, except `llvm` and `gcc` (3600 s), `paraview` (3000 s) and
  `mesa` (600 s). These are calibrated on a measured ParaView 5.13.1 build
  of 62 nodes: 59 ordinary nodes averaged 34 s, llvm took 3276 s, paraview
  2698 s and mesa 116 s;
- otherwise a known-heavy package (600 s check, 4 h build), or a default
  (60 s check; an unknown build counts as long).

The class is `short` up to `JARVIS_PLACEMENT_SHORT_CHECK_SECONDS` (300) or
`JARVIS_PLACEMENT_SHORT_BUILD_SECONDS` (1800), else `long`.

**Recommendation** (for the costliest pending package, which
`recommendation_reason` names when there are several). The first that
applies:

1. a constraint the site recorded for this phase (`login_limited` or
   `compute_job`): this home's decision, else the resource graph's
   `install_placement` answer (see Confirmed answers in the resource graph,
   below). The reason gives who
   answered, when, the answer's `basis`, and where it is recorded. When the
   login host is busy, the reason adds that a `login_limited` run defers to
   a compute job this time;
2. the other phase's recorded answer, where cost orders the phases: a check
   recorded as `compute_job` recommends `compute_job` for builds (a build is
   at least as heavy), and a build recorded as `login_limited` recommends
   `login_limited` for checks;
3. otherwise cost and load:
   - when the login host is busy now (see `defer` below): `compute_job`;
   - a short check: `login_limited` (it saves a queue wait);
   - a long check: `compute_job`;
   - a short build: `compute_job` until the site has answered
     `login_limited` for builds. Login builds stay an option the person
     picks, because many sites' admins object to compiling on login nodes;
   - a long build: `compute_job`.

`ask_each_time` is not a constraint, so it never becomes a recommendation.
The same rules give the "this time" question (`once`) and every package
override its recommendation. The question still offers every choice: a
recorded answer is a recommendation with its basis, never applied for a
phase the person has not answered here.

The question carries `recommended`, `recommendation_reason` and
`recommendation_basis` (`source`: `site_decision`, `resource_graph`, either
with the other phase in brackets when inferred (`inferred_for`), or
`heuristic`; plus `constraint`, `answered_by`, `answered_at`, `basis`,
`issue_id`, `subject`, `graph`). Each `package_overrides[]` entry carries
its own `question`, `candidates`, `recommended`, `recommendation_reason` and
`recommendation_basis`, from that package's estimate.

**Confirmed answers in the resource graph.** A person's per-phase site
answer of `login_limited` or `compute_job` is also written to JARVIS's own
resource graph (in the site home when `JARVIS_RESOURCE_GRAPH` names a site
graph, else the root's graph; the site graph itself is never written) as a
typed `install_placement` section, so a fresh home that reads the same graph
(a new user, a new relay home, or after its decisions were cleaned) is
recommended the site's answer:

```yaml
install_placement:
  schema_version: jarvis.resource-graph.install-placement.v1
  check:
    constraint: compute_job
    basis: "site docs: no builds or long concretizations on login nodes"
    answered_by: person
    answered_at: "2026-11-25T07:23:00Z"
    issue_id: "sha256:..."
    recorded_in: /home/me/.jarvis-site/decisions.yaml
```

- A site publishes its answers by publishing a graph that carries the
  section through the usual site graph path, and homes point
  `JARVIS_RESOURCE_GRAPH` at it. JARVIS reads the site's answers (marked
  `site_graph: true`) together with its own; per phase the newer one wins.
- Answers recorded before the graph held them are copied in on the next
  placement resolve. A newer answer already in either graph is kept.
- `ask_each_time` and "this time" answers are not written.
- `rg build`, `rg load` and `rg net set|clear` keep the section.
- `--forget install_placement.<phase>` also drops JARVIS's own graph copy of
  that same answer (same `issue_id` and `answered_at`); someone else's
  answer, and any answer in the site graph, stays. The report lists the site
  graph's answers it kept under `site_graph_kept`.
- Graph answers are never standing answers: with no decision in this home,
  the question is asked and the graph answer is its recommendation.

The reason is in `recommendation_reason`.

**Evidence** in the question:

- `login_host`: host, `cores` (affinity), `load` (1/5/15 min),
  `total_mb`/`available_mb`, the person's `own_processes` (`count`,
  `running`, `rss_mb`), and any cgroup v2 CPU/memory limit on the session
  (`cgroup`);
- `limits`: what a `login_limited` run would get right now;
- `queue`: for Slurm, `sbatch --test-only --nodes=1 --time=30` with the
  pipeline's partition/account/qos gives `start_at`/`wait` (one light
  scheduler call). Other schedulers are reported as not measured;
- `site_policy`: the recorded placement answers (`recorded`), the
  resource graph's confirmed answers (`graph`) and the discovered cgroup;
- `packages` (each with its `estimate` and `override_issue_id`) and
  `package_overrides[]`.

An agent may read the site's documentation, propose a choice and pass the
source as the answer's `basis`. The person confirms, and JARVIS records
`basis` with the answer.

**Limits** (measured when the check runs; env knobs in brackets):

- `jobs` = clamp(⌊free cores × 0.25⌋, 1, 4), where free cores = cores (or
  the cgroup CPU limit) − the 1-minute load
  [`JARVIS_LOGIN_CHECK_SHARE`, `JARVIS_LOGIN_CHECK_MAX_JOBS`]. It is applied
  as `spack -c config:build_jobs:<jobs>` and as `OMP_NUM_THREADS` /
  `OPENBLAS_NUM_THREADS` / `MKL_NUM_THREADS`.
- Priority: `nice 19` and `ionice -c 3` (idle I/O) on every Spack process.
- `memory_mb` = clamp(free MB × 0.25, 4096, 8192), as an address-space
  limit (`RLIMIT_AS`) on each Spack process [`JARVIS_LOGIN_CHECK_MEM_SHARE`,
  `JARVIS_LOGIN_CHECK_MIN_FREE_MB`, `JARVIS_LOGIN_CHECK_MAX_MEM_MB`].
- `defer` (with `defer_reasons`): fewer than 1 free core, less than 4096 MB
  free, or 8+ of the person's processes running
  [`JARVIS_LOGIN_CHECK_MIN_FREE_CORES`, `JARVIS_LOGIN_CHECK_MAX_OWN_RUNNING`].
  The work then takes the `compute_job` path for this run, and the report's
  `placement.fallback` says `login_busy: …`.
- No hard time limit (`time_limit: null`). The person can stop it (Ctrl-C,
  or kill the jarvis process); the record is written only when the check
  finishes.

The report's `placement` block (`jarvis.install-placement.v1`) carries:

- `check` / `build`: `{choice, packages: {pkg_id: {spec_name, estimate,
  choice, source, build_command}}, questions}`;
- `ran_here`: `{where: login_limited, why, limits, login_host, queue,
  check_usage / build_usage}`. Usage is `wall_seconds`, `cpu_seconds`,
  `cpu_share_of_one_core` and `peak_child_rss_mb`;
- `fallback` and `evidence`.

The same is recorded as `install-check.json`'s `placement`.

**One concretization per submit.** A `login_limited` check writes
`install-check.json`. The `ppl submit` load that ran it, and a later `ppl
install +check +json` (the report after exit 3), read that record without
another Spack call.

**Contract for a caller** (e.g. CLIO relay):

1. Run `jarvis ppl submit FILE +json` or `jarvis ppl install yaml FILE
   +check +json` as before.
2. Exit 3 with `kind: install_placement` issues in the `jarvis.install`
   report means the person chooses first. Relay the `question`,
   `candidates` (with `recommended` and `does`), `recommendation_reason`,
   `evidence`, `packages` and `package_overrides`.
3. Answer in the same answers file: `{"<issue_id>": "login_limited"}` or
   `{"<issue_id>": {"constraint": "compute_job", "answered_by": "…",
   "basis": "…"}}`. An override's `issue_id` comes from `package_overrides`.
4. Re-run the same command with `--answers` (or `JARVIS_INSTALL_ANSWERS`).
   The answer is recorded per site; later runs do not ask again, except
   under `ask_each_time`.
5. To change a choice: `jarvis ppl install --forget install_placement.check
   +json` (a site subject, an `issue_id`, or the package name for an
   override). The next run asks again.

`JARVIS_INSTALL_ON_LOAD=check` stays the caller's setting.

### Captured load environment

Resolving and loading Spack specs inside every run job can take minutes. So
an install that ends with every package satisfied (a `ppl install`, an
install job, or an install-on-load) captures the environment `spack load`
produced, once, in `<pipeline shared>/install/spack-load-env.json`
(`jarvis.spack-load-env.v1`). It holds `captured_at`, `captured_in` (the
install's execution id), the per-package `hashes`, the `inputs` digests, the
`env_delta` (path variables as `prepend` lists, others as `set`), the MPI
environment, the `watched_paths` (the prefixes it prepended), and the install
record and result. With `JARVIS_SPACK_ENV_DIR` set, the capture lives in that
shared env dir instead (`$JARVIS_SPACK_ENV_DIR/spack-load-env.json`), so every
pipeline and operation sharing the env reuses one capture; its inputs must
still match.

A run's install phase, under both the `install` (default) and `check` policies,
then applies the capture with no Spack call, if it is still fresh. It is
stale, and the run falls back to `spack spec`/`spack load` and writes a new
capture, when:

- any input changed: the packages' `install*` settings, `decisions.yaml` (or
  `JARVIS_SPACK_DECISIONS`), the Spack root or `JARVIS_SPACK_ROOT` /
  `JARVIS_SPACK_CONFIG`, or, with a Spack env, its `spack.lock`;
- a watched path no longer exists (a package was uninstalled or moved);
- the schema differs, or the file is missing or unreadable.

`JARVIS_SPACK_ENV_CAPTURE=off` disables capture and its use. `ppl install`
itself always resolves and captures again. `ppl install +check +json`
reports `env_capture` (`path`, `fresh`, `captured_at`, `stale_reason`).

The run records which path it took as the install report's `activation`,
and in the execution's
[`metadata.phase_timings.env_activation`](executions.md#phase-timings):
`method` (`captured`, `spack_load` or `none`), `seconds`, `policy`, and:

- for `spack_load`: `resolve_seconds` (the find-first `spack spec`/`spack
  find`), `spack_load_seconds`, the capture's `stale_reason`, and
  `capture_written` (the new capture's path);
- for `none`: `why_not_loaded`, e.g. `policy skip`, `open install issues`,
  `no package is installed yet` or `no Spack packages`.

A long `env_activation` with `method: spack_load` therefore says whether the
time went to resolving or to `spack load`, and the `stale_reason` says why
the capture was not used.

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

A site login node may only do light, short work: submit, read records and
light scheduler queries. Every `spack spec`, `spack install`, environment
concretize and build has to run inside a JARVIS-submitted job. For a
scheduled pipeline the install phase makes no Spack call outside an
allocation (see
[Light checks on the submitting host](#light-checks-on-the-submitting-host)).
Builds are also refused at the point where heavy work would start:

| Command on the submitting host | What runs there |
|---|---|
| `ppl install +check` | reads `install-check.json`; no Spack call. With no matching record and placement `login_limited`: the full check here, niced and bounded ([Install placement](#install-placement-where-checks-and-builds-run)). Without a scheduler block (a workstation): `spack spec -I` / `spack find` |
| `sched info` | `sinfo`, `scontrol show partition`, `spack find`: allowed |
| `spack prepare` | config writes, `spack compiler find` / `external find`, a shallow clone when the root is missing; never a build: allowed (run it in a job where even that is too much) |
| `ppl install +submit` (with or without `+check`) | writes the snapshot and calls `sbatch`: allowed; the check and build run in the job |
| `ppl submit <file>` with `JARVIS_INSTALL_ON_LOAD=check` | reads the record, then `sbatch`; exits 3 without submitting when the record has open questions or missing packages; with no matching record: placement `login_limited` checks here first (limited), `compute_job` submits and the job checks, unchosen asks an `install_placement` question (exit 3) |
| `ppl install` (no `+submit`) | exit 0 when the record is satisfied; builds here only under build placement `login_limited`; otherwise refused with exit **4** (or exit 3 for recorded or placement questions), before any Spack call |
| `ppl load` / `ppl run yaml` / `ppl submit <file>` with the default `install` policy | reads the record; exit 3 for recorded questions, otherwise proceeds and the job checks and builds. Without a scheduler block on a host with `sbatch`/`qsub`/`bsub`/`flux` on `PATH`, it resolves locally and is refused with exit **4** if anything must be built |

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
600, `0` for no limit) without readiness fails the pipeline start.

### Services once the last step returns (`after_steps`)

A *step* is a foreground package that isn't a `Service` or `Library` (the
solver). When the last step returns OK, each background package is handled
by its `after_steps` option (a common option for every package):

| `after_steps` | Behaviour |
|---------------|-----------|
| `auto` (default) | `stop` if a step comes after the package in the pipeline (it served those steps, and they are done); `keep` if it is listed after the last step, or the pipeline has no step (the service is the pipeline's purpose) |
| `stop` | call its `stop()` right away (newest first), join its `start()` thread for up to 120 s, and let the run finalize `completed` |
| `keep` | hold the job until its `start()` returns: `jarvis execution stop`, cancel, or the time limit |

A stopped package is not stopped again by the run's `ppl stop`. A `start()`
that raises because `stop()` ended it doesn't fail the run (it's recorded as
`start_error_after_stop`). The decisions are recorded in
[`metadata.phase_timings.services_after_steps`](executions.md#phase-timings).
If a step fails, nothing is stopped early: the run's normal stop and failure
path applies.

### A service that crashes

A service that fails before it is ready fails the start, as any package
does. Once it is ready, a crash depends on whether it served the steps:

| The service | When it crashes | The run |
|-------------|-----------------|---------|
| `after_steps` `stop` (it served the steps) | while the steps run | the steps keep running; when the last step returns OK the run completes (rc 0). The service's component is `failed` (`service crashed: ...`), its decision is `crashed`, and its `stop()` still runs once so it can clean up (an error there is kept as `stop_error_after_crash`) |
| `after_steps` `keep` (it is the pipeline's purpose) | at any time | the run fails, as before |

Either way the run records the application's result and each service's
result separately, in [`metadata.outcomes`](executions.md#outcomes). A
restart policy, if a package has one, is the package's own: its `start()`
only raises once it gave up.

So in a pilot where pvserver starts before xcompact3d, the execution ends
`completed` when xcompact3d returns, instead of holding the allocation until
its wall time. To keep the viewer up after the solver, set `after_steps: keep`
on it, or list it after the solver.

For the viz-plus-solver demo above, the service comes first. pvserver starts and its supervisor reports `ready`. xcompact3d then runs on
its 12 nodes while pvserver serves on the other 4. When xcompact3d returns,
`after_steps: auto` stops pvserver and the execution completes. Before then,
`jarvis execution stop` TERMs the batch shell. The trap runs `ppl stop`, then TERMs the pipeline child
and its whole process tree. That stops both the paraview supervisor (which
finalizes its service runtime as `stopped`) and the solver's `mpirun`, before
the record is finalized.

## Whole cores per rank

On nodes with several hardware threads per core, Slurm counts each hardware
thread as a CPU. Under `CR_CORE`, a task that asks for one CPU gets one
hardware thread, so two ranks share a core and a node fits twice as many
ranks as it has cores. Another job can also take the other half of those
cores. On Ares (2 × 10 cores, 2 threads per core), two 16-rank jobs once
shared a node: 32 ranks ran on 20 cores.

So a Slurm job that requests ranks (`ntasks_per_node` or `ntasks`) also
gets `#SBATCH --ntasks-per-core=1`. Each rank claims a whole core: 16 ranks
per node claim 16 cores (32 CPUs), and 24 ranks on a 20-core node are
refused at submit (`Requested node configuration is not available`) instead
of sharing cores. `cpus_per_task` keeps Slurm's meaning (hardware threads),
rounded up to whole cores: `cpus_per_task: 3` gives each rank 2 cores. A
node without SMT is unchanged.

The person keeps the choice. Any of `ntasks_per_core`, `threads_per_core`,
`cores_per_socket`, `sockets_per_node`, `hint` or `extra_node_info` in
`scheduler:`, or the matching flag in `sbatch_args`, leaves the mapping to
them. `ntasks_per_core: false` restores Slurm's default of one task per
hardware thread. The Ares builtin pipelines that use 24 slots on 20-core
nodes for replicate loops set it. The other schedulers are unchanged.

The request does not set `--exclusive`. Cores the job leaves free can go to
other jobs; set `exclusive: true` to keep the nodes to this job. Slurm
binds nothing when the site's `TaskPlugin` is `task/none` (Ares), so ranks
may float over the node's CPUs. JARVIS's launchers add no binding, since
binding to cores 0..N would collide with another job's ranks on a shared
node.

**The facts behind it.** `sched info` gives each partition's
`sockets_per_node`, `cores_per_socket`, `threads_per_core` and
`physical_cores_per_node` from `sinfo`. The resource graph's `cpu` section
(`jarvis.resource-graph.cpu.v1`) holds what the collector read from
`/sys/devices/system/cpu` on each node it ran on: `hosts.<host>` with
`cpus`, `sockets`, `cores_per_socket`, `physical_cores`, `threads_per_core`
and `collected_at`, plus `smallest` (each field's minimum) and
`heterogeneous`. A collection replaces the hosts it reached and keeps the
others.

A Slurm run's plan (`ppl submit +plan`) carries `cores`
(`jarvis.submit-cores.v1`):

| Field | Meaning |
|-------|---------|
| `partition`, `source` | the submit partition (else the default), and where its topology came from: `sinfo`, `resource_graph.cpu`, or `null` when unknown |
| `sockets_per_node`, `cores_per_socket`, `threads_per_core`, `physical_cores_per_node`, `cpus_per_node`, `heterogeneous` | that topology |
| `ranks_per_node`, `cpus_per_task` | the request |
| `ntasks_per_core`, `ntasks_per_core_source` | `1` from `jarvis`, the person's value from `pipeline.scheduler` (or `.sbatch_args`), or `unset` when no ranks are requested |
| `cores_per_rank`, `cores_claimed_per_node`, `fits` | whole cores per rank, per node, and whether they fit the partition's cores (`null` when unknown or the person chose the mapping) |
| `exclusive`, `meaning`, `override` | whether the nodes are the job's alone, one sentence saying what the request claims, and how to change it |

## Ranks per node inside an allocation

Rank placement is the launcher's job, so packages pass their `nprocs` and
`ppn` and need not read the scheduler. Inside an allocation (`SLURM_JOB_ID`,
`PBS_JOBID`, `LSB_JOBID` or `FLUX_JOB_ID` set), every MPI exec (Open MPI
`-npernode`, MPICH/Intel `-ppn`, Cray `--ppn`) bounds ranks per node by the
scheduler's task geometry:

| Scheduler | Tasks per node |
|-----------|----------------|
| Slurm | `SLURM_NTASKS_PER_NODE`, else `SLURM_TASKS_PER_NODE` (e.g. `8(x2),4`) paired with `SLURM_JOB_NODELIST`. Used only when the job asked for tasks (`ntasks_per_node` or `ntasks`). |
| PBS | lines per host in `PBS_NODEFILE`, else `PBS_NUM_PPN` |
| LSF | `LSB_MCPU_HOSTS` |

When the job asked for no task geometry (Flux, or Slurm with only `nodes`),
the bound is the physical cores the launcher may run on: its CPU affinity,
with the hardware threads of one core counted once. With a `host_slice`, the
bound is the smallest count among the slice's hosts.

The package's `ppn` is kept when it fits the bound and places every rank
(`ppn × hosts ≥ nprocs`), so a step can under-fill its nodes on purpose.
Otherwise the bound wins. A package default can't halve a grant: 8 ranks in
a 1-node, 8-task job no longer launch as `-npernode 4` (`ppr:4:node`, which
Open MPI refuses). It can't exceed the grant either. When `nprocs` needs
more per node than granted, `ppn = ceil(nprocs / hosts)` and the placement
carries a `warning`. Outside an allocation the package's `ppn` is used
unchanged, as is it when `JARVIS_MPI_PLACEMENT=off` is set in the
environment or the package's env.

An adjusted value prints `jarvis: ppn A -> B (reason)` on stderr. Each
launch records its decision as `placement` (`jarvis.launch-placement.v1`)
in `provenance/launch-NNN/launch.json` and `provenance.json`:

```json
{"scheduler": "slurm", "job_id": "27999", "nprocs": 8, "hosts": 1,
 "ppn_requested": 4, "ppn": 8, "adjusted": true,
 "source": "scheduler_geometry", "bound": 8,
 "geometry": {"variable": "SLURM_NTASKS_PER_NODE", "value": "8", ...},
 "reason": "package ppn 4 on 1 host(s) cannot place 8 ranks; 8 per node granted"}
```

`source` is `scheduler_geometry`, `physical_cores` (with `physical_cores`),
or `package` when neither is known.

## Threads per rank

A rank that links a threaded library (OpenMP, OpenBLAS with pthreads, MKL)
starts one thread per CPU it sees unless told otherwise, so 16 ranks on a
40-CPU node can run 600+ threads. Inside an allocation, every MPI exec sets
`OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS` and `MKL_NUM_THREADS` in each
rank's env to the physical cores the allocation gives one rank, one thread
per core. That is the CPUs per rank divided by the node's hardware threads
per core, read from `/sys/devices/system/cpu` on the launch node (1 when
unreadable), at least 1. The CPUs per rank are:

- Slurm `SLURM_CPUS_PER_TASK` when the job asked for it (`cpus_per_task`);
- else the CPUs per node (`SLURM_JOB_CPUS_PER_NODE` with
  `SLURM_JOB_NODELIST`, the smallest among the launch's hosts; under other
  schedulers the launcher's CPU affinity) divided by the ranks per node
  (the launch's `ppn`, else `ceil(nprocs / hosts)`).

With [one rank per core](#whole-cores-per-rank), 16 ranks per node on a
2-thread-per-core node get 32 CPUs, so 2 CPUs and 1 thread per rank.

A variable already in the rank env (the package's or pipeline's env) is
kept, and one set in the launcher's environment is kept and forwarded to
every rank, so a package or a person overrides it by setting it.
`JARVIS_MPI_THREADS=off` in the environment or the package's env sets none.
Outside an allocation nothing is set.

A launch that sets values prints `jarvis: OMP_NUM_THREADS=2 ... per rank`
on stderr and records the decision as `threads` (`jarvis.launch-threads.v1`)
in `provenance/launch-NNN/launch.json` (and inside `placement`):

```json
{"schema_version": "jarvis.launch-threads.v1", "ranks_per_node": 16,
 "cpus": {"per_node": 32, "source": "SLURM_JOB_CPUS_PER_NODE", "value": "32(x2)"},
 "cpus_per_rank": 2, "threads_per_core": {"value": 2, "source": "sysfs"},
 "threads_per_rank": 1,
 "set": {"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"},
 "kept": {}, "override": "..."}
```

`kept` names each variable left as found and `from` (`rank_env` or
`environment`).

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
extra SSH keys are needed, but each node's host key must still be in
`known_hosts`. OpenMPI launches avoid that: with no `ssh_cmd`, inside an
allocation they start their remote daemons through `srun` (see
[shell.md → Launcher Overrides](shell.md#launcher-overrides)).

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
