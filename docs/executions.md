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
record to `running`. The generated scheduler script finalizes it as `completed`
or `failed`, including failures in pre-run hooks, hostfile construction, and
post-run hooks.

## Querying records

Python clients can query an exact ID or list a named pipeline's history:

```python
record = pipeline.get_execution(handle.execution_id)
records = pipeline.list_executions()
progress = handle.progress()
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
```

`execution get +json` and `execution list +json` emit clean machine-readable
JSON. `execution progress +json` returns the latest identity-checked event and
event count for each package alias without exposing storage paths. `ppl run
+json` and `ppl submit +json` emit the handle JSON as their final
line because package and scheduler logs remain visible before it.

During a run, JARVIS binds package shared/private paths to the owned execution
root. It also exports authoritative `JARVIS_EXECUTION_ID`, `JARVIS_PACKAGE_ID`,
`JARVIS_PACKAGE_NAME`, `JARVIS_PROGRESS_PATH`, and
`JARVIS_PROGRESS_TRANSPORT` values before each package starts. Application
packages may report through the selected sidecar/stdout transport, but cannot
choose the authoritative execution or sidecar identity.
