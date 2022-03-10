# Jarvis-CD

Jarvis CD is a continuous deployment software.

## Dependencies

### SCSPKG

```
git clone https://github.com/lukemartinlogan/scspkg.git
cd /path/to/scspkg
bash install.sh
source ~/.bashrc
```

## Install Jarvis

This install script will install jarvis for the particular user
(it is not system-wide).

```bash
cd /path/to/jarvis_cd
bash install.sh
source ~/.bashrc
```

## General Commands

```bash
jarvis [launcher] [operation] --conf /path/to/config
#Initialize a service (e.g., may all
jarvis lustre init --conf config.ini
#Starts an already-initialized service
jarvis lustre start --conf config.ini
#Calls init + start
jarvis lustre setup --conf config.ini
jarvis lustre stop --conf config.ini
jarvis lustre clean --conf config.ini
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
