# Jarvis-CD

Jarvis CD is a continuous deployment software.

## 1. Install Jarvis (Locally)

#### For Regular Users
```{bash}
python3 -m pip install git+https://github.com/scs-lab/jarvis-cd.git --user
```

#### For Developers

```{bash}
cd jarvis-cd
git checkout development
python3 -m pip install -e . --user
```

## 2. Install Jarvis (Distributed)

To install jarvis in a multi-node environment, we include a script called dspack.
dspack can be used to install SSH keys, spack, and then jarivs.
Below we show an example of how to deploy jarvis in Chameleon Cloud.

### 1.1. Bootstrap SSH

If you're planning on cloning private github repos, you need to install SSH
keys. To do this, do the following:

```bash
#Install SSH keys: localhost -> head node
dspack setup_ssh --key scs_chameleon_pass --user cc --host ${HOST_IP} --priv_key True
#SSH into head node
ssh cc@${HOST_IP} -i ~/.ssh/scs_chameleon_pass
#Install SSH keys: head node -> all others
dspack setup_ssh --key scs_chameleon_pass --hosts hostfile.txt  --priv_key True
```

### 1.2. Installing spack and Jarvis

```
dspack jarvis install --key scs_chameleon_pass --hosts hostfile.txt
dspack spack install --key scs_chameleon_pass --hosts hostfile.txt
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