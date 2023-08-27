LabStor is a distributed semi-microkernel for building data processing services.

# Installation

```bash
spack install labstor@master
```

OR

```bash 
spack install mochi-thallium~cereal@0.10.1 cereal catch2@3.0.1 mpich \
yaml-cpp boost hermes_shm
spack load mochi-thallium~cereal@0.10.1 cereal catch2@3.0.1 mpich \
yaml-cpp boost hermes_shm
git clone https://github.com/lukemartinlogan/labstor.git
cd labstor
mkdir build
cd build
cmake ../
make -j8
popd

scspkg create labstor
scspkg prepend-env labstor LABSTOR_PATH ${PWD}
scspkg prepend-env labstor PATH ${PWD}/build/bin
scspkg prepend-env labstor LD_LIBRARY_PATH ${PWD}/build/bin
module load labstor
```

# Labstor

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
jarvis pipeline create labstor-test
```

## 3. Load Environment

Create the environment variables
```bash
spack load labstor
```````````

Store the current environment in the pipeline.
```bash
jarvis pipeline env build
```

## 4. Add pkgs to the Pipeline

Create a Jarvis pipeline
```bash
jarvis pipeline append labstor --sleep=10
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