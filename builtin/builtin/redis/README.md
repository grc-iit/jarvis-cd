Redis is a key-value store

# Installation

```bash
spack install redis
```

# Redis

Runs a standalone server on a single-host hostfile, or a redis cluster when
the hostfile has >1 host.

## Options

- `port` (default 6379)
- `single_instance` (default false) — force ONE standalone server on the
  FIRST host even when the hostfile has >1 host, skipping the cluster
  branch. Use when redis is a metadata singleton: cluster mode is DB0-only,
  so clients that `SELECT` a non-zero DB (e.g. a JuiceFS `meta_url` of
  `redis://.../1`) break against a cluster, and N servers sharing one
  `nodes.conf` on a shared filesystem collide. Default keeps the legacy
  behaviour (cluster iff >1 host).

## Startup/teardown hardening

- The server runs with `--dir <private_dir>` and any stale `dump.rdb` there
  is removed pre-launch: redis loads `./dump.rdb` from its CWD at startup,
  and a stale dump from a prior run (or a newer redis version) either
  crashes startup ("Can't handle RDB format version N") or leaks old keys.
  The shipped `redis.conf` also sets `save ""` (no snapshotting — this is
  an ephemeral service) and `pidfile ""` (`/var/run` is not writable in
  unprivileged containers; the pidfile is unused since redis is
  non-daemonized and tracked directly).
- After launch, `start()` waits for `redis-cli ping` → `PONG` (30 × 1s,
  warn-only) before returning, so dependent packages don't race a server
  that is still binding; standalone servers are then `flushall`'d for a
  fresh slate per run.
- `stop()` shuts down gracefully (`redis-cli shutdown nosave`), falls back
  to a force-kill, then waits for the port to be free so a back-to-back
  restart doesn't race the dying server.

All redis-cli probes run in the deployment context (inside the container
instance for a container deploy), so `redis-cli` need not exist host-side.
