FIO

# Installation

```bash
spack install fio
```

# FIO

Container-aware fio driver. Supports two targeting modes:

- **Filename mode** (default): `out` is the test file path
  (`--filename=<out>`); legacy behaviour, unchanged.
- **Directory mode**: set `target_dir` to run fio against a directory
  (`--directory=<target_dir>`), e.g. a FUSE mountpoint. `out` is ignored
  and the directory is created in the deployment context (inside the
  container instance for a container deploy, where the mount may only
  exist in the instance's namespace).

## Options

| option | default | fio flag / effect |
|---|---|---|
| `write` / `read` | true / false | `--rw=write/read/readwrite` |
| `mode` | unset | `--rw` override of the bools; adds `randwrite`/`randread` |
| `xfer` | `1m` | `--bs` |
| `total_size` | `32m` | `--size` (per job) |
| `iodepth` | 1 | `--iodepth` |
| `nprocs` | 1 | `--numjobs` |
| `out` | `/tmp/fio_test.bin` | `--filename` (filename mode) |
| `target_dir` | unset | `--directory` (directory mode) |
| `direct` | false | `--direct` |
| `random` | false | `--randrepeat` (legacy quirk, kept for back-compat) |
| `engine` | `psync` | `--ioengine` |
| `fio_bin` | `fio` | binary path |
| `runtime` | 0 | `--runtime=N --time_based` when > 0 |
| `use_thread` | false | `--thread` (jobs as threads, one address space) |
| `fallocate` | `native` | `--fallocate` (omitted when `native` = fio default; use `none` for FUSE mounts that reject fallocate) |
| `log` | unset | plain-text `--output` |
| `output_file` | unset | JSON report + metrics (below) |
| `single_instance` | false | pin to first host (below) |
| `exec_mode` | `pssh` | pssh or mpi fan-out |

Defaults reproduce the previous package's fio command line exactly, so
existing pipelines are unaffected.

## JSON metrics (`output_file`)

When `output_file` is set, fio runs with
`--group_reporting --output-format=json --output=<shared_dir>/<output_file>`
and `_get_stat` parses the report into results.csv columns — for each op
(read, write) with nonzero `io_bytes`:

- `<pkg_id>.<op>.agg_bw_mbps` — aggregate bandwidth (MiB/s)
- `<pkg_id>.<op>.iops`
- `<pkg_id>.<op>.lat_mean_us` — mean total latency (µs)
- `<pkg_id>.<op>.lat_p99_us` — 99th-percentile completion latency (µs)
- `<pkg_id>.<op>.total_io_mb`

The report is written where fio runs and re-read from disk in `_get_stat`
because the sweep runner reloads a fresh package instance before collecting
stats — the on-disk JSON is the contract between the two methods.
`<pkg_id>.runtime` is always recorded.

## `single_instance`

On a multi-node pipeline every package receives the full hostfile. A fio
baseline that models ONE client (e.g. a single client against NFS, or a
FUSE mount that only exists on the head node) must not fan out to every
node — N nodes would clobber the one shared-dir JSON report and hit
missing mounts. `single_instance: true` slices the hostfile to its first
host. Default keeps the legacy run-on-every-host behaviour.
