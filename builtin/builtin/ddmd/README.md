The Python FLEXible object TRacKeR (ARLDM) is a flexible atmospheric feature tracking software package.
See the [official repo](https://github.com/FlexTRKR/ARLDM) for more detail.

# Dependencies
- Prepare Conda
Get the miniconda3 installation script and run it
```
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh.sh
```
- HDF5 (1.14.0+)
- Require h5py



# Installation

1. Prepare script environment:
```bash
scspkg create ddmd
cd `scspkg pkg src ddmd`
git clone https://github.com/candiceT233/deepdrivemd_pnnl.git deepdrivemd
cd deepdrivemd
scspkg env set arldm DDMD_PATH="`pwd`" HDF5_USE_FILE_LOCKING=FALSE
# scspkg env prepend arldm PATH ${PATH}
```

2. Prepare conda environment:
```bash
export CONDA_OPENMM=hermes_openmm7_ddmd
export CONDA_PYTORCH=hm_ddmd_pytorch

cd "`scspkg pkg src ddmd`/deepdrivemd"
conda env create -f ddmd_openmm7.yaml --name=${CONDA_OPENMM}
conda env create -f ddmd_pytorch.yaml --name=${CONDA_PYTORCH}
```

Install appropriate packages in each conda environment:
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


# DDMD

## 1. Setup Environment

Create the environment variables needed by DDMD.
```bash
module load ddmd
spack load mpich
```


Setup example output data and path.
```bash
EXPERIMENT_PATH=~/experiments/ddmd_runs
mkdir -p $EXPERIMENT_PATH
```


Setup the experiment yaml file. 
- TODO: add `config_template.yaml` setup to pkg.py script
<!-- - Makesure to change the 3 environment variables accordingly. 
- Use absolute paths in the yaml file.
```yaml
clouddata_path: '${INPUT_PATH}' # TODO: Change this to your own path
root_path: '${OUTPUT_PATH}' # TODO: Change this to your own path
landmask_filename: '${INPUT_PATH}/wrf_landmask.nc' # TODO: Change this to your own path
``` -->



## 1. Create a Resource Graph

If you haven't already, create a resource graph. This only needs to be done
once throughout the lifetime of Jarvis. No need to repeat if you have already
done this for a different pipeline.

If you are running distributed tests, set path to the hostfile you are  using.
```bash
jarvis hostfile set /path/to/hostfile
```

Next, collect the resources from each of those pkgs. Walkthrough will give
a command line tutorial on how to build the hostfile.
```bash
jarvis resource-graph build +walkthrough
```

## 2. Create a Pipeline

The Jarvis pipeline will store all configuration data needed by ARLDM.

```bash
jarvis pipeline create arldm_test
module load arldm
```

## 3. Save Environment

Store the current environment in the pipeline.
```bash
jarvis pipeline env build
```

## 4. Add pkgs to the Pipeline


Create a Jarvis pipeline with ARLDM.
```bash
jarvis pipeline append arldm runtest=flintstones arldm_path="`scspkg pkg src arldm`/ARLDM"
```
runtest=
- pororo (~20G)
- flintstones (~15G)
- vistsis (~200MB)
- vistdii (~170MB)

<!-- ```bash
jarvis pipeline append arldm conda_env=arldm runscript=run_mcs_tbpfradar3d_wrf config=${HOME}/experiments/ARLDM/config_wrf_mcs_tbradar_demo.yml arldm_path="`scspkg pkg src arldm`/ARLDM"

jarvis pkg configure arldm conda_env=arldm runscript=run_mcs_tbpfradar3d_wrf config=${HOME}/experiments/ARLDM/config_wrf_mcs_tbradar_demo.yml

``` -->

## 5. Run Experiment

Run the experiment
```bash
jarvis pipeline run
```

## 6. Clean Data (TODO)

Clean data produced by ARLDM
```bash
jarvis pipeline clean
```

# ARLDM Withg Slurm (single node)

Do the above and
```bash
jarvis pipeline sbatch job_name=pyflex_test nnodes=1 ppn=8 output_file=./pyflex_test.out error_file=./pyflex_test.err
```

# ARLDM With Hermes

## 0. Dependencies
- Hermes must compile with HDF5


## 1. Setup Environment

Create the environment variables needed by Hermes + ARLDM
```bash
spack install hermes_shm
scspkg create hermes
cd `scspkg pkg src hermes`
git clone https://github.com/HDFGroup/hermes
cd hermes
mkdir build
cd build
cmake ../ -DCMAKE_BUILD_TYPE="Release" \
    -DCMAKE_INSTALL_PREFIX=`scspkg pkg root hermes` \
    -DHERMES_ENABLE_VFD="ON"

module load hermes
```

## 2. Create a Resource Graph

If you haven't already, create a resource graph. This only needs to be done
once throughout the lifetime of Jarvis. No need to repeat if you have already
done this for a different pipeline.

If you are running distributed tests, set path to the hostfile you are  using.
```bash
jarvis hostfile set /path/to/hostfile.txt
```

Next, collect the resources from each of those pkgs. Walkthrough will give
a command line tutorial on how to build the hostfile.
```bash
jarvis resource-graph build +walkthrough
```

## 3. Create a Pipeline

The Jarvis pipeline will store all configuration data needed by Hermes
and ARLDM.

```bash
jarvis pipeline create hermes_arldm_test
```

## 3. Save Environment

Store the current environment in the pipeline.
```bash
jarvis pipeline env build
```

## 4. Add pkgs to the Pipeline

Create a Jarvis pipeline with Hermes, the Hermes MPI-IO interceptor,
and gray-scott
```bash
jarvis pipeline append hermes --sleep=10 include=$EXPERIMENT_PATH --output_dir=$EXPERIMENT_PATH
jarvis pipeline append hermes_api +vfd
jarvis pipeline append arldm runscript=vistsis arldm_path="`scspkg pkg src arldm`/ARLDM"
```

## 5. Run the Experiment

Run the experiment
```bash
jarvis pipeline run
```

## 6. Clean Data (TODO)

To clean data produced by Hermes + Gray-Scott:
```bash
jarvis pipeline clean
```