# metagem

MetaGEM metagenomics workflow — the container ships the full snakemake
DAG but the Jarvis pkg invokes only the `qfilter` stage (`fastp`),
which is the I/O-dominant front of the workflow. The inline shell in
`pkg.py` handles sample staging, replicate fan-out, fastp, and the
per-file cp into the FUSE-mounted output dir.

## Native workflow parameters (`_configure_menu`)

| Parameter | Default | Effect on I/O | Effect on compute |
|---|---|---|---|
| `sample_replicates` | `1` | N clones of each base sample (sample1/2/3) → fastp processes 3N pairs — **linear** in N | linear (each pair is one fastp run) |
| `cores` | `4` | n/a | `fastp --thread` — saturates around 16 on ~600 MB samples |
| `nprocs` | `1` | unused (snakemake/inline-shell drives the work) | unused |
| `out` | `/tmp/metagem_out` | qfiltered outputs land under `<out>/qfiltered/<sid>/` | n/a |

**Compute-dominated stages**: gzip decompress (igzip) + fastp quality
trim + gzip recompress. At 16 threads, fastp is roughly 50/50
I/O/compute on the bundled samples.

## I/O-only benchmark mode (inline shell already replaces fastp)

`pkg.py`'s container-mode inline shell now writes per-rep dirs with
`dd if=/dev/urandom` directly, skipping fastp entirely. The dataset
staging steps (download → STAGE, then clone N reps from base samples)
are also bypassed — there's no source-data dependency in I/O-only mode.

```yaml
env:
  METAGEM_PER_SAMPLE_MB: "700"   # per-sample R1+R2 total
```

Each sample writes `R1.fastq.gz` and `R2.fastq.gz` of
`PER_SAMPLE_MB / 2` MiB plus tiny `.json` / `.html` reports.

### Per-rep budget = 3 samples × PER_SAMPLE_MB

Default = 3 × 700 MiB = **2100 MiB/rep** (1.05 GiB/rep).

## Tuning matrix

| Goal | Knob | Rule of thumb |
|---|---|---|
| **More I/O total** | bump `sample_replicates` | linear in N (3N sample dirs at PER_SAMPLE_MB each) |
| **Larger per-sample writes** | bump `METAGEM_PER_SAMPLE_MB` | linear |
| **Bring fastp compute back** | revert the inline shell to the fastp branch (keep `dd` removed) or bind-mount original `/opt/run_metagem.sh` | shifts wall time toward compression |
| **Shorter wall** | drop `sample_replicates` | linear |

## Measured calibration (ares, 4-node SLURM, NFS-backed bind-mount out)

| Variant | sample_replicates | PER_SAMPLE_MB | Wall | I/O |
|---|---|---|---|---|
| Real fastp | 8 | n/a (real data) | 14+ min (cancelled) | ~22 GB qfiltered |
| Real fastp | 32 | n/a | 1046 s (17 min, cancelled) | 22 GB |
| **I/O-proxy (dd)** | **12** | **700** | **160 s (2.7 min)** | **26.4 GB** ✓ |

YAML lives at `builtin/pipelines/ares/metagem_apptainer_test.yaml`.
