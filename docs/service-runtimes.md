# Execution-owned service runtimes

JARVIS applications may expose a long-lived network service as part of a
durable pipeline execution. JARVIS owns the execution and package identity,
persists lifecycle observations without scraping stdout, and returns current
service records from the same `ExecutionHandle` used for status, progress, and
artifacts.

```python
handle = pipeline.submit(wait=False)
for runtime in handle.service_runtimes().service_runtimes:
    print(runtime.service_instance_id, runtime.lifecycle, runtime.base_url)
```

The machine-readable CLI form is:

```bash
jarvis execution service-runtimes <execution-id> \
  --pipeline-id <pipeline> +json
```

The result is an exact `jarvis.execution.service-runtimes.v1` document with
`schema_version`, `execution_id`, `pipeline_id`, `execution_state`, `terminal`,
and `service_runtimes`. The array contains the latest validated revision for
every concrete service instance. Current reporters emit
`jarvis.service-runtime.v2`; readers retain strict v1 compatibility for stored
executions created before authorization became part of the runtime contract.

## Package reporting contract

Before package startup, execution core exports:

- `JARVIS_EXECUTION_ID`;
- `JARVIS_PACKAGE_NAME`;
- `JARVIS_PACKAGE_ID`;
- `JARVIS_SERVICE_RUNTIME_PATH`, an owner-private JSONL sidecar inside the
  execution root.

Packages use `ServiceRuntimeReporter.from_environment(...)`. They choose the
service instance, actual advertised host and bound port, protocol, and
intrinsic dataset descriptor; they cannot choose another execution/package
identity or storage path. Every revision is appended under an inter-process
lock and fsynced. Revisions increase by one per `service_instance_id` and the
validated lifecycle is:

```text
starting -> ready <-> degraded -> stopping -> stopped
     |          |          |          |
     +----------+----------+----------+-> failed
```

`stopped` and `failed` cannot reopen. Host, port, protocol, endpoint paths,
delivery mode, and dataset descriptor cannot change within one service
instance.

Every public v2 report contains only
`authorization:{"scheme":"bearer","token_sha256":"<64 lowercase hex>"}`.
`ServiceRuntimeReport`, execution snapshots, `service-runtimes +json`, and
their normal Python representations never contain the raw Bearer capability.
The package generates the 64-hex token in an owner-private file, passes only
that file path to the child service, and stores the raw authority beside the
public report in an exact `jarvis.service-runtime.private.v1` envelope. Token
files and private sidecars remain owner-readable only.

A trusted cluster-side connector resolves the raw authority through the
separate, non-MCP command only after it has the complete current public
identity:

```bash
jarvis execution resolve-service-runtime-authority <execution-id> \
  --pipeline-id <pipeline-id> \
  --package-id <package-id> \
  --service-instance-id <service-instance-id> \
  --revision <positive-integer> \
  --token-sha256 <64-lowercase-hex> \
  +json
```

The command returns exactly one `jarvis.execution.service-runtime-authority.v1`
JSON line containing that identity and
`authorization:{"scheme":"bearer","token":"<64 lowercase hex>"}`. A stale
revision, digest, service, package, execution, or pipeline fails closed. This
resolver is an internal connector boundary, not an agent-facing discovery
surface. Relay injects the resolved credential into the cluster-side request
and must never expose it to VIGIL, a browser, logs, or local agent tool results.

Each report explicitly contains `/healthz`, `/live-data`, `/events`, `/state`,
and `/commands` paths. `delivery_mode` is `push`. These cluster endpoints are
not desktop URLs: relay owns authenticated routing, connectors, and local URL
publication. Every ParaView service GET and POST, including `/healthz`, rejects
a missing or incorrect Bearer token with HTTP 401.

## Dataset descriptor

`jarvis.dataset-descriptor.v1` contains only intrinsic data facts:

- a stable `dataset_id`, semantic `kind`, and format;
- 1-512 unique, ordered, normalized absolute cluster members and optional
  physical timestep values;
- up to 256 unique array identities with association, component count, and
  optional units, plus optional finite bounds;
- a required canonical lowercase SHA-256 fingerprint and an optional source
  JARVIS artifact identity and checksum.

The fingerprint covers canonical JSON for the complete descriptor with the
`fingerprint` field omitted. Member ordering, arrays, bounds, and optional
source artifact therefore participate in identity. Both JARVIS execution core
and the staged `pvpython` service enforce the same stable constraints and
recompute the digest before opening any file. The service also verifies that
every selected member exists before readiness.

