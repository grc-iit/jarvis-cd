
This is the visualizer used for Hermes.

# 1. Installation
```bash
# git clone https://github.com/JaimeCernuda/HermesViz
scspkg create HermesViz
cd $(scspkg pkg src HermesViz)
git clone https://github.com/JaimeCernuda/hermes -b visualizer
scspkg env prepend HermesViz HERMES_VIZ_ROOT "${PWD}/hermes/visualizer"
scspkg env prepend HermesViz PATH "${PWD}/hermes/visualizer"
scspkg env prepend HermesViz PYTHONPATH "${PWD}/hermes/visualizer"
module load HermesViz
python3 -m pip install flask
# NOTE(llogan): flask depends on click2, which may conflict with coverage-lcov installed by jarvis-util
# Just unintall coverage-lcov
python3 -m pip install -r hermes/visualizer/requirments.txt
```

# 2. Usage

## 2.1. Master Node
```
local_port=5001
remote_port=5001
ares_node=ares-comp-18
ssh -L ${local_port}:localhost:${remote_port} -fN ${ares_node}

local_port=4000
remote_port=4000
ares_node=ares-comp-25
ssh -L ${local_port}:localhost:${remote_port} -fN ${ares_node}
```

## 2.2. Compute Node

For spack installs:
```
spack load hermes
spack unload python
jarvis pipeline create hermes_viz
jarvis pipeline append hemres_viz
```

For manual installs:
```
spack load hermes_shm
module load hermes_run
spack unload python
```

## 2.3. Personal Machine
```
local_port=4000
remote_port=4000
ares_node=llogan@ares.cs.iit.edu
ssh -L ${local_port}:localhost:${remote_port} -fN ${ares_node}

local_port=5001
remote_port=5001
ares_node=llogan@ares.cs.iit.edu
ssh -L ${local_port}:localhost:${remote_port} -fN ${ares_node}
```

Locate process spawned by ssh -L
```
lsof -i :5001
```