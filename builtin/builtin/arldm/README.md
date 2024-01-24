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

Prepare script environment:
```bash
scspkg create arldm
cd `scspkg pkg src arldm`
git clone https://github.com/candiceT233/ARLDM
cd ARLDM
scspkg env set arldm ARLDM_PATH="`pwd`" HDF5_USE_FILE_LOCKING=FALSE
# scspkg env prepend arldm PATH ${PATH}
```

Prepare conda environment:
```bash
cd "`scspkg pkg src arldm`/ARLDM"
YOUR_HDF5_DIR="`which h5cc |sed 's/.\{9\}$//'`"
conda env create -f arldm_conda.yml
conda activate arldm
pip install -e .
HDF5_MPI="OFF" HDF5_DIR=${YOUR_HDF5_DIR} pip install --no-cache-dir --no-binary=h5py h5py
conda deactivate
```

# ARLDM

## 1. Setup Environment

Create the environment variables needed by ARLDM.
```bash
module load arldm
```
<!-- conda activate arldm -->

## Data Preparation

```shell
EXPERIMENT_PATH=~/experiments/ARLDM
EXPERIMENT_INPUT_PATH=$EXPERIMENT_PATH/input_data
mkdir -p $EXPERIMENT_INPUT_PATH $EXPERIMENT_INPUT_PATH/zippack

python3 -m pip install gdown
```

### pororo
* Download the PororoSV dataset [here](https://drive.google.com/file/d/11Io1_BufAayJ1BpdxxV2uJUvCcirbrNc/view?usp=sharing).
```shell
cd $EXPERIMENT_INPUT_PATH
gdown https://drive.google.com/u/0/uc?id=11Io1_BufAayJ1BpdxxV2uJUvCcirbrNc&export=download
unzip pororo.zip
mv pororo.zip $EXPERIMENT_INPUT_PATH/zippack
```


### flintstones
* Download the FlintstonesSV dataset [here](https://drive.google.com/file/d/1kG4esNwabJQPWqadSDaugrlF4dRaV33_/view?usp=sharing).
```shell
cd $EXPERIMENT_INPUT_PATH
gdown https://drive.google.com/u/0/uc?id=1kG4esNwabJQPWqadSDaugrlF4dRaV33_&export=download
unzip flintstones_data.zip
mv flintstones_data.zip $EXPERIMENT_INPUT_PATH/zippack
```


### VIST
* Download the VIST-SIS url links [here](https://visionandlanguage.net/VIST/json_files/story-in-sequence/SIS-with-labels.tar.gz)
```shell
cd $EXPERIMENT_INPUT_PATH
wget https://visionandlanguage.net/VIST/json_files/story-in-sequence/SIS-with-labels.tar.gz
tar -vxf SIS-with-labels.tar.gz
mv SIS-with-labels.tar.gz $EXPERIMENT_INPUT_PATH/zippack
```
* Download the VIST-DII url links [here](https://visionandlanguage.net/VIST/json_files/description-in-isolation/DII-with-labels.tar.gz)
```shell
cd $EXPERIMENT_INPUT_PATH
wget https://visionandlanguage.net/VIST/json_files/description-in-isolation/DII-with-labels.tar.gz
tar -vxf DII-with-labels.tar.gz
mv DII-with-labels.tar.gz $EXPERIMENT_INPUT_PATH/zippack
```
* Download the VIST images running
```shell
python data_script/vist_img_download.py --json_dir $EXPERIMENT_PATH/input_data/dii --img_dir $EXPERIMENT_PATH/input_data/visit_img --num_process 32
```


Setup example output data and path.
```bash
OUTPUT_PATH=$EXPERIMENT_PATH/output_data
mkdir -p $OUTPUT_PATH
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

# ARLDM Withg Slurm

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
jarvis pipeline append arldm runscript=run_mcs_tbpfradar3d_wrf config=$YAML_PATH arldm_path="`scspkg pkg src arldm`/ARLDM"
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