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
| `spack`          | a Spack spec, e.g. `ior@3.3.0 +hdf5`     | find-first: reuse what Spack already has, build only missing nodes, then `spack load /<hash>` its env into the run |
| `pip`            | one or more pip requirements             | find-first into a venv at `<jarvis root>/envs/pip/<install_env or pipeline>` (or `$JARVIS_INSTALL_ENV_DIR/pip/<install_env or env>`); its `bin` goes first on `PATH` |
| `conda`          | conda package specs                      | find-first into an env at `<jarvis root>/envs/conda/<install_env or pipeline>` (or `$JARVIS_INSTALL_ENV_DIR/conda/...`), from `install_channels` |
| `container`      | the image to pull (or empty: built from the package Dockerfiles) | find-first: reuse the image if the engine has it, else pull or build it |

Every method has a check (`jarvis ppl install +check`) that reports what is
missing as `not_installed` without installing, and an install that builds
only that, inside an allocation. A failure is typed (`pip_install_failed`,
`conda_install_failed`, `conda_bootstrap_failed`, `container_pull_failed`,
`container_image_not_ready`, `venv_failed`). What JARVIS cannot detect is a question in `issues.jsonl`,
answered through `answers.json` and recorded in `decisions.yaml`, never a
silent default:

| Issue | When | Answer |
|-------|------|--------|
| `python_missing` | no Python with `venv` and `ensurepip` for the pip venv. JARVIS tries the Python it runs on first (it is on every node JARVIS runs on), then `python3` on `PATH` | a Python (or set `install_python`) |
| `conda_select` | several condas | one of them (or set `install_conda`) |
| `conda_missing` | no conda | `miniforge`: JARVIS installs Miniforge into the site home (`<site>/miniforge3`) in the install job |
| `container_engine` | the pipeline names no `container_engine` and several engines run as this user | one of them |
| `container_engine_missing` | no apptainer, rootless podman or rootless docker | none: install one, or name `container_engine` |
| `container_engine_unusable` | the engine is installed but cannot run here (`podman info` fails, or a pull fails on its store); the engine's own error is the detail | none: the remedy says what to fix on the node |
| `install_method` | the package lists several `install_methods` and more than one fits this site. A method fits when its tool is here: for Spack, a Spack JARVIS already uses or a site Spack it finds (then a `spack_missing` question, or the site's recorded answer, picks which) | one of the fitting methods |

Pip and conda never install an MPI: a query naming one (conda's `mpi4py`
too) is refused as `mpi_from_package_manager`. MPI codes use Spack, which
reuses the site's MPI. A shared rootful docker daemon is never picked on its
own; podman gets a private `XDG_RUNTIME_DIR` on batch nodes that have none.

When rootless podman's default store is on a network file system (the home
on most clusters), JARVIS gives each node its own store under the node's
temp dir (`JARVIS_PODMAN_STORE=node|default` overrides the detection; a
user `storage.conf` wins) and archives the image once on the shared file
system for the other nodes to load. A matching archive satisfies the check
on any host, whatever its own store (a login node that serves the home
locally keeps the default store, so an image a compute node pulled is only
in the archive), and every run passes it to the nodes. Rootless podman 3.x ignores
`CONTAINERS_STORAGE_CONF`, so every podman call JARVIS makes or starts
(podman-compose included, on every node) goes through a node-local `podman`
wrapper first on `PATH`. It passes the store as `--root`, `--runroot`,
`--storage-driver` and `--tmpdir`. When podman's default OCI runtime is
missing on a node (a login node may have `crun` while compute nodes only
have `runc`), the wrapper finds one that node has and passes `--runtime`.
It probes once per node and caches the result in the node's store dir. A
node whose temp dir cannot hold the store (full, read-only, or missing, so
that Python would fall back to the working directory on the shared file
system) is `container_engine_unusable`, never a store in the working
directory.

A prebuilt image runs as SSH-ready containers, one per node: JARVIS starts
them with `/bin/sh` and runs `sshd` in each, and packages reach them over
SSH or `podman exec` (`bash -c`). At install, JARVIS probes the image for
`bash`, `sshd`, `ssh` and `ssh-keygen`. An image that lacks any of them
(plain `alpine`, say) gets a local image made from it,
`localhost/jarvis-ready/<image>`, that adds them with the image's own
package manager (apk, apt-get, dnf, microdnf, yum or zypper). JARVIS makes
it with `run` then `commit`, not `build` (rootless podman 3 cannot run a
build step without a systemd user session), and keeps the script it ran in
`<container env>/ready/ready.sh`. That image is
the one the nodes run and the one archived. The install records which image
runs in `<container env>/deploy.json`, so the check knows it without running
a container. An image installed before this record existed is
`not_installed` ("make ... ready to run") until one install makes it. An
image JARVIS cannot make ready (no `/bin/sh`, no known package manager, or
no mirror) is `container_image_not_ready`, with a remedy, before any job
runs.

