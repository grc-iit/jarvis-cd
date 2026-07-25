LabStor is a distributed semi-microkernel for building data processing services.

# Installation

```bash
spack install redis
```

## v3 options (#526 regression pipelines)

- `clients` (default `50` = tool default) — parallel client connections
  (`-c`). For a "threads" sweep, zip with `nthreads` so a row means N
  connections driven by N threads.
- `nthreads` — I/O-issuing threads (`--threads`); `count` — total requests
  (`-n`); `req_size` — request size in integer bytes (`-d`).
- `single_instance` (default `false`) — run the benchmark on the FIRST host
  (next to a head-pinned `single_instance` redis) and skip cluster
  addressing.

Output is captured with `--csv` (replacing the human-readable report) and
teed to `<shared_dir>/<pkg_id>_redis_bench.csv`. results.csv columns:
`{pkg_id}.set_rps`, `.get_rps`, `.set_p50_ms`, `.get_p50_ms`, `.set_p99_ms`,
`.get_p99_ms`, `.runtime`.
