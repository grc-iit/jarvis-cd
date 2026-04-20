#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

apt-get update && apt-get install -y --no-install-recommends \
        python3-venv \
        python3-dev \
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
git clone --depth 1 \
        https://github.com/DeepDriveMD/DeepDriveMD-pipeline.git /opt/DeepDriveMD

# Embed the self.cfg patch inline (fixes bare cfg reference vs self.cfg)
cat > /opt/DeepDriveMD/deepdrivemd-selfcfg.patch << 'PATCH'
diff --git a/deepdrivemd/deepdrivemd.py b/deepdrivemd/deepdrivemd.py
index d8f77c2..d897b0b 100644
--- a/deepdrivemd/deepdrivemd.py
+++ b/deepdrivemd/deepdrivemd.py
@@ -67,10 +67,10 @@ class PipelineManager:

         self.pipeline.add_stages(self.generate_molecular_dynamics_stage())

-        if not cfg.aggregation_stage.skip_aggregation:
+        if not self.cfg.aggregation_stage.skip_aggregation:
             self.pipeline.add_stages(self.generate_aggregating_stage())

-        if self.stage_idx % cfg.machine_learning_stage.retrain_freq == 0:
+        if self.stage_idx % self.cfg.machine_learning_stage.retrain_freq == 0:
             self.pipeline.add_stages(self.generate_machine_learning_stage())
         self.pipeline.add_stages(self.generate_model_selection_stage())

PATCH

cd /opt/DeepDriveMD \
    && git apply deepdrivemd-selfcfg.patch \
    && /opt/ddmd-env/bin/pip install --no-cache-dir -e .

# Tiny benchmark input: 1FME (36-residue test structure) in sys1/
mkdir -p /opt/ddmd-bench/sys1 \
    && curl -L -o /opt/ddmd-bench/sys1/comp.pdb \
        https://files.rcsb.org/download/1FME.pdb \
    && cp /opt/ddmd-bench/sys1/comp.pdb /opt/ddmd-bench/1FME-folded.pdb

# Embed the DeepDriveMD config template (placeholders substituted at runtime)
cat > /opt/deepdrivemd.template.yaml << 'YAML'
title: "DeepDriveMD-container-smoke"
resource: local.localhost
queue: ""
schema_: local
project: ""
walltime_min: 30
max_iteration: __MAX_ITER__
cpus_per_node: 4
gpus_per_node: 1
hardware_threads_per_cpu: 1
experiment_directory: __EXPERIMENT_DIR__
node_local_path: null
molecular_dynamics_stage:
    pre_exec: []
    executable: /bin/echo
    arguments:
    - "MD_TASK_PLACEHOLDER"
    cpu_reqs:
        processes: 1
        process_type: null
        threads_per_process: 1
        thread_type: null
    gpu_reqs:
        processes: 0
        process_type: null
        threads_per_process: 0
        thread_type: null
    num_tasks: __NUM_TASKS__
    task_config:
        initial_pdb_dir: /opt/ddmd-bench
aggregation_stage:
    pre_exec: []
    executable: /bin/echo
    arguments:
    - "AGGREGATION_TASK_PLACEHOLDER"
    cpu_reqs:
        processes: 1
        process_type: null
        threads_per_process: 1
        thread_type: null
    gpu_reqs:
        processes: 0
        process_type: null
        threads_per_process: 0
        thread_type: null
    skip_aggregation: true
    task_config: {}
machine_learning_stage:
    pre_exec: []
    executable: /bin/echo
    arguments:
    - "ML_TASK_PLACEHOLDER"
    cpu_reqs:
        processes: 1
        process_type: null
        threads_per_process: 1
        thread_type: null
    gpu_reqs:
        processes: 0
        process_type: null
        threads_per_process: 0
        thread_type: null
    retrain_freq: 1
    task_config: {}
model_selection_stage:
    pre_exec: []
    executable: /bin/echo
    arguments:
    - "MODEL_SELECTION_TASK_PLACEHOLDER"
    cpu_reqs:
        processes: 1
        process_type: null
        threads_per_process: 1
        thread_type: null
    gpu_reqs:
        processes: 0
        process_type: null
        threads_per_process: 0
        thread_type: null
    task_config: {}
agent_stage:
    pre_exec: []
    executable: /bin/echo
    arguments:
    - "AGENT_TASK_PLACEHOLDER"
    cpu_reqs:
        processes: 1
        process_type: null
        threads_per_process: 1
        thread_type: null
    gpu_reqs:
        processes: 0
        process_type: null
        threads_per_process: 0
        thread_type: null
    task_config: {}
YAML

# Embed the run script
cat > /opt/run_ddmd.sh << 'RUN_SCRIPT'
#!/bin/bash
# Materialise a DeepDriveMD config from the template and run one
# pipeline experiment end-to-end. Uses /bin/echo placeholders for all
# four stages (see the template).
# Usage: run_ddmd.sh <experiment_dir> [<max_iter> [<num_tasks>]]
set -euo pipefail
EXP_DIR="${1:?experiment_dir required}"
MAX_ITER="${2:-1}"
NUM_TASKS="${3:-1}"

mkdir -p "$EXP_DIR"
CFG="$EXP_DIR/deepdrivemd.yaml"
sed \
    -e "s|__EXPERIMENT_DIR__|$EXP_DIR|g" \
    -e "s|__MAX_ITER__|$MAX_ITER|g" \
    -e "s|__NUM_TASKS__|$NUM_TASKS|g" \
    /opt/deepdrivemd.template.yaml > "$CFG"

# radical.entk writes sandboxes under $HOME/radical.pilot.sandbox by
# default; point it at the experiment dir so each run is self-contained.
export RADICAL_BASE="$EXP_DIR"
mkdir -p "$EXP_DIR/radical"

python -m deepdrivemd.deepdrivemd -c "$CFG"

# Success: experiment dir populated with at least one stage sub-dir.
if find "$EXP_DIR" -type d -name 'stage*' -print -quit | grep -q .; then
    echo "=== SUCCESS: DeepDriveMD pipeline completed under $EXP_DIR ==="
    exit 0
fi
echo "FAIL: no stage output dirs under $EXP_DIR"
exit 1
RUN_SCRIPT

chmod +x /opt/run_ddmd.sh

export PATH=/opt/ddmd-env/bin:${PATH}
