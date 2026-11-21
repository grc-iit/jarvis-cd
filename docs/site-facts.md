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
| `details.schema_version` | `jarvis.site-facts.v1` |
| `details.fact` | `net.launch` |
| `details.stage` | `check` (before submit) or `launch` (inside the job) |
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
  `ucx_device`.

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
| `control_fallback` | no network holds every node's hostname address, so control fell back (possibly to the data network) | advisory |
| `collected_outside_allocation` | the net section wasn't collected inside a scheduler allocation | advisory |
| `hosts_not_in_graph` | before submit: the job asks more nodes than the graph was collected on. At launch: the [hosts check](resource-graph-network.md#hosts-the-graph-has-not-seen) is a `warning` | before submit, block for MPICH and Intel MPI, advisory otherwise. At launch, advisory |

At the `launch` stage a question never blocks, because the allocation is
already running. It is recorded with the launch instead.

A reason whose roles are all covered by a person's `rg net set` override is
listed under `resolved_by_override` rather than asked.

Each question is evaluated only for launches over more than one node, and
only when the graph has a net section.

## Where questions appear

| Where | What |
|-------|------|
| `jarvis rg facts show +json` | the `jarvis.site-facts.v1` report (below) |
| `jarvis rg net show +json` | the same report under `site_facts` |
| `jarvis ppl check +json` | `site_facts`. Blocking questions go to `questions[]`, advisory ones to `warnings[]`, and `ok` is false while `questions` is non-empty. The command exits 3 on questions (5 on errors) |
| `jarvis ppl submit` | refuses before any scheduler call with exit 3 and the blocking issues (`+json` prints `{exit_code, issues}`). Advisory questions are submitted with the job |
| execution record | `metadata.site_facts`: `schema_version`, `issues[]` (deduplicated by `issue_id`), `confirmed[]`, and `stages[]`, one per evaluation (`stage`, `launch`, `blocking`, `issue_ids`, `confirmed`) |
| launch provenance | `provenance/launch-*/launch.json` → `network.site_facts`, when the launch raised or confirmed a question |

The report is:

```json
{
  "schema_version": "jarvis.site-facts.v1",
  "stage": "check",
  "answers_path": "<graph dir>/site-facts/answers.yaml",
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

Every command with `+json` prints only the document.

| Refusal (exit 5) | When |
|------------------|------|
| `site_fact_unknown` | the `issue_id` was never asked, or `forget` found nothing |
| `site_fact_answer_stale` | the evidence changed since the question was asked; `details.current_issue_id` names the current one |
| `site_fact_answer_invalid` | no `--reason`, a malformed value, or a value the graph excludes |

## The store

Answers live per site, next to the graph (`JARVIS_RESOURCE_GRAPH`, else the
JARVIS root's graph):

```text
<graph dir>/site-facts/
  answers.yaml     # per fact: value, reason, source: person, set_by, set_at,
                   # proposed_by, issue_id, evidence_digest, validation
  asked/<hex>.json # each question asked, with the inputs that produced it
  .lock            # flock taken around every write
```

An answer suppresses its question only while the current evidence digest
equals the recorded one. When the evidence changes, the new question carries
`details.previous_answer` and `supersedes`, and the old `issue_id` is
refused as stale.

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
