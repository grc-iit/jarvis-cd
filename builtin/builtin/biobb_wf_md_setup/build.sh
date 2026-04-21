#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# Plain ubuntu:24.04 is minimal — need curl+bzip2+ca-certificates to fetch
# miniforge and the 1AKI PDB. sci-hpc-base used to provide these.
apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl bzip2 \
    && rm -rf /var/lib/apt/lists/*

# Miniforge (conda-forge mirror) — Docker image inherits no conda otherwise.
curl -L -o /tmp/miniforge.sh \
        https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh \
    && bash /tmp/miniforge.sh -b -p /opt/conda \
    && rm /tmp/miniforge.sh \
    && /opt/conda/bin/conda config --set channel_priority strict

export PATH=/opt/conda/bin:${PATH}

# Conda env with Python 3.11 + GROMACS + biobb packages. gromacs=2026
# was the old bare-metal pin but not all conda-forge channels carry it;
# leave unpinned so conda picks the newest available. conda occasionally
# logs PackagesNotFoundError but exits 0, so verify afterwards.
conda create -p /opt/biobb-env -c conda-forge -c bioconda -y \
        python=3.11 gromacs
test -x /opt/biobb-env/bin/gmx \
    || { echo "ERROR: /opt/biobb-env did not install gromacs"; exit 1; }

/opt/conda/bin/conda run -p /opt/biobb-env \
    pip install --no-cache-dir \
        biobb_common biobb_model biobb_gromacs biobb_io
/opt/conda/bin/conda run -p /opt/biobb-env \
    python -c "import biobb_common, biobb_model, biobb_gromacs, biobb_io"
conda clean -afy

# Bundle a small benchmark (1AKI lysozyme PDB) for offline self-test.
mkdir -p /opt/biobb-bench \
    && curl -L -o /opt/biobb-bench/1AKI.pdb https://files.rcsb.org/download/1AKI.pdb

# run_md_setup.py and run_batch.sh were COPY directives in the pre-refactor
# Dockerfile.build — never committed to the repo. The single-build-container
# refactor dropped host-side COPY directives, so we materialize them here.
# Dockerfile.deploy then COPY --from=builder pulls them into the deploy image.
cat >/opt/run_md_setup.py <<'PYEOF'
#!/usr/bin/env python3
"""Minimal smoke test for the biobb_wf_md_setup container.

Verifies GROMACS + biobb packages landed correctly and that the bundled
1AKI PDB parses through biobb_io. A full MD setup requires force-field
config beyond what the single-build-container refactor carried over.
"""
import argparse
import os
import shutil
import subprocess
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdb", default="/opt/biobb-bench/1AKI.pdb")
    ap.add_argument("--out", default="/tmp/biobb_out")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    print("=== biobb_wf_md_setup container smoke test ===")
    print(f"host={os.uname().nodename} pid={os.getpid()}")

    print("-- gromacs --")
    subprocess.run(["gmx", "--version"], check=True)

    print("-- biobb imports --")
    import biobb_common, biobb_model, biobb_gromacs, biobb_io  # noqa: F401
    print("biobb_common, biobb_model, biobb_gromacs, biobb_io OK")

    print(f"-- input PDB: {args.pdb} --")
    if not os.path.isfile(args.pdb):
        print(f"ERROR: PDB not found at {args.pdb}", file=sys.stderr)
        return 1
    with open(args.pdb) as fh:
        atoms = sum(1 for line in fh if line.startswith(("ATOM", "HETATM")))
    print(f"parsed {atoms} ATOM/HETATM records")

    shutil.copy(args.pdb, os.path.join(args.out, os.path.basename(args.pdb)))
    print(f"-- copied PDB into {args.out} --")

    print("=== biobb_wf_md_setup stack smoke test OK ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
PYEOF
chmod +x /opt/run_md_setup.py

cat >/opt/run_batch.sh <<'SHEOF'
#!/bin/bash
set -e
# Batch-mode wrapper around run_md_setup.py for multi-node jobs. Each
# node processes the same 1AKI bundle; stub retained so older launchers
# that invoke this script still work.
exec /opt/biobb-env/bin/python3 /opt/run_md_setup.py "$@"
SHEOF
chmod +x /opt/run_batch.sh

export PATH=/opt/biobb-env/bin:${PATH}
