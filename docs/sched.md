# Site scheduler facts (`jarvis sched`)

The `sched` menu reports what a site offers before anything is submitted:
the scheduler's partitions and their limits, the MPIs an install would
choose from, the network from the site resource graph, and the site skills.
A caller such as relay reads it instead of running `sinfo`, `scontrol` and
`spack find` itself.

| Command | Purpose |
|---------|---------|
| `jarvis sched info [--provider P] [+no_mpi] [+json]` | One `jarvis.sched-info.v1` document |

## `jarvis sched info`

```bash
jarvis sched info +json
jarvis sched info --provider slurm +no_mpi +json
```

| Option | Meaning |
|--------|---------|
| `--provider` | The scheduler provider. The default is the first client found on `PATH`: `sbatch` (slurm), `qsub` (pbs), `bsub` (lsf), `flux` (flux) |
| `+no_mpi` | Skip the MPI discovery (`spack find`) |
| `+json` | Print the document as one JSON line |

It runs only light, short reads, so it is safe on a login node: `sinfo`,
`scontrol show partition -o`, one `spack find` per MPI provider and
`spack config get packages`, plus file reads of the resource graph and the
site skills. It never submits, concretizes or builds. A missing piece
becomes an entry in `issues`, never a failure, and the command exits `0`.

### The document

| Field | Meaning |
|-------|---------|
| `schema_version` | `jarvis.sched-info.v1` |
| `provider` | the provider used, or `null` when none was found |
| `providers_supported` | the providers JARVIS has (`slurm`, …) |
| `host`, `observed_at` | where and when it was read |
| `in_allocation` | the allocation this process runs in (the same check the [login-node build guard](scheduler.md#install-jobs) uses), or `null` on a login node |
| `account` | the submit defaults set in the environment: `SBATCH_ACCOUNT`, `SLURM_ACCOUNT`, `SBATCH_QOS`, `SBATCH_PARTITION`, `PBS_ACCOUNT`, `LSB_DEFAULTPROJECT` |
| `partitions[]` | see below |
| `mpi` | see below (absent with `+no_mpi`) |
| `net` | the resource graph's network: `graph`, `site_graph`, `present`, `effective` (`data` and `control`), `override`, `collected_at`, `collected_in` (see [resource-graph-network.md](resource-graph-network.md)) |
| `site_skills[]` | the skills stored next to the graph: `name`, `description`, `path`, `sha256` (see [site-skills.md](site-skills.md)) |
| `issues[]` | what is missing, see below |

Each `partitions[]` entry (Slurm) comes from `sinfo`, with the per-node rows
of one partition merged:

| Field | Meaning |
|-------|---------|
| `partition`, `default`, `available` | the name, whether it is the default (`*`), and its state (`up`) |
| `allocated_nodes`, `idle_nodes`, `other_nodes`, `total_nodes` | node counts |
| `max_time`, `max_time_s` | the time limit as Slurm prints it, and in seconds |
| `cpus_per_node`, `memory_mb_per_node`, `gres` | per-node resources |
| `limits` | from `scontrol show partition -o`: `max_time`, `default_time` (with `_s` seconds), `min_nodes`, `max_nodes`, `max_cpus_per_node`, `default_mem_per_cpu_mb`, `max_mem_per_node_mb`, `qos`, `allow_accounts`, `allow_qos`, `allow_groups` (lists), `state`, `total_nodes`, `total_cpus`, `priority_tier`, `oversubscribe`, `exclusive_user`. `UNLIMITED` is kept as that string |

The `mpi` block:

| Field | Meaning |
|-------|---------|
| `launchers` | the MPI launchers JARVIS can drive (`openmpi`, `mpich`, `intel_mpi`, `cray_mpich`) |
| `path_mpiexec` | the `mpiexec` (or `mpirun`) on `PATH`, if any |
| `spack_available` | whether JARVIS has a Spack to run |
| `spack_candidates[]` | the MPI candidates a Spack install would offer, with the same `id`, `name`, `version`, `constraint`, `prefix`, `hashes`, `external_hashes`, `modules` and `source` as an `ambiguous_provider` question (see [MPI selection](pipeline_tests.md#mpi-selection)) |
| `choice_needed` | always present: `true` when there is more than one candidate, so an install will ask unless a package or a recorded answer pins one |
| `note` | a sentence explaining `choice_needed` when it is `true` |
| `spack_error` | set when Spack itself failed |

### Issues

`issues[]` uses two schemas, told apart by `schema_version`:

| `kind` | Schema | When |
|--------|--------|------|
| `scheduler_missing` | `jarvis.issue.v1` | no scheduler client on `PATH` and no `--provider` |
| `scheduler_unsupported` | `jarvis.issue.v1` | the provider is not one JARVIS has (`details.supported` lists them) |
| `scheduler_query_failed` | `jarvis.issue.v1` | the provider reported no partitions |
| `resource_graph_net_missing` (and the other network issues) | `jarvis.issue.v1` | the resource graph has no usable `net` section, as `jarvis rg net +json` reports it |
| `spack_missing` | `jarvis.install-issue.v1` | JARVIS has no Spack. It is the same issue, with the same `issue_id` and `site_spacks[]`, that `ppl install +check` raises (see [Missing Spack](pipeline_tests.md#missing-spack-spack_missing)) |

The `spack_missing` issue can be answered here too. Write the answer to
`answers.json` and pass it to `ppl install --answers`, or prepare a JARVIS
Spack with the candidate's `prepare` command (see [spack.md](spack.md)).
