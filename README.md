Jarvis-cd is a unified platform for deploying various applications, including
storage systems and benchmarks. Many applications have complex configuration
spaces and are difficult to deploy across different machines.

We provide a builtin repo which contains various applications to deploy.
We refer to applications as "jarivs pkgs" which can be connected to form
"deployment pipelines".

# 0.1 Dependencies

Jarvis-CD depends on jarvis-util. jarvis-util contains functions to execute
binaries in python and collect their output.

```bash
git clone https://github.com/scs-lab/jarvis-util.git
cd jarvis-util
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
```

# 0.2. Installation

## 0.2.1. Install the jarvis-cd python package
```bash
cd /path/to/jarvis-cd
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
```

## 0.2. Configuring Jarvis

## 0.2.1. Bootstrapping from a specific machine

Jarivs has been pre-configured on some machines. To bootstrap from
one of them, run the following:

```bash
jarvis bootstrap from ares
```

NOTE: Jarvis must be installed from the compute nodes in Ares, NOT the master node. This is because we store configuration data in /mnt/ssd by default, which is only on compute nodes. We do not store data in /tmp since it will be eventually destroyed.

To check the set of available machines to bootstrap from, run:
```bash
jarvis boostrap list
```

## 0.2.2. Creating a new configuration

A configuration can be generated as follows:
```bash
jarvis init [CONFIG_DIR] [PRIVATE_DIR] [SHARED_DIR (optional)]
```

* **CONFIG_DIR:** A directory where jarvis metadata for pkgs and pipelines
are stored. This directory can be anywhere that the current user can access.
* **PRIVATE_DIR:** A directory which is common across all machines, but
stores data locally to the machine. Some jarvis pkgs require certain data to
be stored per-machine. OrangeFS is an example.
* **SHARED_DIR:** A directory which is common across all machines, where
each machine has the same view of data in the directory. Most jarvis pkgs
require this, but on machines without a global filesystem (e.g., Chameleon Cloud),
this parameter can be set later.

For a personal machine, these directories can be the same directory.
