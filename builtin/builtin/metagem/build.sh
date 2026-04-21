#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# Plain ubuntu:24.04 is minimal — need curl+git+bzip2 to fetch miniforge
# and the metaGEM source. sci-hpc-base used to provide these.
apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git bzip2 \
    && rm -rf /var/lib/apt/lists/*

# Miniforge (includes mamba)
curl -L -o /tmp/miniforge.sh \
        https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh \
    && bash /tmp/miniforge.sh -b -p /opt/conda \
    && rm /tmp/miniforge.sh \
    && /opt/conda/bin/conda config --set channel_priority strict

export PATH=/opt/conda/bin:${PATH}

# Snakemake 9 + fastp for qfilter. Pin dropped from 9.18.2 because it was
# yanked from conda-forge/bioconda; the bare-metal recipe no longer
# resolves. Let conda pick the newest snakemake 9.x so the smoke test
# stays current. conda sometimes logs PackagesNotFoundError but still
# exits 0, so verify the prefix exists after each create.
conda create -p /opt/metagem-env -c conda-forge -c bioconda -y \
        python=3.12 'snakemake>=9,<10'
test -x /opt/metagem-env/bin/snakemake \
    || { echo "ERROR: /opt/metagem-env did not install snakemake"; exit 1; }

# channel_priority=strict was set above, so list conda-forge first — with
# bioconda first, libdeflate (conda-forge only) can't be picked up and
# fastp won't resolve.
conda create -n metagem -c conda-forge -c bioconda -y fastp=0.23
test -x /opt/conda/envs/metagem/bin/fastp \
    || { echo "ERROR: metagem conda env did not install fastp"; exit 1; }

conda clean -afy

# metaGEM source. The original Dockerfile.build applied
# `metagem-conda-activate.patch` (which rewrites upstream's pre-4.4
# `source activate` to `conda activate` inside `conda shell.bash hook`).
# That patch was lost in the single-build-container refactor — never
# committed to the repo. Smoke test skips the patch; if the canonical
# patch is ever recovered, reapply it here before using the workflow.
git clone --depth 1 https://github.com/franciscozorrilla/metaGEM.git /opt/metaGEM

# Smoke-test driver materialized inside the build container (the
# pre-refactor Dockerfile.build used COPY for run_metagem.sh; the
# single-build-container architecture dropped host-side COPY directives,
# so we generate it here). Dockerfile.deploy then COPY --from=builder
# pulls /opt/run_metagem.sh into the deploy image.
cat >/opt/run_metagem.sh <<'SHEOF'
#!/bin/bash
set -e
# Minimal smoke test: verify the snakemake + fastp + metaGEM source
# landed correctly in the deploy image. A full metaGEM run needs a
# real metagenomic sample set and the lost conda-activate patch.
echo "=== metaGEM container smoke test ==="
echo "host=$(hostname) pid=$$"

echo "-- snakemake --"
/opt/metagem-env/bin/snakemake --version

echo "-- fastp --"
/opt/conda/envs/metagem/bin/fastp --version 2>&1 | head -1

echo "-- metaGEM source --"
test -f /opt/metaGEM/Snakefile && echo "Snakefile present"
test -d /opt/metaGEM/workflow && echo "workflow/ present" || echo "workflow/ absent (ok — layout varies by upstream revision)"

echo "=== metaGEM stack smoke test OK ==="
SHEOF

chmod +x /opt/run_metagem.sh

export PATH=/opt/metagem-env/bin:${PATH}
