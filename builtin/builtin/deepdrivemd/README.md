# deepdrivemd

DeepDriveMD-pipeline: adaptive biomolecular MD orchestrated by RADICAL
EnTK. The 4 stages (`molecular_dynamics_stage`,
`aggregation_stage`, `machine_learning_stage`, `model_selection_stage`,
`agent_stage`) each invoke `/opt/ddmd_io_task.sh <stage>` in the
container. The default task writes 4 MiB of `/dev/urandom` as a stand-in
for an OpenMM trajectory frame.

## Native workflow parameters (`_configure_menu`)

| Parameter | Default | Effect on I/O | Effect on compute |
|---|---|---|---|
| `iterations` | `2` | iterations × num_tasks MD writes — **linear** | each iteration adds an ml+ms+agent round, plus RADICAL scheduling overhead |
| `num_tasks` | `nprocs` (fallback) | MD bytes per iteration scale linearly | adds RADICAL per-task launch cost; capped by `cpus_per_node` in the template |
| `nprocs` / `ppn` | `1` / `1` | used as default for `num_tasks` if unset | n/a |
| `replicates` | `1` | runs the full DDMD pipeline N times back-to-back (each into `rep_NNN/`) — pays the full RADICAL bootstrap N times | linear |
| `out` | `/tmp/ddmd_out` | RADICAL workdir + MD outputs land under `<out>/run/stage*/` | n/a |

**Compute floor**: ~3 min for RADICAL Pilot bootstrap (rabbitmq install
on first run, broker startup, pilot agent + bridge launch). Per-task
scheduling adds ~5-30 s/task. The MD compute itself is just `dd` — no
real OpenMM — so the per-task wall is dominated by orchestration.

## I/O-only benchmark mode (two layers of override)

### Layer 1: bigger MD writes per task

```yaml
container_binds:
  - ${HOME}/jarvis-bench-scripts/ddmd_io_task_big.sh:/opt/ddmd_io_task.sh
env:
  DDMD_MD_BYTES: "1073741824"   # 1 GiB per MD task
  DDMD_MD_BS:    "8388608"      # 8 MiB block size
```

Replaces only the per-task script; keeps RADICAL orchestration so the
task graph is unchanged. `DDMD_MD_BYTES` directly multiplies MD write
volume per task.

### Layer 2: bypass RADICAL entirely (5-min wall target)

```yaml
container_binds:
  - ${HOME}/jarvis-bench-scripts/ddmd_run_io_only.sh:/opt/run_ddmd.sh
env:
  DDMD_MD_BYTES: "1073741824"
  DDMD_MD_BS:    "8388608"
```

Replaces `/opt/run_ddmd.sh` itself with a bash-only loop that runs
`num_tasks` parallel dd writes per iteration, then stamps the
ml/ms/agent files without RADICAL. Same file layout under
`/mnt/wrp_cte/ddmd/` so downstream consumers don't care. Wall collapses
from ~6 min to ~45 s for the same 20 GiB.

## Tuning matrix

| Goal | Knob | Rule of thumb |
|---|---|---|
| **More MD I/O** | bump `num_tasks` or `DDMD_MD_BYTES` | linear |
| **More iterations of the task graph** | bump `iterations` | linear in I/O, also adds ml/ms/agent + RADICAL scheduling per iter |
| **Less RADICAL compute** | layer 2 bind-mount above | wall floor drops from ~3 min to ~5 s |
| **Match `num_tasks` to template parallelism** | template has `cpus_per_node: 4` by default — set `num_tasks` ≤ that for max concurrency, else tasks serialize | only relevant in native/layer-1 modes |

## Measured calibration (ares, 4-node SLURM, NFS-backed overlay)

| Variant | num_tasks | MD_BYTES | Wall | I/O |
|---|---|---|---|---|
| Native (4 MiB stub) | 4 | 4 MiB | 7+ min | ~64 MiB MD |
| Layer 1 (bigger MD) | 16 | 1 GiB | 8.0 min | 16 GiB MD + ml read-back ≈ 32 GB |
| Layer 1 (no ml read) | 20 | 1 GiB | 6.3 min | 20 GiB MD |
| **Layer 2 (bash-only)** | **20** | **1 GiB** | **45 s** | **21.5 GB** ✓ |

YAML lives at `builtin/pipelines/ares/deepdrivemd_apptainer_test.yaml`.
