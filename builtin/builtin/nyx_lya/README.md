Nyx is an adaptive mesh, massively-parallel, cosmological simulation code. 

# Installation

To compile the code we require C++11 compliant compilers that support MPI-2 or higher implementation. If threads or accelerators are used, we require OpenMP 4.5 or higher, Cuda 9 or higher, or HIP-Clang.

## 1. Install Dependencies


## 1. Install AMReX

```bash
git clone https://github.com/AMReX-Codes/amrex.git
pushd amrex
mkdir build
pushd build
cmake .. -DAMReX_HDF5=ON -DAMReX_PARTICLES=ON -DAMReX_PIC=ON -DBUILD_SHARED_LIBS=ON -DCMAKE_INSTALL_PREFIX=/path/to/amrex/install
make -j8
make install
popd
popd
```

## 2. Install Nyx

```bash
git clone https://github.com/AMReX-astro/Nyx.git
pushd Nyx
mkdir build
pushd build
cmake .. -DCMAKE_PREFIX_PATH=/path/to/amrex/install -DAMReX_DIR=/path/to/amrex/install/Tools/CMake/ -DNyx_SINGLE_PRECISION_PARTICLES=OFF -DNyx_OMP=OFF
make -j8
export NYX_PATH=`pwd`/Exec
popd
popd
```

# Nyx LyA

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

The Jarvis pipeline will store all configuration data needed by Nyx LyA.

```bash
jarvis pipeline create nyx-lya-test
```

## 4. Add pkgs to the Pipeline

Create a Jarvis pipeline with Nyx LyA
```bash
jarvis pipeline append nyx_lya --nyx_install_path=$NYX_PATH --initial_z=190.0 --final_z=180.0 --plot_z_values="188.0 186.0" --output=/path/to/output_files
```
**nyx_install_path**: this argument is required, otherwise it will report an error.
You can use the default arguments for other arguments. But it may take a while.

## 5. Run Experiment

Run the experiment
```bash
jarvis pipeline run
```

## 6. Clean Data

Clean data produced by Nyx LyA
```bash
jarvis pipeline clean
```