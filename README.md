# Jarvis-CD
Jarvis-CD is a unified platform for deploying various applications, including
storage systems and benchmarks. Many applications have complex configuration
spaces and are difficult to deploy across different machines.

We provide a builtin repo which contains various applications to deploy.
We refer to applications as "jarivs pkgs" which can be connected to form
"deployment pipelines".

## Installation

Get the GRC spack repo (if you haven't already):
```bash
git clone https://github.com/grc-iit/grc-repo
spack repo add grc-repo
```

Install jarvis-cd:
```bash
spack external find python
spack install py-jarvis-cd
```

Spack packages must be loaded to use them.
You'll have to do this for each new terminal.
```bash
spack load py-jarvis-cd
```

## Building the Jarvis Configuration

### Bootstrapping for a single-node machine

You may be trying to test things on just a single node. 

In this case, run:
```bash
jarvis bootstrap from local
```

### Bootstrapping from a specific machine

Jarvis has been pre-configured on some machines. To bootstrap from
one of them, run the following:

```bash
jarvis bootstrap from ares
```

NOTE: Jarvis must be installed from the compute nodes in Ares, NOT the master node. This is because we store configuration data in /mnt/ssd by default, which is only on compute nodes. We do not store data in /tmp since it will be eventually destroyed.

To check the set of available machines to bootstrap from, run:
```bash
jarvis bootstrap list
```

### Creating a new configuration

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

In addition to initializing the jarvis conf file, you must also build a resource graph.

#### Set the active Hostfile

The hostfile contains the set of nodes that the pipeline will run over.
This is structured the same way as a traditional MPI hostfile.

An example hostfile:

```txt
ares-comp-20
ares-comp-[21-25]
```

To set the active hostfile, run:

```bash
jarvis hostfile set /path/to/hostfile
```

Note that every time you change the hostfile, you will need to update the
pipeline. Jarvis does not automatically detect changes to this file.

```bash
jarvis ppl update
```

#### Building the Resource Graph

The resource graph is a snapshot of your systems network and storage.
Many packages depend on it for their configurations. The Hermes I/O system, for example,
uses this to identify valid networks and buffering locations.
```bash
jarvis rg build
```

## Manual Installation (Mainly Devs)

### Jarvis-Util
Jarvis-CD depends on jarvis-util. jarvis-util contains functions to execute
binaries in python and collect their output.

```bash
git clone https://github.com/grc-iit/jarvis-util.git
cd jarvis-util
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
```

### Scspkg

Scspkg is a tool for building modulefiles using a CLI. It's not strictly
necessary for Jarvis to function, but many of the readmes use it to provide
structure to manual installations.

```bash
git clone https://github.com/grc-iit/scspkg.git
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
echo "module use \`scspkg module dir\`" >> ~/.bashrc
```

The wiki for scspkg is [here](https://github.com/grc-iit/scspkg.git).

### Jarvis-CD

```bash
cd /path/to/jarvis-cd
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
```

### Net Test

Network test tool for identifying valid networks.
```bash
spack install chi-nettest
```
