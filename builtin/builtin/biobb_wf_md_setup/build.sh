#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# Miniforge (conda-forge mirror) — Docker image inherits no conda otherwise.
curl -L -o /tmp/miniforge.sh \
        https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh \
    && bash /tmp/miniforge.sh -b -p /opt/conda \
    && rm /tmp/miniforge.sh \
    && /opt/conda/bin/conda config --set channel_priority strict

export PATH=/opt/conda/bin:${PATH}

# Conda env with Python 3.10 + GROMACS 2026 + biobb packages
conda create -p /opt/biobb-env python=3.10 -c conda-forge -c bioconda -y \
        gromacs=2026 \
    && /opt/conda/bin/conda run -p /opt/biobb-env \
        pip install --no-cache-dir \
            biobb_common biobb_model biobb_gromacs biobb_io \
    && conda clean -afy

# Bundle a small benchmark (1AKI lysozyme PDB) for offline self-test.
mkdir -p /opt/biobb-bench \
    && curl -L -o /opt/biobb-bench/1AKI.pdb https://files.rcsb.org/download/1AKI.pdb

# Pipeline drivers (staged in CWD from pkg_dir by jarvis).
cp run_md_setup.py /opt/run_md_setup.py
cp run_batch.sh    /opt/run_batch.sh
chmod +x /opt/run_md_setup.py /opt/run_batch.sh

export PATH=/opt/biobb-env/bin:${PATH}
