# rna_seq_star_deseq2

snakemake RNA-seq pipeline: cutadapt → STAR genome index build → STAR
align → featureCounts → DESeq2 differential expression. The container
ships ngs-test-data S. cerevisiae paired FASTQs at
`/opt/rnaseq-bench/`; `/opt/run_rnaseq.sh` drives snakemake.

## Native workflow parameters (`_configure_menu`)

| Parameter | Default | Effect on I/O | Effect on compute |
|---|---|---|---|
| `cores` | `4` | n/a | snakemake `--cores` — STAR rules pin `threads: 2` internally so adding cores helps mainly for cutadapt + featureCounts parallelism |
| `replicates` | `1` | loops the entire snakemake DAG N times into `rep_NNN/` subdirs — full STAR index rebuild + alignment each pass | linear (every rep wipes the snakemake scratch) |
| `nprocs` | `1` | unused (snakemake drives the work) | unused |
| `out` | `/tmp/rnaseq_out` | final results tree staged via `cp -r` from `/tmp/rnaseq-scratch/results` | n/a |

**Compute-dominated stages**: STAR genome generate (~1 min on this
genome size) + STAR align (~2-3 min per sample at 2 threads) +
rseqc QC (~1 min) + DESeq2 R fit (~30 s). featureCounts + cutadapt
are I/O-heavy. snakemake per-rule overhead adds ~10-20 s/rule.

## I/O-only benchmark mode (bind-mount override)

```yaml
container_binds:
  - ${HOME}/jarvis-bench-scripts/rna_seq_io_only.sh:/opt/run_rnaseq.sh
env:
  RNASEQ_BAM_GB: "5"    # per-sample BAM size
  RNASEQ_TSV_MB: "64"   # diffexp / counts table size
```

Writes the same `<out>/star/<sample>/Aligned.sortedByCoord.out.bam` +
`<out>/diffexp/*.tsv` + `<out>/counts/*.tsv` layout that snakemake
produces, but uses `dd if=/dev/urandom` instead of STAR / DESeq2.
Hard-codes 4 sample names (A1_1, A2_1, B1_1, B2_1) matching the
bundled bench data.

### Per-run budget = 4 × BAM_GB + 3 × TSV_MB

Default = 4 × 5 GiB + 3 × 64 MiB = ~20.2 GiB.

## Tuning matrix

| Goal | Knob | Rule of thumb |
|---|---|---|
| **More I/O per sample (I/O-only)** | bump `RNASEQ_BAM_GB` | linear |
| **More I/O total (I/O-only)** | bump `replicates`, or edit the script to add more samples | linear |
| **More I/O (native)** | bump `replicates`; the bundled FASTQ is fixed so the per-rep volume is fixed | linear; ~6-8 GB BAM per rep |
| **Less STAR compute** | use I/O-only bind-mount | wall drops 8-10× |
| **Saturate cores** | bump `cores` only helps cutadapt + featureCounts; STAR's `threads: 2` is hard-coded in the snakemake config | bound by the snakefile |

## Measured calibration (ares, 4-node SLURM, NFS-backed bind-mount out)

| Variant | replicates | Wall | I/O |
|---|---|---|---|
| Native snakemake | 1 | 565 s (9.4 m, completed) | ~7 GB overlay |
| Native snakemake | 1 | 1023 s (17 m) | ~7 GB |
| **I/O-proxy** | **1** | **120 s (2.0 m)** | **21.7 GB** ✓ |

YAML lives at `builtin/pipelines/ares/rna_seq_star_deseq2_apptainer_test.yaml`.
