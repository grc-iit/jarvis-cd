#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# Filebench build deps (autoconf/bison/flex). util-linux supplies setarch,
# used at runtime to disable ASLR when sysctl isn't writable.
apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates git \
        build-essential autoconf automake libtool bison flex \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

# Filebench dropped out of Ubuntu 22.04 universe. Build from upstream GitHub.
git clone --depth 1 https://github.com/filebench/filebench.git /opt/filebench
cd /opt/filebench
libtoolize
aclocal
autoheader
automake --add-missing
autoconf
./configure --prefix=/opt/filebench/install
make -j$(nproc)
make install
