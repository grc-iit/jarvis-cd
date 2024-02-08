# DeepDriveMD-F (DDMD)
DeepDriveMD: Deep-Learning Driven Adaptive Molecular Simulations (file-based continual learning loop).
See the [official repo](https://github.com/DeepDriveMD/DeepDriveMD-pipeline) for more detail.

# Table of Content
0. [Dependencies](#0-dependencies)
1. [Installation](#1-installation)
2. [Running DDMD](#2-running-ddmd)
3. [DDMD with Slurm](#3-ddmd-with-slurm)
4. [DDMD + Hermes](#4-ddmd--hermes)
5. [DDMD on Node Local Storage (FIXME)](#5-ddmd-on-node-local-storage)
6. [DDMD + Hermes on Node Local Storage (FIXME)](#6-ddmd--hermes-on-node-local-storage)
7. DDMD + Hermes with Multinodes Slurm (TODO)



# 0. Dependencies

## 0.1. conda
- Prepare Conda
Get the miniconda3 installation script and run it
```
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh.sh
```

## 0.2. jarvis-cd & scspkg
Follow steps here: https://github.com/grc-iit/jarvis-cd


## 0.3. spack
Spack is used to install HDF5, MPICH, and Hermes.
Install spack steps are here: https://spack.readthedocs.io/en/latest/getting_started.html#installation

## 0.4. HDF5 (1.14.0+)
HDF5 is require, (1.14.0+) is required by Hermes and h5py==3.8.0.

Use spack to install hdf5
```bash
spack install hdf5@1.14.0+hl~mpi
```


## 0.5. MPI
Either OpenMPI or MPICH works, this is required by Hermes(VFD depend on MPIIO adaptor)


Use spack to install mpich
```bash
spack install mpich@3.4.3
``` 



# 1. Installation

## 1.1 create ddmd scs package
```bash
scspkg create ddmd
cd `scspkg pkg src ddmd`
git clone https://github.com/candiceT233/deepdrivemd_pnnl.git deepdrivemd
cd deepdrivemd
export DDMD_PATH="`pwd`"
scspkg env set ddmd DDMD_PATH=$DDMD_PATH HDF5_USE_FILE_LOCKING=FALSE
```


## 1.2 Prepare conda environment and python packages
### 1.2.1 Set conda environment variable
```bash
export CONDA_OPENMM=hermes_openmm7_ddmd
export CONDA_PYTORCH=hm_ddmd_pytorch
```


### 1.2.2 Create the respective conda environment with YML files
```bash
cd "`scspkg pkg src ddmd`/deepdrivemd"
conda env create -f ddmd_openmm7.yaml --name=${CONDA_OPENMM}
conda env create -f ddmd_pytorch.yaml --name=${CONDA_PYTORCH}
```



### 1.2.2 Update the conda environment python packages
- for `CONDA_OPENMM`
```bash
cd `scspkg pkg src ddmd`
conda activate $CONDA_OPENMM
export DDMD_PATH="`pwd`"

cd $DDMD_PATH/submodules/MD-tools
pip install -e .

cd $DDMD_PATH/submodules/molecules
pip install -e .

pip uninstall h5py;
HDF5_MPI="OFF" HDF5_DIR=${YOUR_HDF5_DIR} pip install --no-cache-dir --no-binary=h5py h5py==3.8.0

conda deactivate
```


- for `CONDA_PYTORCH`
```bash
cd `scspkg pkg src ddmd`
conda activate $CONDA_PYTORCH
export DDMD_PATH="`pwd`"

cd $DDMD_PATH/submodules/MD-tools
pip install .

cd $DDMD_PATH/submodules/molecules
pip install .

cd $DDMD_PATH
pip install .

pip uninstall h5py;
HDF5_MPI="OFF" HDF5_DIR=${YOUR_HDF5_DIR} pip install --no-cache-dir --no-binary=h5py h5py==3.8.0

conda deactivate
```


# 2. Running DDMD

## 2.1. Setup Environment
Currently setup input path in a shared storage.
Setup experiment input and ouput paths:
```bash
EXPERIMENT_PATH=~/experiments/ddmd_runs #NFS
mkdir -p $EXPERIMENT_PATH
```


## 2.2. Create a Resource Graph

If you haven't already, create a resource graph. This only needs to be done
once throughout the lifetime of Jarvis. No need to repeat if you have already
done this for a different pipeline.

For details building resource graph, please refer to https://github.com/grc-iit/jarvis-cd/wiki/2.-Resource-Graph.

If you are running distributed tests, set path to the hostfile you are  using.
```bash
jarvis hostfile set /path/to/hostfile
```

Next, collect the resources from each of those pkgs. Walkthrough will give
a command line tutorial on how to build the hostfile.
```bash
jarvis resource-graph build +walkthrough
```

## 2.3 Create a Pipeline

The Jarvis pipeline will store all configuration data needed by DDMD.

```bash
jarvis pipeline create ddmd_test
```

## 2.4. Save Environment
Create the environment variables needed by DDMD.
```bash
spack load hdf5@1.14.0+hl~mpi mpich
module load ddmd
```

Store the current environment in the pipeline.
```bash
jarvis pipeline env build +CONDA_OPENMM +CONDA_PYTORCH +DDMD_PATH
```

## 2.5. Add pkgs to the Pipeline
Create a Jarvis pipeline with DDMD.
```bash
jarvis pipeline append ddmd
```

## 2.6. Run Experiment

Run the experiment
```bash
jarvis pipeline run
```

## 2.8. Clean Data

Clean data produced by DDMD
```bash
jarvis pipeline clean
```



# 3. DDMD With Slurm

## 3.1 Local Cluster
`ppn` must equal or greater than `num_workers`,which is default to 1.
```bash
jarvis pipeline sbatch job_name=ddmd_test nnodes=1 ppn=2 output_file=./ddmd_test.out error_file=./ddmd_test.err
```

## 3.2 Multi Nodes Cluster (TODO)
DDMD with jarvis-cd is currently only set to run with single node and using CPU.
    - Multiple CPU worker not tested
    - GPU not tested



# 4. DDMD + Hermes

## 4.0. Dependencies
### 4.0.1 HDF5
Hermes must compile with HDF5, makesure [download HDF5-1.14.0 with spack](#04-hdf5-1140).

### 4.0.2 Install Hermes dependencies with spack
```bash
spack load hdf5@1.14.0+hl~mpi mpich@3.4.3
spack install hermes_shm ^hdf5@1.14.0+hl~mpi ^mpich@3.4.3
```

### 4.0.3 Install Hermes with scspkg
```bash
spack load hermes_shm
scspkg create hermes
cd `scspkg pkg src hermes`
git clone https://github.com/HDFGroup/hermes
cd hermes
mkdir build
cd build
cmake ../ -DCMAKE_BUILD_TYPE="Release" \
    -DCMAKE_INSTALL_PREFIX=`scspkg pkg root hermes` \
    -DHERMES_ENABLE_MPIIO_ADAPTER="ON" \
    -DHERMES_MPICH="ON" \
    -DHERMES_ENABLE_POSIX_ADAPTER="ON" \
    -DHERMES_ENABLE_STDIO_ADAPTER="ON" \
    -DHERMES_ENABLE_VFD="ON" \

```

## 4.1. Setup Environment

Create the environment variables needed by Hermes + DDMD
```bash
spack load hermes_shm
module load hermes ddmd
```

## 4.2. Create a Resource Graph

Same as [above](#2-create-a-resource-graph).

## 4.3. Create a Pipeline

The Jarvis pipeline will store all configuration data needed by Hermes
and DDMD.

```bash
jarvis pipeline create hermes_ddmd_test
```

## 4.4. Save Environment

Store the current environment in the pipeline.
```bash
jarvis pipeline env build +CONDA_OPENMM +CONDA_PYTORCH +DDMD_PATH
```

## 4.5. Add pkgs to the Pipeline

Create a Jarvis pipeline with Hermes, using the Hermes VFD interceptor.
```bash
jarvis pipeline append hermes_run --sleep=10 include=$EXPERIMENT_PATH
jarvis pipeline append hermes_api +vfd
jarvis pipeline append ddmd update_envar=true
```

## 4.6. Run the Experiment (TODO)

Run the experiment
```bash
jarvis pipeline run
```

## 4.7. Clean Data

To clean data produced by DDMD:
```bash
jarvis pipeline clean
```



# 5. DDMD on Node Local Storage
For cluster that has node local storage, you can stagein data from shared storage, then run ddmd.

## 5.1 Setup Environment
Currently setup DEFAULT input path in a shared storage, below is a example on Ares cluster using node local nvme.
```bash
RUN_SCRIPT=vistsis # can change to other datasets
EXPERIMENT_PATH=~/experiments/ddmd_run # NFS
INPUT_PATH=$EXPERIMENT_PATH/input_data # NFS
cd $EXPERIMENT_PATH; export PRETRAIN_MODEL_PATH=`realpath model_large.pth`

LOCAL_EXPERIMENT_PATH=/mnt/nvme/$USER/ddmd_run
LOCAL_INPUT_PATH=$LOCAL_EXPERIMENT_PATH/input_data
LOCAL_OUTPUT_PATH=$LOCAL_EXPERIMENT_PATH/output_data
```

## 5.2. Download Pretrain Model and Input Data
Same as above [download pretrain](#22-download-pretrain-model) and [download input](#23-download-input-data).

## 5.3. Create a Resource Graph
Same as [above](#23-create-a-resource-graph)

## 5.4. Create a Pipeline

The Jarvis pipeline will store all configuration data needed by DDMD.

```bash
jarvis pipeline create ddmd_local
```

## 5.5. Save Environment
Create the environment variables needed by DDMD.
```bash
spack load hdf5@1.14.0+hl~mpi mpich@3.4.3
module load ddmd
```


Store the current environment in the pipeline.
```bash
jarvis pipeline env build +CONDA_OPENMM +CONDA_PYTORCH +DDMD_PATH
```


## 5.6. Add pkgs to the Pipeline
Add data_stagein to pipeline before ddmd.
- For `RUN_SCRIPT=vistsis` you need to stage in three different input directories:
```bash 
jarvis pipeline append data_stagein dest_data_path=$LOCAL_INPUT_PATH \
user_data_paths=$INPUT_PATH/vistdii,$INPUT_PATH/vistsis,$INPUT_PATH/visit_img,$PRETRAIN_MODEL_PATH \
mkdir_datapaths=$LOCAL_INPUT_PATH,$LOCAL_OUTPUT_PATH
```

- For other `RUN_SCRIPT`, you only need to stagein one directory:
```bash 
RUN_SCRIPT=pororo
jarvis pipeline append data_stagein dest_data_path=$LOCAL_INPUT_PATH \
user_data_paths=$INPUT_PATH/$RUN_SCRIPT \
mkdir_datapaths=$LOCAL_INPUT_PATH,$LOCAL_OUTPUT_PATH
```


Create a Jarvis pipeline with DDMD.
```bash
jarvis pipeline append ddmd runscript=$RUN_SCRIPT ddmd_path="`scspkg pkg src ddmd`/DDMD" local_exp_dir=$LOCAL_EXPERIMENT_PATH
```

## 5.7. Run the Experiment

Run the experiment
```bash
jarvis pipeline run
```

## 5.8. Clean Data

To clean data produced by Hermes + DDMD:
```bash
jarvis pipeline clean
```



# 6. DDMD + Hermes on Node Local Storage
Every step the same as [DDMD + Hermes](#4-ddmd-with-hermes), except for when creating a Jarvis pipeline with Hermes, using the Hermes VFD interceptor:
- Example using `RUN_SCRIPT=vistsis` you need to stage in three different input directories.
```bash
# Setup env
RUN_SCRIPT=vistsis # can change to other datasets
EXPERIMENT_PATH=~/experiments/ddmd_run # NFS
INPUT_PATH=$EXPERIMENT_PATH/input_data # NFS
cd $EXPERIMENT_PATH; export PRETRAIN_MODEL_PATH=`realpath model_large.pth`

LOCAL_EXPERIMENT_PATH=/mnt/nvme/$USER/ddmd_run
LOCAL_INPUT_PATH=$LOCAL_EXPERIMENT_PATH/input_data
LOCAL_OUTPUT_PATH=$LOCAL_EXPERIMENT_PATH/output_data

# add pkg to pipeline
jarvis pipeline append data_stagein dest_data_path=$LOCAL_INPUT_PATH \
user_data_paths=$INPUT_PATH/vistdii,$INPUT_PATH/vistsis,$INPUT_PATH/visit_img,$PRETRAIN_MODEL_PATH \
mkdir_datapaths=$LOCAL_INPUT_PATH,$LOCAL_OUTPUT_PATH

jarvis pipeline append hermes_run --sleep=10 include=$LOCAL_EXPERIMENT_PATH

jarvis pipeline append hermes_api +vfd

jarvis pipeline append ddmd runscript=vistsis ddmd_path="`scspkg pkg src ddmd`/DDMD" update_envar=true local_exp_dir=$LOCAL_EXPERIMENT_PATH
```


# 7. DDMD + Hermes with Multinodes Slurm (TODO)
Multinodes DDMD is not supported yet.

