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

# Snakemake 7 (V-pipe's load_configfile call was removed in v8+).
conda create -p /opt/vpipe-env -c conda-forge -c bioconda -y \
        python=3.10 snakemake=7.32.4 \
    && conda clean -afy

# V-pipe source. Checkout a concrete release tag for reproducibility; the
# 2026-03-27 Ares run used master but this container pins to v3.0.0.
git clone --depth 1 --branch v3.0.0 \
        https://github.com/cbg-ethz/V-pipe.git /opt/V-pipe

# NOTE: run_vpipe.sh must be copied separately (was a COPY directive)

export PATH=/opt/vpipe-env/bin:${PATH}
# Isolate from any user-site packages (bare-metal benchmark hit this bug).
export PYTHONNOUSERSITE=1
