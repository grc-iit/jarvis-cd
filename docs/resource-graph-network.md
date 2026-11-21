# Resource graph network (`jarvis rg net`)

The resource graph's `net` section records each node's network interfaces.
From it, JARVIS chooses the network every MPI launch uses. No pipeline
hardcodes an interface, so the same pipeline works on any cluster that has a
resource graph. A person can override the choice in the graph, and the
override keeps its provenance.

## Where the graph lives

| Setting | Graph file |
|---------|------------|
| `JARVIS_RESOURCE_GRAPH=<path>` | `<path>`: one site-level graph shared by every JARVIS root on the site, such as the per-job roots a relay creates. `jarvis init` never overwrites it. |
| unset | `<JARVIS_ROOT>/resource_graph.yaml` (default `~/.ppi-jarvis/resource_graph.yaml`) |

`jarvis rg path` prints the active file. `rg build`, `rg net set` and
`rg net clear` write to it atomically.

`rg net show|set|clear` need no initialized root when
`JARVIS_RESOURCE_GRAPH` is set. Without it, on a root that has no
`jarvis_config.yaml`, they refuse with exit 5 and a `jarvis_not_initialized`
issue (`field: JARVIS_ROOT`) under `+json`. Every other command that needs an
initialized root reports the same issue under `+json`.

## Collecting the `net` section

Collect the section on the compute nodes, never on a login node. A login
node usually sees other networks than the compute nodes. Submit the
`builtin.resource_graph` package as a JARVIS job:

```yaml
name: site_net
pkgs:
  - pkg_type: builtin.resource_graph   # net_only: true by default
scheduler:
  name: slurm
  nodes: 2
  partition: debug
  time: "00:05:00"
```

```bash
JARVIS_RESOURCE_GRAPH=/site/jarvis/resource_graph.yaml \
  jarvis ppl submit site_net.yaml +wait +json
```

