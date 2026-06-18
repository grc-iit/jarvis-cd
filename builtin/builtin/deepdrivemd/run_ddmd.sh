#!/bin/bash
# Materialise a DeepDriveMD config from the template and run one
# pipeline experiment end-to-end. Uses /bin/echo placeholders for all
# four stages (see the template).
# Usage: run_ddmd.sh <workdir> [<max_iter> [<num_tasks>]]
#
# Layout:
#   <workdir>/                  — staging root (config, radical sandboxes)
#   <workdir>/deepdrivemd.yaml  — rendered config
#   <workdir>/radical/          — RADICAL_BASE (pre-created)
#   <workdir>/run/              — DDMD experiment_directory (MUST NOT exist
#                                 when DDMD starts; DDMD creates it)
set -euo pipefail
WORKDIR="${1:?workdir required}"
MAX_ITER="${2:-1}"
NUM_TASKS="${3:-1}"

mkdir -p "$WORKDIR" "$WORKDIR/radical"
EXP_DIR="$WORKDIR/run"
# DDMD's ExperimentConfig validator refuses if experiment_directory exists,
# so wipe any leftover from a prior run.
rm -rf "$EXP_DIR"

# radical.entk requires a RabbitMQ broker. rabbitmq-server cannot be
# baked into the merged pipeline image (jarvis's pipeline-merge step
# only copies /opt, /usr/local, and /usr/lib/x86_64-linux-gnu from
# per-package deploy images, so apt-installed files in /usr/sbin,
# /etc/rabbitmq, /usr/lib/rabbitmq, and /usr/lib/erlang are dropped).
# Install it at runtime instead — the merged container has
# network_mode: host and a writable apt. One-time cost per fresh
# container start; a stamp file skips reinstallation on the same
# container.
if [ ! -x /usr/sbin/rabbitmq-server ]; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        rabbitmq-server
    rm -rf /var/lib/apt/lists/*
fi
if ! pgrep -x beam.smp >/dev/null 2>&1; then
    # Apptainer note: `rabbitmq-server -detached` does not return inside
    # an apptainer instance. The double-fork pattern relies on the parent
    # shell exiting once rabbitmq's launcher script (which waits on
    # beam.smp) has handed off — but apptainer's PID 1 (`appinit`) does
    # not reap that wrapper chain, so the parent shell stays in `wait`
    # and run_ddmd.sh blocks forever on this line. Launching the broker
    # with `nohup ... &` and `disown` gives the same daemonized broker
    # without depending on PID-1's reaping behavior.
    nohup /usr/sbin/rabbitmq-server >/tmp/rabbitmq-server.log 2>&1 &
    disown
    # Wait for the broker to be reachable. Newer rabbitmqctl versions
    # split the old `status` into multiple sub-commands; `await_startup`
    # is the canonical readiness probe and exits 0 once the broker is
    # accepting connections.
    for _ in $(seq 1 60); do
        /usr/sbin/rabbitmqctl await_startup >/dev/null 2>&1 && break
        sleep 1
    done
fi
export RMQ_HOSTNAME=localhost
export RMQ_PORT=5672
export RMQ_USERNAME=guest
export RMQ_PASSWORD=guest

CFG="$WORKDIR/deepdrivemd.yaml"
sed \
    -e "s|__EXPERIMENT_DIR__|$EXP_DIR|g" \
    -e "s|__MAX_ITER__|$MAX_ITER|g" \
    -e "s|__NUM_TASKS__|$NUM_TASKS|g" \
    /opt/deepdrivemd.template.yaml > "$CFG"

# radical.entk writes sandboxes under $HOME/radical.pilot.sandbox by
# default; point it at the workdir so each run is self-contained.
export RADICAL_BASE="$WORKDIR"

# radical.pilot launches helper subprocesses (radical-pilot-bridge,
# radical-pilot-worker, etc.) via PATH lookup. jarvis's docker-exec env
# does not include /opt/ddmd-env/bin or /usr/sbin, so prepend both.
export PATH="/opt/ddmd-env/bin:/usr/sbin:${PATH}"

# The host's $HOME may have a miniconda install; with apptainer's
# auto-bind it shows up as /home/$USER/miniconda3 inside the instance
# and radical.pilot's bootstrap auto-detects "anaconda mode" from any
# leftover CONDA_* env vars and then tries to source `conda` from a
# binary that isn't compatible with the container userspace. Strip the
# leak so radical.pilot uses the SIF's /opt/ddmd-env (pinned in the
# resource_local.json patch from build.sh).
unset CONDA_DEFAULT_ENV CONDA_EXE CONDA_PREFIX CONDA_PROMPT_MODIFIER \
      CONDA_PYTHON_EXE CONDA_SHLVL CONDA_PREFIX_1 CONDA_PREFIX_2 \
      _CE_CONDA _CE_M

/opt/ddmd-env/bin/python3 -m deepdrivemd.deepdrivemd -c "$CFG"

# Success: experiment dir populated with at least one stage sub-dir.
if ! find "$EXP_DIR" -type d -name 'stage*' -print -quit | grep -q .; then
    echo "FAIL: no stage output dirs under $EXP_DIR"
    exit 1
fi

# Verify iowarp CTE FUSE was actually exercised: the task stages write
# under /mnt/wrp_cte/ddmd (the wrp_cte_libfuse mountpoint). If it is
# non-empty, CTE saw real blob traffic.
CTE_OUT="${DDMD_CTE_OUT:-/mnt/wrp_cte/ddmd}"
CTE_COUNT=0
CTE_BYTES=0
if [ -d "$CTE_OUT" ]; then
    CTE_COUNT=$(find "$CTE_OUT" -maxdepth 1 -type f 2>/dev/null | wc -l)
    CTE_BYTES=$(find "$CTE_OUT" -maxdepth 1 -type f -printf '%s\n' 2>/dev/null \
                | awk 'BEGIN{s=0}{s+=$1}END{print s}')
fi
echo "=== SUCCESS: DeepDriveMD pipeline completed under $EXP_DIR ==="
echo "=== CTE FUSE traffic: $CTE_COUNT files, $CTE_BYTES bytes under $CTE_OUT ==="
if [ "$CTE_COUNT" -eq 0 ]; then
    echo "WARNING: no files landed in $CTE_OUT — iowarp FUSE was not exercised" >&2
fi
exit 0
