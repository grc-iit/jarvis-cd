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

It runs only light, short reads, so it is safe on a login node: on Slurm
`sinfo` and `scontrol show partition -o`; on PBS `qstat -Qf` and `qstat -Bf`;
on LSF `bqueues -l` and `bparams`; on Flux `flux queue list`. It also runs one
`spack find` (kept to the MPI providers) and
`spack config get packages`, plus file reads of the resource graph and the
site skills. It never submits, concretizes or builds. A missing piece
becomes an entry in `issues`, never a failure, and the command exits `0`.

### The document

| Field | Meaning |
|-------|---------|
| `schema_version` | `jarvis.sched-info.v1` |
| `provider` | the provider used, or `null` when none was found |
| `providers_supported` | the providers JARVIS can submit to (`slurm`, …) |
| `providers_queues_readable` | the providers whose queues and limits `sched info` reads (`flux`, `lsf`, `pbs`, `slurm`) |
| `host`, `observed_at` | where and when it was read |
| `in_allocation` | the allocation this process runs in (the same check the [login-node build guard](scheduler.md#install-jobs) uses), or `null` on a login node |
| `account` | the submit defaults set in the environment: `SBATCH_ACCOUNT`, `SLURM_ACCOUNT`, `SBATCH_QOS`, `SBATCH_PARTITION`, `PBS_ACCOUNT`, `LSB_DEFAULTPROJECT` |
| `partitions[]` | see below |
| `mpi` | see below (absent with `+no_mpi`) |
| `net` | the resource graph's network: `graph`, `site_graph`, `present`, `form` (`v1`, `legacy_list`, `unreadable`, `absent`), `effective` (`data` and `control`), `override`, `collected_at`, `collected_in`, `legacy` (a legacy list as read) and `error` (why it is unreadable) (see [resource-graph-network.md](resource-graph-network.md)) |
| `cpu` | the resource graph's `cpu` section (`jarvis.resource-graph.cpu.v1`): the topology the collector read on each node it ran on, or `null` before a collection (see [Whole cores per rank](scheduler.md#whole-cores-per-rank)) |
| `site_skills[]` | the site's skills: `name`, `description`, `path`, `sha256`, `source` (`site` when the site publishes it beside its graph, `jarvis` for JARVIS's own; see [site-skills.md](site-skills.md)) |
| `issues[]` | what is missing, see below |

Each `partitions[]` entry (Slurm) comes from `sinfo`, with the per-node rows
of one partition merged:

| Field | Meaning |
|-------|---------|
| `partition`, `default`, `available` | the name, whether it is the default (`*`), and its state (`up`) |
| `allocated_nodes`, `idle_nodes`, `other_nodes`, `total_nodes` | node counts |
| `max_time`, `max_time_s` | the time limit as Slurm prints it, and in seconds |
| `cpus_per_node`, `memory_mb_per_node`, `gres` | per-node resources. `cpus_per_node` counts hardware threads (Slurm CPUs) |
| `sockets_per_node`, `cores_per_socket`, `threads_per_core` | the node layout (`sinfo` `%X`, `%Y`, `%Z`) |
| `physical_cores_per_node` | `sockets_per_node` × `cores_per_socket`: physical cores, without hyperthreads. Size one MPI rank per core from this, not from `cpus_per_node` |
| `heterogeneous` | `true` when the partition's nodes differ. The per-node fields then give the smallest node, so a job sized from them fits on any node of the partition |
| `limits` | the per-job limits, with the same keys for every scheduler (see [Queue limits](#queue-limits)) |
| `limits_unknown` | the `limits` keys the scheduler did not report |
| `limits_source` | where the limits came from: `provider`, `command`, `fields` (each known key's native field) and, when the read failed, `error` (`command`, `exit_code`, `output`) |

#### PBS, LSF and Flux queues

JARVIS cannot submit to PBS, LSF or Flux yet, but `sched info` reads their
queues. Each queue is one `partitions[]` entry with the same fields. Only
`partition`, `default`, `available`, `total_nodes`, `max_time` and the three
`limits` fields are filled; the per-node fields are `null`.

| Scheduler | Queues and limits from | Default queue from | Extra fields |
|-----------|------------------------|--------------------|--------------|
| PBS Pro, OpenPBS, Torque | `qstat -Qf` (a queue limit the queue leaves unset falls back to the server's in `qstat -Bf`) | `qstat -Bf` `default_queue` | `queue_type` (`Execution` or `Route`), `route_destinations`, `from_route_only` |
| LSF | `bqueues -l` | `bparams` `Default Queues` (it can name several) | none |
| Flux | `flux queue list` with an explicit format (older flux-core: the `policy` format, then the default table, whose node range is the effective one) | the `*` on the queue name | `parent` (virtual queues) |

`default` is `null` when the default queue could not be read. Flux without
named queues has a single anonymous queue: `partition` is `null` and
`default` is `true`.

#### Queue limits

Every `limits` holds all of these keys:

| Key | Slurm (`scontrol`) | PBS (`qstat -Qf`) | LSF (`bqueues -l`) | Flux (`flux queue list`) |
|-----|--------------------|-------------------|--------------------|--------------------------|
| `max_time`, `max_time_s` | `MaxTime` | `resources_max.walltime` | `MAXIMUM LIMITS` `RUNLIMIT` (as printed, e.g. `2880.0 min of HOST_MODEL`) | `TLIMIT` |
| `default_time`, `default_time_s` | `DefaultTime` | `resources_default.walltime` | `DEFAULT LIMITS` `RUNLIMIT` | `TDEFAULT` |
| `min_nodes` | `MinNodes` | `resources_min.nodect` | unknown | `NNODES` low end |
| `max_nodes` | `MaxNodes` | `resources_max.nodect` (Torque: the count in `resources_max.nodes`) | `HOSTLIMIT_PER_JOB` | `NNODES` high end |
| `state` | `State` | `enabled` and `started` | `STATUS` | `EN` and `ST` |
| `priority_tier` | `PriorityTier` | `Priority` | `PRIO` | unknown |
| `allow_groups` | `AllowGroups` | `acl_groups` when `acl_group_enable` is set, else `["ALL"]` | `USERS` when it is `all` or only groups | unknown |
| `total_nodes`, `total_cpus` | `TotalNodes`, `TotalCPUs` | `resources_available.nodect`, `.ncpus` | unknown | unknown |
| `max_cpus_per_node`, `default_mem_per_cpu_mb`, `max_mem_per_node_mb`, `qos`, `allow_accounts`, `allow_qos`, `oversubscribe`, `exclusive_user` | the `scontrol` field of that name | unknown | unknown | unknown |

The values mean:

* A number or a string is the scheduler's value. `_s` keys are seconds.
* `UNLIMITED` means unlimited (Slurm `UNLIMITED`, Flux `inf`).
* `null` with the key **not** in `limits_unknown` means the scheduler reports
  no value (Slurm `NONE` or `N/A`).
* `null` with the key in `limits_unknown` means unknown, never unlimited. The
  scheduler does not expose it, or printed nothing for it, or the read
  failed (`limits_source.error`). PBS and LSF print only the limits a queue
  sets, so an unknown there is often unset, but other site policy (hooks,
  application profiles, QoS) may still limit jobs.

`state` uses Slurm's words: `UP` (accepts and runs jobs), `DOWN` (accepts, does
not run), `DRAIN` (runs, does not accept new jobs), `INACTIVE`. PBS and Flux
map enabled/started, and LSF maps `Open`/`Closed` and `Active`/`Inact`.
`available` is the same state in lower case (`up`, `down`, `drain`, `inact`).

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
| `scheduler_unsupported` | `jarvis.issue.v1` | JARVIS cannot submit to this provider (`details.supported` lists the ones it can). For PBS, LSF and Flux the queues are still read, and `details.queues_read` is `true` |
| `scheduler_query_failed` | `jarvis.issue.v1` | the provider reported no partitions, or a queue or limits read failed (`details` holds the raw `command`, `exit_code` and `output`) |
| `resource_graph_net_missing` (and the other network issues) | `jarvis.issue.v1` | the resource graph has no usable `net` section, as `jarvis rg net +json` reports it. A legacy list-form `net` is read and gives the advisory `resource_graph_net_legacy` instead; a `net` or graph file that can't be read gives `resource_graph_net_unreadable` with the error (see [Resource graph network](resource-graph-network.md)). `net.form` says which |
| `spack_missing` | `jarvis.install-issue.v1` | JARVIS has no Spack. It is the same issue, with the same `issue_id` and `site_spacks[]`, that `ppl install +check` raises (see [Missing Spack](pipeline_tests.md#missing-spack-spack_missing)) |
| `jarvis_not_initialized` | `jarvis.issue.v1` | `JARVIS_ROOT` has no `jarvis_config.yaml`. The rest of the document is still filled in, and the command still exits 0; `field` is `JARVIS_ROOT` and `remedy` says to run `jarvis init` once on that root. Pipeline commands refuse until then |

`sched info` does not need an initialized root. It reads the site graph
named by `JARVIS_RESOURCE_GRAPH` when that is set, overlaid with JARVIS's
own graph in the site home (see
[Site-level graph](resource_graph.md#site-level-graph-jarvis_resource_graph)),
and otherwise `<JARVIS_ROOT>/resource_graph.yaml` if it exists.

The `spack_missing` issue can be answered here too. Write the answer to
`answers.json` and pass it to `ppl install --answers`, or prepare a JARVIS
Spack with the candidate's `prepare` command (see [spack.md](spack.md)).
