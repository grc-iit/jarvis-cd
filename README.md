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

## General Commands

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
