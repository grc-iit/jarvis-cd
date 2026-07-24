# Using `builtin.paraview`

Use `server` mode for a native ParaView client, `batch` for a site-owned
`pvbatch` script, and `service` for an execution-owned agent/relay runtime.

The service itself publishes cluster endpoints only. Obtain desktop-local URLs
through the configured relay/gateway instead of creating an undocumented SSH
tunnel. Query the durable JARVIS handle to discover readiness:

```bash
jarvis execution service-runtimes <execution-id> \
  --pipeline-id <pipeline> +json
```

To start from a previously exported canonical scene, set
`initial_scene=/path/to/scene.json` when appending or editing the service step.
The setting is a declared local-file input and is materialized by JARVIS before
launch. Omit it for the generic root-only scene. Export the current reusable
scene with a revisioned service command:

```json
{
  "schema_version": "jarvis.paraview.command.v2",
  "command_id": "export-reusable-scene",
  "operation": "export_scene",
  "expected_revision": 12,
  "arguments": {
    "filename": "scenes/reusable-scene.json",
    "fingerprint_constraint": "exact"
  }
}
```

Use `fingerprint_constraint:"compatible"` only when the target descriptor may
differ but must retain the exported kind, format, topology, bounds, timestep
count, and required fields.

See [the service-runtime reference](../../../docs/service-runtimes.md) for
configuration and command examples.
