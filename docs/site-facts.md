# Site facts (`jarvis rg facts`)

JARVIS decides deployment details from cluster facts it collected into the
[resource graph](resource_graph.md). The rules are deterministic: JARVIS
collects, excludes, ranks and applies, and stays silent when the pick is
confident. When a pick rests on a fact JARVIS cannot settle by itself, it
doesn't guess silently. It raises a typed **site fact** question that
carries the evidence, the candidates and the probes. An agent investigates
and proposes an answer, a person confirms it, and JARVIS validates and
records it for the site. The next run is exact and silent again, until the
evidence changes.

The same contract covers every uncertain site fact. The first one is
`net.launch`, the MPI launch network (see
[Resource graph network](resource-graph-network.md)).

```text
JARVIS (deterministic)            agent (adaptive)              person
----------------------            ----------------              ------
collect, rank, pick
uncertain? -> site_fact_uncertain ->  run details.probes
              (evidence,              (read-only), maybe a
               candidates, probes)    2-node smoke job
                                      propose a value + why  ->  confirm
validate the value  <------------------------------------------- rg facts answer
record (source: person) per site
next run: confirmed, silent
evidence changed -> ask again, naming the previous answer
```

JARVIS never runs agent-supplied code. An answer is a value (`accept`, or a
CIDR, interface or address per role) that JARVIS checks against the graph.

## The issue: `site_fact_uncertain`

The question is a `jarvis.issue.v1` with `kind: site_fact_uncertain`,
`subject` (for example `net`), and `field: resource_graph.site_facts.<fact>`.
Its identity is `{fact, evidence_digest}`, so `issue_id` stays the same for
as long as the evidence does, at check time and at launch alike.