The descriptor cannot choose a camera, color transfer, threshold, scene graph,
or rendering recipe. Selecting data never silently selects a view.

## ParaView service state

`builtin.paraview mode=service` launches a real `pvpython` service under a
JARVIS supervisor. `ready` is reported only after the versioned health probe
succeeds. Three consecutive probe failures report `degraded`; a later success
reports recovery. Process exit reports `stopped` or `failed`, and owned
termination is bounded.

`GET /state` returns `jarvis.paraview.service-state.v2`. Its `pipeline` has
exactly these fields:

```json
{
  "timestep": {"index": 0, "value": null, "count": 0},
  "nodes": [
    {
      "node_id": "node_root",
      "kind": "reader",
      "input_node_ids": [],
      "filter": null,
      "output": {
        "topology": "unknown",
        "raw_data_type": null,
        "bounds": null,
        "point_count": 0,
        "cell_count": 0,
        "arrays": []
      }
    }
  ],
  "representations": [
    {
      "representation_id": "rep_root",
      "node_id": "node_root",
      "type": "surface",
      "visible": true,
      "opacity": 1.0,
      "point_size_px": null,
      "color": {"mode": "solid", "rgb": [0.8, 0.8, 0.8]}
    }
  ],
  "measurements": [],
  "camera": {
    "position": [1.0, 1.0, 1.0],
    "focal_point": [0.0, 0.0, 0.0],
    "view_up": [0.0, 1.0, 0.0],
    "parallel_scale": 1.0,
    "projection": "perspective",
    "view_angle": 30.0
  },
  "selection": null,
  "artifacts": []
}
```

The real state always starts with `node_root` and `rep_root`. The root node is
the reader, and every derived node has one explicit input plus a `slice`,
`clip`, `threshold`, or point-scalar `contour` record. Each node output reports
semantic `topology`, raw VTK data type, nullable real bounds, nullable point and
cell counts, and exact array identities. Topology comes only from the semantic
descriptor-kind tokens and filter semantics; dataset IDs are never recipes.
Table topology is rejected because the v2 scene contract has no table
representation. Runtime discovery accepts at most 256 point and cell arrays in
total and fails explicitly rather than publishing a truncated schema.

A representation is an independently addressable ParaView actor with
`representation_id`, `node_id`, `surface|points` type, visibility, opacity,
nullable point size, and either solid RGB or field color. Multiple actors may
reference the same node. Field color state records the exact array identity,
observed range, tuple count, `scalar|magnitude` observation basis, resolved
preset, native linear/log scale, range policy, applied transfer range, and
scalar-bar state. Every field-colored actor owns a separate ParaView lookup
table. A request
may send `preset:null` for the package's verified generic default; authoritative
state reports the resolved non-null `Cool to Warm` preset.
Each non-root actor is also registered as an independent ParaView
`representations` proxy under a deterministic service-local name. Removal and
transaction rollback unregister that display and delete its private lookup
table when present, so actor churn cannot leave hidden proxy-manager state
behind.

Measurements are durable scene evidence, independent of stdout. Each has a
stable ID, node and exact field identity, explicit `scalar|magnitude` basis,
1-32 requested timestep indexes, per-timestep samples, and a deterministic
aggregate. Samples report physical timestep value, observed range, tuple count,
and bounded 128-bin ParaView Histogram evidence or an explicit unavailable
reason. At most 128 samples are stored across the complete service state.

The pipeline timestep is cross-checked against immutable dataset discovery.
Static data is exactly `{index:0,value:null,count:0}`. A multi-member timed
descriptor is accepted only when the ParaView reader exposes the same nonzero
time-axis count; JARVIS never claims temporal progression from repeated static
updates. Before initial node discovery, the reader is updated at its exact first
physical time. A newly created filter is likewise updated at the current exact
reader time, so its first summary cannot describe a different timestep.

## ParaView v2 commands

`POST /commands` accepts an exact command object:

```json
{
  "schema_version": "jarvis.paraview.command.v2",
  "command_id": "agent-command-42",
  "operation": "set_timestep",
  "expected_revision": 1,
  "arguments": {"index": 3}
}
```

Supported operations are:

