
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
ssh -L 4000:localhost:4000 -t ares
function ares-port-forward {
    # Usage: gdb-port-forward <node-number> <remote-port> [local-port]

    local node_number=$(printf "%02d" $1) # Ensures two digits for the node number
    local remote_port=$2
    local local_port=${3:-$2} # Use remote port as local port if no local port is specified

    if [[ -z $node_number || -z $remote_port ]]; then
        echo "Usage: gdb-port-forward <node-number> <remote-port> [local-port]"
        return 1
    fi

    # Establish SSH tunnel through ares to the specific node
    ssh -L ${local_port}:localhost:${remote_port} -t ares ssh -L ${remote_port}:localhost:${remote_port} "ares-comp-${node_number}"
}
```

On your personal machine:
```
```
