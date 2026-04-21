#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# Plain ubuntu:24.04 is minimal — install the full toolchain here rather
# than relying on a bespoke base image. curl + git fetch the DDMD source
# and the 1FME benchmark PDB; build-essential covers any source wheels.
apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git \
        python3 python3-venv python3-dev build-essential pkg-config \
    && rm -rf /var/lib/apt/lists/*

python3 -m venv /opt/ddmd-env \
    && /opt/ddmd-env/bin/pip install --upgrade pip wheel

# Pinned versions mirror the bare-metal Ares recipe: pydantic v1 (DDMD's
# schema is v1-only), relaxed h5py and PyYAML constraints (repo pins do
# not build on Python 3.10+).
/opt/ddmd-env/bin/pip install --no-cache-dir \
        'pydantic==1.10.26' \
        'PyYAML==6.0.3' \
        'h5py==3.16.0' \
        numpy scipy MDAnalysis pathos tqdm \
        openmm==8.5.0 \
        radical.entk==1.103.0 radical.pilot==1.103.2 radical.utils==1.103.1 \
        'git+https://github.com/braceal/MD-tools.git'

# DeepDriveMD source. The original Dockerfile.build applied a
# `deepdrivemd-selfcfg.patch` (lost in the single-build-container
# refactor — never committed to the repo). Smoke test skips the patch
# and the editable install; if the patch is ever recovered, reapply
# here before `pip install -e .`.
git clone --depth 1 \
        https://github.com/DeepDriveMD/DeepDriveMD-pipeline.git /opt/DeepDriveMD

# Tiny benchmark input: 1FME (36-residue test structure) in sys1/
mkdir -p /opt/ddmd-bench/sys1 \
    && curl -L -o /opt/ddmd-bench/sys1/comp.pdb \
        https://files.rcsb.org/download/1FME.pdb \
    && cp /opt/ddmd-bench/sys1/comp.pdb /opt/ddmd-bench/1FME-folded.pdb

# Smoke-test artifacts materialized inside the build container (the
# pre-refactor Dockerfile.build used COPY for run_ddmd.sh and
# deepdrivemd.template.yaml; the single-build-container architecture
# dropped host-side COPY directives, so we generate them here).
cat >/opt/ddmd_smoke.py <<'PYEOF'
"""
Minimal smoke test for the DeepDriveMD container image. Verifies the
core deps (openmm, MDAnalysis, h5py, pydantic, numpy) import, then runs
a 100-step Langevin integration on the 1FME benchmark structure
(bundled at /opt/ddmd-bench/1FME-folded.pdb). Avoids RADICAL-EnTK's
full MongoDB orchestration — a proper DDMD run needs the lost
`deepdrivemd-selfcfg.patch` plus external services.
"""
import os
import sys

import numpy as np
import h5py
import pydantic
import MDAnalysis

import openmm
import openmm.app as app
import openmm.unit as unit

print(f"host={os.uname().nodename} pid={os.getpid()}")
print(f"python={sys.version.split()[0]} openmm={openmm.version.full_version} "
      f"MDAnalysis={MDAnalysis.__version__} h5py={h5py.__version__} "
      f"pydantic={pydantic.VERSION} numpy={np.__version__}")

pdb = app.PDBFile('/opt/ddmd-bench/1FME-folded.pdb')
forcefield = app.ForceField('amber14-all.xml', 'amber14/tip3pfb.xml')
system = forcefield.createSystem(pdb.topology, nonbondedMethod=app.NoCutoff,
                                 constraints=app.HBonds)
integrator = openmm.LangevinMiddleIntegrator(
    300 * unit.kelvin, 1 / unit.picosecond, 0.002 * unit.picoseconds)
simulation = app.Simulation(pdb.topology, system, integrator,
                            openmm.Platform.getPlatformByName('CPU'))
simulation.context.setPositions(pdb.positions)
simulation.minimizeEnergy(maxIterations=50)
simulation.step(100)
state = simulation.context.getState(getEnergy=True)
ke = state.getKineticEnergy().value_in_unit(unit.kilojoule_per_mole)
pe = state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
print(f"openmm step=100 KE={ke:.2f} kJ/mol PE={pe:.2f} kJ/mol")
print("=== DeepDriveMD stack smoke test OK ===")
PYEOF

cat >/opt/run_ddmd.sh <<'SHEOF'
#!/bin/bash
set -e
# DDMD_ITERATIONS and DDMD_OUT are set by the Jarvis package's start()
# but the smoke test ignores them (a full DDMD run would write
# per-iteration artifacts into $DDMD_OUT).
mkdir -p "${DDMD_OUT:-/tmp/ddmd_out}"
exec /opt/ddmd-env/bin/python3 /opt/ddmd_smoke.py
SHEOF

# Placeholder template so Dockerfile.deploy's COPY --from=builder
# doesn't fail; the smoke test does not read this file.
cat >/opt/deepdrivemd.template.yaml <<'YEOF'
# Placeholder template — the original deepdrivemd.template.yaml was
# never committed to the repo. Replace once the canonical template
# (plus the deepdrivemd-selfcfg.patch) is recovered.
title: ddmd-smoke-placeholder
YEOF

chmod +x /opt/run_ddmd.sh

export PATH=/opt/ddmd-env/bin:${PATH}
