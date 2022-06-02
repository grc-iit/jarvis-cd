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
#Create the basic confuration files the user needs to edit in the current directory
jarvis lustre scaffold
#Initialize the directories/confuration files required before launching server processes
jarvis lustre init --conf conf.yaml
#Starts an already-initialized service
jarvis lustre start --conf conf.yaml
#Calls init + start
jarvis lustre setup --conf conf.yaml
#Stops a service that has been started (but data is still kept)
jarvis lustre stop --conf conf.yaml
#Destroys all data stored by the service
jarvis lustre clean --conf conf.yaml
#Calls stop + clean
jarvis lustre destroy --conf conf.yaml
#Calls Destroy + Initialize + Start
jarvis lustre reset --conf conf.yaml
```

## Deploy Orangefs

```bash
# sample ini can be found and overriden at launchers/orangefs/default.ini
jarvis orangefs start --conf conf.yaml
```

## Undeploy Orangefs
```bash
jarvis orangefs stop --conf conf.yaml
```
