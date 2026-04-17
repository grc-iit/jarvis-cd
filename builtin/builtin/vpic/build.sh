#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git cmake build-essential \
    openmpi-bin libopenmpi-dev \
    && rm -rf /var/lib/apt/lists/*

# Clone VPIC-Kokkos with bundled Kokkos (cached until repo changes)
git clone --recursive https://github.com/lanl/vpic-kokkos.git /opt/vpic-kokkos

# Build VPIC core library
cd /opt/vpic-kokkos \
    && cmake -S . -B build \
        -DCMAKE_BUILD_TYPE=Release \
        ##CMAKE_FLAGS## \
    && cmake --build build -j$(nproc)

##POST_BUILD##export PATH=/opt/vpic-kokkos/build/bin:${PATH}
