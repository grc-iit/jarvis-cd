#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# Miniforge (includes mamba)
curl -L -o /tmp/miniforge.sh \
        https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh \
    && bash /tmp/miniforge.sh -b -p /opt/conda \
    && rm /tmp/miniforge.sh \
    && /opt/conda/bin/conda config --set channel_priority strict

export PATH=/opt/conda/bin:${PATH}

# Snakemake 9 (matches the bare-metal benchmark recipe) + fastp for qfilter.
conda create -p /opt/metagem-env -c conda-forge -c bioconda -y \
        python=3.10 snakemake=9.18.2 \
    && conda create -n metagem -c bioconda -c conda-forge -y \
        fastp=0.23 \
    && conda clean -afy

# metaGEM source, with the conda-activate patch applied. Upstream still
# uses the pre-4.4 `source activate` idiom which breaks on newer conda;
# the patch rewrites it to `conda activate` inside `conda shell.bash hook`.
git clone --depth 1 https://github.com/franciscozorrilla/metaGEM.git /opt/metaGEM
# NOTE: metagem-conda-activate.patch must be copied separately (was a COPY directive)
cd /opt/metaGEM && git apply metagem-conda-activate.patch

# NOTE: run_metagem.sh must be copied separately (was a COPY directive)

export PATH=/opt/metagem-env/bin:${PATH}
