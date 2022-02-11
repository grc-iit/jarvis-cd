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

### Environment Modules

```
scspkg create modules
cd `scspkg pkg-src modules`
curl -LJO https://github.com/cea-hpc/modules/releases/download/v4.7.1/modules-4.7.1.tar.gz
tar xfz modules-4.7.1.tar.gz
cd modules-4.7.1
./configure --prefix=`scspkg pkg-root modules`
make
make install
echo "source \`scspkg pkg-root modules\`/init/bash" >> ~/.bashrc
echo "module use \`scspkg modules-path\`" >> ~/.bashrc
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
python jarvis orangefs start config.ini
```

## Undeploy Orangefs
```bash
python jarvis orangefs stop config.ini
```
