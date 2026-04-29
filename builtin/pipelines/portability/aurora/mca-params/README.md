# Aurora multi-node MPI tuning files

User-level MPI tuning required to run any jarvis Aurora pipeline at
**>32 nodes** inside the apptainer container.

## What's here

| File | Goes to | Layer |
|---|---|---|
| `openmpi-mca-params.conf` | `~/.openmpi/mca-params.conf` | OpenMPI runtime |
| `pmix-mca-params.conf`    | `~/.pmix/mca-params.conf`    | PMIx runtime (separate library) |

Both files are read automatically by the relevant library at startup.
Apptainer bind-mounts your `$HOME` into the container, so the files
appear at the same paths inside the container without any code changes
or SIF rebuild.

## Install

From any login node or compute node:

```bash
mkdir -p ~/.openmpi ~/.pmix
cp builtin/pipelines/portability/aurora/mca-params/openmpi-mca-params.conf ~/.openmpi/mca-params.conf
cp builtin/pipelines/portability/aurora/mca-params/pmix-mca-params.conf ~/.pmix/mca-params.conf
```

That's it. Future jarvis multi-node runs pick the files up automatically.

## Remove

```bash
rm ~/.openmpi/mca-params.conf ~/.pmix/mca-params.conf
```

Reverts to OpenMPI/PMIx defaults. (Multi-node runs at <=32 nodes still
work without these files; only larger scales need them.)

## What problem they solve

Two stacked failures during `MPI_Init` at 64+ nodes (384+ ranks):

1. **TCP wireup explosion.** Aurora compute nodes expose 8 HSN
   interfaces (`hsn0`–`hsn7`) plus a bonded management ethernet.
   OpenMPI defaults to advertising every available IP on every
   interface, producing ~1.3 M peer-to-peer TCP connection attempts
   at 384 ranks. Wireup never finishes; MPI_Init hangs and the job
   dies at walltime. Fix: `btl_tcp_if_include = hsn0` (and
   `oob_tcp_if_include`) in the OpenMPI file restricts to one HSN.

2. **PMIx GDS shmem fault.** The system libpmix.so.2 in the Ubuntu
   24.04 container fails inside `gds_shmem.c` at 384 ranks with
   `PMIX_ERR_NOMEM` (despite `/dev/shm` being 504 GB and empty),
   then segfaults in the cleanup path. Fix: `gds = ^shmem` in the
   PMIx file forces the hash backend (heap memory), bypassing the
   broken code path.

## Why two separate files

OpenMPI and PMIx are two different libraries with two **separate**
MCA frameworks. The `gds` parameter only takes effect via the
PMIx-layer file (`~/.pmix/mca-params.conf`). Setting `gds = hash`
in the OpenMPI file is silently ignored — OpenMPI's MCA parser
doesn't know about that framework.

We verified this experimentally during debugging: putting the GDS
exclusion in the OpenMPI file changed nothing; moving it to the
PMIx file is what actually disabled shmem.

## Validation

64-node Nyx run on Aurora with both files in place:

| Metric | Value |
|---|---|
| MPI ranks | 384 |
| SYCL devices initialized | 384 / 384 |
| Wall time | 86 s |
| Volume written | 2.16 TiB |
| I/O fraction | 81 % |
| All PVC tiles peak HBM | 97–101 GiB (uniform) |

Same configuration without the files: hangs in `MPI_Init` for >480 s
and is killed by PBS walltime. Zero kernels execute, zero bytes
written.

## What does NOT need patching

- No source code in `jarvis-cd` needs modification.
- No SIF rebuild required.
- No environment variables to export.

The repo's existing `jarvis_cd/shell/exec_factory.py` already injects
the necessary launch-time `--mca` flags (`plm rsh`, `plm_rsh_no_tree_spawn`,
`routed direct`, `plm_rsh_num_concurrent`) for multi-node bring-up.
These tuning files complement those; both layers are needed at
64+ nodes.

## Per-parameter rationale

Each line in both files has an inline comment explaining the
specific problem it addresses, the cost when omitted, and the cost
of the workaround. Read the files themselves for details.

## When to re-tune

These settings are conservative — they should work from 32 to ~512
nodes without modification. If you scale to >512 nodes and still hit
issues, candidate next levers:

- Spread BTL across two HSN interfaces (`btl_tcp_if_include = hsn0,hsn1`)
  for higher aggregate bandwidth at the cost of doubling wireup count.
- Increase `plm_rsh_num_concurrent` (currently overridden to 32 by
  `exec_factory.py`).
- Investigate libfabric/OFI-based PMI on the host instead of containerized
  TCP-only OpenMPI.
