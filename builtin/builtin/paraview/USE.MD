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

See [the service-runtime reference](../../../docs/service-runtimes.md) for
configuration and command examples.
