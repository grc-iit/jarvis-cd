Hermes is a multi-tiered I/O buffering platform.

# Installation

```bash
spack install hermes@master
OR
spack install hermes_shm
spack load hermes_shm
git clone https://github.com/lukemartinlogan/hermes.git -b hermes-1.0 hermes-1.0
cd hermes
mkdir build
cd build
cmake ../
make -j8
cd ../

scspkg create hermes
scspkg env set hermes HERMES_PATH=${PWD}
scspkg env prepend hermes PATH ${PWD}/build/bin
scspkg env prepend hermes LD_LIBRARY_PATH ${PWD}/build/bin
module load hermes
```

# Hermes

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

The Jarvis pipeline will store all configuration data.
```bash
jarvis pipeline create hermes_test
```

## 3. Load Environment

Create the environment variables
```bash
spack load hermes@master
```````````

Store the current environment in the pipeline.
```bash
jarvis pipeline env build
```

## 4. Add pkgs to the Pipeline

Create a Jarvis pipeline
```bash
jarvis pipeline append hermes --sleep=5
```

## 5. Run Experiment

Run the experiment
```bash
jarvis pipeline run
```

## 6. Clean Data

Clean produced data
```bash
jarvis pipeline clean
```

# Hermes (+IOR)

To install IOR:
```
spack install ior
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

The Jarvis pipeline will store all configuration data.
```bash
jarvis pipeline create hermes_ior_test
```

## 3. Load Environment

Create the environment variables
```bash
spack load hermes@master ior
```````````

Store the current environment in the pipeline.
```bash
jarvis pipeline env build
```

## 4. Add pkgs to the Pipeline

Create a Jarvis pipeline
```bash
jarvis pipeline append hermes --sleep=5
jarvis pipeline append hermes_api +posix -mpi
jarvis pipeline append ior api=posix xfer=4k block=1m out=/tmp/test_hermes/ior.bin
```

## 5. Run Experiment

Run the experiment
```bash
jarvis pipeline run
```

## 6. Clean Data

Clean produced data
```bash
jarvis pipeline clean
```
