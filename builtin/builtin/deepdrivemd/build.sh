#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# DeepDriveMD — adaptive biomolecular simulation framework.
# CPU-only in this configuration (placeholder /bin/echo stages in the
# YAML); re-wire molecular_dynamics_stage.executable to a real OpenMM
# driver for production MD.
# Mirrors awesome-scienctific-applications/deepdrivemd/Dockerfile.

apt-get update && apt-get install -y --no-install-recommends \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

python3 -m venv /opt/ddmd-env \
    && /opt/ddmd-env/bin/pip install --upgrade pip wheel

# Pinned versions mirror the bare-metal Ares recipe: pydantic v1 (DDMD's
# schema is v1-only), relaxed h5py and PyYAML constraints (repo pins do
# not build on Python 3.10).
/opt/ddmd-env/bin/pip install --no-cache-dir \
        'pydantic==1.10.26' \
        'PyYAML==6.0.3' \
        'h5py==3.16.0' \
        numpy scipy MDAnalysis pathos tqdm \
        openmm==8.5.0 \
        radical.entk==1.103.0 radical.pilot==1.103.2 radical.utils==1.103.1 \
        'git+https://github.com/braceal/MD-tools.git'

# DeepDriveMD source with the self.cfg patch applied.
# The patch, template yaml, and run_ddmd.sh are staged in CWD from pkg_dir
# by jarvis — the upstream Dockerfile COPYs them in one at a time.
git clone --depth 1 \
        https://github.com/DeepDriveMD/DeepDriveMD-pipeline.git /opt/DeepDriveMD
cp deepdrivemd-selfcfg.patch /opt/DeepDriveMD/deepdrivemd-selfcfg.patch
cd /opt/DeepDriveMD \
    && git apply deepdrivemd-selfcfg.patch \
    && /opt/ddmd-env/bin/pip install --no-cache-dir -e .
cd -

# Tiny benchmark input: 1FME (36-residue test structure) in sys1/
mkdir -p /opt/ddmd-bench/sys1 \
    && curl -L -o /opt/ddmd-bench/sys1/comp.pdb \
        https://files.rcsb.org/download/1FME.pdb \
    && cp /opt/ddmd-bench/sys1/comp.pdb /opt/ddmd-bench/1FME-folded.pdb

cp deepdrivemd.template.yaml /opt/deepdrivemd.template.yaml
cp run_ddmd.sh               /opt/run_ddmd.sh
chmod +x /opt/run_ddmd.sh

export PATH=/opt/ddmd-env/bin:${PATH}
