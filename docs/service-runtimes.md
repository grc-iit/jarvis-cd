# Execution-owned service runtimes

JARVIS applications may expose a long-lived network service as part of a
durable pipeline execution. JARVIS owns the execution and package identity,
persists lifecycle observations without scraping stdout, and returns the
current service records from the same `ExecutionHandle` used for status,
progress, and artifacts.

```python
handle = pipeline.submit(wait=False)
services = handle.service_runtimes()
for runtime in services.service_runtimes:
    print(runtime.service_instance_id, runtime.lifecycle, runtime.base_url)
```

The machine-readable CLI form is:

```bash
jarvis execution service-runtimes <execution-id> \
  --pipeline-id <pipeline> +json
```

The result is an exact `jarvis.execution.service-runtimes.v1` document with
`schema_version`, `execution_id`, `pipeline_id`, `execution_state`, `terminal`,
and `service_runtimes`. The array contains the latest
`jarvis.service-runtime.v1` revision for every concrete service instance.

## Package reporting contract

Before package startup, execution core exports:

- `JARVIS_EXECUTION_ID`;
- `JARVIS_PACKAGE_NAME`;
- `JARVIS_PACKAGE_ID`;
- `JARVIS_SERVICE_RUNTIME_PATH`, an exact owner-private JSONL sidecar inside
  the execution root.

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

Each report explicitly contains `/healthz`, `/live-data`, `/events`, `/state`,
and `/commands` paths. `delivery_mode` is `push`. These cluster endpoints are
not desktop URLs: relay owns authenticated routing, tunnels/connectors, and
local URL publication.

## Dataset descriptor

`jarvis.dataset-descriptor.v1` contains only intrinsic data facts:

- a stable `dataset_id`, `kind`, and format;
- a bounded ordered list of normalized absolute cluster members and optional
  timestep values;
- optional discovered arrays, associations, component counts, units, and
  bounds;
- a required SHA-256 fingerprint and optional source JARVIS artifact identity.

The fingerprint is the lowercase SHA-256 of canonical JSON for the complete
descriptor with the `fingerprint` field omitted. Member ordering, arrays,
bounds, and optional source artifact therefore participate in identity. Both
JARVIS and the staged pvpython service recompute it. The service also verifies
that every selected member exists before readiness. This avoids reading huge
site datasets merely to identify a bounded catalog selection; when content
integrity is known, `source_artifact.sha256` carries that separate fact.

It deliberately cannot contain a camera, active field, colormap, filter,
threshold, scene, or recipe. Selecting data does not silently select a view.
The generic ParaView service accepts visualization choices only as explicit
versioned commands.

## ParaView service state and commands

`builtin.paraview mode=service` launches a real `pvpython` service under a
JARVIS Python supervisor. The supervisor reports `starting` only after process
creation and reports `ready` only after the versioned HTTP health response
passes. It continues probing for the service lifetime: three consecutive
failures report `degraded`, a later successful probe reports recovery to
`ready`, and process exit reports `stopped` or `failed`. Owned termination is
bounded; a child that ignores terminate is killed after the grace period, and
metadata-reporting failure never bypasses child cleanup. Missing ParaView
imports, a failed startup probe, or a nonzero process exit fail explicitly.

`GET /state` returns exactly:

```json
{
  "schema_version": "jarvis.paraview.service-state.v1",
  "service_instance_id": "srv_...",
  "revision": 1,
  "execution_id": "jarvis_...",
  "dataset": {
    "descriptor": {},
    "discovery": {
      "arrays": [],
      "bounds": null,
      "timestep_values": []
    }
  },
  "pipeline": {
    "timestep": {},
    "active_field": null,
    "filters": [],
    "colormap": null,
    "camera": {},
    "selection": null,
    "artifacts": []
  }
}
```

`POST /commands` accepts an exact `jarvis.paraview.command.v1` object:

```json
{
  "schema_version": "jarvis.paraview.command.v1",
  "command_id": "agent-command-42",
  "operation": "set_timestep",
  "expected_revision": 1,
  "arguments": {"index": 3}
}
```

