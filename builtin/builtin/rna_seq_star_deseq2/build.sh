#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# Plain ubuntu:24.04 is minimal — need curl+git+bzip2 to fetch miniforge
# and the workflow source. sci-hpc-base used to provide these.
# Split from any `&&` chain: bash's `set -e` does not abort on a mid-chain
# failure (only the final command triggers it), so a failed apt-get used
# to silently pass through.
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl git bzip2
rm -rf /var/lib/apt/lists/*

# Miniforge (includes mamba) — Snakemake's conda frontend.
curl -L -o /tmp/miniforge.sh \
    https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
bash /tmp/miniforge.sh -b -p /opt/conda
rm /tmp/miniforge.sh
/opt/conda/bin/conda config --set channel_priority strict

export PATH=/opt/conda/bin:${PATH}

# Snakemake 9. The old bare-metal pin (9.18.2 + python=3.10) no longer
# resolves: 9.18.2 was yanked, and snakemake 9 requires python>=3.11.
# Let conda pick the newest 9.x so the smoke test stays current. conda
# sometimes logs PackagesNotFoundError but still exits 0, so verify.
conda create -p /opt/rnaseq-env -c conda-forge -c bioconda -y \
        python=3.12 'snakemake>=9,<10'
test -x /opt/rnaseq-env/bin/snakemake \
    || { echo "ERROR: /opt/rnaseq-env did not install snakemake"; exit 1; }
conda clean -afy

# Upstream workflow. The old bare-metal recipe pinned v2.2.0 which no
# longer exists in upstream (tag was removed or renamed). v2.1.2 is the
# latest surviving v2.x tag and keeps the smoke test close to the
# original Ares-benchmark layout.
git clone --depth 1 --branch v2.1.2 \
    https://github.com/snakemake-workflows/rna-seq-star-deseq2.git \
    /opt/rna-seq-star-deseq2

# Bundled S. cerevisiae benchmark + pipeline driver. Jarvis stages aux
# files (including directories) into CWD via docker cp, but that step
# suppresses exit codes (pipeline.py), so a silent staging failure would
# surface as a confusing "cp: cannot stat" below. Check first and fail
# loud.
[ -d bench ] || { echo "ERROR: bench/ not staged in $(pwd) by jarvis" >&2; exit 1; }
[ -f run_rnaseq.sh ] || { echo "ERROR: run_rnaseq.sh not staged in $(pwd) by jarvis" >&2; exit 1; }
cp -r bench /opt/rnaseq-bench
cp run_rnaseq.sh /opt/run_rnaseq.sh
chmod +x /opt/run_rnaseq.sh

export PATH=/opt/rnaseq-env/bin:${PATH}
export PYTHONNOUSERSITE=1
