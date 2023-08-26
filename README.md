Jarvis-cd is a unified platform for deploying various applications, including
storage systems and benchmarks. Many applications have complex configuration
spaces and are difficult to deploy across different machines.

We provide a builtin repo which contains various applications to deploy.
We refer to applications as "jarivs pkgs" which can be connected to form
"deployment pipelines".

Check out our wiki [here](https://github.com/scs-lab/jarvis-cd/wiki) 
for more details. 

## Dependencies

Jarvis-CD depends on jarvis-util. jarvis-util contains functions to execute
binaries in python and collect their output.

```bash
git clone https://github.com/scs-lab/jarvis-util.git
cd jarvis-util
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
```

## Installation

### 1. Install the jarvis-cd python package
```bash
cd /path/to/jarvis-cd
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
```

### 2. Generate the jarvis configuration file.
```bash
jarvis init [CONFIG_DIR] [PRIVATE_DIR] [SHARED_DIR (optional)]
```

* **CONFIG_DIR:** A directory where jarvis metadata for pkgs and pipelines
are stored. This directory can be anywhere that the current user can access.
This can be stored local to a particular machine or on a PFS.
* **PRIVATE_DIR:** A directory which is common across all machines, but
stores data locally to the machine. Some jarvis pkgs require certain data to
be stored per-machine. OrangeFS is an example. /tmp for example is typically
private.
* **SHARED_DIR:** A directory which is common across all machines, where
each machine has the same view of data in the directory. Not all jarvis
pkgs require a SHARED_DIR. The SHARED_DIR could be on a PFS, for example.