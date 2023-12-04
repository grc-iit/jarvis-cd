The Python FLEXible object TRacKeR (PyFLEXTRKR) is a flexible atmospheric feature tracking software package.
See the [official repo](https://github.com/FlexTRKR/PyFLEXTRKR) for more detail.

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

Prepare script environment:
```bash
scspkg create pyflextrkr
cd `scspkg pkg src pyflextrkr`
git clone https://github.com/candiceT233/PyFLEXTRKR
cd PyFLEXTRKR
export PYFLEXTRKR_PATH=`pwd`
scspkg env set pyflextrkr PYFLEXTRKR_PATH="${PYFLEXTRKR_PATH}"
```




Prepare conda environment:
```bash
YOUR_HDF5_DIR="`which h5cc |sed 's/.\{9\}$//'`"
conda env create -f environment.yml
conda activate flextrkr
pip install -e .
HDF5_MPI="OFF" HDF5_DIR=${YOUR_HDF5_DIR} pip install --no-cache-dir --no-binary=h5py h5py
pip install xarray[io]
conda deactivate
```

<!-- ```bash
# gray-scott example
scspkg create gray-scott
cd `scspkg pkg src gray-scott`
git clone https://github.com/pnorbert/adiosvm
cd adiosvm/Tutorial/gs-mpiio
mkdir build
pushd build
cmake ../ -DCMAKE_BUILD_TYPE=Release
make -j8
export GRAY_SCOTT_PATH=`pwd`
scspkg env set gray_scott GRAY_SCOTT_PATH="${GRAY_SCOTT_PATH}"
scspkg env prepend gray_scott PATH "${GRAY_SCOTT_PATH}"
module load gray_scott
spack load mpi adios2
``` -->

# Pyflextrkrt

## 1. Setup Environment

Create the environment variables needed by Pyflextrkr.
```bash
module load pyflextrkr
```
<!-- conda activate flextrkr -->



Setup example input data and path.
```bash
INPUT_PATH=~/experiments/flextrkr_run/input_data/wrf_tbradar
mkdir -p $INPUT_PATH
wget https://portal.nersc.gov/project/m1867/PyFLEXTRKR/sample_data/tb_radar/wrf_tbradar.tar.gz -O ${INPUT_PATH}/wrf_tbradar.tar.gz

tar -xvzf ${INPUT_PATH}wrf_tbradar.tar.gz -C ${INPUT_PATH}
# Remove downloaded tar file
rm -fv ${INPUT_PATH}wrf_tbradar.tar.gz
```


Setup example output data and path.
```bash
OUTPUT_PAT=~/experiments/flextrkr_run/wrf_tbradar
mkdir -p $OUTPUT_PAT
```


Setup the experiment yaml file. 
- Makesure to change the 3 environment variables accordingly. 
- Use absolute paths in the yaml file.
```yaml
clouddata_path: '${INPUT_PATH}' # TODO: Change this to your own path
root_path: '${OUTPUT_PAT}' # TODO: Change this to your own path
landmask_filename: '${INPUT_PATH}/wrf_landmask.nc' # TODO: Change this to your own path
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

Store the current environment in the pipeline.
```bash
jarvis pipeline env build
```

## 4. Add pkgs to the Pipeline
** Currently only support running runscript=run_mcs_tbpfradar3d_wrf **



Create a Jarvis pipeline with Pyflextrkr.
```bash
jarvis pipeline append pyflextrkr runscript=run_mcs_tbpfradar3d_wrf config=/path_to_experiment_config/config.yml
```

<!-- ```bash
jarvis pipeline append pyflextrkr conda_env=flextrkr runscript=run_mcs_tbpfradar3d_wrf config=${HOME}/experiments/flextrkr_runs/config_wrf_mcs_tbradar_demo.yml

jarvis pkg configure pyflextrkr conda_env=flextrkr runscript=run_mcs_tbpfradar3d_wrf config=${HOME}/experiments/flextrkr_runs/config_wrf_mcs_tbradar_demo.yml

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

Do the above and
```bash
jarvis pipeline sbatch job_name=pyflex_test nnodes=1 ppn=8 output_file=./pyflex_test.out error_file=./pyflex_test.err
```

# Pyflextrkr With Hermes

## 0. Dependencies
- Hermes must compile with HDF5


## 1. Setup Environment

Create the environment variables needed by Hermes + Pyflextrkr
```bash
# On personal
spack install hermes@master adios2
spack load hermes adios2
# On Ares
module load hermes/master-feow7up adios2/2.9.0-mmkelnu
# export GRAY_SCOTT_PATH=${HOME}/adiosvm/Tutorial/gs-mpiio/build
export PATH="${GRAY_SCOTT_PATH}:$PATH"
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
jarvis pipeline create gs-hermes
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
jarvis pipeline append hermes --sleep=10 --output_dir=${HOME}/gray-scott
jarvis pipeline append hermes_api +mpi
jarvis pipeline append gray_scott
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
