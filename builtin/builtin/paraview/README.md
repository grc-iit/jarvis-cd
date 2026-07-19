# ParaView

`builtin.paraview` supports three explicit modes:

- `server` launches `pvserver` for a native ParaView client;
- `batch` launches a user/site-owned `pvbatch` script and records structured
  progress and generated artifacts;
- `service` launches the generic JARVIS HTTP/SSE runtime for relay and agentic
  clients.

Service mode requires a `jarvis.dataset-descriptor.v1` JSON document. The
descriptor contains intrinsic identity, bounded source members, optional time
and array facts, and a SHA-256 fingerprint. It cannot contain a camera,
threshold, color transfer, or scene recipe. Those are explicit versioned
commands sent to the running service.

Service-state v2 is an explicit scene graph, not a set of global view aliases.
It starts with a stable reader node and visible root actor. Commands create
branching slice, clip, threshold, and point-scalar contour nodes; create or
update independently addressable surface/point actors; measure fields over
explicit timesteps; fit a camera over explicit actors and real bounds; inspect
one targeted actor; and export exactly the visible scene. Multiple actors may
reference the same node, and each field-colored actor explicitly owns a
separate ParaView lookup table and opacity transfer function. Field changes,
solid coloring, actor removal, and rollback retire those proxies explicitly,
and scalar bars are hidden before an actor-removal frame. A hidden actor's
authoritative state always disables its scalar bar; visible actors may
intentionally expose independent bars.

`measure_field` returns a stable measurement with per-timestep samples and a
deterministic aggregate. Observed range, tuple count, bounded native Histogram
evidence, and `scalar|magnitude` basis remain distinct from the transfer range
applied to pixels. Actor range policies are current full-range recomputation,
frozen explicit bounds, or frozen percentiles from a named measurement. Native
linear/log scales are supported; symmetric-log is not claimed. Sending
`preset:null` requests the package's verified generic default and state reports
the resolved non-null preset.

`inspect_selection` requires a representation ID plus either an explicit
point/cell index or normalized viewport drag box. Point actors use ParaView's
surface-point picker and surface actors use its surface-cell picker. Results
contain bounded real IDs, explicit empty status, or explicit unsupported
status; JARVIS never guesses a world-space location from browser coordinates.
Element indexes are process-0 identities and are rejected for composite data;
viewport materialization is refused when the relevant source count is unknown
or exceeds 10,000,000 elements.

Camera state explicitly records position, focal point, view-up, positive
parallel scale, `perspective|parallel` projection, and a view angle strictly
between 0 and 180 degrees. Runtime array discovery admits at most 256 point and
cell arrays in total and fails rather than truncating. Table topology is not a
supported v2 scene.

Every command is transactional through backend mutation, frame render,
complete state validation, canonical response validation, and semantic commit.
Artifact export stages a private PNG and uses an fsynced deterministic
transaction marker to recover power loss without rewriting durable artifact
history. State, response, command, frame, scene-object, timestep, and stored
measurement sizes are explicitly bounded.

```bash
jarvis ppl append paraview \
  mode=service \
  dataset_descriptor=/site/descriptors/asteroid-subset.json \
  service_bind_host=127.0.0.1 \
  service_advertise_host=127.0.0.1 \
  nprocs=1
jarvis ppl submit +no_wait +json
jarvis execution service-runtimes <execution-id> +json
```

The package resolves `pvpython`, `pvbatch`, or `pvserver` (according to mode)
from the JARVIS pipeline execution environment. Service mode is intrinsically
headless: the package probes the selected launcher and chooses one supported
backend, preferring `--mesa` and otherwise using
`--force-offscreen-rendering`. Executable paths and raw launcher flags are
intentionally not package parameters. If the selected environment does not
provide the mode-specific ParaView executable, the package fails before
application launch with an explicit dependency error.

JARVIS reports the actual service host/port and lifecycle through the durable
execution handle. The public v2 runtime record contains only the SHA-256 digest
of its owner-private 64-hex Bearer credential, while every service request
requires the raw token. A trusted cluster-side connector resolves that secret
through JARVIS's explicit non-MCP authority resolver and injects it on the
cluster side; VIGIL, browsers, logs, normal runtime queries, and local
agent-facing results must never receive it. Direct SSH tunnel instructions are
intentionally not part of the package contract.

See [`docs/service-runtimes.md`](../../../docs/service-runtimes.md) for the
exact report, state, command, SSE, and artifact schemas.