| Field | Meaning |
|-------|---------|
| `severity` | `block` or `advisory` (also in `details.severity`) |
| `blocking` | `true` for `block` |
| `reason_codes` | the codes of `details.reasons[]`, top level, e.g. `["ucx_device_unusable"]`, so a caller can switch on them without walking `details` |
| `details.schema_version` | `jarvis.site-facts.v1` |
| `details.fact` | `net.launch` |
| `details.stage` | `check` (before submit), `launch` (inside the job, facts a check already saw) or `launch_collected` (inside the job, facts the launch [collected itself](resource-graph-network.md#collected-at-launch)) |
| `details.reasons[]` | `code`, `severity`, `roles` (`data`, `control`), `message`, `consequence`, plus per-code facts (`near`, `names`, `port_states`, `hosts`) |
| `details.evidence` | the raw facts the question rests on (below) |
| `details.evidence_digest` | `sha256:` of the canonical evidence JSON |
| `details.pick` | what JARVIS would use: `data`, `control`, `flavour`, `consequence_if_wrong[]` |
| `details.probes[]` | read-only commands an agent may run on the nodes, each with `command` and `why` |
| `details.answer` | `field`, accepted `values`, the `command`, an `answers_file` example, `answers_flag`, `suppressed_while` |
| `details.previous_answer` | when the evidence changed: the old answer's `issue_id`, `evidence_digest`, `value`, `reason`, `set_by`, `set_at` |
| `supersedes` | the old answer's `issue_id` |
| `candidates[]` | every network with `rank`, `cidr`, `interface`, `kind`, `speed_mbps`, `picked_as`, and `why` it ranks there (or why it is excluded) |
| `remedy` | `jarvis rg facts answer <issue_id> --value accept --reason <why>`, or `jarvis rg net set ...` |

### Evidence and its digest

`details.evidence` holds only raw facts, so a digest changes only when the
cluster does:

- `fact`, `flavour_needs_names` (true for MPICH and Intel MPI),
  `graph_covers_launch`;
- `collected_on`, `collected_in_allocation`;
- the networks (CIDR, interface, kind, speed, the nodes they reach,
  exclusions);
- per node: the control addresses, and each interface's `name`, `cidr`,
  `addr`, `kind`, `speed_mbps`, `state`, `rdma_device`, `rdma_port_state` and
  `ucx_device`, plus `rdma_port`, `rdma_link_layer`, `ucx_status` and
  `ucx_transports` when the collector recorded them, and the node's `ucx`
  `status`, `ucx_info` and `rdma_devices`. Keys an older collector didn't
  record stay out, so those graphs keep their digests.

It holds no timestamps, flavour name, reason codes or override, so
re-collecting the same cluster gives the same `issue_id`.

## `net.launch` reasons and severity

A reason blocks only when a wrong pick could make the run fail or silently
use the wrong network. Everything else is advisory: the run goes ahead with
JARVIS's pick, and the issue rides along so an agent can still engage.

| Code | When | Severity |
|------|------|----------|
| `no_reachable_network` | no usable network reaches every node of the graph | block |
| `near_equal_data` | another network has the data network's class and is within 10% of its speed, or both speeds are unknown | advisory |
| `interface_names_differ` | the data or control network has different interface names on the nodes | block for MPICH and Intel MPI (they take a name); advisory when the flavour isn't known yet; silent for Open MPI (it takes the CIDR) |
| `rdma_not_active` | the data network has an RDMA device whose port isn't ACTIVE on every node, so `UCX_NET_DEVICES` isn't set | advisory |
| `ucx_device_unusable` | the data network's RDMA port is ACTIVE, but UCX on some nodes can't use it (`ucx_status` `not_listed_by_ucx`, `ucx_info_missing` or `ucx_info_failed`), so `UCX_NET_DEVICES` isn't set. `ucx` maps each such host to its `status`, `port`, `link_layer`, `ucx_info`, `ucx_version` and `ucx_rdma_devices` | advisory |
| `control_fallback` | no network holds every node's hostname address, so control fell back (possibly to the data network) | advisory |
| `collected_outside_allocation` | the net section wasn't collected inside a scheduler allocation | advisory |
| `hosts_not_in_graph` | before submit: the job asks more nodes than the graph was collected on (`plan: collect_at_launch`: the launch collects their facts first). At launch, when that collection didn't run for them: the [hosts check](resource-graph-network.md#hosts-the-graph-has-not-seen) is a `warning` | advisory |
| `device_unverified` | the graph's `ucx_device` is withheld. At launch: because some launch hosts aren't in the graph (`cause: hosts_not_in_graph`), or because the graph predates UCX verification and the collection at launch failed (`cause: unverified_graph`); it carries `setting`, `value`, `hosts`. Before submit: only the `unverified_graph` case (`plan: collect_at_launch`) | advisory |
| `net_not_collected` | before submit: the graph has no net section and the job spans more than one node. The issue says the launch will collect the facts (`details.plan: collect_at_launch`); there is nothing to answer, and its identity is `{fact, reason}` | advisory |

At the `launch` stage a question never blocks, because the allocation is
already running and a check already saw those facts. It is recorded with the
launch instead. At the `launch_collected` stage the launch collected the
facts itself, so no check saw them: a `block` reason refuses the launch with
exit 3, and the issue (with `details.collected_at_launch`) lands in the
execution record's `metadata.issues`. The facts are saved to the graph, so
the next check asks the same question before submit, and an answer recorded
with `rg facts answer` covers the next launch.

A reason whose roles are all covered by a person's `rg net set` override is
listed under `resolved_by_override` rather than asked.

### Transport fallback (UCX → TCP) is a proposal, not an ask

When UCX can't be used on the data network (`rdma_not_active`,
`ucx_device_unusable`, `device_unverified`), JARVIS withholds
`UCX_NET_DEVICES`, and for Open MPI also UCX (`OMPI_MCA_pml` and
`OMPI_MCA_osc` set to `^ucx`), so the run uses TCP. TCP is correct but
slower, so this is advisory and never exits 3:

- before submit, the issue goes to the check's `warnings[]` and the record's
  `metadata.site_facts`;
- at launch, inside the allocation, it goes to `launch.json` `advisories`,
  because nobody is there to answer.

It is still a proposed decision that the person sees. The issue's
`details.pick` holds JARVIS's choice and `consequence_if_wrong`, and
`reason_codes` names the cause. The person can confirm the proposal with
`rg facts answer <issue_id> --value accept`, which silences it while the
evidence is unchanged. Or they can choose otherwise: `rg net set` an
override, or re-collect the facts where the ranks run
(`details.probes`).

Each question is evaluated only for launches over more than one node. With
no net section, the only check-time question is `net_not_collected`. Cray
MPICH and `JARVIS_MPI_NETWORK=off` (in the environment or the pipeline's
env) get no network questions.

Before submit the per-host reasons (`rdma_not_active`, `ucx_device_unusable`,
`control_fallback`) look at every host of the graph. At a launch they look
only at the launch hosts the graph holds: in a graph merged from several
jobs, a host outside this launch raises nothing for it. The evidence (and so
`evidence_digest` and `issue_id`) stays graph-wide at both stages, so an
answer recorded before submit still covers the launch.

## Where questions appear

| Where | What |
|-------|------|
| `jarvis rg facts show +json` | the `jarvis.site-facts.v1` report (below) |
| `jarvis rg net show +json` | the same report under `site_facts` |
| `jarvis ppl check +json` | `site_facts`. Blocking questions go to `questions[]`, advisory ones to `warnings[]`, and `ok` is false while `questions` is non-empty. The command exits 3 on questions (5 on errors). `site_facts.net_plan` says what the launch will do about the network: `action` (`collect_at_launch` or `use_graph`), `reasons`, `note`; `site_facts.net_present` says whether the graph has a net section |
| `jarvis ppl submit` | refuses before any scheduler call with exit 3 and the blocking issues (`+json` prints `{exit_code, issues}`). Advisory questions are submitted with the job |
| execution record | `metadata.site_facts`: `schema_version`, `issues[]` (deduplicated by `issue_id` and `reason_codes`; a launch's evaluation of a question, scoped to its hosts, replaces the check's graph-wide one, and each launch's distinct version carries `launch`), `advisories[]` (the non-blocking ones), `confirmed[]`, `superseded_by_launch[]` (the check's `net.launch` issue ids that a launch's own evaluation replaced: before submit the hosts aren't known, so the check judges the whole graph; once a launch evaluated its hosts, that evaluation is the record, even when it found nothing), and `stages[]`, one per evaluation (`stage`, `launch`, `blocking`, `issue_ids`, `confirmed`). Updated at check/submit, again when each launch is captured, and at finalization. The full report is always in `<execution>/site-facts.json`. The record's metadata is capped (48 KB), so when the report exceeds 24 KB the record keeps a trimmed copy: issues and advisories keep their identity, `severity`, `blocking`, `reason_codes`, `message`, `remedy`, `details.reasons` (without per-host evidence) and `details.pick`, then only identity and codes if that is still too large. `trimmed: {reason, level, full_in}` marks it, each trimmed issue has `trimmed: true`, and `full_in` is the path of `site-facts.json` |
| launch provenance | `provenance/launch-*/launch.json` → top-level `advisories` and `site_facts`, and `network.site_facts` / `network.advisories`, for every multi-node launch the network rules evaluated (with an empty `issues[]` when that launch raised nothing) |
| the launcher object | after an `MpiExecInfo` launch, the executor (`Exec`, `OpenMpiExec`, ...) has `advisories`, `site_facts`, `network` and `launch_json` attributes, and `launch_record()` returns a `jarvis.mpi-launch.v1` dict (`command`, `nprocs`, `ppn`, `hosts`, `network`, `advisories`, `site_facts`, `placement`, `launch_json`). A package that writes its own launch file should embed `launch_record()`, not rebuild it from executor attributes, so `advisories` is never lost |

The report is:

```json
{
  "schema_version": "jarvis.site-facts.v1",
  "stage": "check",
  "answers_path": "<site home>/site-facts/answers.yaml",
  "issues": [],
  "confirmed": [{"fact": "net.launch", "issue_id": "sha256:...", "evidence_digest": "sha256:...",
                 "value": "accept", "reason": "...", "set_by": "...", "set_at": "...",
                 "source": "person", "reasons": ["near_equal_data"]}],
  "resolved_by_override": [{"fact": "net.launch", "reasons": ["interface_names_differ"], "override": {}}],
  "blocking": false
}
```

The MPI flavour comes from the pipeline's last install (a single MPI), or
from `JARVIS_MPI_FLAVOUR` (`openmpi`, `mpich`, `intel`, `cray`). When it is
unknown, name-dependent reasons are advisory.

## Commands

```bash
jarvis rg facts show [--nodes N] [--flavour F] [+json]
jarvis rg facts answer <issue_id> --value accept|'{"data": X, "control": Y}' --reason R \
                       [--set_by NAME] [--proposed_by AGENT] [+json]
jarvis rg facts answer --answers answers.json [+json]
jarvis rg facts forget <fact> [+json]
```

- `show` evaluates the facts for a launch over `--nodes` nodes (default:
  the graph's nodes) and `--flavour`. It also lists the recorded `answers`.
  It exits 0 whether or not there are questions. Without a net section it
  reports `net_present: false` and the `resource_graph_net_missing` issue as
  `net_issue`.
- `answer` validates and records an answer for the site:
  - `accept` keeps JARVIS's pick;
  - a JSON object with `data` and/or `control` sets those roles. Each value
    is a CIDR, an interface name, an address inside a network, or `none`.
    It is checked against the graph: a network the graph excludes (shared
    bridge, link-local, loopback, down) is refused. A well-formed value the
    graph has never seen (for example a bond the collector didn't report) is
    kept with `known: false`. The roles are applied as a person's `rg net
    set` override, so they win at launch;
  - `--reason` is required and kept as provenance. `--set_by` defaults to
    `$USER`; `--proposed_by` names the agent that proposed the value;
  - `--answers` takes `{"<issue_id>": {"value", "reason", "set_by", "proposed_by"}}`.
- `forget` removes the recorded answer for a fact, so JARVIS asks again. An
  override the answer set stays in the graph until `jarvis rg net clear`.
  When the site also publishes an answer for the fact, the report names it
  under `published_still_applies`.

Every command with `+json` prints only the document.

| Refusal (exit 5) | When |
|------------------|------|
| `site_fact_unknown` | the `issue_id` was never asked, or `forget` found nothing |
| `site_fact_answer_stale` | the evidence changed since the question was asked; `details.current_issue_id` names the current one |
| `site_fact_answer_invalid` | no `--reason`, a malformed value, or a value the graph excludes |
| `site_fact_published` | `forget` of a fact whose only answer is the one the site publishes, which JARVIS only reads |

## The store

Answers live per site, in JARVIS's site home (see
[Site-level graph](resource_graph.md#site-level-graph-jarvis_resource_graph)):
`JARVIS_SITE_HOME`, else `~/.ppi-jarvis/site` when `JARVIS_RESOURCE_GRAPH`
names a site graph, else the JARVIS root:

```text
<site home>/site-facts/
  answers.yaml     # per fact: value, reason, source: person, set_by, set_at,
                   # proposed_by, issue_id, evidence_digest, validation
  asked/<hex>.json # each question asked, with the inputs that produced it
  .lock            # flock taken around every write
```

An answer suppresses its question only while the current evidence digest
equals the recorded one. When the evidence changes, the new question carries
`details.previous_answer` and `supersedes`, and the old `issue_id` is
refused as stale.

A site may publish confirmed answers beside its graph, in
`<dir of JARVIS_RESOURCE_GRAPH>/site-facts/answers.yaml` (same layout).
JARVIS reads them, tagged `published_in`, and never writes there. An answer
in JARVIS's own store for the same fact wins.

## For callers (relay, CLIO)

Pass the issues through unchanged. The loop for an agent is:

1. Read `details.reasons`, `details.evidence` and `candidates`.
2. Investigate with `details.probes` (read-only: `ip`, `/sys/class/net`,
   `ibv_devinfo`, `ucx_info`, or a 2-node smoke job through
   `jarvis ppl submit`). Don't run anything else on the person's behalf.
3. Propose a value with its reason, and name the consequence from
   `details.pick.consequence_if_wrong`.
4. Ask the person to confirm. Then run `jarvis rg facts answer <issue_id>
   --value ... --reason ... --set_by <person> --proposed_by <agent>`.
5. For a blocking question, re-run `jarvis ppl check` or `jarvis ppl submit`.
   For an advisory one, the run has already gone ahead.
