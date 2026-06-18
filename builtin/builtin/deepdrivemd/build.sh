#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# DeepDriveMD — adaptive biomolecular simulation framework.
# CPU-only in this configuration (placeholder /bin/echo stages in the
# YAML); re-wire molecular_dynamics_stage.executable to a real OpenMM
# driver for production MD.
# Mirrors awesome-scienctific-applications/deepdrivemd/Dockerfile.

apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git \
        build-essential pkg-config \
        python3-venv \
        python3-dev \
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

# DeepDriveMD source with the self.cfg patch applied.
# The patch, template yaml, and run_ddmd.sh are staged in CWD from pkg_dir
# by jarvis — the upstream Dockerfile COPYs them in one at a time.
git clone --depth 1 \
        https://github.com/DeepDriveMD/DeepDriveMD-pipeline.git /opt/DeepDriveMD
cp deepdrivemd-selfcfg.patch /opt/DeepDriveMD/deepdrivemd-selfcfg.patch
cd /opt/DeepDriveMD \
    && git apply deepdrivemd-selfcfg.patch \
    && /opt/ddmd-env/bin/pip install --no-cache-dir --no-deps -e .
cd -

# Tiny benchmark input: 1FME (36-residue test structure) in sys1/
mkdir -p /opt/ddmd-bench/sys1 \
    && curl -L -o /opt/ddmd-bench/sys1/comp.pdb \
        https://files.rcsb.org/download/1FME.pdb \
    && cp /opt/ddmd-bench/sys1/comp.pdb /opt/ddmd-bench/1FME-folded.pdb

cp deepdrivemd.template.yaml /opt/deepdrivemd.template.yaml
cp run_ddmd.sh               /opt/run_ddmd.sh
cp ddmd_io_task.sh           /opt/ddmd_io_task.sh
chmod +x /opt/run_ddmd.sh /opt/ddmd_io_task.sh

# RabbitMQ + Erlang. radical.entk needs an AMQP broker, but the
# `rabbitmq-server` apt package's postinst expects to chown system
# users (rabbitmq, _apt) which fakeroot can't do without subuid
# mappings. Instead we extract the .debs (and their hard
# dependencies) into the rootfs via `dpkg-deb -x` — no postinst, no
# uid changes, just the binaries and config skeletons. run_ddmd.sh
# starts the broker via `rabbitmq-server` directly so the postinst
# omission is fine.
mkdir -p /tmp/rabbit-debs
cd /tmp/rabbit-debs
apt-get update
apt-get install --download-only -y --no-install-recommends \
        rabbitmq-server erlang-base erlang-asn1 erlang-crypto \
        erlang-eldap erlang-ftp erlang-inets erlang-mnesia \
        erlang-os-mon erlang-parsetools erlang-public-key \
        erlang-runtime-tools erlang-snmp erlang-ssl \
        erlang-syntax-tools erlang-tftp erlang-tools erlang-xmerl \
        libodbc2 libodbcinst2 libsctp1 libsnmp40 libwxbase3.2-1t64 \
        libwxgtk3.2-1t64 libsensors5 libsensors-config socat
cp /var/cache/apt/archives/*.deb .
for d in *.deb; do
    dpkg-deb -x "$d" /
done
cd /
rm -rf /tmp/rabbit-debs /var/lib/apt/lists/*

# rabbitmq's run_ddmd.sh does its own user/group setup; pre-create the
# minimal accounts here so no postinst hook is needed at start time.
getent group rabbitmq >/dev/null || groupadd -r rabbitmq || true
id -u rabbitmq >/dev/null 2>&1 || \
    useradd -r -g rabbitmq -d /var/lib/rabbitmq -s /usr/sbin/nologin rabbitmq || true
mkdir -p /var/lib/rabbitmq /var/log/rabbitmq /etc/rabbitmq
chown -R rabbitmq:rabbitmq /var/lib/rabbitmq /var/log/rabbitmq /etc/rabbitmq 2>/dev/null || true

# Pin radical.pilot's `local.localhost` resource to the in-container
# venv. Default behavior detects conda from the calling shell ($HOME on
# the host has miniconda3) and tries to bootstrap from
# /home/llogan/miniconda3 which lives outside the SIF — `conda` isn't on
# PATH inside the instance, so bootstrap_0.sh aborts with "Loading of
# conda env failed!". Forcing virtenv=/opt/ddmd-env, virtenv_mode=use,
# and python_dist=default keeps the agent strictly inside the SIF.
RP_RES_JSON=$(/opt/ddmd-env/bin/python3 -c \
    "import radical.pilot, os; print(os.path.join(os.path.dirname(radical.pilot.__file__), 'configs', 'resource_local.json'))")
/opt/ddmd-env/bin/python3 - "$RP_RES_JSON" <<'PYEOF'
import json, re, sys
path = sys.argv[1]
with open(path) as f:
    raw = f.read()
# resource_local.json ships with `#`-prefixed comments that aren't
# valid JSON; strip them before parsing.
raw_clean = re.sub(r'^\s*#.*$', '', raw, flags=re.MULTILINE)
cfg = json.loads(raw_clean)
for name, entry in cfg.items():
    if name in ('localhost', 'localhost_test'):
        entry['virtenv']      = '/opt/ddmd-env'
        entry['virtenv_mode'] = 'use'
        entry['python_dist']  = 'default'
        entry['rp_version']   = 'installed'
with open(path, 'w') as f:
    json.dump(cfg, f, indent=4)
print('Patched', path)
PYEOF

export PATH=/opt/ddmd-env/bin:${PATH}