- `set_timestep` with an exact index;
- `measure_field` with `node_id`, name, association, and 1-32 unique indexes;
- `create_filter` for slice, clip, threshold, or point-scalar contour;
- `set_representation` to create or update one actor;
- `remove_scene_object` with dependency enforcement;
- `fit_camera` over explicit visible actor IDs and explicit timesteps;
- `set_camera` for explicit camera components;
- `inspect_selection` against one exact actor;
- `export_artifact` for the exact current visible actor set;
- `export_scene` for a canonical reusable declarative scene manifest.

Commands are serialized against authoritative state. A stale
`expected_revision` returns HTTP 409 `revision_conflict`. Repeating an exact
`command_id` request returns the original result without reapplying it; reusing
the ID for different content returns `idempotency_conflict`. At most 4096
distinct commands are accepted, while all accepted IDs remain replayable for
the service lifetime. Canonical request and response bytes are stored exactly
and have a cumulative 64 MiB budget. A command that would exceed that budget is
rolled back before semantic commit; earlier entries remain exactly replayable.

`measure_field` is the prerequisite for robust frozen percentile color:

```json
{
  "representation_id": null,
  "node_id": "node_root",
  "type": "surface",
  "visible": true,
  "opacity": 1.0,
  "point_size_px": null,
  "color": {
    "mode": "field",
    "field": {"name": "temperature", "association": "point"},
    "preset": null,
    "invert": false,
    "scale": {"mode": "linear"},
    "range_policy": {
      "mode": "measurement_percentile",
      "measurement_id": "mea_...",
      "lower_percentile": 1.0,
      "upper_percentile": 99.0,
      "timestep_behavior": "freeze"
    },
    "scalar_bar_visible": true
  }
}
```

Other range policies are exact full-range recomputation
`{mode:"full",timestep_behavior:"recompute"}` and frozen explicit bounds
`{mode:"fixed",range:[lower,upper],timestep_behavior:"freeze"}`. Log scale
requires a strictly positive increasing range. Native symmetric-log behavior
is not claimed. Setting an actor to `visible:false` authoritatively resolves
both scalar-bar visibility and frame embedding to false; multiple visible
field actors may still expose independent scalar bars. Every field actor owns
its exact separate LUT and opacity transfer function. A field change, switch
to solid color, actor removal, or rollback explicitly retires superseded
proxies, and removal hides the scalar bar before rendering the candidate frame.

Creating a filter changes only topology. It never hides, shows, recolors, or
fits actors. `fit_camera` unions real bounds for only the requested visible
actors across only the requested timesteps, preserves camera orientation, and
restores the original time. Camera state always includes position, focal point,
view-up, positive parallel scale, `perspective|parallel` projection, and a view
angle strictly between 0 and 180 degrees. Position and focal point must differ;
view-up must be nonzero and non-collinear with the viewing direction. Parallel
fit scales ParaView's reset parallel scale by the requested padding;
perspective fit scales the post-reset camera distance by that padding. Failed
mutations restore the full scene, camera, time, source, frame, state, and
revision.

Selection always targets one representation. Element selection verifies the
real point/cell count and returns the process-0 local index without invoking a
picker, render, or selection-highlight mutation. Bare indexes are rejected for
composite, multiblock, partitioned, hierarchical, and AMR data because they do
not identify a unique element. Viewport selection uses `SelectSurfacePoints`
for point actors and `SelectSurfaceCells` for surface actors, returns at most
256 real IDs while preserving the total count, and reports explicit `empty` or
`unsupported` status rather than inventing world coordinates. An unknown
source count, or more than 10,000,000 relevant source elements, returns
`unsupported` before picker materialization. The opaque ParaView highlight is
cleared before returning the clean frame.

The `jarvis.paraview.command-result.v2` contains the full resulting state and
real operation result. `/events` pushes state revisions; `/live-data` pushes
bounded `jarvis.paraview.frame.v1` PNG events. State is limited to 8 MiB,
responses to 10 MiB, commands to 64 KiB, and frames to 32 MiB. The server
admits at most 32 simultaneous HTTP connections and eight SSE subscribers,
with bounded header, body, write, and heartbeat intervals. Excess connections
or subscribers receive HTTP 503, shutdown interrupts idle and streaming
connections, and each frame is base64-encoded once per revision and shared as
one immutable event payload rather than amplified per subscriber.

