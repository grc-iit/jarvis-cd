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
threshold, filter, colormap, or scene recipe. Those are explicit versioned
commands sent to the running service.

`inspect_selection` supports either an explicit point/cell index or a
normalized human viewport drag box. Viewport selection is resolved by
ParaView's real visible-cell picker and returns bounded IDs, an explicit empty
result, or an explicit unsupported result; it never guesses a world-space
location from browser coordinates.

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
execution handle. A relay owns authenticated desktop routing; direct SSH
tunnel instructions are intentionally not part of the package contract.

See [`docs/service-runtimes.md`](../../../docs/service-runtimes.md) for the
exact report, state, command, SSE, and artifact schemas.
