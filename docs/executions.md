# Pipeline execution handles

Every `Pipeline.run()` and `Pipeline.submit()` operation owns a JARVIS
execution identity and returns an `ExecutionHandle`. The handle is a stable
reference, not a snapshot of status. It always contains the JARVIS
`execution_id`, `pipeline_id`, and execution `mode`. Scheduler fields are
explicitly null for a direct run. A scheduler submission also identifies its
provider and, after acceptance, its provider-native job ID and optional
cluster.

```python
handle = pipeline.run(execution_id="render-2018-asteroid")
print(handle.execution_id)
record = handle.refresh()

live = pipeline.run(wait=False)
while not live.refresh().terminal:
    print(live.progress().to_dict())
    print(live.artifacts().to_dict())
    print(live.service_runtimes().to_dict())

scheduled = pipeline.submit(wait=False)
print(scheduled.scheduler_provider)   # "slurm"
print(scheduled.scheduler_native_id)  # e.g. "104857"
```

The changing `ExecutionRecord` is stored at
`<pipeline_shared_dir>/executions/<execution_id>/.jarvis-execution.json`.
Records are per execution; `last_submission` remains only a compatibility
projection of the most recent scheduler submission. Creating another run does
not overwrite earlier execution records.

Direct `run()` remains blocking by default. Its record transitions through
`preparing`, `running`, and `completed` or `failed`. A scheduler record moves
through submission and then the isolated runtime snapshot advances that same
record to `running`. The generated scheduler script finalizes it as `completed`,
`failed` or `canceled`, including failures in pre-run hooks, hostfile
construction, and post-run hooks.

`Pipeline.get_execution()` reconciles a submitted scheduler record with its
configured provider. This closes the case where the scheduler accepts an ID
but rejects the job before the generated script can activate and finalize the
record, for example when a scheduler log directory is unavailable. The query
stores a typed provider observation on a state transition. Missing jobs become
failed only after the startup grace period; an unavailable provider query
leaves the last durable state intact.

## Querying records

Python clients can query an exact ID or list a named pipeline's history:

```python
record = pipeline.get_execution(handle.execution_id)
records = pipeline.list_executions()
progress = handle.progress()
artifacts = handle.artifacts()
services = handle.service_runtimes()
```

CLI queries accept `--pipeline-id`, so a handle remains queryable after the
operator changes the current pipeline:

```bash
jarvis ppl run current --execution-id render-2018-asteroid +json
jarvis ppl run current +no_wait +json
jarvis ppl submit visualization.yaml --execution-id render-2018-asteroid +json
jarvis execution get render-2018-asteroid --pipeline-id visualization +json
jarvis execution list --pipeline-id visualization +json
jarvis execution progress render-2018-asteroid --pipeline-id visualization +json
jarvis execution artifacts render-2018-asteroid --pipeline-id visualization +json
jarvis execution service-runtimes render-2018-asteroid --pipeline-id visualization +json
jarvis execution cancel render-2018-asteroid --pipeline-id visualization +json
jarvis execution stop render-2018-asteroid --pipeline-id visualization +wait +json
```

`execution get +json` and `execution list +json` emit clean machine-readable
JSON. `execution progress +json` returns the latest identity-checked event and
event count for each package alias. `execution artifacts +json` returns the
current identity-checked artifact manifest. Neither query exposes its internal
sidecar paths. `ppl run +json` and `ppl submit +json` emit the handle JSON as
their final line because package and scheduler logs remain visible before it.

`execution service-runtimes +json` returns the latest identity-checked service
revision for every package-owned instance. See [Execution-owned service
runtimes](service-runtimes.md) for the package SPI and generic ParaView
HTTP/SSE command contract.

## Cancelling and stopping

`execution cancel` and `execution stop` control scheduler executions only, and
print the resulting record like `execution get`. They are idempotent: a
terminal record is returned unchanged. In Python they are
`pipeline.cancel_execution(id)` and
`pipeline.stop_execution(id, wait_seconds=None)`.

| Command | Scheduler action | Final state |
|---------|------------------|-------------|
| `execution cancel` | `scancel <job>` | `canceled`, unless the job had already reached another terminal state |
| `execution stop` | `scancel --signal=TERM --batch <job>`; the batch shell runs `jarvis ppl stop`, then reaps the pipeline | `completed` after a clean stop, `failed` if `ppl stop` failed; a pending job is canceled instead |

These commands record their work in the execution's `metadata`:

- `cancel_request` / `stop_request` (`jarvis.execution.control.v1`): `action`,
  `status` (`requested`, `accepted`, `job_ended`, `fallback_cancel`,
  `failed`), `provider`, `native_id`, `cluster`, the exact `command` and the
  scheduler's `result`. A `failed` request does not affect finalization.
- `termination`: written by the batch shell's TERM trap. It holds `reason`
  (`stop_request`, `cancel_request` or `scheduler` for a time limit or an
  external `scancel`), `signal`, `received_at`, `stopped_at`,
  `stop_return_code` and `child_reaped`.

Install jobs (`jarvis ppl install +submit`) add `action: install` and a live
`install` progress document. See
[scheduler.md → Install jobs](scheduler.md#install-jobs).

During a run, JARVIS binds package shared/private paths to the owned execution
root. It also exports authoritative `JARVIS_EXECUTION_ID`, `JARVIS_PACKAGE_ID`,
`JARVIS_PACKAGE_NAME`, `JARVIS_PROGRESS_PATH`, and
`JARVIS_PROGRESS_TRANSPORT`, `JARVIS_ARTIFACT_PATH`, and
`JARVIS_ARTIFACT_TRANSPORT`, and `JARVIS_SERVICE_RUNTIME_PATH` values before
each package starts. Application
packages may report through the selected sidecar/stdout transports, but cannot
choose the authoritative execution or sidecar identity. See
[Generated artifacts](artifacts.md) for the package contract.
