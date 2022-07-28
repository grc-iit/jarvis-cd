#DAOS

## Install
```bash
jarvis daos install --sys=centos8
jarvis daos install --sys=centos7
jarvis daos install --sys=leap15
jarvis daos install --sys=ubuntu20
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
jarvis daos scaffold default
jarvis daos init
jarvis daos start
```