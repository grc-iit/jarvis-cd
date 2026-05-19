# montage

NASA/IPAC Montage astronomical mosaic engine — 10-stage pipeline
(mImgtbl → mProjExec → mImgtbl → mOverlaps → mDiffExec → mFitExec →
mBgModel → mBgExec → mImgtbl → mAdd). The SIF pre-stages the M17
J-band 0.2° benchmark; `/opt/run_mosaic.sh` runs the chain.

## Native workflow parameters (`_configure_menu`)

| Parameter | Default | Effect on I/O | Effect on compute |
|---|---|---|---|
| `region` / `band` / `size` | `M17` / `j` / `0.2` | bigger `size` → more raw FITS tiles → more bytes through mProjExec / mAdd | bigger `size` → quadratic-ish mProjExec compute |
| `mosaic_replicates` | `1` | runs the full 10-stage pipeline N times into `host-<host>/rep_NNN/` — **linear** I/O | each rep redoes interpolation + add |
| `scratch_dir` | `${HOME}/montage-scratch` | scratch is the projection working tree; ends up at NFS scratch when `size > 0.2°` | n/a |
| `out` | `${HOME}/montage_out` | final FITS staging dir | n/a |

**Compute-dominated stages**: `mProjExec` (image reprojection
interpolation) is the wall-time hog at any non-trivial size. `mAdd` and
`mBgExec` are I/O-heavy. `mImgtbl` and the `*.tbl` index passes are
metadata-only.

## I/O-only benchmark mode (bind-mount override)

```yaml
container_binds:
  - ${HOME}/jarvis-bench-scripts/montage_io_only.sh:/opt/run_mosaic.sh
env:
  MONTAGE_N_TILES:  "20"    # how many projected + corrected tiles to write
  MONTAGE_TILE_MB:  "512"   # each tile's size
  MONTAGE_MOSAIC_MB: "512"  # final mosaic.fits size
```

Writes the same directory layout the real `/opt/run_mosaic.sh`
produces (`projected/*.fits`, `corrected/*.fits`, `mosaic.fits`, plus
the seven `.tbl` index files) without running `mProjExec` or `mAdd`.

### Per-rep budget = 2 × N_TILES × TILE_MB + MOSAIC_MB

Default = 2 × 20 × 512 + 512 = 20,992 MiB ≈ **20.5 GiB/rep**.

## Tuning matrix

| Goal | Knob | Rule of thumb |
|---|---|---|
| **More I/O per rep (I/O-only)** | bump `MONTAGE_TILE_MB` or `MONTAGE_N_TILES` | linear |
| **More I/O total (I/O-only)** | bump `mosaic_replicates` | linear |
| **More I/O (native)** | bump `size` (triggers runtime IRSA fetch); or bump `mosaic_replicates` | size: super-linear data growth |
| **Less interpolation compute** | use I/O-only bind-mount | wall ≈ I/O bytes ÷ NFS bw |
| **Different image archive** | tweak `region` + `band`; needs network reach from compute nodes | varies |

## Measured calibration (ares, 4-node SLURM, NFS-backed bind-mount out)

| Variant | mosaic_replicates | per-rep MB | Wall | I/O |
|---|---|---|---|---|
| Native (M17 0.2°) | 80 | ~24 | 565 s (9.4 m) | ~1.9 GB |
| Native (M17 0.2°) | 30 | ~24 | 421 s (7.0 m) | ~720 MB |
| Native (M17 0.2°) | 20 | ~24 | 260 s (4.3 m) | ~480 MB |
| **I/O-proxy** | **1** | **20.5 GiB** | **141 s (2.4 m)** | **22 GB** ✓ |

YAML lives at `builtin/pipelines/ares/montage_apptainer_test.yaml`.
