#!/bin/bash
set -e

CUDA_ARCH="${CUDA_ARCH:-##CUDA_ARCH##}"

# CMakeLists.txt and gray_scott.cu are staged in CWD from pkg_dir by jarvis.
mkdir -p /opt/gray_scott
cp CMakeLists.txt gray_scott.cu /opt/gray_scott/

# Build (expensive — cached when source and CMakeLists unchanged)
cd /opt/gray_scott \
    && cmake -S . -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCH} \
        -DHDF5_ROOT=/opt/hdf5 \
    && cmake --build build -j$(nproc)

export PATH=/opt/gray_scott/build:${PATH}
