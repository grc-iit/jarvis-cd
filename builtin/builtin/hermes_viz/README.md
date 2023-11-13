
This is the visualizer used for Hermes.

# Installation
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

# Usage

On the master node:
```
local_port=4000
remote_port=4000
ares_node=ares-comp-10
ssh -L ${local_port}:localhost:${remote_port} -t ares ssh -L ${remote_port}:localhost:${remote_port} "${ares_node}"
```

For spack installs:
```
spack load hermes
spack unload python
```

For manual installs:
```
spack load hermes_shm
module load hermes_run
spack unload python
```

On your personal machine:
```

```
