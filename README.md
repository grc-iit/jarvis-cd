# Jarvis-CD

Jarvis CD is a continuous deployment software.

## 1. Dependencies

Jarvis requires the following:
1. Python 3.6+
2. Spack

dependencies.sh installs python.  
jarvis-bootstrap installs spack, scs-repo, and jarvis. 

## 2. Installing Jarvis Locally or on a PFS

The following commands will install jarvis locally.  
```bash
cd jarvis-cd
PREFIX=${HOME} bash dependencies.sh
source ~/.bashrc
pip3 install -e . --user -r requirements.txt
jarvis deps scaffold local
jarvis deps local-install all
source ~/.bashrc
```

To customize the installation of dependencies, modify the conf.yaml produced by the scaffold command.
```yaml
JARVIS_SHARED: true
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
Note, JARVIS_SHARED should not be changed from the value provided by scaffold.

## 3. Basic Commands

```bash
jarvis [launcher] [operation] --conf /path/to/conf
#Create directory where configuration data should be stored
cd ${HOME}
mkdir daos_example
cd daos_example
#Create the jarvis configuration file (there are multiple to choose from)
jarvis daos scaffold []
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
  username: cc
  port: 22
  key: scs_chameleon_pass
  key_dir: ${HOME}/.ssh
  dst_key_dir: /home/cc/.ssh
ssh_keys:
  github:
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
PREFIX=${HOME} bash dependencies.sh
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
HOSTS: hostfile.txt
SSH:
  username: cc
  key: scs_chameleon_pass
  key_dir: ${HOME}/.ssh
ssh_keys:
  github:
    key: id_rsa
    key_dir: ${HOME}/.ssh
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