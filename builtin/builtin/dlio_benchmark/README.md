# DLIO Benchmark

`builtin.dlio_benchmark` runs one native
[DLIO Benchmark](https://github.com/argonne-lcf/dlio_benchmark) workload
configuration under MPI. The package expects `dlio_benchmark` and the MPI
launcher to be resolved in the JARVIS pipeline environment before execution.
It does not install Python dependencies or discover software at run time.

The package owns the mechanics shared by DLIO studies:

- optional dataset generation followed by one training I/O phase;
- explicit dataset, output, and checkpoint locations;
- typed reader, dataset, epoch, computation, and checkpoint controls;
- checked MPI and cache-policy process status;
- native output, generated dataset, and checkpoint artifact lifecycles.

Scientific sweeps and comparisons remain explicit pipeline composition. For
example, compare reader concurrency by adding separately named DLIO steps with
different `read_threads` values and a shared, pre-generated dataset. This keeps
every measured cell visible in the pipeline rather than hiding a study inside
the package.

## Cache policy

`cache_policy=none` is the portable default and makes no cold-cache claim.
`cache_policy=sync` flushes dirty buffers without evicting the page cache.
`cache_policy=drop_caches` is an explicit privileged request. It uses
noninteractive `sudo -n` and fails the package if the site has not authorized
the operation. The package never silently falls back between policies.

## Important settings

| Setting | Default | Meaning |
|---|---:|---|
| `workload` | `unet3d_a100` | Installed DLIO workload profile |
| `generate_data` | `false` | Generate data before the training I/O phase |
| `data_path` | empty | `data/<workload>` below package shared storage |
| `output_path` | `output` | Native DLIO output collection |
| `read_threads` | unset | Reader threads per rank |
| `nprocs` / `ppn` | `8` / `8` | MPI ranks and ranks per node |
| `timeout_seconds` | `3600` | Maximum runtime for each MPI phase |
| `checkpoint` | `true` | Enable checkpoint writes for supported workloads |
| `checkpoint_path` | empty | `checkpoints/<workload>` below package shared storage |
| `cache_policy` | `none` | `none`, `sync`, or `drop_caches` |

Additional typed controls cover training-file count, samples per file, record
and resize lengths, batch size, training and evaluation phases, epochs, emulated
computation time, model checkpoint size, checkpoint timing, `fsync`, and
DFTracer enablement.

## Example

```bash
jarvis pipeline create dlio-study
jarvis pipeline append dlio_benchmark \
  workload=resnet50_v100 \
  generate_data=true \
  num_files_train=256 \
  read_threads=4 \
  checkpoint_size_bytes=104857600 \
  cache_policy=sync \
  nprocs=4 ppn=4
jarvis pipeline run
```

The output is a normal JARVIS batch execution. A zero native process exit
finalizes the declared artifact collections; a nonzero exit leaves them
explicitly incomplete and fails the package.
