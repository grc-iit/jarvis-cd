# Jarvis-CD

Jarvis CD is a continuous deployment software. 

## Install Jarvis

This install script will install jarvis for the particular user
(it is not system-wide).

```bash
cd /path/to/jarvis_cd
bash install.sh
source ~/.bashrc
```

## Deploy Orangefs

```bash
# sample ini can be found and overriden at launchers/orangefs/default.ini
python jarvis orangefs start config.ini
```

## Undeploy Orangefs
```bash
python jarvis orangefs stop config.ini
```

