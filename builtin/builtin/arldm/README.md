The AR-LDM (Auto-Regressive Latent Diffusion Models) is a latent diffusion model auto-regressively conditioned on history captions and generated images.
See the [official repo](https://github.com/xichenpan/ARLDM) for more detail.

# Table of Content
0. [Dependencies](#0-dependencies)
1. [Installation](#1-installation)
2. [Running ARLDM](#2-running-arldm)
3. [ARLDM with Slurm](#3-arldm-with-slurm)
4. [ARLDM + Hermes](#4-arldm--hermes)
5. [ARLDM on Node Local Storage](#5-arldm-on-node-local-storage)
6. [ARLDM + Hermes on Node Local Storage](#6-arldm--hermes-on-node-local-storage)
7. ARLDM + Hermes with Multinodes Slurm (TODO)



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
Either OpenMPI or MPICH works, this is required by Hermes and mpi4py


Use spack to install mpich
```bash
spack install mpich@3.4.3
```

## 0.6. Installation Tools
You need `wget` and `gdown` to download the datasets online:
- wget
You can install wget either with `apt-get` or `spack`
```bash
sudo apt-get install wget
# or
spack install wget
spack load wget
# check if wget is usable
which wget
```
- gdown
```shell
python3 -m pip install gdown==4.5.1 # or 4.6.0
pip show gdown
```


# 1. Installation

## 1.1 create arldm scs package
```bash
scspkg create arldm
cd `scspkg pkg src arldm`
git clone https://github.com/candiceT233/ARLDM
cd ARLDM
git switch ares # Use the ares branch
export ARLDM_PATH=`scspkg pkg src arldm`/ARLDM
scspkg env set arldm ARLDM_PATH=$ARLDM_PATH HDF5_USE_FILE_LOCKING=FALSE
```


## 1.2 Prepare conda environment and python packages:
```bash
cd `scspkg pkg src arldm`/ARLDM

YOUR_HDF5_DIR="`which h5cc |sed 's/.\{9\}$//'`"
conda env create -f arldm_conda.yml
conda activate arldm
pip uninstall h5py;
HDF5_MPI="OFF" HDF5_DIR=${YOUR_HDF5_DIR} pip install --no-cache-dir --no-binary=h5py h5py==3.8.0
conda deactivate
```



# 2. Running ARLDM

## 2.1. Setup Environment
Currently setup input path in a shared storage, below is a example on Ares cluster.
Setup experiment input and ouput paths:
```bash
EXPERIMENT_PATH=~/experiments/arldm_run
EXPERIMENT_INPUT_PATH=$EXPERIMENT_PATH/input_data
OUTPUT_PATH=$EXPERIMENT_PATH/output_data

mkdir -p $EXPERIMENT_INPUT_PATH $OUTPUT_PATH $EXPERIMENT_INPUT_PATH/zippack
```

## 2.2. Download Pretrain Model
The pretrain model is ~ 3.63 GB (~ 10 mins on Ares)
```bash
cd $EXPERIMENT_PATH
conda activate arldm
wget https://storage.googleapis.com/sfr-vision-language-research/BLIP/models/model_large.pth
export PRETRAIN_MODEL_PATH=`realpath model_large.pth`
conda deactivate
scspkg env set arldm PRETRAIN_MODEL_PATH=$PRETRAIN_MODEL_PATH
```

## 2.3. Download Input Data
You should prepare at least one dataset to run the script. There are 4 available datasets for download `vistsis`, `vistdii`, `pororo`, and `flintstones`.

### 2.3.1 VISTSIS and VISTDII

* Original VIST-SIS (~23MB) url links [here](https://visionandlanguage.net/VIST/json_files/story-in-sequence/SIS-with-labels.tar.gz)
```shell
cd $EXPERIMENT_INPUT_PATH
wget https://visionandlanguage.net/VIST/json_files/story-in-sequence/SIS-with-labels.tar.gz
tar -vxf SIS-with-labels.tar.gz
mv sis vistsis # ~ 172M

# save downloaded package to different directory
mv SIS-with-labels.tar.gz $EXPERIMENT_INPUT_PATH/zippack
```
* Original VIST-DII (~18MB) url links [here](https://visionandlanguage.net/VIST/json_files/description-in-isolation/DII-with-labels.tar.gz)
```shell
cd $EXPERIMENT_INPUT_PATH
wget https://visionandlanguage.net/VIST/json_files/description-in-isolation/DII-with-labels.tar.gz
tar -vxf DII-with-labels.tar.gz
mv dii vistdii # ~ 125M

# save downloaded package to different directory
mv DII-with-labels.tar.gz $EXPERIMENT_INPUT_PATH/zippack
```
Download the VIST images by running below command (this will take over 2 hours on Ares)
```shell
cd $ARLDM_PATH
python data_script/vist_img_download.py --json_dir $EXPERIMENT_PATH/input_data/vistdii --img_dir $EXPERIMENT_PATH/input_data/visit_img --num_process 12
```

### 2.3.3 flintstones 
* Original FlintstonesSV dataset [here](https://drive.google.com/file/d/1kG4esNwabJQPWqadSDaugrlF4dRaV33_/view?usp=sharing).
```shell
cd $EXPERIMENT_INPUT_PATH
gdown "1kG4esNwabJQPWqadSDaugrlF4dRaV33_&confirm=t" # ~10 mins on Ares
unzip flintstones_data.zip # 4.9G, ~2 mins on Ares
mv flintstones_data flintstones # 6.6G
mv flintstones_data.zip $EXPERIMENT_INPUT_PATH/zippack
```
<!-- gdown https://drive.google.com/u/0/uc?id=1kG4esNwabJQPWqadSDaugrlF4dRaV33_&export=download -->


### 2.3.2 pororo 
* Original PororoSV dataset [here](https://drive.google.com/file/d/11Io1_BufAayJ1BpdxxV2uJUvCcirbrNc/view?usp=sharing).
```shell
cd $EXPERIMENT_INPUT_PATH
gdown "11Io1_BufAayJ1BpdxxV2uJUvCcirbrNc&confirm=t" # ~30 mins on Ares
unzip pororo.zip # 15GB
mv pororo_png pororo # 17GB
mv pororo.zip $EXPERIMENT_INPUT_PATH/zippack
```
<!-- gdown https://drive.google.com/u/0/uc?id=11Io1_BufAayJ1BpdxxV2uJUvCcirbrNc&export=download -->


## 2.3. Create a Resource Graph

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

## 2.4 Create a Pipeline

The Jarvis pipeline will store all configuration data needed by ARLDM.

```bash
jarvis pipeline create arldm_test
```

## 2.5. Save Environment
Create the environment variables needed by ARLDM.
```bash
spack load hdf5@1.14.0+hl~mpi
module load arldm
```
<!-- conda activate arldm -->

Store the current environment in the pipeline.
```bash
jarvis pipeline env build
```

## 2.6. Add pkgs to the Pipeline
Create a Jarvis pipeline with ARLDM.
```bash
jarvis pipeline append arldm runscript=vistsis arldm_path="`scspkg pkg src arldm`/ARLDM"
```

<!-- ```bash
jarvis pipeline append arldm conda_env=arldm runscript=vistsis arldm_path="`scspkg pkg src arldm`/ARLDM"

jarvis pkg configure arldm conda_env=arldm runscript=run_mcs_tbpfradar3d_wrf config=${HOME}/jarvis-cd/builtin/builtin/arldm/example_config/config_template.yml
``` -->

## 2.7. Run Experiment

Run the experiment
```bash
jarvis pipeline run
```

## 2.8. Clean Data

Clean data produced by ARLDM
```bash
jarvis pipeline clean
```



# 3. ARLDM With Slurm

## 3.1 Local Cluster
`ppn` must equal or greater than `num_workers`,which is default to 1.
```bash
jarvis pipeline sbatch job_name=arldm_test nnodes=1 ppn=2 output_file=./arldm_test.out error_file=./arldm_test.err
```

## 3.2 Multi Nodes Cluster (TODO)
ARLDM with jarvis-cd is currently only set to run with single node and using CPU.
    - Multiple CPU worker not tested
    - GPU not tested



# 4. ARLDM + Hermes

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

Create the environment variables needed by Hermes + ARLDM
```bash
spack load hermes_shm
module load hermes arldm
```

## 4.2. Create a Resource Graph

Same as [above](#2-create-a-resource-graph).

## 4.3. Create a Pipeline

The Jarvis pipeline will store all configuration data needed by Hermes
and ARLDM.

```bash
jarvis pipeline create hermes_arldm_test
```

## 4.4. Save Environment

Store the current environment in the pipeline.
```bash
jarvis pipeline env build
```

## 4.5. Add pkgs to the Pipeline

Create a Jarvis pipeline with Hermes, using the Hermes VFD interceptor.
```bash
jarvis pipeline append hermes_run --sleep=10 include=$EXPERIMENT_PATH
jarvis pipeline append hermes_api +vfd
jarvis pipeline append arldm runscript=vistsis arldm_path="`scspkg pkg src arldm`/ARLDM" update_envar=true
```

## 4.6. Run the Experiment

Run the experiment
```bash
jarvis pipeline run
```

## 4.7. Clean Data

To clean data produced by Hermes + ARLDM:
```bash
jarvis pipeline clean
```



# 5. ARLDM on Node Local Storage
For cluster that has node local storage, you can stagein data from shared storage, then run arldm.

## 5.1 Setup Environment
Currently setup DEFAULT input path in a shared storage, below is a example on Ares cluster using node local nvme.
```bash
RUN_SCRIPT=vistsis # can change to other datasets
EXPERIMENT_PATH=~/experiments/arldm_run # NFS
INPUT_PATH=$EXPERIMENT_PATH/input_data # NFS
cd $EXPERIMENT_PATH; export PRETRAIN_MODEL_PATH=`realpath model_large.pth`

LOCAL_EXPERIMENT_PATH=/mnt/nvme/$USER/arldm_run
LOCAL_INPUT_PATH=$LOCAL_EXPERIMENT_PATH/input_data
LOCAL_OUTPUT_PATH=$LOCAL_EXPERIMENT_PATH/output_data
```

## 5.2. Download Pretrain Model and Input Data
Same as above [download pretrain](#22-download-pretrain-model) and [download input](#23-download-input-data).

## 5.3. Create a Resource Graph
Same as [above](#23-create-a-resource-graph)

## 5.4. Create a Pipeline

The Jarvis pipeline will store all configuration data needed by ARLDM.

```bash
jarvis pipeline create arldm_local
```

## 5.5. Save Environment
Create the environment variables needed by ARLDM.
```bash
spack load hdf5@1.14.0+hl~mpi mpich@3.4.3
module load arldm
```


Store the current environment in the pipeline.
```bash
jarvis pipeline env build
```


## 5.6. Add pkgs to the Pipeline
Add data_stagein to pipeline before arldm.
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


Create a Jarvis pipeline with ARLDM.
```bash
jarvis pipeline append arldm runscript=$RUN_SCRIPT arldm_path="`scspkg pkg src arldm`/ARLDM" local_exp_dir=$LOCAL_EXPERIMENT_PATH
```

## 5.7. Run the Experiment

Run the experiment
```bash
jarvis pipeline run
```

## 5.8. Clean Data

To clean data produced by Hermes + ARLDM:
```bash
jarvis pipeline clean
```



# 6. ARLDM + Hermes on Node Local Storage
Every step the same as [ARLDM + Hermes](#4-arldm-with-hermes), except for when creating a Jarvis pipeline with Hermes, using the Hermes VFD interceptor:
- Example using `RUN_SCRIPT=vistsis` you need to stage in three different input directories.
```bash
# Setup env
RUN_SCRIPT=vistsis # can change to other datasets
EXPERIMENT_PATH=~/experiments/arldm_run # NFS
INPUT_PATH=$EXPERIMENT_PATH/input_data # NFS
cd $EXPERIMENT_PATH; export PRETRAIN_MODEL_PATH=`realpath model_large.pth`

LOCAL_EXPERIMENT_PATH=/mnt/nvme/$USER/arldm_run
LOCAL_INPUT_PATH=$LOCAL_EXPERIMENT_PATH/input_data
LOCAL_OUTPUT_PATH=$LOCAL_EXPERIMENT_PATH/output_data

# add pkg to pipeline
jarvis pipeline append data_stagein dest_data_path=$LOCAL_INPUT_PATH \
user_data_paths=$INPUT_PATH/vistdii,$INPUT_PATH/vistsis,$INPUT_PATH/visit_img,$PRETRAIN_MODEL_PATH \
mkdir_datapaths=$LOCAL_INPUT_PATH,$LOCAL_OUTPUT_PATH

jarvis pipeline append hermes_run --sleep=10 include=$LOCAL_EXPERIMENT_PATH

jarvis pipeline append hermes_api +vfd

jarvis pipeline append arldm runscript=vistsis arldm_path="`scspkg pkg src arldm`/ARLDM" update_envar=true
```


# 7. ARLDM + Hermes with Multinodes Slurm (TODO)
Multinodes ARLDM is not supported yet.

