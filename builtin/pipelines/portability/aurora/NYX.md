# Nyx on Aurora — step-by-step guide

This guide walks you through running the **Nyx** cosmological simulation code
on **Aurora** (Argonne's Intel-GPU exascale supercomputer) using **jarvis**
(the pipeline tool that automates the build and launch). You don't need to
install Nyx or any compilers — jarvis builds everything inside a container.

If you've never used any of these tools before, follow the sections in order.

For a non-Aurora machine, see
[`builtin/builtin/nyx/README.md`](../../../builtin/nyx/README.md) for
pointers to the Delta / CPU guides.

---

## What you'll get

By the end of this guide you will have:

1. Built a self-contained container image that holds the Nyx simulation
   compiled with Intel GPU support (SYCL).
2. Run Nyx on **one** Aurora GPU node (4 MPI ranks × 1 GPU tile, ~10 s).
3. Run the same Nyx on **two** Aurora GPU nodes (12 MPI ranks × 12 GPU
   tiles, ~15 s), writing plot files to shared storage.

---

## Quick glossary

- **Aurora**: ALCF supercomputer with Intel CPUs and Intel Data Center GPU
  Max (PVC) accelerators — each compute node has 6 PVCs.
- **PBS**: the batch scheduler. You ask it for compute nodes with `qsub`.
- **Apptainer** (formerly Singularity): HPC-friendly container runtime.
- **SIF**: the single-file container image that Apptainer reads.
- **jarvis**: our pipeline orchestrator; it builds the SIF, launches MPI,
  manages hostfiles for you.
- **Pipeline**: a YAML that describes what jarvis should run. We provide
  ready-made pipelines for Nyx single-node and multi-node.
- **SYCL**: the programming model that lets Nyx/AMReX run on Intel GPUs
  (the Intel counterpart of NVIDIA CUDA).

---

## 0. One-time setup

You need an ALCF account with an allocation that lets you submit jobs to a
GPU queue (the examples use `-q debug -A gpu_hack`; replace with your
project code).

Install jarvis once in a Python virtual environment:

```bash
# from an Aurora login node (aurora-uan-XXXX):
git clone https://github.com/grc-iit/jarvis-cd.git ~/jarvis-cd
python3 -m venv ~/jarvis-venv
source ~/jarvis-venv/bin/activate
pip install -e ~/jarvis-cd
```

Any time you open a new shell, activate the venv:

```bash
source ~/jarvis-venv/bin/activate
```

---

## 1. Build the container (login node, once)

The build clones Nyx, pulls Intel's oneAPI image, and compiles Nyx with SYCL
into a single `.sif` file on Lustre. It needs internet, so do it on a **login
node** (compute nodes are offline).

```bash
source ~/jarvis-venv/bin/activate

# jarvis reads the pipeline's hostfile path at load time, so create a
# placeholder — we'll overwrite it with real hostnames later.
echo localhost > ~/hostfile_single.txt

# Trigger the build. First run takes ~10–15 min (Docker pull + compile).
jarvis ppl load yaml ~/jarvis-cd/builtin/pipelines/portability/aurora/nyx_single_node.yaml
```

When it finishes you'll see:

```
Apptainer SIF ready: /lus/flare/projects/<project>/<user>/jarvis_shared/nyx_aurora_single_node/nyx_aurora_single_node.sif
Loaded pipeline: nyx_aurora_single_node
```

The 4.6 GB SIF is now on Lustre, visible to every compute node. You only
rerun this step if you change the build recipe.

---

## 2. Single-node run (4 ranks × 1 PVC tile)

### 2a. Get a compute node from PBS

On a **login node**:

```bash
qsub -l select=1 -l walltime=00:15:00 -l filesystems=flare \
     -A gpu_hack -q debug -I
```

When PBS is ready, it drops you on a compute node with a prompt like
`isamuradli@x4218c0s3b0n0:~>`. That `x4218c0s3b0n0` is your node's hostname.

### 2b. Run on the compute node

```bash
source ~/jarvis-venv/bin/activate

# Tell jarvis which node(s) to use. For single-node, just this one:
echo "$HOSTNAME" > ~/hostfile_single.txt
echo "$HOSTNAME" > /lus/flare/projects/<project>/<user>/jarvis_shared/nyx_aurora_single_node/hostfile
jarvis hostfile set ~/hostfile_single.txt

# Launch Nyx
jarvis ppl run
```

(Replace `<project>` and `<user>` with your project code and username — one
easy way is `echo "$HOSTNAME" | tee ~/hostfile_single.txt /lus/flare/projects/gpu_hack/isamuradli/jarvis_shared/nyx_aurora_single_node/hostfile`.)

### 2c. What success looks like

You should see output that includes:

```
Initializing AMReX ...
MPI initialized with 4 MPI processes
Initializing SYCL...
SYCL initialized with 4 devices.
...
PLOTFILE: file = /lus/flare/.../jarvis_shared/nyx_aurora_single_node/nyx_aurora/nyx_out/plt00100
Total GPU global memory (MB) spread across MPI: [131040 ... 131040]
AMReX (...) finalized
```

Three checks confirm everything worked:

- `SYCL initialized with 4 devices` — 4 MPI ranks, each got a PVC tile.
- `131040 MB` per rank — 131 GB is PVC HBM. If you see `MB: [0 ... 0]`
  or a much smaller number, the CPU fallback ran instead of the GPU.
- 11 plotfiles appear (`plt00000`, `plt00010`, …, `plt00100`) in the
  `nyx_out/` directory printed above.

---

## 3. Multi-node run (12 ranks × 2 nodes × 6 PVC tiles)

### 3a. Ask PBS for 2 nodes

On a **login node**:

```bash
qsub -l select=2 -l walltime=00:30:00 -l filesystems=flare \
     -A gpu_hack -q debug -I
```

Change `select=2` to any larger number for more scale-out; just keep
`nprocs = select × ppn` consistent (see §5 if you want to tune).

### 3b. Prepare the multi-node pipeline

The recipe is identical to the single-node one, so you can **reuse the SIF
you already built** (saves ~15 min) by hardlinking it:

```bash
source ~/jarvis-venv/bin/activate

# Create the multi-node pipeline's shared dir and reuse the existing SIF.
# Hardlinks are zero-copy on the same filesystem.
mkdir -p /lus/flare/projects/<project>/<user>/jarvis_shared/nyx_aurora_multi_node
ln -f /lus/flare/projects/<project>/<user>/jarvis_shared/nyx_aurora_single_node/nyx_aurora_single_node.sif \
      /lus/flare/projects/<project>/<user>/jarvis_shared/nyx_aurora_multi_node/nyx_aurora_multi_node.sif
```

Then load the multi-node pipeline — jarvis skips the rebuild because the
SIF is already there:

```bash
cat $PBS_NODEFILE | awk -F. '{print $1}' > ~/hostfile_multi.txt
cp ~/hostfile_multi.txt /lus/flare/projects/<project>/<user>/jarvis_shared/nyx_aurora_multi_node/hostfile
jarvis ppl load yaml ~/jarvis-cd/builtin/pipelines/portability/aurora/nyx_multi_node.yaml
jarvis hostfile set ~/hostfile_multi.txt
```

### 3c. Run

```bash
jarvis ppl run
```

### 3d. What success looks like

```
MPI initialized with 12 MPI processes
SYCL initialized with 12 devices.
...
PLOTFILE: file = /lus/flare/.../jarvis_shared/nyx_aurora_multi_node/nyx_aurora/nyx_out/plt00100
[The Arena] max space (MB) allocated spread across MPI: [98280 ... 98280]
AMReX (...) finalized
```

- `MPI initialized with 12 MPI processes` — 6 ranks on each of the 2 nodes.
- `SYCL initialized with 12 devices` — one PVC tile per rank.
- `FAB kilobyte spread [A ... B]` should show A and B within a few percent
  — that's your load balance across the nodes.

---

## 4. Where the plot files live

Nyx writes plotfiles under the pipeline's jarvis-managed directory on
Lustre (shared across all nodes):

```
/lus/flare/projects/<project>/<user>/jarvis_shared/nyx_<pipeline>/nyx_aurora/nyx_out/
plt00000  plt00010  plt00020  ...  plt00100
```

You can post-process them with AMReX-aware tools (`amrvis`, `yt`, `VisIt`,
`ParaView` with the `AMReX` reader, etc.). The plotfiles survive after PBS
exits, so you can analyze them from a login node later.

---

## 5. Tuning — number of ranks, grid size, steps

Edit `builtin/pipelines/portability/aurora/nyx_{single,multi}_node.yaml`
and re-run `jarvis ppl load yaml ...` to pick up the changes (rebuild is
skipped as long as the SIF exists).

| Setting | What it controls | Typical values |
|---|---|---|
| `nprocs` | total MPI ranks | `select × ppn` (must match) |
| `ppn` | ranks per node | 1–6 (one per PVC tile) |
| `n_cell` | grid size `"nx ny nz"` | 128³ small, 256³ medium, 512³ large |
| `max_step` | number of timesteps | 100 for quick check, 1000+ for real |
| `plot_int` | steps between plotfiles | 10, 25, 100 |
| `max_level` | AMR refinement depth | 0 (single level) – 3 |

Rule of thumb on Aurora:
- Single-node test: `nprocs: 4, ppn: 4, n_cell: "128 128 128"` (~10 s).
- Two-node test: `nprocs: 12, ppn: 6, n_cell: "256 256 256"` (~15 s).
- Production: `nprocs: select × 6, ppn: 6`.

---

## 6. Troubleshooting

**"Connection closed by \<IP\> port 22"**
> Your pipeline hostfile points at a compute node from a previous PBS
> allocation. Overwrite both `~/hostfile_*.txt` and
> `/lus/flare/.../jarvis_shared/<pipeline>/hostfile` with the current
> `$HOSTNAME` (single-node) or `cat $PBS_NODEFILE` (multi-node).

**"No device of requested type 'info::device_type::gpu' available"**
> You're on a login node (no GPUs) or the SIF was built for a different
> oneAPI version. Rerun on a `-l select=N` compute allocation.

**"Error: Hostfile not found: /home/.../hostfile_single.txt"**
> You deleted the hostfile between sessions. Recreate it with
> `echo localhost > ~/hostfile_single.txt` before `jarvis ppl load yaml`.

**"Couldn't open file ... plt00000.temp/Level_0/Cell_D_NNNNN"** (multi-node)
> Your pipeline is writing plotfiles to `/tmp`, which is per-node
> ephemeral. The current YAML defaults to shared Lustre — if you
> overrode it, set `out:` to somewhere under `/lus/flare/...`.

**`Read -1, ... errno = 14` lines flooding the log**
> Harmless OpenMPI shared-memory transfer warnings inside apptainer's
> user namespace. The run is still correct. The container already sets
> `OMPI_MCA_btl_vader_single_copy_mechanism=none` to suppress these;
> if you still see them, rebuild the SIF.

---

## 7. Configuration reference

| YAML key (under `pkgs:`) | Type | Default | Description |
|---|---|---|---|
| `nprocs` | int | 4 | Total MPI ranks across all nodes |
| `ppn` | int | 4 | Ranks per node (≤ 6 on Aurora) |
| `max_step` | int | 100 | Number of coarse timesteps |
| `n_cell` | str | `"128 128 128"` | Base grid, `"nx ny nz"` |
| `max_level` | int | 0 | AMR refinement levels |
| `out` | str | *(shared_dir)* | Plot file directory — leave unset for the safe default |
| `plot_int` | int | 10 | Steps between plotfiles (`-1` = no plots) |
| `gpu_backend` | str | `sycl` on Aurora YAML | Backend: `sycl`, `cuda`, or `none` |
| `base_image` | str | hpckit | Docker/Apptainer base image |

---

## 8. Running on other machines

Nyx+jarvis also supports NVIDIA CUDA and CPU-only. Use one of these YAML
shapes:

**NVIDIA CUDA (e.g. Delta / Polaris):**
```yaml
pkgs:
  - pkg_type: builtin.nyx
    pkg_name: nyx_cuda
    gpu_backend: cuda
    cuda_arch: 80
    base_image: nvidia/cuda:12.8.0-devel-ubuntu24.04
```

**CPU-only:**
```yaml
pkgs:
  - pkg_type: builtin.nyx
    pkg_name: nyx_cpu
    gpu_backend: none
    base_image: ubuntu:24.04
```

---

## 9. What's inside the container (for the curious)

If you want to understand why the Aurora build is non-trivial:

- **Compiler**: Intel `icpx` from `intel/oneapi-hpckit:2025.0`, targeting
  portable SPIR-V via SYCL JIT (no ahead-of-time target lock-in).
- **MPI**: OpenMPI 4 installed via `apt` — not Intel MPI. Intel MPI's
  launcher needs `/opt/cray/libfabric` and `$PBS_NODEFILE`, neither of
  which exists inside a rootless apptainer container.
- **Level Zero runtime**: Intel's `libze-intel-gpu1` package (the PVC
  backend); the Aurora host's `i915` kernel driver is ABI-compatible with
  it, so no host library bindings are needed at run time.
- **Device selection**: `ONEAPI_DEVICE_SELECTOR=level_zero:gpu` pins SYCL
  to Level Zero (the Intel GPU path), not OpenCL.

The full recipe lives in `builtin/builtin/nyx/sycl/build.sh`; every
non-obvious choice has a comment explaining why.
