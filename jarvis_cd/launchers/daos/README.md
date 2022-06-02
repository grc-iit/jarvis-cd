#DAOS

## Install
```bash
scspkg create daos
cd `scspkg pkg-src daos`
git clone --recurse-submodules -b release/2.0 https://github.com/daos-stack/daos.git
cd daos

#EL (including CentOS)
sudo ./utils/scripts/install-ubuntu20.sh
#OpenSUSE
sudo ./utils/scripts/install-leap15.sh
#Ubuntu
./utils/scripts/install-ubuntu20.sh

scons prefix=`scspkg pkg-root daos` --config=force --build-deps=yes install
scspkg set-env daos DAOS_ROOT `scspkg pkg-root daos`
module load daos
```

## Deploy

```bash
${DAOS_ROOT}/lib64/daos/certgen/gen_certificates.sh
sudo ${DAOS_ROOT}/bin/dmg network scan -o conf.yaml
sudo ${DAOS_ROOT}/bin/daos_server -o conf.yaml 
```