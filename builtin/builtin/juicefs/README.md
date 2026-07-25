JuiceFS is a POSIX-compatible distributed filesystem: file data lives in an
object store, file metadata in a transactional engine (here Redis).

# Installation

JuiceFS ships as a single static binary:

```bash
curl -fsSL https://github.com/juicedata/juicefs/releases/download/v1.2.3/juicefs-1.2.3-linux-amd64.tar.gz \
  | tar -xz -C /usr/local/bin juicefs
```

# Juicefs

Formats a JuiceFS volume (idempotent) and FUSE-mounts it in `start`;
unmounts in `stop`; removes the local cache/bucket dirs in `clean`.

The expected pipeline order is `redis -> juicefs -> <benchmark driver>` so
the Redis metadata engine accepts connections before `juicefs format` runs.
Pair it with `builtin.redis` (use redis `single_instance: true` on
multi-node pipelines — the default `meta_url` selects DB 1, which cluster
mode cannot serve).

## Options

- `meta_url` (default `redis://127.0.0.1:6379/1`) — metadata engine URL
- `meta_use_head` (default `false`) — rewrite `meta_url`'s host to the
  pipeline hostfile's FIRST host at start. For multi-node pipelines where
  every node mounts JuiceFS but redis is head-pinned (`single_instance`):
  keep the loopback `meta_url` and set this true (redis must accept
  non-loopback connections; the shipped `redis.conf` binds `0.0.0.0`)
- `storage` (default `file`) — object backend: `file` (local dir) or `gs`
- `bucket` — bucket URL for non-file storage (e.g. `gs://my-bucket`)
- `gcs_credentials` — path to a GCS service-account JSON key; exported as
  `GOOGLE_APPLICATION_CREDENTIALS` at start/stop, never stored in config
- `data_dir` (default `${HOME}/juicefs_data`) — local object-store dir
  (`--bucket` when `storage=file`)
- `mountpoint` (default `${HOME}/juicefs_mnt`) — FUSE mountpoint
- `name` (default `jfsbench`) — volume name passed to `juicefs format`
- `cache_dir` / `cache_size_mb` (default 512) — local cache; JuiceFS's own
  default cap is 100 GiB, which can ENOSPC a small node-local scratch
- `format_fresh` (default true) — wipe the local bucket dir before format
  (`storage=file` only; never wipes a cloud bucket)
- `mount_wait` (default 20) — seconds to wait for mount readiness
- `extra_mount_opts` — appended verbatim to `juicefs mount`
- `single_instance` (default false) — pin format/mount to the FIRST host on
  a multi-node hostfile (JuiceFS here is a head-node-only mount whose redis
  metadata lives on the head node)

## Container deploys

All lifecycle commands are container-wrapped via
`jarvis_cd.util.container_utils`, so the FUSE mount lives in the pipeline's
apptainer/container instance namespace and directory setup happens there
too (host-side `os.makedirs` of in-container paths would fail). Rootless
apptainer needs `container_fakeroot: true` at the pipeline level for FUSE.
