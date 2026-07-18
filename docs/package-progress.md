# Package progress providers

JARVIS progress has two layers. Execution core owns identity, persistence, and
queryable state. A package may add application-specific observations through a
`progress.py` file beside its `pkg.py`; it does not own the execution ID or the
sidecar path.

Before a package starts, JARVIS supplies these environment variables:

- `JARVIS_EXECUTION_ID`: the stable JARVIS execution reference;
- `JARVIS_PACKAGE_NAME`: the package type, such as `builtin.paraview`;
- `JARVIS_PACKAGE_ID`: the package instance or pipeline alias, which remains
  distinct when a pipeline uses the same package more than once;
- `JARVIS_PROGRESS_PATH`: an exact JSONL sidecar inside the JARVIS-owned
  execution root;
- `JARVIS_PROGRESS_TRANSPORT`: `sidecar` for directly reachable shared paths,
  or `stdout` when the application runs inside a container and JARVIS must
  validate and persist framed output from the host.

Filesystem repositories added with `jarvis repo add` can implement
`<repository>/<package>/progress.py` and export
`adapter_from_package(package: dict[str, Any])`. JARVIS resolves this sibling
module from the package class already loaded from the repository. Publishing a
Python distribution or entry point is not required.

The package-local factory returns the minimal JARVIS provider protocol:
`observe_progress(text)`, `finalize_progress()`, and `reset_progress()`. The
first two methods return typed `ProgressObservation` values. Package providers
interpret application output only; JARVIS adds execution/package identity,
assigns sequence numbers, validates the event schema, and owns persistence.
Relay-specific acceptance hooks remain available only through the separate
`clio_relay.package_progress_adapters` compatibility entry points and are not
required of ordinary JARVIS repository packages.

## Event contract

Each JSONL line is a `jarvis.progress.v1` event. It identifies both
`package_name` and `package_id`, the JARVIS `execution_id`, a lifecycle `state`,
and a monotonically increasing `sequence`. Quantitative `current`, `total`, and
`unit` fields are optional. An event is determinate only when the application
provides a real positive total; JARVIS does not turn elapsed time, log volume,
or lifecycle states into estimated percentages.

The generic JARVIS-side `ProgressReporter` uses only the Python standard
library and can emit either `JARVIS_PROGRESS {json}` lines or the
execution-owned sidecar. Application launchers should use the generic reporter
while application interpretation stays in their package-local provider. The
reporter honors `JARVIS_PROGRESS_TRANSPORT`; container applications emit stdout
so they never need direct access to a host-only execution path.

## ParaView

`builtin.paraview` supports both `server` and generic `batch` modes. Batch mode
accepts a user or site-owned script and semantic script arguments. It resolves
`pvbatch` from the pipeline execution environment and detects supported
headless launcher arguments itself, selecting one backend rather than stacking
backend-selection flags. The bundled
`progress_reporter.py` is intentionally Python 3.10-compatible and imports no
JARVIS modules because ParaView may bundle an older Python than JARVIS. It emits
structured stdout only; the JARVIS parent validates identity and sequence, then
persists the event to the exact execution-owned sidecar. The embedded ParaView
runtime never opens that authoritative file itself.

A pvbatch script calls `frame_completed` only after its render/write operation,
or `timestep_completed` only after updating the real ParaView pipeline. It may
provide the actual total when known. Without a total, JARVIS truthfully records
completed units as indeterminate progress. Only MPI rank zero emits the shared
progress stream. `pvserver` reports readiness only after observing its real
waiting-for-client or accepting-connections output; readiness is a state, never
a percentage. Dataset selection, camera configuration, and rendering semantics
remain in the user or site script rather than the generic package.

## LAMMPS

LAMMPS parsing lives in `builtin/builtin/lammps/progress.py`, beside the package
launcher. It interprets LAMMPS run blocks and thermo timing, while the central
`jarvis_cd.progress` package remains application-independent. The former
`jarvis_cd.progress.lammps` import is a compatibility shim for integrations
that have not yet migrated.
