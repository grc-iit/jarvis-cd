The Python FLEXible object TRacKeR (PyFLEXTRKR) is a flexible atmospheric feature tracking software package.
See the [official repo](https://github.com/FlexTRKR/PyFLEXTRKR) for more detail.

# Table of Content
0. [Dependencies](#0-dependencies)
1. [Installation](#1-installation)
2. [Running Pyflextrkr](#2-running-pyflextrkr)
3. [Pyflextrkr with Slurm](#3-pyflextrkr-with-slurm)
4. [Pyflextrkr + Hermes](#4-pyflextrkr--hermes)
5. [Pyflextrkr on Node Local Storage](#5-pyflextrkr-on-node-local-storage)
6. [Pyflextrkr + Hermes on Node Local Storage](#6-pyflextrkr--hermes-on-node-local-storage)
7. [Pyflextrkr + Hermes with Multinodes Slurm (TODO)](#7-pyflextrkr--hermes-on-multinodes-slurm-todo)



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
You need `wget` to download the datasets online:
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



# 1. Installation

## 1.1 create pyflextrkr scs package
```bash
scspkg create pyflextrkr
cd `scspkg pkg src pyflextrkr`
git clone https://github.com/candiceT233/PyFLEXTRKR
cd PyFLEXTRKR
git switch ares # User the ares branch
scspkg env set pyflextrkr PYFLEXTRKR_PATH="`pwd`"
```


## 1.2 Prepare conda environment and python packages:
```bash
cd `scspkg pkg src pyflextrkr`/PyFLEXTRKR

YOUR_HDF5_DIR="`which h5cc |sed 's/.\{9\}$//'`"

conda env create -f ares_flextrkr.yml -n flextrkr
conda activate flextrkr
pip install -e .
HDF5_MPI="OFF" HDF5_DIR=${YOUR_HDF5_DIR} pip install --no-cache-dir --no-binary=h5py h5py==3.8.0
pip install xarray[io] mpi4py
conda deactivate
```



# 2. Running Pyflextrkr
Example is using `TEST_NAME=run_mcs_tbpfradar3d_wrf` (only supported with dataset download).
## 2.1. Setup Environment
Currently setup input path in a shared storage, below is a example on Ares cluster.
```bash
TEST_NAME=run_mcs_tbpfradar3d_wrf
EXPERIMENT_PATH=~/experiments/pyflex_run # NFS

export EXPERIMENT_INPUT_PATH=$EXPERIMENT_PATH/input_data # NFS
scspkg env set pyflextrkr EXPERIMENT_INPUT_PATH=$EXPERIMENT_INPUT_PATH
mkdir -p $EXPERIMENT_INPUT_PATH
```

## 2.2. Download Input Data
Setup example input data `wrf_tbradar.tar.gz` and path.to `run_mcs_tbpfradar3d_wrf.tar.gz`
```bash
cd $EXPERIMENT_INPUT_PATH
wget https://portal.nersc.gov/project/m1867/PyFLEXTRKR/sample_data/tb_radar/wrf_tbradar.tar.gz -O $TEST_NAME.tar.gz
mkdir $TEST_NAME
tar -xvzf $TEST_NAME.tar.gz -C $TEST_NAME

# Remove downloaded tar file
rm -rf $EXPERIMENT_INPUT_PATH/$TEST_NAME.tar.gz
```


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

The Jarvis pipeline will store all configuration data needed by Pyflextrkr.

```bash
jarvis pipeline create pyflextrkr_test
```

## 2.5. Save Environment
Create the environment variables needed by Pyflextrkr.
```bash
spack load hdf5@1.14.0+hl~mpi mpich@3.4.3
module load pyflextrkr
```
<!-- conda activate flextrkr -->

Store the current environment in the pipeline.
```bash
jarvis env build pyflextr +PYFLEXTRKR_PATH +EXPERIMENT_INPUT_PATH
jarvis pipeline env copy pyflextr
```

## 2.6. Add pkgs to the Pipeline
** Currently only support running `runscript=run_mcs_tbpfradar3d_wrf` **

Create a Jarvis pipeline with Pyflextrkr.
```bash
jarvis pipeline append pyflextrkr runscript=run_mcs_tbpfradar3d_wrf
```


## 2.7. Run Experiment

Run the experiment, output are generated in `$EXPERIMENT_INPUT_PATH/output_data`.
```bash
jarvis pipeline run
```

## 2.8. Clean Data

Clean data produced by Pyflextrkr
```bash
jarvis pipeline clean
```



# 3. Pyflextrkr With Slurm

## 3.1 Local Cluster
Do the above and `ppn` must match `nprocesses`
```bash
jarvis pipeline sbatch job_name=pyflex_test nnodes=1 ppn=8 output_file=./pyflex_test.out error_file=./pyflex_test.err
```

## 3.2 Multi Nodes Cluster
Do the above and `ppn` must greater than match `nprocesses`/`nnodes` 
    (e.g. `nnodes=2 ppn=8` allocates 16 processes in total, and `nprocesses` must not greater than 16)

Configure Pyflextrkr to parallel run mode with MPI-Dask (0: serial, 1: Dask one-node cluster, 2: Dask multinode cluster)
```bash
jarvis pkg configure pyflextrkr run_parallel=2 nprocesses=8
```

```bash
jarvis pipeline sbatch job_name=pyflex_2ntest nnodes=2 ppn=4 output_file=./pyflex_2ntest.out error_file=./pyflex_2ntest.err
```



# 4. Pyflextrkr + Hermes

## 4.0. Dependencies
### 4.0.1 HDF5
Hermes must compile with HDF5, makesure [download HDF5-1.14.0 with spack](#4-hdf5-1140).

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

Create the environment variables needed by Hermes + Pyflextrkr
```bash
spack load hermes_shm
module load hermes pyflextrkr
```

## 4.2. Create a Resource Graph

Same as [above](#2-create-a-resource-graph).

## 4.3. Create a Pipeline

The Jarvis pipeline will store all configuration data needed by Hermes
and Pyflextrkr.

```bash
jarvis pipeline create hermes_pyflextrkr_test
```

## 4.4. Save Environment

Store the current environment in the pipeline.
```bash
jarvis pipeline env build +PYFLEXTRKR_PATH
```

## 4.5. Add pkgs to the Pipeline

Create a Jarvis pipeline with Hermes, the Hermes VFD interceptor,
and pyflextrkr (must use `flush_mode=sync` to prevent [this error](#oserror-log))
```bash
jarvis pipeline append hermes_run --sleep=10 include=$EXPERIMENT_PATH flush_mode=sync
jarvis pipeline append hermes_api +vfd
jarvis pipeline append pyflextrkr runscript=$TEST_NAME update_envar=true
```

## 4.6. Run the Experiment

Run the experiment, output are generated in `$EXPERIMENT_INPUT_PATH/output_data`.
```bash
jarvis pipeline run
```

## 4.7. Clean Data

To clean data produced by Hermes + Pyflextrkr:
```bash
jarvis pipeline clean
```



# 5. Pyflextrkr on Node Local Storage
For cluster that has node local storage, you can stagein data from shared storage, then run pyflextrkr.

## 5.1 Setup Environment
Currently setup DEFAULT input path in a shared storage, below is a example on Ares cluster using node local nvme.


The shared storage path is same as before:
```bash
export TEST_NAME=run_mcs_tbpfradar3d_wrf
EXPERIMENT_PATH=~/experiments/pyflex_run # NFS
EXPERIMENT_INPUT_PATH=$EXPERIMENT_PATH/input_data # NFS
mkdir -p $EXPERIMENT_INPUT_PATH
```
Setup the node local experiment paths:
```bash
LOCAL_EXPERIMENT_PATH=/mnt/nvme/$USER/pyflex_run
LOCAL_INPUT_PATH=$LOCAL_EXPERIMENT_PATH/input_data
```

## 5.2. Download Input Data
Same as [above](#22-download-input-data).

## 5.3. Create a Resource Graph
Same as [above](#23-create-a-resource-graph)

## 5.4. Create a Pipeline

The Jarvis pipeline will store all configuration data needed by Pyflextrkr.

```bash
jarvis pipeline create pyflextrkr_local
```

## 5.5. Save Environment
Create the environment variables needed by Pyflextrkr.
```bash
spack load hdf5@1.14.0+hl~mpi mpich@3.4.3
module load pyflextrkr
```


Store the current environment in the pipeline.
```bash
jarvis pipeline env build +PYFLEXTRKR_PATH
```

## 5.6. Add pkgs to the Pipeline
Add data_stagein to pipeline before pyflextrkr.
```bash 
jarvis pipeline append data_stagein dest_data_path=$LOCAL_INPUT_PATH \
user_data_paths=$EXPERIMENT_INPUT_PATH/$TEST_NAME \
mkdir_datapaths=$LOCAL_INPUT_PATH
```

Create a Jarvis pipeline with Pyflextrkr.
```bash
jarvis pipeline append pyflextrkr runscript=$TEST_NAME local_exp_dir=$LOCAL_INPUT_PATH
```

## 5.7. Run the Experiment

Run the experiment, output are generated in `$LOCAL_INPUT_PATH/output_data`.
```bash
jarvis pipeline run
```

## 5.8. Clean Data

To clean data produced by Hermes + Pyflextrkr:
```bash
jarvis pipeline clean
```



# 6. Pyflextrkr + Hermes on Node Local Storage
Every step the same as [Pyflextrkr + Hermes](#4-pyflextrkr--hermes), except for when creating a Jarvis pipeline with Hermes, using the Hermes VFD interceptor:
```bash
# Setup env
TEST_NAME=run_mcs_tbpfradar3d_wrf
EXPERIMENT_PATH=~/experiments/pyflex_run # NFS
INPUT_PATH=$EXPERIMENT_PATH/input_data # NFS
mkdir -p $EXPERIMENT_INPUT_PATH

LOCAL_EXPERIMENT_PATH=/mnt/nvme/$USER/pyflex_run
LOCAL_INPUT_PATH=$LOCAL_EXPERIMENT_PATH/input_data

# add pkg to pipeline
jarvis pipeline append data_stagein dest_data_path=$LOCAL_INPUT_PATH \
user_data_paths=$EXPERIMENT_INPUT_PATH/$TEST_NAME \
mkdir_datapaths=$LOCAL_INPUT_PATH

jarvis pipeline append hermes_run --sleep=10 include=$LOCAL_EXPERIMENT_PATH

jarvis pipeline append hermes_api +vfd

jarvis pipeline append pyflextrkr runscript=$TEST_NAME update_envar=true local_exp_dir=$LOCAL_INPUT_PATH
```



# 7. Pyflextrkr + Hermes on Multinodes Slurm (TODO)
Steps are the same as [Pyflextrkr with Slurm](#3-pyflextrkr-With-slurm), but not working yet due to OSError.

## OSError Log
```log
2024-01-03 18:49:03,523 - pyflextrkr.idfeature_driver - INFO - Identifying features from raw data
2024-01-03 18:49:04,969 - pyflextrkr.idfeature_driver - INFO - Total number of files to process: 17
free(): invalid size
2024-01-03 18:49:07,944 - distributed.worker - WARNING - Compute Failed
Key:       idclouds_tbpf-0bb7839d-ac6e-4e51-b309-ec2bc6667641
Function:  execute_task
args:      ((<function idclouds_tbpf at 0x7fce75d6da80>, '/home/mtang11/experiments/pyflex_runs/input_data/run_mcs_tbpfradar3d_wrf/wrfout_rainrate_tb_zh_mh_2015-05-06_04:00:00.nc', (<class 'dict'>, [['ReflThresh_lowlevel_gap', 20.0], ['abs_ConvThres_aml', 45.0], ['absolutetb_threshs', [160, 330]], ['area_thresh', 36], ['background_Box', 12.0], ['clouddata_path', '/home/mtang11/experiments/pyflex_runs/input_data/run_mcs_tbpfradar3d_wrf/'], ['clouddatasource', 'model'], ['cloudidmethod', 'label_grow'], ['cloudtb_cloud', 261.0], ['cloudtb_cold', 241.0], ['cloudtb_core', 225.0], ['cloudtb_warm', 261.0], ['col_peakedness_frac', 0.3], ['dask_tmp_dir', '/tmp/pyflextrkr_test'], ['databasename', 'wrfout_rainrate_tb_zh_mh_'], ['datatimeresolution', 1.0], ['dbz_lowlevel_asl', 2.0], ['dbz_thresh', 10], ['duration_range', [2, 300]], ['echotop_gap', 1], ['enddate', '20150506.1600'], ['etop25dBZ_Thresh', 10.0], ['feature_type', 'tb_pf_radar3d'], ['feature_varname', 'feature_number'], ['featuresize_varname',
kwargs:    {}
Exception: "OSError('Unable to synchronously open file (file signature not found)')"

Traceback (most recent call last):
  File "/mnt/common/mtang11/scripts/scspkg/packages/pyflextrkr/src/PyFLEXTRKR/runscripts/run_mcs_tbpfradar3d_wrf.py", line 115, in <module>
    idfeature_driver(config)
  File "/mnt/common/mtang11/scripts/scspkg/packages/pyflextrkr/src/PyFLEXTRKR/pyflextrkr/idfeature_driver.py", line 66, in idfeature_driver
    final_result = dask.compute(*results)
                   ^^^^^^^^^^^^^^^^^^^^^^
  File "/home/mtang11/miniconda3/envs/flextrkr/lib/python3.11/site-packages/dask/base.py", line 595, in compute
    results = schedule(dsk, keys, **kwargs)
              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/mtang11/miniconda3/envs/flextrkr/lib/python3.11/site-packages/distributed/client.py", line 3243, in get
    results = self.gather(packed, asynchronous=asynchronous, direct=direct)
              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/mtang11/miniconda3/envs/flextrkr/lib/python3.11/site-packages/distributed/client.py", line 2368, in gather
    return self.sync(
           ^^^^^^^^^^
  File "/home/mtang11/miniconda3/envs/flextrkr/lib/python3.11/site-packages/distributed/utils.py", line 351, in sync
    return sync(
           ^^^^^
  File "/home/mtang11/miniconda3/envs/flextrkr/lib/python3.11/site-packages/distributed/utils.py", line 418, in sync
    raise exc.with_traceback(tb)
  File "/home/mtang11/miniconda3/envs/flextrkr/lib/python3.11/site-packages/distributed/utils.py", line 391, in f
    result = yield future
             ^^^^^^^^^^^^
  File "/home/mtang11/miniconda3/envs/flextrkr/lib/python3.11/site-packages/tornado/gen.py", line 767, in run
    value = future.result()
            ^^^^^^^^^^^^^^^
  File "/home/mtang11/miniconda3/envs/flextrkr/lib/python3.11/site-packages/distributed/client.py", line 2231, in _gather
    raise exception.with_traceback(traceback)
  File "/mnt/common/mtang11/scripts/scspkg/packages/pyflextrkr/src/PyFLEXTRKR/pyflextrkr/idclouds_tbpf.py", line 98, in idclouds_tbpf
    rawdata = xr.open_dataset(filename, engine='h5netcdf') # , engine='h5netcdf' netcdf4 h5netcdf
      ^^^^^^^^^^^^^^^^^
  File "/home/mtang11/miniconda3/envs/flextrkr/lib/python3.11/site-packages/xarray/backends/api.py", line 566, in open_dataset
    backend_ds = backend.open_dataset(
      ^^^^^^^^^^^^^^^^^
  File "/home/mtang11/miniconda3/envs/flextrkr/lib/python3.11/site-packages/xarray/backends/h5netcdf_.py", line 413, in open_dataset
    store = H5NetCDFStore.open(
  ^^^^^^^^^^^^^^^^^
  File "/home/mtang11/miniconda3/envs/flextrkr/lib/python3.11/site-packages/xarray/backends/h5netcdf_.py", line 176, in open
    return cls(manager, group=group, mode=mode, lock=lock, autoclose=autoclose)
  ^^^^^^^^^^^^^^^^^
  File "/home/mtang11/miniconda3/envs/flextrkr/lib/python3.11/site-packages/xarray/backends/h5netcdf_.py", line 127, in __init__
    self._filename = find_root_and_group(self.ds)[0].filename
  ^^^^^^^^^^^^^^^^^
  File "/home/mtang11/miniconda3/envs/flextrkr/lib/python3.11/site-packages/xarray/backends/h5netcdf_.py", line 187, in ds
    return self._acquire()
  ^^^^^^^^^^^^^^^^^
  File "/home/mtang11/miniconda3/envs/flextrkr/lib/python3.11/site-packages/xarray/backends/h5netcdf_.py", line 179, in _acquire
    with self._manager.acquire_context(needs_lock) as root:
  ^^^^^^^^^^^^^^^^^
  File "/home/mtang11/miniconda3/envs/flextrkr/lib/python3.11/contextlib.py", line 137, in __enter__
    return next(self.gen)
^^^^^^^^^^^^^^^
  File "/home/mtang11/miniconda3/envs/flextrkr/lib/python3.11/site-packages/xarray/backends/file_manager.py", line 198, in acquire_context
    file, cached = self._acquire_with_cache_info(needs_lock)
  ^^^^^^^^^^^^^^^^^
  File "/home/mtang11/miniconda3/envs/flextrkr/lib/python3.11/site-packages/xarray/backends/file_manager.py", line 216, in _acquire_with_cache_info
    file = self._opener(*self._args, **kwargs)
^^^^^^^^^^^
  File "/home/mtang11/miniconda3/envs/flextrkr/lib/python3.11/site-packages/h5netcdf/core.py", line 1051, in __init__
    self._h5file = self._h5py.File(
^^^^^^^
  File "/home/mtang11/miniconda3/envs/flextrkr/lib/python3.11/site-packages/h5py/_hl/files.py", line 567, in __init__
    fid = make_fid(name, mode, userblock_size, fapl, fcpl, swmr=swmr)
^^^^^^^^^^^
  File "/home/mtang11/miniconda3/envs/flextrkr/lib/python3.11/site-packages/h5py/_hl/files.py", line 231, in make_fid
    fid = h5f.open(name, flags, fapl=fapl)
  ^^^^^^^^^^^^^^^^^
  File "h5py/_objects.pyx", line 54, in h5py._objects.with_phil.wrapper
  File "h5py/_objects.pyx", line 55, in h5py._objects.with_phil.wrapper
  File "h5py/h5f.pyx", line 106, in h5py.h5f.open
OSError: Unable to synchronously open file (file signature not found)
```