The service deliberately rejects the legacy command schema with
`unsupported_schema`. Legacy global visualization operations are not accepted
through the v2 endpoint; clients must coordinate command, result, and state
upgrades together. The frame envelope remains v1 because it contains pixels,
not service state.

## Artifact publication and recovery

`export_artifact` requires the lexically sorted IDs of every and only currently
visible representation. The `jarvis.artifact.v1` metadata records those IDs
and a canonical pre-export scene digest. The identical event appears in the
command result, pipeline artifact state, and `ExecutionHandle.artifacts()`.
Before publication, semantic commit verifies that the staged event is bound to
the current execution ID, package name and ID, service instance, command ID,
visible actor IDs, and current scene digest. An identity mismatch rolls the
command back and publishes neither image nor artifact event.

Rendering first creates and fsyncs a private bounded PNG. The controller then
validates the candidate frame, complete state, and canonical response before
semantic commit. Commit creates an owner-private deterministic transaction
marker with `O_EXCL`, fsyncs its expected event/output/checksum and the exact
pre-append ledger size, sequence, and prefix digest, links the PNG, and appends
the event under the artifact-sidecar lock. Complete durable ledger entries are
never rewritten.

On retry or service startup, an exact marker makes every power-loss point
attributable. A marker with its exact private staged PNG resumes the link and
append; a matching already-linked PNG finishes the append; a complete event
validates the output and clears the marker. If neither the private stage nor
the linked output exists, recovery durably removes only that marker and a later
retry can restage the export. An attributable incomplete event tail is
truncated only to its verified prior prefix and re-appended. Mismatched
markers, stale sequences, changed ledger prefixes, unsafe paths, and
pre-existing output without a marker fail explicitly. Rollback deletes only
private staging and never removes a published event.

`export_scene` accepts a normalized relative `.json` filename and an `exact` or
`compatible` fingerprint constraint. It projects the authoritative state into
`jarvis.paraview.scene-manifest.v1`: portable topological aliases replace
runtime node/actor IDs; measurement-derived transfer ranges are frozen while
their source policy remains provenance; and camera, scalar-bar, filter,
timestep, package/runtime version, descriptor fingerprint, compatibility, and
resource requirements remain explicit. Dataset member paths, execution and
scheduler identity, service host/port, and authorization never enter the
manifest. The output is a finalized `visualization_scene` JARVIS artifact with
media type `application/vnd.jarvis.paraview.scene+json`.

An accepted `initial_scene` is copied into the new execution as a finalized
`provenance` artifact whose metadata identifies the source scene artifact,
source final revision, manifest checksum, and current descriptor fingerprint.
Both imported and exported scene artifacts therefore appear through
`jarvis execution artifacts` and `ExecutionHandle.artifacts()`. JSON payloads
use the same checksum-bound marker, link, append, recovery, and rollback
protocol as PNG outputs.

## ParaView package configuration

Service mode requires `nprocs=1` and a durable `ppl run` or `ppl submit`
execution:

```yaml
pkg_type: builtin.paraview
pkg_name: viewer
mode: service
dataset_descriptor: /site/descriptors/asteroid-subset.json
initial_scene: /site/scenes/asteroid-evidence.json
service_bind_host: 127.0.0.1
service_advertise_host: 127.0.0.1
service_port: 0
service_startup_timeout: 600
nprocs: 1
```

`initial_scene` is optional and declared with
`jarvis.configuration-input-binding.v1` as a regular local file. A relay may
therefore stage a Host-local scene through its transparent input-binding
contract. JARVIS privately copies at most 2 MiB into the owned service root,
and the service validates the entire scene against the opened dataset before
the health endpoint can report ready. Rejections emit
`JARVIS_PARAVIEW_SCENE_REJECTION` followed by a bounded
`jarvis.paraview.scene-rejection.v1` JSON record; filesystem paths and secrets
are not included.

The package resolves `pvpython` from the pipeline execution environment. It
probes that executable and selects one supported headless backend itself:
`--mesa` when advertised, otherwise `--force-offscreen-rendering`. Site paths
and raw launcher flags are not semantic package parameters. A missing or
unusable dependency fails before the supervisor starts.

The backend is loopback-only. The relay connector runs inside the owned
allocation and dials `127.0.0.1`; JARVIS rejects non-loopback bind or advertised
hosts. A zero port selects an ephemeral port and the actual bound port is
persisted in the runtime report.
