#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# Miniforge (includes mamba) — Snakemake's conda frontend.
curl -L -o /tmp/miniforge.sh \
        https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh \
    && bash /tmp/miniforge.sh -b -p /opt/conda \
    && rm /tmp/miniforge.sh \
    && /opt/conda/bin/conda config --set channel_priority strict

export PATH=/opt/conda/bin:${PATH}

conda create -p /opt/rnaseq-env -c conda-forge -c bioconda -y \
        python=3.10 snakemake=9.18.2 \
    && conda clean -afy

# Upstream workflow. Pinned to v2.2.0 (the release the bare-metal Ares
# benchmark validated against).
git clone --depth 1 --branch v2.2.0 \
        https://github.com/snakemake-workflows/rna-seq-star-deseq2.git \
        /opt/rna-seq-star-deseq2

# NOTE: bench/ directory and run_rnaseq.sh must be copied separately (were COPY directives)

export PATH=/opt/rnaseq-env/bin:${PATH}
export PYTHONNOUSERSITE=1
