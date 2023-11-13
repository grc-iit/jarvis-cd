
This is the visualizer used for Hermes.

# Installation
```bash
# git clone https://github.com/JaimeCernuda/HermesViz
scspkg create HermesViz
cd $(scspkg pkg src HermesViz)
git clone https://github.com/lukemartinlogan/HermesViz.git
scspkg prepend env HERMES_VIZ_ROOT "${PWD}/HermesViz"
scspkg prepend env PATH "${PWD}/HermesViz"
scspkg prepend env PYTHONPATH "${PWD}/HermesViz"
module load HermesViz

```

# Usage

On the master node:
```
local_port=4000
remote_port=4000
ares_node=ares-comp-10
ssh -L ${local_port}:localhost:${remote_port} -t ares ssh -L ${remote_port}:localhost:${remote_port} "${ares_node}"
```

On your personal machine:
```
```
