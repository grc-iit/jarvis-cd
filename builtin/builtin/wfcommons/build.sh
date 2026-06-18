#!/bin/bash
# Build a self-contained python venv at /opt/wfcommons-env with the
# wfcommons framework + WfBench translator/runtime installed, plus the
# pipeline driver under /opt/wfcommons-driver.
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates curl git \
    python3 python3-venv python3-pip python3-dev \
    build-essential \
    openssh-server openssh-client
rm -rf /var/lib/apt/lists/*

# SSH setup (so this image's deploy variant works in MPI cluster mode).
mkdir -p /var/run/sshd /root/.ssh
ssh-keygen -A
ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519
cat /root/.ssh/id_ed25519.pub >> /root/.ssh/authorized_keys
chmod 700 /root/.ssh
chmod 600 /root/.ssh/authorized_keys
sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
sed -i 's/#PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config
printf "StrictHostKeyChecking no\nUserKnownHostsFile /dev/null\n" >> /etc/ssh/ssh_config

# Dedicated venv keeps wfcommons (and its numpy/networkx/pandas deps)
# isolated from system python.
python3 -m venv /opt/wfcommons-env
/opt/wfcommons-env/bin/pip install --no-cache-dir --upgrade pip wheel setuptools

# Install the wfcommons python library.
/opt/wfcommons-env/bin/pip install --no-cache-dir wfcommons

# wfcommons 1.4's wheel build doesn't reliably ship `bin/wfbench` or
# `bin/cpu-benchmark` (setup.py's scripts= + dynamic data_files don't
# survive the modern PEP 517 build path in some envs). Install them by
# hand from the sdist: download, compile cpu-benchmark with the local
# g++, then drop both into the venv's bin/.
mkdir -p /opt/wfcommons-src
/opt/wfcommons-env/bin/pip download --no-cache-dir --no-deps \
    --no-binary=:all: --dest /opt/wfcommons-src wfcommons
tar -xzf /opt/wfcommons-src/wfcommons-*.tar.gz -C /opt/wfcommons-src
WFC_SRC=$(ls -d /opt/wfcommons-src/wfcommons-*/ | head -1)
make -C "${WFC_SRC%/}"
install -Dm755 "${WFC_SRC%/}/bin/cpu-benchmark" /opt/wfcommons-env/bin/cpu-benchmark
# wfbench is a python script; install it with the venv's interpreter as
# shebang so BashTranslator picks up an absolute, in-container path.
install -Dm755 "${WFC_SRC%/}/bin/wfbench" /opt/wfcommons-env/bin/wfbench
sed -i '1s|^#!.*python.*|#!/opt/wfcommons-env/bin/python3|' /opt/wfcommons-env/bin/wfbench
rm -rf /opt/wfcommons-src

# Sanity: importable + recipe + translator accessible, and the wfbench
# console scripts are on PATH (BashTranslator copies them via
# shutil.which at translation time). The %post build env doesn't carry
# /opt/wfcommons-env/bin on PATH automatically — set it for the check
# (the %environment block does it for runtime).
export PATH=/opt/wfcommons-env/bin:${PATH}
/opt/wfcommons-env/bin/python3 - <<'PY'
import shutil, sys
import wfcommons
print("wfcommons version:", getattr(wfcommons, "__version__", "unknown"))
from wfcommons import MontageRecipe  # noqa: F401
from wfcommons.wfbench import WorkflowBenchmark, BashTranslator  # noqa: F401
for tool in ("wfbench", "cpu-benchmark"):
    p = shutil.which(tool)
    if not p:
        sys.exit(f"missing CLI on PATH: {tool}")
    print(f"  {tool}: {p}")
PY

# Stage the driver. jarvis copies aux files (anything alongside pkg.py)
# into the build container's CWD via docker cp before invoking build.sh.
# Verify before copying so a silent staging failure surfaces here.
[ -f run_wfbench.py ] || { echo "ERROR: run_wfbench.py not staged in $(pwd)" >&2; exit 1; }
mkdir -p /opt/wfcommons-driver
cp run_wfbench.py /opt/wfcommons-driver/run_wfbench.py
chmod +x /opt/wfcommons-driver/run_wfbench.py