The nodes run `podman-compose` from JARVIS's own environment, by absolute
path (that `bin` is not on a node's `PATH`), then from `PATH`, then
`podman compose`. With none of these, the start is refused as
`container_compose_missing`. A failed `up`, or a container that is not
running on every node right after it, refuses the start as
`container_start_failed`, with the engine's error or the container's last
log, before any package starts.

A package that installs several ways lists them, and leaves `install_method`
empty:

```yaml
install_methods:
  - {method: spack, query: py-rich}
  - {method: pip, query: "rich==13.7"}
```

JARVIS uses the one whose tool this site has (Spack, a `python3`, a conda, a
container engine), asks when several do, and records the answer for the
package.

Spack packages are resolved one by one (below).

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

Which Spack runs:

- With `JARVIS_SPACK_ROOT=<root>`, JARVIS calls `<root>/bin/spack` by path
  for every command (`find`, `spec`, `concretize`, `install`, `load`). The
  `spack` on `PATH` is never used, and it is an error when `<root>/bin/spack`
  is missing. The Spack is also isolated from the user's `~/.spack` and the
  machine's `/etc/spack`: JARVIS exports `SPACK_DISABLE_LOCAL_CONFIG=1` and
  drops any inherited `SPACK_ENV`. That variable removes Spack's `system`
  and `user` scopes (Spack 0.23 `spack.config.create`), so the Spack reads
  only `<root>/etc/spack/defaults`, the site scope `<root>/etc/spack` (each
  with its `linux/` subdirectory), and the `-C`/`-e` scopes JARVIS passes.
  Put the site's `upstreams.yaml`, `packages.yaml`, `compilers.yaml` and
  `config.yaml` in `<root>/etc/spack`. This is the layout
  [`jarvis spack prepare`](spack.md) creates: a private Spack whose upstream
  is the site Spack, with `mpi: buildable: false`, the site's compiler
  preference and its module and tool externals. `install-record.json`
  (`spack_config_scopes`) and the run's
  [provenance record](executions.md#provenance-record) (`config_scopes`)
  list the scopes that were in effect.
- Without it, `$SPACK_ROOT/share/spack/setup-env.sh` is sourced when
  `SPACK_ROOT` is set, and `spack` comes from `PATH`.

`JARVIS_SPACK_CONFIG=<dir>` is still accepted. It is exported as
`SPACK_USER_CONFIG_PATH` and turns on the same isolation, but Spack ignores
that directory while local config is disabled, so it never adds
configuration. Older releases of this branch defaulted it to
`<root>/../spack-user-config`, which was never read either. Nothing is set by
default now.

After installation, the environment that `spack load --sh /<hash>` prints is
merged into the run, so the resolved binaries are on
`PATH`/`LD_LIBRARY_PATH`.

### Find-first Spack installs

For each Spack package, JARVIS:

1. Collects every package's `install_repos` (see
   [Package Spack repos](#package-spack-repos-install_repos)).
2. Resolves the spec with `spack spec -I -l <install_query> <recorded
   constraints>`. Every node is marked installed (`[+]`), upstream/site
   (`[^]`), external (`[e]`) or missing (` - `).
3. Picks the MPI when the query doesn't pin one with `^<mpi>`. A recorded site
   decision wins. The candidates are every installed or upstream MPI
   (`spack find` over openmpi, mpich, mvapich2, intel-oneapi-mpi,
   cray-mpich, …, merged by prefix) plus every MPI external configured in
   `packages.yaml` (`spack config get packages`). Spack only lists an
   external in `spack find` once something has used it, so a site module MPI
   such as `mpich: externals: [{spec: mpich@4.1.1, modules: [mpich/4.1.1]}]`
   is found here. An external with the same prefix as an install (or, for a
   module-only external, the same name and version) is merged into that
   install. When a site registers its MPI install again as a `packages.yaml`
   external at the same prefix, `spack find` shows two hashes for one
   prefix. The candidate then pins the real install's hash, the one whose
   `{external_path}` is empty, because upstream dependents such as adios2
   link that one. Pinning the external hash would make Spack rebuild them.
   The external hash is pinned only when no real install shares the prefix.
   With exactly one candidate, JARVIS pins it: `^<name>/<hash7>`
   for an install, or `^<external spec>` for an unused external. With
   several, it asks with `ambiguous_provider` and never takes `spack spec`'s
   silent default. This applies to `ppl install +check`, to install mode,
   and to an install-on-load (`JARVIS_INSTALL_ON_LOAD=check` or `install`)
   in a `ppl submit` job. All of them exit `3`. Nothing is asked when the
   query already names the MPI: a `^openmpi…` dependency, or a spec whose
   root is itself an MPI (e.g. `install: openmpi/smvs5sq`).
4. Reuses everything already present. Only in install mode, and only for
   what is missing, it builds through the pipeline's Spack environment (see
   [Per-pipeline Spack environment](#per-pipeline-spack-environment)). It
   then re-resolves to verify the nodes are installed.
5. Writes `install-record.json` (`jarvis.install-record.v1`): the resolved
   spec, root hash and `lock_hash`, node counts by status, what was built,
   the MPI with its source, version and prefix, and the `spack_env` block.
6. Loads the result with `spack load /<lock hash>` (the root hash when no
   lock exists yet) and exports the chosen MPI to the run (see
   [MPI selection](#mpi-selection)).

Spack output goes to an append-only `install.log`. Outside a scheduler job,
the files live in `<pipeline_shared_dir>/install/`. In a `ppl install +submit`
job they live in `<execution>/install/` (see
[scheduler.md → Install jobs](scheduler.md#install-jobs)).

JARVIS doesn't guess choices it can't resolve. Each one becomes a line in
`issues.jsonl` (`jarvis.install-issue.v1`), and the command exits with code
`3`:

| `kind` | When | Answerable |
|--------|------|------------|
| `ambiguous_provider` | several MPI candidates (installed, upstream or `packages.yaml` externals) and neither the query nor a recorded answer pins one (`subject: mpi`, `scope: site`); `candidates[]` lists `{id, key, constraint, aliases, source, prefix, hashes, implies}` (`hashes` puts the pinned real install first), plus `modules` for module externals; see [Stable MPI candidates](#stable-mpi-candidates) | yes, with one candidate's `constraint` (or an `aliases` entry, its `id` or its `key`) |
| `spack_missing` | the pipeline has Spack packages but JARVIS has no Spack to run (`subject: spack`, `scope: site`, `package: "*"`); `site_spacks[]` (also `candidates[]`) lists the Spack checkouts found on the host, see [Missing Spack](#missing-spack-spack_missing) | yes, with one `site_spacks` `root` |
| `variant_conflict` | the concretizer reports a conflict | yes, with a spec constraint (e.g. `~cuda`) |
| `concretize_failed` | any other concretizer error (`detail` holds the excerpt) | yes, with a spec constraint |
| `no_recipe` | Spack has no recipe for the package | no: add `install_repos` or change `install_query` |
| `compiler_unavailable` | a compiler the install would use can't run on the build host (`subject: compiler`, `scope: site`): `compiler`, `host`, `missing_executables` (or `probe_failures` when it exists but can't build a test program), `usable_compilers`, `origin`, plus `to_build` and `reused_specs` when reused upstream specs pull the DAG onto it. With `compiler: null` and `missing_languages`/`needed_languages`, no usable compiler on the build host has every language (`c`, `cxx`, `fortran`) the nodes to build need; see [spack.md → Compilers on the build host](spack.md#compilers-on-the-build-host) | yes, with one usable compiler (`candidates[]`), which becomes a binding `require`; the language form has no candidates (add a compiler, or pin the install job's nodes) |
| `not_installed` | `+check` found nodes that still need building | no: run `jarvis ppl install` |
| `install_unchecked` | a scheduled pipeline's check on its submitting host found no matching `install-check.json` (JARVIS runs no Spack there); `packages`, `reason`, `record`, `remedy[]`; see [scheduler.md → Light checks](scheduler.md#light-checks-on-the-submitting-host) | no: check in a job, `jarvis ppl install +check +submit` (or `+submit` to also build) |
| `install_placement` | where a scheduled pipeline's full check or build runs at this site is not chosen yet (or the site chose `ask_each_time`). One site question per phase (`install_placement.check` / `install_placement.build`) covers every package; `phase`, `cost_class` (the costliest package's), `cost_classes`, `covers`, `once`, `recommended`, `recommendation_reason`, `recommendation_basis` (a site answer recorded in this home or the resource graph's `install_placement`, with who, when and `basis`; else cost and load), `packages[]` (with `estimate`), `login_host`, `limits`, `queue`, `site_policy`, `package_overrides[]` (each with its own recommendation); see [scheduler.md → Install placement](scheduler.md#install-placement-where-checks-and-builds-run) | yes: `login_limited`, `compute_job` or `ask_each_time` (an `install_placement.<phase>.occasion` question: the first two, this run only); recorded per site, or per package for an override; `basis` records why |

The `issue_id` is a stable `sha256:` digest of the question's content, so it
stays the same across re-runs. Answer by writing `answers.json`:

```json
{"sha256:3f…": {"constraint": "^mpich/fffffff", "answered_by": "jaime", "ask_id": "a-17"}}
```

A plain string value (`{"sha256:3f…": "^mpich/fffffff"}`) also works. Then
re-run with `--answers answers.json`, or export
`JARVIS_INSTALL_ANSWERS=answers.json` for an install-on-load. An answer must
name one of the issue's candidates when it has any: its `constraint`, one of
its `aliases`, its `id` or its `key`. It is stored as the candidate's current
`constraint`. Applied answers
are recorded in `<pipeline shared>/install/decisions.yaml`
(`jarvis.install-decisions.v1`: `site:` entries such as `mpi`, and
per-package constraint lists). The file is per pipeline, not per execution,
so an answer given to an install job is reused by every later run job of
that pipeline. That is how the chosen MPI reaches a run (see
[MPI selection](#mpi-selection)). Set `JARVIS_SPACK_DECISIONS` to share one
decisions file across pipelines on a site.

Writes to `decisions.yaml` (recording an answer, re-keying one, `--forget`)
hold an exclusive lock on `.decisions.yaml.lock` beside it. They re-read the
file under that lock before changing it and replace it atomically. So
answers that land at the same time, from several jobs, pipelines sharing
`JARVIS_SPACK_DECISIONS`, or threads, all keep their entries, and none is
lost and re-asked. The lock is `flock` (on Windows, `msvcrt.locking`), so on a
shared filesystem it needs working `flock`, the same as the Spack env lock.

#### Decision provenance

A recorded answer is replayed only for the `issue_id` it answered. Each
entry in `decisions.yaml` stores the `issue_id`, `constraint`, `answered_by`,
`ask_id` and `answered_at`. An MPI answer also stores the chosen
`candidate_key` and `candidate_id`, and `candidate_keys`, the set the
question offered. When the question changes (for example a third MPI
appears, so the candidate set changes), the new issue gets a new `issue_id`.
JARVIS then asks again instead of replaying the old answer. The new issue
carries `supersedes` (the old `issue_id`) and `previous_answer`. When the
earlier choice is still offered, it also carries `previous_candidate_id` and
`previous_candidate_key`, so a caller can preselect it.

#### Stable MPI candidates

Spack lists one MPI differently over time. Before anything is built against
a site's `packages.yaml` external, the external is not in `spack find`, so the
candidate pins its spec (`^openmpi@5.0.5-cphqvsy`). After the first install,
`spack find` lists it as an external hash at the same prefix
(`^openmpi/smvs5sq`). Through an upstream it may also show a real install
hash. The `ambiguous_provider` question is therefore keyed on each candidate's
`key`, not its constraint:

| `key` | When |
|-------|------|
| `prefix:<normalized prefix>` | the candidate has a prefix (installs, upstreams, prefix externals) |
| `modules:<name>@<version>:<modules>` | a module-only external, before and after it enters `spack find` |
| `spec:<spec>` | neither (rare) |

The `issue_id` digests the sorted keys. The `id` comes only from the key: for
a Spack layout prefix (`.../openmpi-5.0.5-<32-char hash>`) it is
`openmpi-5.0.5-cphqvsy`. So the same MPIs keep the same `issue_id` and
candidate ids before and after an external enters `spack find`. Each
candidate's `aliases` lists every constraint it can be pinned with (each
hash at its prefix and each `packages.yaml` spec).

A replayed answer uses the chosen candidate's current `constraint`. The
entry in `applied_decisions` then has `resolved_by: issue_id` and
`recorded_constraint` (what was stored), and `decisions.yaml` is re-keyed
in place (`rekeyed_at`), keeping `answered_by`, `ask_id` and `answered_at`.
An entry written by an older JARVIS has a constraint-based `issue_id` and no
`candidate_keys`. That id can't be recomputed reliably: older listings
offered each same-prefix `packages.yaml` external as its own candidate. So
JARVIS accepts such an entry once, when its `constraint` names exactly one
current candidate (as its constraint, an alias, its id or its key). The
answer is applied with `resolved_by: legacy_issue_id` and
`recorded_issue_id` (the old id), and the entry is re-keyed (`rekeyed_from`
holds the old id). From then on, the strict rule above applies. A legacy
entry whose constraint names no single candidate is listed in
`stale_decisions` and asked again.

Several `packages.yaml` externals at one prefix (for example two module
names for the same install) are one candidate. Their specs are its
`aliases`, and their `modules` are merged, whether or not the prefix is in
`spack find` yet.

Every `jarvis.install-report.v1` (`ppl install [+check] +json`) reports:

| Field | Content |
|-------|---------|
| `applied_decisions[]` | each answer this run used: `issue_id`, `kind`, `subject`, `scope`, `package`, `constraint`, `answered_by`, `ask_id`, `answered_at`, `source` (`answers_file` for a `--answers`/`JARVIS_INSTALL_ANSWERS` answer, `decisions_file` for a replayed one), `path` (the file it came from), and `recorded_to` (the decisions file it was written to); for an MPI also `candidate_id`, `candidate_key`, `resolved_by` (`issue_id` or `legacy_issue_id`), and `recorded_constraint`/`recorded_issue_id` when the stored entry differed |
| `stale_decisions[]` | recorded answers that were not replayed because the question changed: `issue_id`, `constraint`, `candidate_key`, `path`, `answered_at`, `superseded_by`, `reason`, and `added_candidates`/`removed_candidates` (keys) when the old set is known |
| `open_issues[]` | the `issue_id`s still unanswered |
| `decisions_path` | the decisions file in use |
| `last_install` | `+check` only: what the last install wrote, even as a `+submit` job under `<execution>/install` (see [Install records](#install-records)) |
| `answer_with` | how to answer: `flag` (`--answers <answers.json>`), `env` (`JARVIS_INSTALL_ANSWERS`), the answers `format`, and the `replay` rule |

A caller therefore needs no store of its own. It shows `applied_decisions` as
"chosen before", asks the person about `open_issues`, and passes the answers
back with `--answers`.

#### Install records

A check outside a job writes its own files to `<pipeline shared>/install/`.
An install job writes to `<execution>/install/`. So every install, in a job
or not, also writes `<pipeline shared>/install/last-install.json`
(`jarvis.install-pointer.v1`), with `execution_id`, `phase` (`done`,
`needs_decision`, `refused` or `failed`), `install_dir`, `record`, `log`,
`issues`, `decisions` and `written_at`. `ppl install +check +json` reads it
and reports `last_install`: the pointer, plus the record's `finished_at`,
per-package `status`, `spec` and `mpi` (`hash`, `prefix`, `candidate_id`,
`candidate_key`), `applied_decisions`, `open_issues`, `resolved_issues` and
`spack_env`. `open_issues` lists only the record's questions that are still
open, so a resumed caller never re-presents a stale ask. A question the
current check raises again stays open. The others move to
`resolved_issues[]`, each with `issue_id` and `resolved_by`:

| `resolved_by` | When | Extra fields |
|---------------|------|--------------|
| `decision` | `decisions.yaml` answers it (by `issue_id` or `rekeyed_from`) | `constraint`, `decision_issue_id`, `scope` |
| `answer` | this round's `answers.json` answers it | |
| `superseded` | a newer question replaced it (`supersedes`, or a `stale_decisions` entry) | `superseded_by` |
| `not_raised` | the current check no longer raises it | `note` |

A pip, conda or container install job that stops for a question
(`phase: needs_decision`) writes no record, only its `issues.jsonl`; the check
reads the questions from there. Those questions depend on the host that asked:
a compute node's Python, conda or container engine can differ from the login
host's. So a check that does not raise one again does not resolve it
(`not_raised` never applies). It stays in `open_issues`, its document is in
`last_install.open_questions[]`, and the check adds it to its own `issues[]`
with `raised_by: install_job` and the job's `execution_id`. The check then
exits for a decision, and the caller asks the person.

When no pointer exists, because the install ran under an older JARVIS, the
newest install-mode
`<pipeline shared>/executions/*/install/install-record.json` is used instead
(`found_by: scan`). Decisions are never split this way: `decisions.yaml` is
always `<pipeline shared>/install/decisions.yaml` (or
`JARVIS_SPACK_DECISIONS`), for checks, local installs and install jobs alike.
Provenance reads the same record.

#### Install failures

When `spack concretize` or `spack install` itself fails, JARVIS doesn't
leave a bare exit code and a log tail. It classifies the output into one
typed issue, appends it to `issues.jsonl`, marks the package `status:
failed` with its `issue_id`, and exits `6`:

| `kind` | Matched by | `remedy` |
|--------|------------|----------|
| `compiler_unavailable` | `CompilerAccessError: Compiler '…' has executables that are missing…` (`origin: spack install`); `missing_executables` are the paths Spack named, and that compiler is dropped from `usable_compilers` | answer with a usable compiler, or re-run `jarvis spack prepare --compiler` inside a job |
| `compiler_unavailable` | a compiler that exists but can't build: `C compiler cannot create executables` (autoconf), `The C compiler "…" is not able to compile a simple test program` (CMake), `no acceptable C compiler found`, `cannot run C compiled programs`, in Spack's output or its build log. `compiler` is the failed node's compiler (from the concretized spec, `compiler_identified: true`); `build_failure` and `probe_failures` quote the line; `candidates` are the usable compilers minus that one. Checked before `build_failed`, so this never surfaces as a generic build failure | answer with a usable compiler; the next run takes the failed one out and binds the answer (see [spack.md → Compilers on the build host](spack.md#compilers-on-the-build-host)) |
| `fetch_failed` | `FetchError`, `All fetchers failed`, `ChecksumError`, `Manual download is required`, … | fix the build node's network or proxy, or add a mirror |
| `build_failed` | `ProcessError`, `InstallError`, `Command exited with status`, … | read `build_log`, then change the spec or the recipe |
| `variant_conflict`, `no_recipe`, `concretize_failed` | a failed environment `concretize` | answer with a spec constraint |
| `install_failed` | anything else | read `log_tail` and `install.log` |

Every failure issue carries `host`, `stage` (`install` or `concretize`),
`packages`, `failed_node` (the last `==> Installing` line), `log`,
`log_tail` (the last 40 lines), `build_log` when Spack named one,
`message` and `remedy`. In a `ppl install +submit` job the execution ends
`failed` with return code `6`, and its `metadata.install` progress has
`phase: failed`, `open_issues` and `issue_kinds`, so a caller can turn the
issue into an ask without reading the log.

#### Missing Spack (`spack_missing`)

Before planning, JARVIS checks that it has a Spack to run. These are light
file checks and a `PATH` lookup; nothing is executed. Spack counts as
missing when `JARVIS_SPACK_ROOT` has no `bin/spack`, when `SPACK_ROOT` has no
`share/spack/setup-env.sh`, or when neither is set and `spack` is not on
`PATH`. Then `ppl install [+check]` (and a check- or install-mode
install-on-load in a `ppl submit` job) exits `3` with one `spack_missing`
issue, instead of failing on the first `spack` call. Every Spack package in
`install-record.json` shows `status: blocked, blocked_by: spack_missing`.

The issue lists the Spack checkouts the host offers in `site_spacks[]` (the
same list is in `candidates[]`). They are probed in this order, and each one
must have `bin/spack`:

1. `JARVIS_SITE_SPACKS`, an `os.pathsep`-separated list a site sets;
2. `<dir of JARVIS_RESOURCE_GRAPH>/spack`, a Spack kept next to the site
   resource graph;
3. the Spack behind each Spack module tree (`<root>/share/spack/lmod` or
   `<root>/share/spack/modules`) on the login `MODULEPATH`, which is how a
   site publishes the software it built with Spack;
4. the well-known locations `/opt/spack`, `/usr/local/spack`,
   `/opt/apps/spack`, `/apps/spack`, `/sw/spack`, `/shared/spack` and
   `~/spack`;
5. a `spack` next to a broken `JARVIS_SPACK_ROOT` or `SPACK_ROOT`.

| Field of a `site_spacks` entry | Meaning |
|--------------------------------|---------|
| `id` | `spack-1`, `spack-2`, … |
| `root`, `constraint` | the checkout's absolute path (the value to answer with) |
| `version` | the release read from `lib/spack/spack/__init__.py` (nothing is run), or `null` |
| `executable` | `<root>/bin/spack` |
| `source` | `JARVIS_SITE_SPACKS`, `resource_graph_site`, `site_modules`, `well_known`, `JARVIS_SPACK_ROOT_sibling` or `SPACK_ROOT_sibling` |
| `use` | the environment setting that selects it (`JARVIS_SPACK_ROOT=<root>`) |
| `prepare` | the command that makes a JARVIS Spack chained to it (`jarvis spack prepare --site_spack <root>`, see [spack.md](spack.md)) |

`site_spacks` is empty when nothing was found. The question then asks for a
root, and any directory with `bin/spack` is accepted. Answer like any other
issue:

```json
{"sha256:9c…": {"constraint": "/opt/spack", "answered_by": "jaime"}}
```

An answer without `bin/spack` is refused. The answer is recorded as the site
decision `spack` in `decisions.yaml` and becomes `JARVIS_SPACK_ROOT` for that
process, so the install, the run's `spack load` and the provenance record all
use it. A shared site Spack is usually read-only, and Spack cannot run from a
root whose site scope (`etc/spack`) or install tree (`opt/spack`) this user
cannot write. When the answer is such a root, JARVIS keeps the answer and
uses its own Spack instead: `<site record>/spacks/<release>-<digest>`, where
the site record is the directory of `JARVIS_RESOURCE_GRAPH`, else
`JARVIS_ROOT`, else `~/.ppi-jarvis`. It prepares that root once, as
`jarvis spack prepare --site_spack <answer>` would, with the answer as its
upstream, so find-first reuses what the site built. Concurrent installs wait
on one lock, and later ones find `site-prep.json` and skip the work. An
`ambiguous_compiler` question from that preparation is raised like any other
install issue. A later run replays it (`applied_decisions[].source:
decisions_file`). When the set of candidates changes, for example a new
checkout appears, the question gets a new `issue_id`. The old answer is then
listed in `stale_decisions` and offered as `previous_answer`, with
`supersedes` naming the old issue. A caller that prefers a prepared JARVIS
Spack runs the entry's `prepare` command inside a job and answers with the
prepared root. [`jarvis sched info +json`](sched.md) reports the same issue
without a pipeline.

`JARVIS_SPACK_CONFIG=<dir>` (or `JARVIS_SPACK_ROOT`) turns on
`SPACK_DISABLE_LOCAL_CONFIG=1`, so Spack reads no user or system
configuration. Put site configuration in `<root>/etc/spack` (see above).

### Package Spack repos (`install_repos`)

A package can ship its own Spack recipes. `install_repos` is a list of Spack
repo directories (each holding a `repo.yaml`). Relative entries are relative to
the package's directory, and `~` and `${VAR}` are expanded:

```yaml
pkgs:
  - pkg_type: clio.xcompact3d
    install_method: spack
    install_query: xcompact3d@5.0
    install_repos: [spack]        # <clio repo>/xcompact3d/spack/repo.yaml
```

The repos never touch the user's or the site's Spack configuration. There is
no `spack repo add`. Instead:

- The pipeline environment's `spack.yaml` lists them under `spack: repos:`,
  so `concretize` and `install` see them. A change in the repos counts as a
  change in the environment and triggers a re-concretize.
- For commands outside the environment (`spec`, `find`, `load`), JARVIS
  writes a config scope `<install dir>/spack-scope/repos.yaml` and passes it
  as `spack -C <scope>`.
- `install-record.json` records the absolute paths (`spack_repos`) and the
  scope (`spack_scope`).

A missing directory, or one without `repo.yaml`, is an error before Spack
runs.

### Per-pipeline Spack environment

Each pipeline owns an anonymous Spack environment at
`<pipeline_shared_dir>/spack-env/`. It lives in the pipeline's persistent
shared directory, not an execution's, so the lock an install job writes is the
one later run jobs load:

```yaml
# <pipeline_shared_dir>/spack-env/spack.yaml (generated)
spack:
  specs:
  - xcompact3d ^openmpi/abc1234     # each package's resolved spec, in order
  view: false
  concretizer:
    reuse: true                     # keep installed, upstream and external nodes
    unify: false                    # each root concretizes as the planner resolved it
```

When something is missing, or the lock was concretized from different specs,
an install runs `spack -e <dir> concretize -f` and then `spack -e <dir>
install --fail-fast`. `spack.lock` is the install record. Its root hashes are
stored as each package's `lock_hash`, its path and `sha256` go into
`install-record.json → spack_env`, and a copy is saved as
`<install dir>/spack.lock` for that execution. If nothing is missing and the
lock matches the specs, Spack is not run again, and a run only does
`spack load /<lock hash>`. The environment is built only when every package
resolved. If any package needs a decision, nothing is built until it's
answered. `JARVIS_SPACK_ENV=off` falls back to one `spack install
<spec>` per package, with no lock.

#### Shared environments (`JARVIS_SPACK_ENV_DIR`)

Set `JARVIS_SPACK_ENV_DIR=<dir>` to use `<dir>` in place of
`<pipeline_shared_dir>/spack-env`. The check, the install and a run's load all
use it, so several pipelines with the same packages can share one environment
and one lock. For example, a caller can key the directory on the package
digests (`<site home>/jarvis-cd/spack-envs/<sha256 of the sorted package
digests>`). Set it on the submitting process, as with `JARVIS_INSTALL_ON_LOAD`.
The job inherits it.

`JARVIS_INSTALL_ENV_DIR=<dir>` does the same for the pip venvs and conda envs:
they go to `<dir>/<method>/<install_env or env>` in place of the JARVIS root's
`envs`, so a caller that gives each job its own JARVIS root still has the
install job's env in the run.

Every environment operation holds a `flock` on `<dir>/.jarvis-env.lock`:

- An install holds it **exclusively** around the manifest write, `concretize`,
  `install`, and reading and copying `spack.lock`.
- A check, or a load that only reads the lock, holds it **shared**.

A second install that finds the lock held logs `waiting for
<dir>/.jarvis-env.lock` to `install.log` and reports `phase:
waiting_env_lock`. It blocks until the holder finishes. It then re-runs `spack
spec -I` for the packages it saw as missing. If the holder built them and the
lock matches its specs, it reuses the lock (`lock_hash`) and makes no `spack
-e` call. If the specs differ, it re-concretizes the shared environment in
turn. Put the directory on a file system that honours `flock` across nodes;
Linux NFS clients map it to NFS byte-range locks.

### MPI selection

After loading, the installer exports the MPI that the packages resolved to.
That is the answered `ambiguous_provider` decision, the single installed
candidate, or the query's own `^mpi`:

| Variable | Example (a site Open MPI 5.0.5) |
|----------|---------------------------------|
| `JARVIS_MPI_NAME` | `openmpi` |
| `JARVIS_MPI_SPEC` | `openmpi@5.0.5/abc1234` |
| `JARVIS_MPI_TYPE` | `openmpi` (also `mpich`, `intel_mpi`, `cray_mpich`) |
| `JARVIS_MPI_PREFIX` | `/opt/site/openmpi-5.0.5` |

`<prefix>/bin` is also put first on `PATH`, because `spack load` does not
add externals such as a site MPI. Every `MpiExecInfo` launch then uses that
MPI:

- `JARVIS_MPI_TYPE` picks the launcher class without probing `mpiexec
  --version`.
- When the pipeline sets no `mpi_cmd`, the launcher is
  `<prefix>/bin/mpiexec`.
- For Open MPI, `--prefix <prefix>` is passed with the package's
  `--hostfile`, so daemons that `mpirun` starts over ssh find the same
  install on the other nodes. This is what an Open MPI built without Slurm
  (such as Ares's 5.0.5) needs.

Nothing is exported when no package uses MPI or packages resolved to
different MPIs. In that case the launcher falls back to detecting the
`mpiexec` on `PATH`. An explicit `mpi_cmd` still wins.

### `jarvis ppl install`

```bash
jarvis ppl install +check +json                 # installed vs missing, no builds
jarvis ppl install +json                        # build what is missing
jarvis ppl install --answers answers.json +json # apply answers, then build
jarvis ppl install yaml pipeline.yaml +check    # a YAML instead of the current pipeline
jarvis ppl install +submit +json                # the same, as a 1-node scheduler job
jarvis ppl install +allow_local +json           # build on this host outside an allocation
jarvis ppl install --forget mpi +json           # forget a recorded answer, then ask again
```

With `+json`, stdout carries exactly one JSON document: the install report,
or with `+submit` the `jarvis.execution.handle.v1` handle. Submit progress
lines (script path, hostfile, the `sbatch` command) go to stderr.

`--forget <selector>` removes recorded answers from `decisions.yaml` and
prints a `jarvis.install-forget.v1` document with `decisions_path`,
`forgotten[]` (each with `selector` and `forgotten_at`) and `remaining[]`.
It only forgets; with `+check` or `+submit` it then continues, and the
report carries the forget document as `forgotten`. The selector is one of:

- `all`;
- an `issue_id`, or a prefix of at least 8 hex digits (`rekeyed_from`
  matches too);
- a site subject: `mpi`, `compiler`, `spack`, or an install placement
  subject such as `install_placement.check` (which also forgets the older
  per-class `install_placement.check.short` / `.long` entries; see
  [scheduler.md → Install placement](scheduler.md#install-placement-where-checks-and-builds-run));
- a package name;
- a recorded `constraint`, `candidate_id` or `candidate_key`.

A selector that matches nothing refuses with `decision_not_found` (exit 5),
whose `details.recorded` lists what is recorded, and nothing is removed. The
next `ppl install +check` asks the question again.

The final stdout line with `+json` is a `jarvis.install-report.v1` document:
`mode`, `satisfied`, per-installer `status`/`packages`, `issues`,
`issues_path`, `log`, `record_path`, `exit_code`, and the decision fields
`applied_decisions`, `stale_decisions`, `open_issues`, `decisions_path`,
`last_install` (`+check` only) and
`answer_with` (see [Decision provenance](#decision-provenance)). With `+json`,
stdout carries only the document; logs and Spack output go to stderr. The exit code is `0`
when everything is satisfied, `3` when a decision is needed or `+check`
found missing packages, `4` when a build was refused because it would run
outside a scheduler allocation, `6` when Spack itself failed (the report
then has `failed: true` and `failures[]`, the typed issues' `issue_id`s; see
[Install failures](#install-failures)), and `1` on other errors.
Installers other than Spack report `unchecked` under `+check`.

Without `+submit` or `+allow_local`, `ppl install` refuses to build, run
`spack install` or concretize the Spack environment unless it already runs
inside an allocation (`SLURM_JOB_ID`, `PBS_JOBID`, `LSB_JOBID` or
`FLUX_JOB_ID`). This keeps builds off site login nodes. Nothing is refused
when everything is already installed. For a pipeline with a `scheduler:`
block, `ppl install` and `ppl install +check` outside an allocation run no
Spack command at all: they answer from the last full check's
`install-check.json`, or raise `install_unchecked` (exit 3) / refuse (exit 4)
when there is none. Where the site's install placement is `login_limited`
they run the full check (and an allowed build) on the submitting host
instead, niced and bounded, and while no placement is chosen they ask an
`install_placement` question (exit 3). See
[scheduler.md → Install placement](scheduler.md#install-placement-where-checks-and-builds-run),
[scheduler.md → Light checks](scheduler.md#light-checks-on-the-submitting-host) and
[scheduler.md → Login nodes](scheduler.md#login-nodes-no-builds-outside-an-allocation).

`ppl load` and `ppl run yaml` still run the install phase on load. Set
`JARVIS_INSTALL_ON_LOAD` to `install` (default), `check` (verify, exit 3 when
something is missing) or `skip`. With `install`, the same refusal (exit 4)
applies when a scheduler client (`sbatch`, `qsub`, `bsub`, `flux`) is on
`PATH`. A scheduled pipeline's load outside an allocation instead defers the
check to the job (`activation.method: deferred`), and exits 3 only for
questions the record already holds. Set `JARVIS_ALLOW_LOCAL_BUILD=1` to opt
in to local Spack work.

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

When a Spack install chose the MPI, `JARVIS_MPI_*` select the launcher
without an `mpi_cmd`; see [MPI selection](#mpi-selection).

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
