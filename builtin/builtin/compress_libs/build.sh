#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git cmake build-essential pkg-config \
    zlib1g-dev libbz2-dev liblzo2-dev libzstd-dev liblz4-dev liblzma-dev \
    libbrotli-dev libsnappy-dev libblosc2-dev libzfp-dev \
 && rm -rf /var/lib/apt/lists/*

# FPZIP
cd /tmp
rm -rf fpzip fpzip-build
git clone https://github.com/LLNL/fpzip.git
cmake -S fpzip -B fpzip-build \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local \
    -DBUILD_SHARED_LIBS=ON -DBUILD_TESTING=OFF -DBUILD_UTILITIES=OFF
make -C fpzip-build -j$(nproc)
make -C fpzip-build install
ldconfig
rm -rf /tmp/fpzip*

# SZ3
cd /tmp
rm -rf SZ3 sz3-build
git clone https://github.com/szcompressor/SZ3.git
cmake -S SZ3 -B sz3-build \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local \
    -DBUILD_SHARED_LIBS=ON -DBUILD_TESTING=OFF
make -C sz3-build -j$(nproc)
make -C sz3-build install
ldconfig
rm -rf /tmp/SZ3 /tmp/sz3-build

# std_compat
cd /tmp
rm -rf std_compat std_compat-build
git clone https://github.com/robertu94/std_compat.git
cmake -S std_compat -B std_compat-build \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local -DBUILD_TESTING=OFF
make -C std_compat-build -j$(nproc)
make -C std_compat-build install
ldconfig
rm -rf /tmp/std_compat*

# LibPressio (with ZFP, SZ3, FPZIP backends)
cd /tmp
rm -rf libpressio libpressio-build
git clone https://github.com/robertu94/libpressio.git
cmake -S libpressio -B libpressio-build \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local \
    -DLIBPRESSIO_HAS_ZFP=ON -DLIBPRESSIO_HAS_SZ3=ON -DLIBPRESSIO_HAS_FPZIP=ON \
    -DBUILD_SHARED_LIBS=ON -DBUILD_TESTING=OFF
make -C libpressio-build -j$(nproc)
make -C libpressio-build install
ldconfig
rm -rf /tmp/libpressio*
