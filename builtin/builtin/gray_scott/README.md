# IOWarp Gray-Scott

`builtin.gray_scott` runs the direct Gray-Scott application maintained in
[`iowarp/clio-core`](https://github.com/iowarp/clio-core), under
`external/iowarp-gray-scott`. It does not require Coeus or Hermes.

## Install

The Clio Core Spack repository exposes the project as `iowarp`. Enable ADIOS2
when installing the direct application:

```bash
git clone https://github.com/iowarp/clio-core.git
spack repo add clio-core/installers/spack
spack install iowarp@main+adios2
spack load iowarp@main+adios2
```

You can also build `external/iowarp-gray-scott` directly with CMake. The
resulting executable is named `gray-scott`.

## Run with JARVIS

The executable can be selected explicitly, which is recommended for durable
and live-validated runs:

```bash
jarvis ppl create gray-scott-test
jarvis ppl append builtin.gray_scott \
  executable=/absolute/path/to/gray-scott \
  nprocs=1 ppn=1 width=32 height=32 \
  steps=20 out_every=10 \
  outdir=/shared/results/gray-scott.bp
jarvis ppl run
```

The package writes the complete settings schema expected by Clio Core,
including checkpoint and ADIOS2 memory-selection defaults. Its file-backed
default uses BP5. If `outdir` is omitted, JARVIS writes under this package's
pipeline-shared directory rather than temporary host storage.

## Durable progress and artifacts

For an owned execution, JARVIS records simulation-timestep progress and one
evolving artifact named `gray-scott-timesteps`. The artifact is a shared
cluster-path collection with media type `application/x-adios2-bp` and format
`adios2-bp5`. A successful process exit after the final observed timestep
finalizes both progress and the artifact; a nonzero exit never makes that
claim.

When `checkpoint=true`, JARVIS also reports
`gray-scott-restart-checkpoint` at the configured `checkpoint_output` path.
The direct Clio Core writer does not emit a checkpoint-completion message, so
JARVIS snapshots that exact path before launch and reports it only if it was
created or changed by the owned process. A zero exit finalizes the checkpoint;
a nonzero exit records a changed checkpoint as incomplete. Package cleanup
removes the exact main-output and checkpoint paths, including independently
configured locations, without wildcard deletion.

Use the execution handle or reopen the execution later:

```python
handle = pipeline.run()
record = handle.refresh()
progress = handle.progress()
artifacts = handle.artifacts()
```

Coeus-specific engines and adapter experiments belong to the Coeus package and
repository rather than this direct application package.
