#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates git \
        build-essential cmake \
        libleveldb-dev libhiredis-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# BIND_ROCKSDB omitted: Ubuntu's stock RocksDB is older than current YCSB-cpp
# requires. LevelDB + Redis give us a working benchmark without pinning to a
# specific RocksDB release.
git clone --depth 1 --recurse-submodules \
        https://github.com/ls4154/YCSB-cpp.git /opt/ycsb-cpp \
    && cd /opt/ycsb-cpp \
    && make -j$(nproc) BIND_LEVELDB=1 BIND_REDIS=1
