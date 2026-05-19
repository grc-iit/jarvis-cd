# biobb_wf_md_setup

BioExcel Building Blocks 5-stage MD setup pipeline (fix side chain →
pdb2gmx → editconf → solvate → solvate top). The native workflow runs
GROMACS through `biobb_*` Python wrappers; container mode is wired via
`/opt/run_batch.sh` inside the SIF.

## Native workflow parameters (`_configure_menu`)

These parameters live in pkg.py / the pipeline YAML and tune what the
GROMACS pipeline actually does:

| Parameter | Default | Effect on I/O | Effect on compute |
|---|---|---|---|
| `pdb_file` | `/opt/biobb-bench/1AKI.pdb` | bigger PDB → bigger .gro / topology writes (~linear in atom count) | bigger PDB → more GROMACS work (super-linear) |
| `replicates` | `1` | loops the 5-stage pipeline N times under `rep_NNN/` — I/O scales **linearly** in N | compute scales linearly in N too |
| `nprocs` / `ppn` | `1` / `1` | unused in container mode (GROMACS is launched single-threaded by the wrapper) | unused in container mode |
| `out` | `/tmp/biobb_out` | controls where the final per-rep tree gets staged via `cp -r`; sub-directory on NFS dominates the write traffic in container mode | n/a |

**Compute-dominated stages**: `fix_side_chain` (~50%), `editconf`
(~10%), `solvate` (~30%). `pdb2gmx` is mostly I/O on small PDBs.

## I/O-only benchmark mode (bind-mount override)

For benchmarking storage rather than GROMACS, bind-mount
`biobb_io_only.sh` over `/opt/run_batch.sh` in the YAML:

```yaml
container_binds:
  - ${HOME}/jarvis-bench-scripts/biobb_io_only.sh:/opt/run_batch.sh
  - ${HOME}/jarvis-runs:${HOME}/jarvis-runs
env:
  BIOBB_FIX_MB:  "32"    # fix_side_chain stage size
  BIOBB_P2G_MB:  "128"   # pdb2gmx .gro size
  BIOBB_EDIT_MB: "128"   # editconf .gro size
  BIOBB_SOLV_MB: "700"   # solvate .gro size (the heavy one)
  BIOBB_TOP_MB:  "16"    # both topology .zip files
```

The proxy script writes `/dev/urandom` blocks at the configured sizes
into the same `rep_NNN-<hostname>/` layout the real workflow uses, so
storage backends see the same file count + naming.

### Per-replicate budget = FIX + P2G + EDIT + SOLV + 2 × TOP

Default = 32 + 128 + 128 + 700 + 16 + 16 = **1020 MiB/rep**.

## Tuning matrix (I/O-only mode)

| Goal | Knob | Rule of thumb |
|---|---|---|
| **More I/O per rep** | bump `BIOBB_SOLV_MB` (largest single stage) | 2× → ~1.6× total/rep |
| **More I/O total** | bump `replicates` | linear |
| **Smaller files** | drop the per-stage MB knobs, bump `replicates` | shifts toward open/close overhead |
| **Shorter wall** | drop `replicates` (each rep is one full dd-pass through the budget) | linear |
| **Bypass compute** | use the bind-mount mode above | wall ≈ I/O bytes ÷ NFS bw |

## Measured calibration (ares, 4-node SLURM, NFS-backed overlay)

I/O-only mode, `replicates: 20`, default per-stage MB:

- **Wall**: 225 s (3.8 min)
- **I/O written**: 20 GiB (20 × 1020 MiB) → ≥20 GB target ✓
- **Effective write rate**: ~92 MB/s (single-host NFS stream)

YAML lives at `builtin/pipelines/ares/biobb_wf_md_setup_apptainer_test.yaml`.
