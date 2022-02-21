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

## Deploy Orangefs

```bash
# sample ini can be found and overriden at launchers/orangefs/default.ini
jarvis orangefs start --conf config.ini
```

## Undeploy Orangefs
```bash
jarvis orangefs stop --conf config.ini
```
