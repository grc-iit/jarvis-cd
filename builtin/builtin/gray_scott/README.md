Gray-Scott is a 3D 7-Point stencil code

# Installation

```bash
git clone https://github.com/pnorbert/adiosvm
cd adiosvm/Tutorial/gs-mpiio
mkdir build
cd build
cmake ../ -DCMAKE_BUILD_TYPE=Release
```

# Gray Scott

## 1. Create a Resource Graph

## 2. Create a Pipeline

## 3. Load Environment

## 4. Add Nodes to the Pipeline

## 5. Run Experiment

## 6. Clean Data

# Gray Scott With Hermes

## 1. Create a Resource Graph

If you haven't already, create a resource graph. This only needs to be done
once throughout the lifetime of Jarvis. No need to repeat if you have already
done this for a different pipeline.

If you are running distributed tests, set path to the hostfile you are  using.
```bash
jarvis hostfile set 
```

Next, collect the resources from each of those nodes. Walkthrough will give
a command line tutorial on how to build the hostfile.
```bash
jarvis resource-graph build +walkthrough
```

## 2. Create a Pipeline

The Jarvis pipeline will store all configuration data needed by Hermes
and Gray Scott.

```bash
jarvis pipeline create gs-hermes
```

## 3. Load Environment

Create the environment variables needed by Hermes + Gray Scott
```bash
spack load --only dependencies hermes
ADIOSVM_PATH=/path/to/adiosvm
GRAY_SCOTT_PATH=${ADIOSVM_PATH}/Tutorial/gs-mpiio
export PATH="${GRAY_SCOTT_PATH}:$PATH"
```

Store the current environment in the pipeline.
```bash
jarvis build env
```

## 4. Add Nodes to the Pipeline

Create a Jarvis pipeline with Hermes, the Hermes MPI-IO interceptor,
and gray-scott
```bash
jarvis append hermes
jarvis append hermes_mpiio
jarvis append gray-scott
```

## 5. Run the Experiment

Run the experiment
```
jarvis run
```

## 6. Clean Data

To clean data produced by Hermes + Gray-Scott:
```bash
jarvis clean
```
