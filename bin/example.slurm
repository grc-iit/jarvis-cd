#!/bin/bash
#SBATCH --mem=16g
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=4    # <- match to OMP_NUM_THREADS
#SBATCH --partition=cpu      # <- or one of: gpuA100x4 gpuA40x4 gpuA100x8 gpuMI100x8
#SBATCH --account=bekn-delta-cpu    # <- match to a "Project" returned by the "accounts" command
#SBATCH --job-name=myjobtest
#SBATCH --time=00:05:00      # hh:mm:ss for the job
#SBATCH --constraint="scratch"
#SBATCH -e slurm-%j.err
#SBATCH -o slurm-%j.out
### GPU options ###
##SBATCH --gpus-per-node=0
##SBATCH --gpu-bind=none     # <- or closest
##SBATCH --mail-user=you@yourinstitution.edu
##SBATCH --mail-type="BEGIN,END" See sbatch or srun man pages for more email options

JARVIS_FIRST=$(jarvis sched hostfile build +slurm_host)
if [ "$JARVIS_FIRST" -eq 0 ]; then
    exit 0
fi
echo "On first node!!!"
jarvis rg build +slurm_host