Supported operations are `set_timestep`, `set_active_field`, `set_camera`,
`apply_filter`, `set_colormap`, `inspect_selection`, and `export_artifact`.
Commands are serialized against authoritative state. A stale
`expected_revision` returns HTTP 409 with `revision_conflict`. Repeating the
same `command_id` and exact payload returns the original result without
applying it again; reusing an ID for different content returns
`idempotency_conflict`. Results remain replayable for the complete service
lifetime. A service accepts at most 4096 distinct commands; after that, a new
ID returns HTTP 429 with `command_limit`, while every previously accepted ID
remains replayable. Restarting the service creates a new service instance and
a new command-ID lifetime.

`inspect_selection` accepts exactly one of two selector forms. A known element
uses `{"association":"point|cell","index":N}`. A human drag box uses
`{"viewport":{"x0":0.1,"y0":0.2,"x1":0.9,"y1":0.8}}`, with finite
normalized coordinates, positive area, and a top-left origin matching the live
PNG. The service converts that rectangle to the fixed render-view pixels and
calls ParaView's real visible-cell surface selector. It returns a `selection`
object with `selector`, `status`, `association`, `selected_count`,
`returned_count`, `truncated`, `ids`, and nullable `reason`; viewport results
also include the normalized `viewport` and exact `pixel_rectangle`. At most 256
real IDs are returned while `selected_count` preserves the full count. A
backend that cannot expose real selection IDs returns `status=unsupported`; an
area with no visible cells returns `status=empty`. JARVIS never approximates a
screen selection as world coordinates.

The `jarvis.paraview.command-result.v1` response contains the resulting full
authoritative state and the real operation result. `/events` pushes those state
revisions as SSE. `/live-data` pushes bounded base64 PNG frames as
`jarvis.paraview.frame.v1` SSE events; a browser never invents state locally.

Camera and filter commands validate their complete semantic input before
mutation. A failed camera render restores the previous camera, filter failures
remove any partial proxy and restore the previous source and camera, and a
successful filter never performs a hidden camera reset. Changing the active
field clears any colormap preset that belonged to the prior field.

`export_artifact` writes only under the package-owned service output root. A
filename is create-once for that service instance: an existing file or symlink
returns HTTP 409 with `artifact_exists` and is never overwritten. The service
renders to a private temporary file, bounds and validates the PNG, fsyncs it,
publishes it atomically, and fsyncs the directory. It then fsyncs a real
`jarvis.artifact.v1` record before returning the artifact result, so
`ExecutionHandle.artifacts()` is the authoritative manifest. If manifest
publication fails, the newly published PNG is removed before the command
fails.

## ParaView package configuration

Service mode requires `nprocs=1` and a durable `ppl run` or `ppl submit`
execution. A minimal package configuration is:

```yaml
pkg_type: builtin.paraview
pkg_name: viewer
mode: service
dataset_descriptor: /site/descriptors/asteroid-subset.json
service_bind_host: 127.0.0.1
service_advertise_host: 127.0.0.1
service_port: 0
service_startup_timeout: 120
nprocs: 1
```

`builtin.paraview` resolves `pvpython` from the pipeline execution
environment's `PATH`. Service mode is intrinsically headless, so the package
probes the installed launcher and selects one supported backend itself:
`--mesa` when advertised, otherwise `--force-offscreen-rendering`. Site paths
and raw ParaView launcher flags are not part of the semantic package contract.
A missing or unusable `pvpython` dependency fails the package explicitly
before the service supervisor starts.

The ParaView backend is deliberately loopback-only. The relay connector must
run inside the execution's owned allocation and dial `127.0.0.1`; JARVIS
rejects non-loopback bind or advertised hosts so unauthenticated service
commands are never exposed to the shared cluster network. A zero
port selects an ephemeral port and the actual bound port is persisted in the
runtime report. Dataset member count is deliberately bounded; an interactive
subset does not require loading every member of a large temporal collection.
