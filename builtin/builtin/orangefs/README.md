In this section we go over how to install and deploy OrangeFS.
NOTE: if running in Ares, OrangeFS is already installed, so skip
to section 5.3.

# 5.1. Install Various Dependencies

```
sudo apt update
sudo apt install -y fuse
sudo apt install gcc flex bison libssl-dev libdb-dev linux-headers-$(uname -r) perl make libldap2-dev libattr1-dev
```

# 5.1. Install OrangeFS (Linux)

OrangeFS is located [on this website](http://www.orangefs.org/?gclid=CjwKCAjwgqejBhBAEiwAuWHioDo2uu8wel6WhiFqoBDgXMiVXc7nrykeE3sf3mIfDFVEt0_7SwRN8RoCdRYQAvD_BwE)
The official OrangeFS github is [here](https://github.com/waltligon/orangefs/releases/tag/v.2.9.8).

```bash
scspkg create orangefs
cd `scspkg pkg src orangefs`
wget https://github.com/waltligon/orangefs/releases/download/v.2.10.0/orangefs-2.10.0.tar.gz
tar -xvzf orangefs-2.10.0.tar.gz
cd orangefs
./prepare
./configure --prefix=`scspkg pkg root orangefs` --enable-shared --enable-fuse
make -j8
make install
scspkg env prepend orangefs ORANGEFS_PATH `scspkg pkg root orangefs`
```

# 5.2. Using MPICH with OrangeFS

MPICH requires a special build when using OrangeFS. Apparantly it's for
performance, but it's a pain to have to go through the extra step.

```bash
scspkg create orangefs-mpich
cd `scspkg pkg src orangefs-mpich`
wget http://www.mpich.org/static/downloads/3.2/mpich-3.2.tar.gz --no-check-certificate
tar -xzf mpich-3.2.tar.gz
cd mpich-3.2
./configure --prefix=`scspkg pkg root orangefs-mpich` --enable-fast=O3 --enable-romio --enable-shared --with-pvfs2=`scspkg pkg root orangefs` --with-file-system=pvfs2
make -j8
make install
```

# 5.3. Creating a pipeline

In Ares:
```
module load orangefs
jarvis pipeline create orangefs
jarvis pipeline env build +ORANGEFS_PATH
jarvis pipeline append orangefs mount=${HOME}/orangefs_client +ares
```

In a machine where you have root access:
```
module load orangefs
jarvis pipeline create orangefs
jarvis pipeline env build +ORANGEFS_PATH
jarvis pipeline append orangefs mount=${HOME}/orangefs_client
```
