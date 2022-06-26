# Jarvis-CD

Jarvis CD is a continuous deployment software.

## 1. Dependencies

Jarvis requires the following:
1. Python 3.6+
2. Spack
3. scs-repo

dependencies.sh installs python.  
jarvis-bootstrap installs spack, scs-repo, and jarvis. 

## 2. Install Jarvis (Locally)

The following commands will install jarvis locally
```bash
cd jarvis_cd
PREFIX=${HOME} bash dependencies.sh
source ~/.bashrc
python3 bin/jarvis-bootstrap scaffold local
python3 bin/jarvis-bootstrap deps install
```

To customize the installation of dependencies, modify the conf.yaml produced by the scaffold command.

```yaml
jarvis_cd:
  repo: https://github.com/lukemartinlogan/jarvis-cd.installer
  branch: develop
  commit: null
  path: ${HOME}/jarvis-cd
spack:
  repo: https://github.com/spack/spack.installer
  branch: releases/v0.18
  commit: null
scs_repo:
  repo: https://github.com/lukemartinlogan/scs-repo.installer
  name: scs-repo
  branch: master
  commit: null
```

## 3. Basic Commands

```bash
jarvis [launcher] [operation] --conf /path/to/conf
#Create directory where configuration data should be stored
cd ${HOME}
mkdir daos_example
cd daos_example
#Create the jarvis configuration file
jarvis daos scaffold
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
jarvis daos scaffold --dir ${HOME}/daos_example
jarvis daos init --dir ${HOME}/daos_example
...
``` 

## 4. Installing a Storage System using Spack

We have developed various spack scripts for installing storage systems.
```bash
spack install daos
```

## 5. Installing a Storage System in a New Machine

Jarvis can be used to deploy a storage system in a new machine.
However, this requires setting up SSH and installing a few dependencies on each node.
Here, we show how to use Jarvis in Chameleon Cloud.

### 5.1. Install Jarvis-CD Locally

Initially, you are on your local machine, and you want to SSH into Chameleon. To do this,
install Jarvis locally on your machine using the steps above in Sections 1-2.

### 5.2. Connect to Chameleon Head Node

The following command will create a YAML file (conf.yaml) in the directory cc:
```bash
mkdir cc
cd cc
jarvis-bootstrap scaffold remote
```

Modify conf.yaml to reflect your allocation and SSH keys:
```yaml
username: cc
ssh_host: the IP of the head node
ssh_port: 22
ssh_keys:
  primary:
    key: scs_chameleon_pass
    key_dir: ${HOME}/.ssh
  github:
    key_name: id_rsa
    key_dir: ${HOME}/.ssh
```

After this, run the following command to do the following:
* Make sure the Chameleon head node is added to your known_hosts file
* Install your public key on the head node
* Install your private key on the head node (only if you need to clone private github repos)
* Ensure SSH directories and keys have proper permissions
```
jarvis-bootstrap setup_ssh
```

Connect to Chameleon using the following command:
```
jarvis-bootstrap ssh
```

### 5.3. Install Jarvis-CD on the Head Node

Install jarvis on the head node using the steps above in Sections 1-2.

### 5.4 Setup SSH between the Head Node and All Others

```bash
mkdir cc
cd cc
jarvis-bootstrap scaffold remote
touch hosts.txt
```

Create a "hostfile" which contains a list of all ip addresses to connect to:
```text
[ip-addr-1]
[ip-addr-2]
...
```

Modify conf.yaml again to reflect your allocation and SSH keys:
```yaml
username: cc
ssh_hosts: hostfile.txt
ssh_port: 22
ssh_keys:
  primary:
    key: scs_chameleon_pass
    key_dir: ${HOME}/.ssh
  github:
    key_name: id_rsa
    key_dir: ${HOME}/.ssh
```

This will setup SSH to all nodes in the "hostfile"
```bash
jarvis-bootstrap setup_ssh
```

### 5.5. Install Jarvis-CD and its Dependencies on All Other Nodes

The following commands will install/update/uninstall Jarvis on all machines listed in hostfile.txt.
```
jarvis-bootstrap deps install
jarvis-bootstrap deps update
jarvis-bootstrap deps uninstall 
```

### 5.6. Install Storage System using Spack

The following command is a distributed wrapper around spack, and will perform a parallel install
of DAOS in all nodes specified in hostfile.txt.
```
jarvis-pssh "spack install daos"
```