Inside an allocation you can also run `jarvis rg build +net_only` directly.
`+net_only` collects only interfaces and keeps the stored storage view. It
[merges per host](#merging-collections) into the stored `net` section. A
full `jarvis rg build` collects both and replaces the whole graph. Each node reports its hostname, its
hostname addresses (`control_addrs`), and per IPv4 interface: `name`, `addr`,
`cidr`, `state`, `speed_mbps`, `kind`, `rdma_device`, `rdma_port_state` and
`ucx_device`. Interfaces with an RDMA device also carry `rdma_port`
(`dev:port`), `rdma_link_layer`, `ucx_status` and `ucx_transports`, and the
node carries a `ucx` summary (see below).

### UCX device verification

A device in sysfs is not necessarily one UCX can use: the port may be down,
the link layer may need transports this UCX build lacks, or the UCX the MPI
loads may have no verbs support at all. So the collector asks UCX itself,
once per node: it runs `ucx_info -v` and `ucx_info -d` (fixed arguments, no
other code) and keeps the devices listed with an RDMA transport (`rc_*`,
`ud_*`, `dc_*`, ...; `tcp`, shared-memory and GPU-copy transports don't
count). Each RDMA interface gets a `ucx_status`:

| `ucx_status` | Meaning |
| --- | --- |
| `usable` | the port is ACTIVE and `ucx_info -d` lists it with an RDMA transport |
| `port_not_active` | the port state isn't ACTIVE |
| `not_listed_by_ucx` | the port is ACTIVE, but this UCX lists no RDMA transport for it |
| `ucx_info_missing` | no `ucx_info` to ask |
| `ucx_info_failed` | `ucx_info` exited non-zero or timed out (`ucx.error`) |

The interface's `ucx_device` is set only when the status is `usable`, and the
network's `ucx_device` only when every member is usable on the same device
(`ucx_verified: true`). Otherwise `UCX_NET_DEVICES` is not set, and the site
fact explains why (`rdma_not_active` or `ucx_device_unusable`). The node's
`ucx` summary records `ucx_info` (path), `version`, `status` and
`rdma_devices` (device to transports).

The `ucx_info` that counts is the one of the UCX the MPI loads. JARVIS uses
`$JARVIS_UCX_INFO` when set, otherwise `ucx_info` on the collecting job's
`PATH`, and passes that absolute path to every node it collects over SSH. If
the MPI uses a different UCX (for example a Spack one), point
`JARVIS_UCX_INFO` at its `ucx_info` and re-collect.

A graph collected before this verification has `ucx_device` without
`ucx_verified`. JARVIS no longer trusts it: the next multi-node MPI launch
[collects the net facts again](#collected-at-launch) on its hosts and pins
the device only if UCX verifies it there. If that collection fails,
`UCX_NET_DEVICES` is withheld (`withheld[].cause: unverified_graph`). A data
network from a person's override is used as set.

### How the collector runs on each node

JARVIS runs the collector locally on its own host and over SSH on every
other host, with the absolute interpreter of the running install:

```bash
env PYTHONPATH=<site-packages that holds jarvis_cd> <abs sys.executable> \
  -m jarvis_cd.core.rg_collector +net_only
```

It does not depend on `PATH`, so a venv, pipx or module install that is called
by path works, even though SSH forwards no environment. The install has to be
on a filesystem every node mounts at the same path. `jarvis_resource_graph`
is a thin wrapper around the same module and can still be run by hand.

### Collection failures

`jarvis rg build` and `builtin.resource_graph` fail when any host returns no
data. They never save a graph built from some of the nodes, because a partial
`net` view could steer MPI onto a network the other nodes don't reach. The
previous graph stays active. The failure is a `jarvis.issue.v1` (exit 5 for
`rg build +json`). For a submitted package, it is in the failed execution
record's `metadata.issues` (`jarvis execution get <id> +json`):

| `kind` | When |
| --- | --- |
| `resource_graph_collect_failed` | no host returned data |
| `resource_graph_collect_partial` | some hosts returned data and others did not |

Both use `field: resource_graph.net`. `details.collected_hosts` lists the
hosts that answered. Each entry of `details.failed_hosts` has `host`, `via`
(`local` or `ssh`), `reason`, `exit_code`, the tail of `stderr`, and the
`command` that was run.

## The `net` section (`jarvis.resource-graph.net.v1`)

```yaml
net:
  schema_version: jarvis.resource-graph.net.v1
  collected_at: '2026-11-21T04:00:00Z'
  collected_on: [ares-comp-01, ares-comp-02]
  collected_in: {provider: slurm, native_id: '5001'}   # null outside a job
  networks:
    - cidr: 172.25.0.0/16
      interface: enp47s0np0          # null when names differ between nodes
      interfaces: {ares-comp-01: enp47s0np0, ares-comp-02: enp47s0np0}
      kind: physical                 # physical, bond, vlan, ipoib, virtual, ...
      speed_mbps: 40000              # slowest member
      rdma_device: rocep47s0
      ucx_device: rocep47s0:1        # only when UCX can use it on every node
      ucx_verified: true
      ucx_status: {ares-comp-01: usable, ares-comp-02: usable}
      nodes: 2
      reaches_all: true
      usable: true
      excluded_reason: null          # loopback, link_local, down, duplicate_address
  selected:
    data:    {cidr: 172.25.0.0/16, interface: enp47s0np0, speed_mbps: 40000, source: graph, ...}
    control: {cidr: 172.20.0.0/16, interface: eno1, source: graph, ...}
  override: null
  nodes: {...}                       # the raw per-node facts
  node_collected:                    # when and in which job each host was collected
    ares-comp-01: {collected_at: '2026-11-21T04:00:00Z', collected_in: {provider: slurm, native_id: '5001'}}
  merge: {mode: merged, kept_hosts: [...], replaced_hosts: [...], added_hosts: [...], collected_on: [...]}
```

### Merging collections

A net-only collection (`rg build +net_only`, `builtin.resource_graph`, or a
[launch-time collect](#collected-at-launch)) merges into the stored section,
per host, under a lock on the graph's directory:

- the collected hosts' entries in `nodes` are replaced;
- hosts it did not collect keep their stored facts, together with their
  `node_collected` time and job;
- a person's `override` is kept;
- `networks`, the exclusions and `selected` are recomputed across the union
  of hosts, so data must still reach every host the graph knows.

`collected_on` is the union. `collected_at` is the latest collection, and
`collected_in` is `null` unless every host was collected inside a job.
`merge.mode` is `new` (no previous section), `merged`, or `replaced` (a
previous section from before per-node facts were stored, with `why`).
`kept_hosts`, `replaced_hosts` and `added_hosts` say what changed. To start
over, run `jarvis rg net clear` or a full `jarvis rg build`.

JARVIS excludes the following networks:

- loopback;
- link-local (`169.254.0.0/16`);
- down links;
- networks where several nodes share the same address (for example
  `docker0` at `172.17.0.1` on every node).

**data** is the fastest usable network that reaches every node. Direct links
(physical, bond, vlan, ipoib) rank before virtual ones. **control** is the
usable network that holds every node's hostname address, falling back to
data.

## Commands

```bash
jarvis rg net show [+json]
jarvis rg net set [--data <cidr|iface|addr|none>] [--control <cidr|iface|addr|none>] \
                  [--reason TEXT] [--set_by NAME] [+json]
jarvis rg net clear [+json]
```

All three print a `jarvis.resource-graph.net-report.v1` document. It has:

- `graph`, the active file, and `site_graph`, which is true under
  `JARVIS_RESOURCE_GRAPH`;
- `present`;
- `collected_at`, `collected_on` and `collected_in`;
- `selected` and `override`;
- `effective`, holding `data`, `control`, and `sources` (`graph` or
  `override` per role);
- `networks`.

If the net was collected outside an allocation, the report carries
`warnings`. Without a net section, the report has `present: false` and a
`resource_graph_net_missing` issue (`field: resource_graph.net`). `set` on a
graph without a net section refuses with that issue and exit 5. With `+json`,
stdout carries only the document.

`rg net set` records a person's override under `net.override`:
`source: person`, `set_at`, `set_by` (default `$USER`), `reason`, and the
`data`/`control` entries. Each entry is resolved against the graph by
CIDR, interface name, or an address inside a network. A value the graph does
not know is kept with `known: false`. `none` disables that role.
Setting only one role keeps the other from the previous override, which is
kept under `previous`. A rebuild of the graph keeps the override. `rg net
clear` removes it.

## How MPI launches use it

Every `MpiExecInfo` launch over more than one host resolves the effective
choice and applies each flavour's own settings:

| Flavour | Launcher | Ranks |
|---------|----------|-------|
| Open MPI / PRRTE | `OMPI_MCA_oob_tcp_if_include` and `PRTE_MCA_oob_tcp_if_include` = control CIDR; `OMPI_MCA_btl_tcp_if_include` = data CIDR | the same, exported to ranks |
| MPICH (Hydra) | `-iface <data iface>` and `HYDRA_IFACE` | `MPIR_CVAR_CH3_NETWORK_IFACE`, `FI_TCP_IFACE` = data iface (`-genv`) |
| Intel MPI | `I_MPI_HYDRA_IFACE` = control iface | `FI_TCP_IFACE`, `FI_SOCKETS_IFACE` = data iface |
| Cray MPICH | nothing: Cray drives the high-speed network itself | nothing |
| any but Cray, when data has a verified `ucx_device` and every launch host is in the graph (or the device comes from an override) | `UCX_NET_DEVICES` | `UCX_NET_DEVICES` |

Open MPI takes CIDRs, which stay correct when interface names differ
between nodes. Hydra and Intel MPI need a name.

Settings that the caller's environment or `container_env` already makes win.
This includes the `_if_exclude` partner of an `_if_include`. They are
reported as `kept_from_caller`. `JARVIS_MPI_NETWORK=off` disables the graph
network for a run. A single-node launch sets nothing.

The applied choice is written to the launch's provenance (`launch.json` →
`network`, schema `jarvis.mpi-network.v1`). It holds `data`, `control`,
`sources`, `override`, `flavour`, `applied` (`launcher_env`, `rank_env`,
`args`), `kept_from_caller`, `hosts`, `hosts_not_in_graph` and, when a
setting was held back, `withheld[]`. Container MPI
launches get the same settings through the container command's
environment.

### Collected at launch

A multi-node MPI launch never goes out without network facts. Before it
applies any setting, `MpiExecInfo` checks the graph against the launch's
hosts. It runs the net-only collector on **every** launch host (read-only:
`ip`, sysfs and `ucx_info`, locally and over SSH, inside the allocation)
when any of these holds:

| Reason | When |
|--------|------|
| `net_missing` | the graph has no `net` section, for example right after a site clean |
| `hosts_not_in_graph` | some launch hosts are not in `net.collected_on` |
| `graph_predates_ucx_verification` | the data pick has an RDMA device but no `ucx_verified` |

It does not collect for Cray MPICH, under `JARVIS_MPI_NETWORK=off`, for a
single-node launch, or when a person's override sets both data and control.

The fresh facts are [merged per host](#merging-collections) into the graph's
`net` section, under a lock on the graph's directory: the launch hosts'
entries are replaced, other hosts and a person's override are kept, and the
selection is recomputed across the union. The launch applies that merged
selection. So a later launch on other nodes adds to the graph instead of
erasing what earlier launches collected, and the next launch on the same
hosts uses the graph without collecting. Because a fresh collect covers every launch host, it also settles
`hosts_not_in_graph` and an unverified UCX device for those hosts: the device
is pinned only when UCX verifies it on each of them.

The launch record's `network.collected_at_launch` has `reasons`, `hosts`,
`collected`, `failed`, `seconds`, `selected` (the merged data and control
CIDR) and `persisted` (`path`, `persisted`, `merge`, or `error`). If the
graph could not be written, the launch still merges in memory.

Advisories a launch raises, including those from its own collect (for
example `ucx_device_unusable` when `ucx_info` fails on one host), appear in
`launch.json` as top-level `advisories` and in `network.advisories` (always
a list), and in the execution record's `metadata.site_facts` as soon as the
launch is captured. Finalization merges them again, so a run killed at its
wall time still carries them.

The launch is refused, with a typed issue in the execution record's
`metadata.issues`, rather than sent out blind:

- **A host returns no facts.** The issue is `resource_graph_collect_failed` or
  `resource_graph_collect_partial` (see [Collection failures](#collection-failures))
  with `details.stage: launch`. The only exception is a collection run for
  `graph_predates_ucx_verification` alone: every host already has facts, so
  the launch uses the stored graph with the device withheld, and records
  `collected_at_launch.fallback`.
- **A `block` site fact in the fresh facts.** No check saw these facts, so
  they go through the check's block rules (stage `launch_collected`), for
  example `no_reachable_network`, or interface names that differ for MPICH or
  Intel MPI. The launch exits 3 with the `site_fact_uncertain` issue, which
  also carries `details.collected_at_launch`. The facts are saved, so the next
  `ppl check` or `ppl submit` raises the same question before submit, and an
  answer with `jarvis rg facts answer` applies to the next run.
- **Advisory site facts** are attached to `network.site_facts`, and the launch
  goes ahead.

Before submit, `ppl check +json` and `ppl submit` say what will happen in
`site_facts.net_plan`: `action` (`collect_at_launch` or `use_graph`), the
`reasons`, and a `note`. With no `net` section, the report has
`net_present: false` and an advisory `site_fact_uncertain` issue with reason
`net_not_collected` (`details.plan: collect_at_launch`; nothing to answer).
To collect ahead of time instead, submit `builtin.resource_graph` with
`net_only: true` over the compute nodes.

### Hosts the graph has not seen

A launch whose hosts the graph has not seen [collects them first](#collected-at-launch),
so after that collection every host is in the graph. The check below is
what a launch reports when the collection did not run for them: under the
`graph_predates_ucx_verification` fallback, or when a person's override sets
both roles. The hosts are listed in `hosts_not_in_graph`, and
`hosts_not_in_graph_check` says whether that matters:

| Field | Meaning |
|-------|---------|
| `severity` | `info`: safe, nothing to do. `warning`: the settings may not fit these hosts |
| `reason` | why, in one sentence |
| `hosts[]` | `host`, its hostname `address`, and `in_chosen_cidrs` (`control`, `data`, both or none) |
| `remedy` | with `warning` only: collect the net section on these hosts |

It is `info` when the flavour takes networks by CIDR (Open MPI and PRRTE)
and every unseen host's hostname address lies in a chosen CIDR. Each node
then binds its own interface in that subnet, just as the graph's nodes do,
so names and speeds that were never collected do not matter. Cray MPICH sets
nothing, so it is `info` too. It is a `warning` when an address is outside
the chosen CIDRs or does not resolve, and for MPICH and Intel MPI, which
set interface names the graph has not seen on those hosts. The launch is
never blocked: a wrong network still shows up as an
[`mpi_network` issue](#network-launch-failures). A `warning` also raises an
advisory `hosts_not_in_graph` [site-fact question](site-facts.md) in
`network.site_facts`. Before submit, a job wider than the graph is an
advisory question for every flavour: the hosts are not known yet, and the
launch collects their facts.

A device name is a per-host fact that CIDRs don't cover: the graph shows the
data network's `ucx_device` usable only on the nodes it saw. So when a
launch host is still outside the graph, `UCX_NET_DEVICES` is withheld and UCX
picks the devices each node has. The CIDR settings still apply. The launch
record's `withheld[]` has `setting`, `value`, `hosts`, `cause`
(`hosts_not_in_graph` or `unverified_graph`, see
[UCX device verification](#ucx-device-verification)) and `reason`; the
`hosts_not_in_graph` entries are also in `hosts_not_in_graph_check.withheld`.
The launch raises an advisory `device_unverified` site-fact question. A
device set by `rg net set` is a person's answer and is never withheld.

## Uncertain picks

When the pick rests on an uncertain fact (near-equal data networks,
interface names that differ, an RDMA port that isn't ACTIVE or that UCX
can't use, a control
fallback, a net collected outside an allocation, unseen hosts, or no
reachable network), JARVIS raises a typed `site_fact_uncertain` question
instead of guessing silently. `rg net show` reports it under `site_facts`.
An agent investigates and proposes, a person confirms with `jarvis rg facts
answer`, and the answer holds until the evidence changes. See
[Site facts](site-facts.md).

## Network launch failures

When a run fails and its output matches an MPI network signature, the
execution record's `metadata.issues[]` gets a `jarvis.issue.v1` of kind
`mpi_network`. Typical signatures are "received unexpected process
identifier", `btl_tcp_endpoint`, "lost communication with a remote daemon",
"no route to host", `oob_tcp` and `HYDRA_sock_connect`. The issue has:

- `field: resource_graph.net.override`: the field a person changes;
- `details.selected_field: resource_graph.net.selected`;
- `details.signatures`, `details.excerpt` and `details.launch_network`: the
  choice that was used;
- `remedy: jarvis rg net set --data <cidr|iface> --control <cidr|iface> --reason <why>`.
