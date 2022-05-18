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
jarvis [launcher] [operation] --conf /path/to/config
#Initialize the directories/configuration files required before launching server processes
jarvis lustre init --conf config.ini
#Starts an already-initialized service
jarvis lustre start --conf config.ini
#Calls init + start
jarvis lustre setup --conf config.ini
#Stops a service that has been started (but data is still kept)
jarvis lustre stop --conf config.ini
#Destroys all data stored by the service
jarvis lustre clean --conf config.ini
#Calls stop + clean
jarvis lustre destroy --conf config.ini
#Calls Destroy + Initialize + Start
jarvis lustre reset --conf config.ini
```

## Deploy Orangefs

```bash
# sample ini can be found and overriden at launchers/orangefs/default.ini
jarvis orangefs start --conf config.ini
```

## Undeploy Orangefs
```bash
jarvis orangefs stop --conf config.ini
```
