# Jarvis-CD

Jarvis CD is a continuous deployment software.

## 1. Dependencies

Jarvis requires the following:
1. Python 3.6+
2. Spack

dependencies.sh installs python.  . 

## 2. Installing Jarvis Locally or on a PFS

The following commands will install jarvis locally.  
```bash
cd jarvis-cd
bash dependencies.sh
source ~/.bashrc
python3 -m pip install -e . --user -r requirements.txt
jarvis deps scaffold local
jarvis deps local-install all
source ~/.bashrc
```

To customize the installation of dependencies, modify the conf.yaml produced by the scaffold command.
```yaml
JARVIS_INSTANCES:
  per_node: /mnt/${USER}/.jarvis
  shared: /home/${USER}/.jarvis
jarvis_cd:
  repo: https://github.com/lukemartinlogan/jarvis-cd.git
  branch: development
  commit: null
  path: ${HOME}/jarvis-cd
spack:
  repo: https://github.com/spack/spack.git
  branch: releases/v0.18
  commit: null
scs_repo:
  repo: https://github.com/lukemartinlogan/scs-repo.git
  name: scs-repo
  branch: master
  commit: null
```
JARVIS_INSTANCES should point to two directories:
* **per-node**: a directory which is common across all nodes, but not located on a shared filesystem
* **shared**: a directory stored on a shared filesystem across all hosts (this is optional)

Certain information, such as mount points, need to be stored per-node, whereas
configuration files can be stored in a shared directory.

Specifying a shared directory
is optional, but specifying a per-node directory is not.

## 3. Basic Commands

```bash
jarvis [launcher] [operation] -C [scaffold] -I [pkg-id]
#Create the jarvis launcher package instance (pkg-id: daos-example) 
jarvis daos create daos-example default
#Cd into the current launcher's directory
cd `jarvis base cd daos-example`
#Initialize the directories/conf files required for launching server processes
jarvis daos init
#Starts an already-initialized service
jarvis daos start
#Stops a service that has been started (but data is still kept)
jarvis daos stop
#Destroys all data stored by the service
jarvis daos clean
#Calls init + start
jarvis daos setup
#Calls stop + clean
jarvis daos destroy
#Calls Destroy + Initialize + Start
jarvis daos reset
```

To run these commands outside of the scaffold directory:
```bash
jarvis daos scaffold -C ${HOME}/daos_example
jarvis daos init -C ${HOME}/daos_example
...
``` 

## 4. Installing a Storage System using Spack

We have developed various spack scripts for installing storage systems.
```bash
spack install daos
```

## 5. Installing Jarvis in Parallel to Setup Shared Storage

Jarvis can be used to deploy a storage system in a new machine.
However, this requires setting up SSH and installing a few dependencies on each node.
Here, we show how to use Jarvis in Chameleon Cloud.

### 5.1. Install Jarvis-CD Locally

Initially, you are on your local machine, and you want to SSH into Chameleon. To do this,
install Jarvis locally on your machine using the steps above in Sections 1-2.

### 5.2. Connect to Head Node Using Jarvis

The following command will create a YAML file (jarvis_conf.yaml) in the directory cc:
```bash
mkdir cc
cd cc
jarvis ssh scaffold remote
```

Modify jarvis_conf.yaml to reflect your allocation and SSH keys:
```yaml
HOSTS: ${SCAFFOLD}/hostfile.txt
SSH:
  primary:
    username: cc
    port: 22
    key: scs_chameleon_pass
    key_dir: ${HOME}/.ssh
    dst_key_dir: /home/cc/.ssh
  github:
    hostname: 'github.com'
    key: id_rsa
    key_dir: ${HOME}/.ssh
    dst_key_dir: /home/cc/.ssh
```

After this, run the following command to do the following:
* Make sure the Chameleon head node is added to your known_hosts file
* Install your public key on the head node
* Install your private key on the head node (only if you need to clone private github repos)
* Ensure SSH directories and keys have proper permissions
* Modify your ${HOME}/.ssh/config file to remember this host
```
jarvis ssh setup
```

Connect to Chameleon using the following command:
```
jarvis ssh shell 1
```

### 5.3. Install Jarvis-CD on the Head Node

Next, install jarvis on the head node. Instead of using the "local" scaffold option, we use the
"remote" scaffold option. This is because Jarvis will have to be installed on all other nodes
in parallel after the head node is set up.

```bash
cd jarvis-cd
bash dependencies.sh
source ~/.bashrc
python3 -m pip install -e . --user -r requirements.txt
jarvis deps scaffold remote
touch hostfile.txt
jarvis deps local-install all
```

Edit "hostfile" to have a line-by-line list of all host ip addresses:
```text
[ip-addr-1]
[ip-addr-2]
...
```

Modify jarvis_conf.yaml to reflect your allocation and SSH keys:
```yaml
HOSTS: ${SCAFFOLD}/hostfile.txt
SSH:
  primary:
    username: cc
    port: 22
    key: scs_chameleon_pass
    key_dir: ${HOME}/.ssh
    dst_key_dir: /home/cc/.ssh
  github:
    hostname: 'github.com'
    key: id_rsa
    key_dir: ${HOME}/.ssh
    dst_key_dir: /home/cc/.ssh
```

This will bootstrap ssh between the host node and all other nodes.
```bash
jarvis ssh setup
```

### 5.5. Install Jarvis-CD and its Dependencies on All Other Nodes

The following commands will install/update/uninstall Jarvis on all machines listed in hostfile.txt.
```
jarvis deps install all
jarvis deps update all
jarvis deps uninstall all
```

NOTE: don't run uninstall all if you don't want to remove your spack installation!!!

### 5.6. Install Storage System using Spack

The following command is a distributed wrapper around spack, and will perform a parallel install
of DAOS in all nodes specified in hostfile.txt.
```
jarvis ssh exec "spack install daos"
```