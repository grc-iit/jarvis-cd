# Jarvis-CD

Jarvis CD is a continuous deployment software.

## Dependencies

### SCSPKG

```
git clone https://github.com/lukemartinlogan/scspkg.git
cd scspkg
bash install.sh
source ~/.bashrc
```

## Install Jarvis

This install script will install jarvis for the particular user
(it is not system-wide).

```bash
cd jarvis-cd
bash install.sh
source ~/.bashrc
```

## Basic Commands

```bash
jarvis [launcher] [operation] --conf /path/to/conf
#Create directory where configuration data should be stored
cd ${HOME}
mkdir lustre_example
cd lustre_example
#Create the jarvis configuration file
jarvis lustre scaffold
#Initialize the directories/conf files required for launching server processes
jarvis lustre init
#Starts an already-initialized service
jarvis lustre start
#Stops a service that has been started (but data is still kept)
jarvis lustre stop
#Destroys all data stored by the service
jarvis lustre clean
#Calls init + start
jarvis lustre setup
#Calls stop + clean
jarvis lustre destroy
#Calls Destroy + Initialize + Start
jarvis lustre reset
```

To run these commands outside of the scaffold directory:
```bash
jarvis lustre scaffold --dir ${HOME}/lustre_example
jarvis lustre init --dir ${HOME}/lustre_example
...
```

## Setup SSH

Jarvis includes a small script for installing SSH keys to a set of nodes.
This is useful for bootstrapping Chameleon instances, for example.

Usage:
```
Must provide either --hosts or --host
usage: install_keys [-h] [--key name] [--port port] [--user username] [--hosts hostfile.txt] [--host ip_addr]
                    [--src_key_dir path] [--dst_key_dir path] [--priv_key bool]

Bootstrap SSH

optional arguments:
  -h, --help            show this help message and exit
  --key name            The name of the public/private key pair within the src_key_dir
  --port port           The port number for ssh
  --user username       The username for ssh
  --hosts hostfile.txt  The set of all hosts to bootstrap
  --host ip_addr        The single host to bootstrap
  --src_key_dir path    Where to search for key pair on the current host
  --dst_key_dir path    Where to install key pair on destination hosts
  --priv_key bool       Whether or not to install private key on hosts
```
Must specify at least on of --host or --hosts.

Example:
```
install_keys --key chameleon --user cc --host [host-ip]
```