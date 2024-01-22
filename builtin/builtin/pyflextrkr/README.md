The Python FLEXible object TRacKeR (PyFLEXTRKR) is a flexible atmospheric feature tracking software package.
See the [official repo](https://github.com/FlexTRKR/PyFLEXTRKR) for more detail.

# Dependencies
- Prepare Conda
Get the miniconda3 installation script and run it
```
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh.sh
```
- HDF5 (1.14.0+), require by h5py
- MPI
    - Either OpenMPI or MPICH
    - Required by mpi4py

# Installation

Prepare script environment:
```bash
scspkg create pyflextrkr
cd `scspkg pkg src pyflextrkr`
git clone https://github.com/candiceT233/PyFLEXTRKR
cd PyFLEXTRKR
scspkg env set pyflextrkr PYFLEXTRKR_PATH="`pwd`"
```




Prepare conda environment in scspkg:
```bash
cd `scspkg pkg src pyflextrkr`/PyFLEXTRKR
git switch ares

YOUR_HDF5_DIR="`which h5cc |sed 's/.\{9\}$//'`"

conda env create -f ares_flextrkr.yml
conda activate flextrkr
pip install -e .
HDF5_MPI="OFF" HDF5_DIR=${YOUR_HDF5_DIR} pip install --no-cache-dir --no-binary=h5py h5py
pip install xarray[io] mpi4py
conda deactivate
```


# Pyflextrkrt

## 1. Setup Environment



Setup example input data `wrf_tbradar.tar.gz` and path.
```bash
EXPERIMENT_PATH=~/experiments/flextrkr_run
INPUT_PATH=$EXPERIMENT_PATH/input_data/run_mcs_tbpfradar3d_wrf
mkdir -p $INPUT_PATH
wget https://portal.nersc.gov/project/m1867/PyFLEXTRKR/sample_data/tb_radar/wrf_tbradar.tar.gz -O ${INPUT_PATH}/run_mcs_tbpfradar3d_wrf.tar.gz

tar -xvzf ${INPUT_PATH}/run_mcs_tbpfradar3d_wrf.tar.gz -C ${INPUT_PATH}

# Remove downloaded tar file
rm -fv ${INPUT_PATH}/run_mcs_tbpfradar3d_wrf.tar.gz
```


Setup example output data and path.
```bash
OUTPUT_PATH=$EXPERIMENT_PATH/wrf_tbradar
mkdir -p $OUTPUT_PATH
```


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

The Jarvis pipeline will store all configuration data needed by Pyflextrkr.

```bash
jarvis pipeline create pyflextrkr_test
```

## 3. Save Environment
Create the environment variables needed by Pyflextrkr.
```bash
module load pyflextrkr
```
<!-- conda activate flextrkr -->

Store the current environment in the pipeline.
```bash
jarvis pipeline env build
```

## 4. Add pkgs to the Pipeline
** Currently only support running runscript=run_mcs_tbpfradar3d_wrf **



Create a Jarvis pipeline with Pyflextrkr.
```bash
jarvis pipeline append pyflextrkr runscript=run_mcs_tbpfradar3d_wrf pyflextrkr_path="`scspkg pkg src pyflextrkr`/PyFLEXTRKR"
```

<!-- ```bash
jarvis pipeline append pyflextrkr conda_env=flextrkr runscript=run_mcs_tbpfradar3d_wrf pyflextrkr_path="`scspkg pkg src pyflextrkr`/PyFLEXTRKR"

jarvis pkg configure pyflextrkr conda_env=flextrkr runscript=run_mcs_tbpfradar3d_wrf config=${HOME}/jarvis-cd/builtin/builtin/pyflextrkr/example_config/run_mcs_tbpfradar3d_wrf_template.yml
``` -->

## 5. Run Experiment

Run the experiment
```bash
jarvis pipeline run
```

## 6. Clean Data (TODO)

Clean data produced by Pyflextrkr
```bash
jarvis pipeline clean
```

# Pyflextrkr Withg Slurm

## Local Cluster
Do the above and `ppn` must match `nprocesses`
```bash
jarvis pipeline sbatch job_name=pyflex_test nnodes=1 ppn=8 output_file=./pyflex_test.out error_file=./pyflex_test.err
```

## Multi Nodes Cluster
Do the above and `ppn` must greater than match `nprocesses`/`nnodes` 
    (e.g. `nnodes=2 ppn=8` allocates 16 processes in total, and `nprocesses` must not greater than 16)
```bash
jarvis pipeline sbatch job_name=pyflex_2ntest nnodes=2 ppn=8 output_file=./pyflex_2ntest.out error_file=./pyflex_2ntest.err
```


# Pyflextrkr With Hermes

## 0. Dependencies
- Hermes must compile with HDF5


## 1. Setup Environment

Create the environment variables needed by Hermes + Pyflextrkr
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
and Pyflextrkr.

```bash
jarvis pipeline create hermes_pyflextrkr_test
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
jarvis pipeline append pyflextrkr runscript=run_mcs_tbpfradar3d_wrf config=$YAML_PATH pyflextrkr_path="`scspkg pkg src pyflextrkr`/PyFLEXTRKR"
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

# TODO
add pyflextrkr with mpirun to support multi-nodes