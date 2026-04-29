#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# metaGEM — Snakemake workflow for genome-scale metabolic reconstruction.
# CPU-only, focused on the qfilter stage (fastp + Snakemake DAG).
# Aux files (metagem-conda-activate.patch, run_metagem.sh) are staged in
# CWD by jarvis from pkg_dir before this script runs.

# Plain ubuntu:24.04 is minimal — need curl+git+bzip2+ca-certificates to
# fetch miniforge and the upstream metaGEM repo.
apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git bzip2 \
    && rm -rf /var/lib/apt/lists/*

# ---- Miniforge (includes mamba) ------------------------------------------------
curl -L -o /tmp/miniforge.sh \
        https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh \
    && bash /tmp/miniforge.sh -b -p /opt/conda \
    && rm /tmp/miniforge.sh \
    && /opt/conda/bin/conda config --set channel_priority strict

export PATH=/opt/conda/bin:${PATH}

# ---- Snakemake 9 + fastp envs --------------------------------------------------
# snakemake=9.18.2 was yanked from conda-forge/bioconda, and snakemake 9
# requires python>=3.11. Let conda pick the newest 9.x on python 3.12 so
# the build stays current. Verify after since conda sometimes logs
# PackagesNotFoundError but still exits 0.
conda create -p /opt/metagem-env -c conda-forge -c bioconda -y \
        python=3.12 'snakemake>=9,<10'
test -x /opt/metagem-env/bin/snakemake \
    || { echo "ERROR: /opt/metagem-env did not install snakemake"; exit 1; }
# fastp=0.23 no longer resolves on current bioconda/conda-forge: every
# 0.23.x pins libdeflate <1.26, but conda-forge has moved to 1.26+. Let
# conda pick the newest fastp; bioconda has 0.24+ that allows libdeflate 1.26.
conda create -n metagem -c bioconda -c conda-forge -y fastp
test -x /opt/conda/envs/metagem/bin/fastp \
    || { echo "ERROR: metagem env did not install fastp"; exit 1; }
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
