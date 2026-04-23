#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# metaGEM — Snakemake workflow for genome-scale metabolic reconstruction.
# CPU-only, focused on the qfilter stage (fastp + Snakemake DAG).
# Base image ##BASE_IMAGE## must supply build tools + ssh.
# Aux files (metagem-conda-activate.patch, run_metagem.sh) are staged in
# CWD by jarvis from pkg_dir before this script runs.

# ---- Miniforge (includes mamba) ------------------------------------------------
curl -L -o /tmp/miniforge.sh \
        https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh \
    && bash /tmp/miniforge.sh -b -p /opt/conda \
    && rm /tmp/miniforge.sh \
    && /opt/conda/bin/conda config --set channel_priority strict

export PATH=/opt/conda/bin:${PATH}

# ---- Snakemake 9 + fastp envs --------------------------------------------------
conda create -p /opt/metagem-env -c conda-forge -c bioconda -y \
        python=3.10 snakemake=9.18.2
conda create -n metagem -c bioconda -c conda-forge -y fastp=0.23
conda clean -afy

# ---- metaGEM source + conda-activate patch --------------------------------------
# Upstream still uses the pre-4.4 'source activate' idiom which breaks on
# newer conda; the patch rewrites it to 'conda activate' via
# 'conda shell.bash hook'.
git clone --depth 1 https://github.com/franciscozorrilla/metaGEM.git /opt/metaGEM
cp metagem-conda-activate.patch /opt/metaGEM/metagem-conda-activate.patch
cd /opt/metaGEM && git apply metagem-conda-activate.patch
cd -

# ---- run_metagem.sh (benchmark entrypoint) --------------------------------------
cp run_metagem.sh /opt/run_metagem.sh
chmod +x /opt/run_metagem.sh

export PATH=/opt/metagem-env/bin:${PATH}
