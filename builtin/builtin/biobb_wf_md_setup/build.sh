#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# Plain ubuntu:24.04 is minimal — need curl+bzip2+ca-certificates to fetch
# miniforge and the 1AKI PDB. sci-hpc-base used to provide these.
apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl bzip2 \
    && rm -rf /var/lib/apt/lists/*

# Miniforge (conda-forge mirror) — Docker image inherits no conda otherwise.
curl -L -o /tmp/miniforge.sh \
        https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh \
    && bash /tmp/miniforge.sh -b -p /opt/conda \
    && rm /tmp/miniforge.sh \
    && /opt/conda/bin/conda config --set channel_priority strict

export PATH=/opt/conda/bin:${PATH}

# Conda env with Python 3.11 + GROMACS + biobb packages. gromacs=2026
# was the old bare-metal pin but not all conda-forge channels carry it;
# leave unpinned so conda picks the newest available. conda occasionally
# logs PackagesNotFoundError but exits 0, so verify afterwards.
conda create -p /opt/biobb-env -c conda-forge -c bioconda -y \
        python=3.11 gromacs
test -x /opt/biobb-env/bin/gmx \
    || { echo "ERROR: /opt/biobb-env did not install gromacs"; exit 1; }

/opt/conda/bin/conda run -p /opt/biobb-env \
    pip install --no-cache-dir \
        biobb_common biobb_model biobb_gromacs biobb_io
/opt/conda/bin/conda run -p /opt/biobb-env \
    python -c "import biobb_common, biobb_model, biobb_gromacs, biobb_io"
conda clean -afy

# Bundle a small benchmark (1AKI lysozyme PDB) for offline self-test.
mkdir -p /opt/biobb-bench \
    && curl -L -o /opt/biobb-bench/1AKI.pdb https://files.rcsb.org/download/1AKI.pdb

# Pipeline drivers (staged in CWD from pkg_dir by jarvis).
cp run_md_setup.py /opt/run_md_setup.py
cp run_batch.sh    /opt/run_batch.sh
chmod +x /opt/run_md_setup.py /opt/run_batch.sh

export PATH=/opt/biobb-env/bin:${PATH}
