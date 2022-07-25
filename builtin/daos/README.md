#DAOS

## Install
```bash
spack install daos sys=centos8
spack install daos sys=centos7
spack install daos sys=leap15
spack install daos sys=ubuntu20
```

### CentOS 8 Prep:
TODO, add patch to automate this
```
sudo dnf -y --disablerepo '*' --enablerepo=extras swap centos-linux-repos centos-stream-repos
sudo dnf -y distro-sync
sudo yum install epel-release
sudo yum config-manager --set-enabled powertools
pip3 install scons pyelftools
```

## Deploy

### Notes

* Daos server requires an odd number of hosts.

### Step 1: Create a hostfile

```bash
```

### Step 2: Deploy

```bash
SCAFFOLD=`pwd`
jarvis daos scaffold
jarvis daos init
jarvis daos start
```

```bash
#Start DAOS server (per-node)
sudo ${DAOS_ROOT}/bin/daos_server start -o ${SCAFFOLD}/daos_server.yaml -d ${SCAFFOLD}
#Start DAOS agents (per-node)
sudo ${DAOS_ROOT}/bin/daos_agent start -o ${SCAFFOLD}/daos_agent.yaml -s ${SCAFFOLD}
#Format DAOS storage (per-node, init)
sudo ${DAOS_ROOT}/bin/dmg storage format -o ${SCAFFOLD}/daos_control.yaml 
#Check if DAOS has started (per-node)
sudo ${DAOS_ROOT}/bin/dmg -o ${SCAFFOLD}/daos_control.yaml system query -v
#Check status (per-node)
cat "/tmp/daos_agent.log"
```

```bash
${DAOS_ROOT}/bin/dmg -o daos_control.yaml pool create -z 100G --label io500_pool
${DAOS_ROOT}/bin/dmg -o daos_control.yaml pool create -z 500M --label io500_pool
daos container create --type POSIX --pool io500_pool
```

```bash
mpssh "dfuse --pool=$DAOS_POOL --container=$DAOS_CONT -m $DAOS_FUSE"
```