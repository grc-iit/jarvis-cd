# Generated artifacts

Every durable execution has a queryable artifact manifest beside its progress
records. The execution handle remains a stable identity; callers query its
current outputs while the workload is running and receive the sealed view after
terminalization.

```python
handle = pipeline.run(wait=False)
snapshot = handle.artifacts()
for artifact in snapshot.artifacts:
    print(artifact.artifact_id, artifact.logical_name, artifact.state)
```

The equivalent machine-readable CLI query is:

```bash
jarvis execution artifacts <execution-id> --pipeline-id <pipeline> +json
```

## Artifact contract

Each `jarvis.artifact.v1` event has an opaque `art_*` identity, execution and
package identity, logical name, kind, role, structure, ownership, lifecycle
state, and optional location, media type, format, size, checksum, message, and
bounded metadata. A manifest is append-only: later revisions update the same
artifact identity without rewriting earlier evidence.

Lifecycle and purpose are separate. For example, a checkpoint can be a fully
`finalized` intermediate artifact. Producing artifacts are sealed as
`incomplete` or `failed` if execution terminates before the package reports real
completion. Already `available` output is not promoted to `finalized` merely
because the process exited.

A `scripted` scheduler record created with `submit=False` is the deliberate
exception to terminal sealing: JARVIS supports activating that exact script
later, so its manifest remains appendable until the activated workload reaches
a real completed, failed, or canceled state.

Locations are transport-neutral references:

- `execution_path` is relative to the owned execution root and is deleted with
  that execution;
- `cluster_path` is an absolute server-side reference and is never treated as
  desktop-local filesystem authority;
- `external_uri` names an explicitly supported remote provider.

Large ADIOS2/BP and HDF5 time series should normally be one `collection`
artifact rather than one agent-visible entry per internal file. JARVIS artifact
IDs deliberately omit site aliases. A relay may qualify an artifact with its
operator-defined target, for example
`clio://ares/jarvis/executions/<execution-id>/artifacts/<artifact-id>`, while
keeping the cluster argument explicit.

## Package providers

Application semantics live beside the package launcher in `artifacts.py`. The
module exports `adapter_from_package(package)` and may return a provider with
`observe_artifacts(text)`, `finalize_artifacts()`, and `reset_artifacts()`.
Providers return typed `ArtifactObservation` values; JARVIS supplies the
authoritative package/execution identity, sidecar path, sequencing, validation,
and persistence.

The combined `Pkg.runtime_line_callback()` handles both progress and artifacts.
Containerized applications may emit validated `JARVIS_ARTIFACT {json}` records
through stdout instead of opening a host-only sidecar. Package providers must
constrain discovered paths to configured application outputs and must not crawl
unbounded directory trees.

JARVIS core automatically records immutable pipeline snapshots, scheduler
scripts, and owned stdout/stderr streams where those streams exist. Diagnostic
artifacts are discoverable metadata, not an authorization to download their
contents; relay or agent-facing content access must apply a separate bounded
access policy.
