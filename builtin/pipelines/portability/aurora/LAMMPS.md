# LAMMPS on Aurora — step-by-step guide

This guide walks you through running the **LAMMPS** molecular-dynamics code
on **Aurora** (Argonne's Intel-GPU exascale supercomputer) using **jarvis**
(the pipeline tool that automates the build and launch). You don't need to
install LAMMPS or any compilers — jarvis builds everything inside a
container.

If you've never used any of these tools before, follow the sections in
order.

For a non-Aurora machine, see
[`builtin/builtin/lammps/README.md`](../../../builtin/lammps/README.md)
for pointers to the Delta / Ares / local-CPU guides.

---

## What LAMMPS is

LAMMPS (Large-scale Atomic/Molecular Massively Parallel Simulator) is an
open-source classical molecular-dynamics code from Sandia National
Laboratories, distributed under the GPL v2. Upstream docs:
<https://docs.lammps.org/>.

The Aurora pipeline provided here builds LAMMPS with:

- **Kokkos** package (so kernels can run on a GPU)
- **Kokkos SYCL backend** (so "GPU" on Aurora means Intel Data Center GPU
  Max, a.k.a. PVC)
- **MOLECULE** + **RIGID** packages. `KSPACE` is intentionally disabled
  on Aurora — see the note in `builtin/builtin/lammps/sycl/build.sh` for
  how to re-enable it with Intel MKL GPU FFT.
- **OpenMPI 4** (from Ubuntu), not Intel MPI — Intel MPI's launcher
  doesn't work inside a rootless apptainer container.

---

## What you'll get

By the end of this guide you will have:

1. Built a self-contained container image that holds LAMMPS compiled with
   Intel GPU (SYCL) support.
2. Run a Lennard-Jones benchmark on **one** Aurora GPU node (4 MPI ranks ×
   1 PVC tile each, 256 000 atoms, ~15 s wall).
3. Run the same LJ benchmark on **two** Aurora GPU nodes (12 ranks × 12
   PVC tiles, 2 048 000 atoms × 2000 steps, ~47 s wall) with trajectory
   dumps written to shared storage.

---

## Quick glossary

- **Aurora**: ALCF supercomputer with Intel CPUs and Intel Data Center GPU
  Max (PVC) accelerators — each compute node has 6 PVCs.
- **PBS**: the batch scheduler. You ask it for compute nodes with `qsub`.
- **Apptainer**: HPC-friendly container runtime.
- **SIF**: the single-file container image that Apptainer reads.
- **jarvis**: our pipeline orchestrator; it builds the SIF, launches MPI,
  manages hostfiles for you.
- **Pipeline**: a YAML that describes what jarvis should run.
- **SYCL**: the programming model that lets LAMMPS/Kokkos run on Intel
  GPUs (the Intel counterpart of NVIDIA CUDA).

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

The build clones LAMMPS, pulls Intel's oneAPI image, and compiles LAMMPS
with Kokkos SYCL into a single `.sif` file on Lustre. It needs internet,
so do it on a **login node** (compute nodes are offline).

```bash
source ~/jarvis-venv/bin/activate

# jarvis reads the pipeline's hostfile path at load time, so create a
# placeholder — we'll overwrite it with real hostnames later.
echo localhost > ~/hostfile_single.txt

# Trigger the build. First run takes ~15 min (Docker pull + compile).
jarvis ppl load yaml ~/jarvis-cd/builtin/pipelines/portability/aurora/lammps_single_node.yaml
```

When it finishes you'll see:

```
Apptainer SIF ready: /lus/flare/projects/<project>/<user>/jarvis_shared/lammps_aurora_single_node/lammps_aurora_single_node.sif
Loaded pipeline: lammps_aurora_single_node
```

The SIF is now on Lustre, visible to every compute node. You only rerun
this step if you change the build recipe.

---

## 2. Single-node run (4 ranks × 1 PVC tile)

### 2a. Get a compute node from PBS

On a **login node**:

```bash
qsub -l select=1 -l walltime=00:30:00 -l filesystems=flare \
     -A gpu_hack -q debug -I
```

When PBS is ready, it drops you on a compute node with a prompt like
`isamuradli@x4515c4s1b0n0:~>`.

### 2b. Run on the compute node

```bash
source ~/jarvis-venv/bin/activate
echo "$HOSTNAME" | tee ~/hostfile_single.txt \
  /lus/flare/projects/<project>/<user>/jarvis_shared/lammps_aurora_single_node/hostfile > /dev/null
jarvis hostfile set ~/hostfile_single.txt
jarvis ppl run
```

(Replace `<project>` and `<user>` with your values.)

### 2c. What success looks like

You should see output containing:

```
KOKKOS mode with Kokkos version 5.1.99 is enabled
  will use up to 1 GPU(s) per node
...
Created 256000 atoms
  1 by 2 by 2 MPI processor grid
...
  pair build: full/bin/kk/device
...
Step        Temp        E_pair      ...
   0        1.44       -6.7733     ...
 100        0.7586     -5.7603     ...
...
1000        0.7045     -5.6770     ...
Performance: 269.196 timesteps/s, 68.914 Matom-step/s
Total wall time: 0:00:15
```

Three checks confirm the GPU actually ran the work:

- `/device` in `pair build: full/bin/kk/device` means the pair kernel ran
  on the Intel GPU. If it said `kk/host` you'd be on CPU.
- `attributes: ... kokkos_device` on the neighbor list — same thing.
- Throughput ~60+ M atom-steps/s/rank is GPU-class; a CPU run would be
  30–60× slower.

Trajectory dumps land in the pipeline's `lammps_out/` directory on
Lustre:

```
/lus/flare/projects/<project>/<user>/jarvis_shared/lammps_aurora_single_node/lammps_aurora/lammps_out/
dump.100.lammpstrj  dump.200.lammpstrj  ...  dump.1000.lammpstrj
```

---

## 3. Multi-node run (12 ranks × 2 nodes × 6 PVC tiles)

### 3a. Ask PBS for 2 nodes

On a **login node**:

```bash
qsub -l select=2 -l walltime=00:30:00 -l filesystems=flare \
     -A gpu_hack -q debug -I
```

### 3b. Prepare the multi-node pipeline

The recipe is identical to the single-node one, so **reuse the SIF you
already built** (saves ~15 min) by hardlinking it:

```bash
source ~/jarvis-venv/bin/activate

# Create the multi-node pipeline's shared dir and reuse the existing SIF.
mkdir -p /lus/flare/projects/<project>/<user>/jarvis_shared/lammps_aurora_multi_node
ln -f /lus/flare/projects/<project>/<user>/jarvis_shared/lammps_aurora_single_node/lammps_aurora_single_node.sif \
      /lus/flare/projects/<project>/<user>/jarvis_shared/lammps_aurora_multi_node/lammps_aurora_multi_node.sif
```

Then load the multi-node pipeline — jarvis skips the rebuild because the
SIF is already there:

```bash
cat $PBS_NODEFILE | awk -F. '{print $1}' > ~/hostfile_multi.txt
cp ~/hostfile_multi.txt /lus/flare/projects/<project>/<user>/jarvis_shared/lammps_aurora_multi_node/hostfile
jarvis ppl load yaml ~/jarvis-cd/builtin/pipelines/portability/aurora/lammps_multi_node.yaml
jarvis hostfile set ~/hostfile_multi.txt
```

### 3c. Run

```bash
jarvis ppl run
```

### 3d. What success looks like

```
Created 2048000 atoms
  2 by 2 by 3 MPI processor grid
...
Performance: 112.855 Matom-step/s
Total wall time: 0:00:47
```

- `2 by 2 by 3 MPI processor grid` = 12 ranks split across the 2-node
  domain.
- `Performance: ... 112.855 Matom-step/s` aggregate throughput.
- `Pair` and `Neigh` time fractions in the `MPI task timing breakdown`
  are small compared to `Comm` (~58 %). That's expected — see §6.

---

## 4. Where dumps and logs live

The jarvis pipeline's shared directory lives on Lustre and is visible on
every node:

```
/lus/flare/projects/<project>/<user>/jarvis_shared/lammps_aurora_<single|multi>_node/
lammps_aurora/
  lammps_out/          # dump.N.lammpstrj trajectory files
  generated_io_input.lmp   # auto-generated LJ input script
hostfile               # your node list
pipeline.yaml          # jarvis's saved pipeline config
```

---

## 5. Tuning — atoms, steps, ranks

Edit `builtin/pipelines/portability/aurora/lammps_{single,multi}_node.yaml`
and re-run `jarvis ppl load yaml ...` to pick up the changes. The SIF is
not rebuilt as long as it exists.

| Setting | What it controls | Typical values |
|---|---|---|
| `nprocs` | total MPI ranks | `select × ppn` |
| `ppn` | ranks per node | 1–6 (one per PVC tile) |
| `io_lattice_size` | FCC lattice size N; atom count = 4 × N³ | 40 (256k), 80 (2M), 128 (8.4M) |
| `io_run_steps` | total MD timesteps | 1 000–100 000 |
| `io_dump_interval` | steps between trajectory dumps | 100–500 (0 = use `script:` instead) |
| `num_gpus` | GPUs each rank should use | 1 (one rank per PVC tile) |
| `gpu_aware_mpi` | pass GPU pointers directly to MPI | `false` (default, safe inside apptainer) |

To run a **bare LAMMPS input deck** instead of the auto-generated LJ:

```yaml
pkgs:
  - pkg_type: builtin.lammps
    pkg_name: lammps_aurora
    gpu_backend: sycl
    nprocs: 4
    ppn: 4
    num_gpus: 1
    script: /opt/lammps/bench/in.lj    # path *inside* the container
    io_dump_interval: 0                 # 0 = use the script above
```

---

## 6. Troubleshooting

**Segmentation fault at `Setting up Verlet run` inside `libze_intel_gpu.so`**
> Your MPI stack isn't GPU-aware, but LAMMPS is passing GPU-resident
> buffers directly to `MPI_Send`. Make sure `gpu_aware_mpi: false` (the
> default) so LAMMPS stages through host memory.

**"Connection closed by \<IP\> port 22"**
> Your pipeline hostfile points at a compute node from a previous PBS
> allocation. Overwrite both `~/hostfile_*.txt` and
> `/lus/flare/.../jarvis_shared/<pipeline>/hostfile` with the current
> `$HOSTNAME` (single-node) or `cat $PBS_NODEFILE` (multi-node).

**"No device of requested type 'info::device_type::gpu' available"**
> You're on a login node (no GPUs) or the SIF was built with an
> incompatible oneAPI version. Rerun on a `-l select=N` compute
> allocation.

**"Error: Hostfile not found: /home/.../hostfile_single.txt"**
> Recreate the placeholder with `echo localhost > ~/hostfile_single.txt`
> before `jarvis ppl load yaml`.

**Build fails in `fft3d_kokkos.cpp`**
> You enabled `PKG_KSPACE=ON` without setting up an Intel MKL GPU FFT.
> Either keep KSPACE disabled (the default for the SYCL build), or edit
> `sycl/build.sh` to add `-DFFT_KOKKOS=MKL_GPU -DFFT_KOKKOS_MKL_GPU=ON`
> and make sure the MKL dev packages are on the image.

**`Comm` fraction dominates the timing breakdown**
> Expected on our default setup: the container's OpenMPI isn't
> Level-Zero-aware, so `gpu_aware_mpi: false` stages every halo exchange
> through host memory. Functional but slow; see §7.

---

## 7. Optional: GPU-aware MPI (performance, not correctness)

With `gpu_aware_mpi: false` (our default) you'll see `Comm` take
50–60 % of wall time because halos are staged through host memory on
every exchange. To recover that time you need an MPI stack compiled
with the matching GPU transport — on Aurora, that's native MPICH built
against libfabric with the `cxi` provider and Level-Zero support. This
lives on the host, outside the apptainer image, and requires extra
bind-mounts to reach from inside the container.

Once your MPI does support GPU pointers, set `gpu_aware_mpi: true` in
the YAML. LAMMPS will then pass Kokkos device pointers directly to MPI
calls.

---

## 8. What's inside the container (for the curious)

- **Compiler**: Intel `icpx` from `intel/oneapi-hpckit:2025.0`, targeting
  portable SPIR-V via SYCL JIT (no ahead-of-time PVC lock-in).
- **MPI**: OpenMPI 4 from Ubuntu apt — not Intel MPI. Intel MPI's
  launcher needs `/opt/cray/libfabric` and `$PBS_NODEFILE`, neither of
  which exists inside a rootless apptainer container.
- **Level Zero runtime**: Intel's `libze-intel-gpu1` package; Aurora's
  host `i915` kernel driver is ABI-compatible, so no host library binds
  are needed at run time.
- **Device selection**: `ONEAPI_DEVICE_SELECTOR=level_zero:gpu` pins SYCL
  to Level Zero (the Intel GPU path), not OpenCL.
- **MPI-in-userns tweak**: `OMPI_MCA_btl_vader_single_copy_mechanism=none`
  disables CMA (process_vm_readv) single-copy transfer, which fails under
  apptainer's unprivileged user namespace.

The full recipe lives in `builtin/builtin/lammps/sycl/build.sh`; every
non-obvious choice has a comment explaining why.
