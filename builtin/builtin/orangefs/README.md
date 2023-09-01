In this section we go over how to install and deploy OrangeFS.

# 5.1. Install OrangeFS (Linux)

OrangeFS is located [on this website](http://www.orangefs.org/?gclid=CjwKCAjwgqejBhBAEiwAuWHioDo2uu8wel6WhiFqoBDgXMiVXc7nrykeE3sf3mIfDFVEt0_7SwRN8RoCdRYQAvD_BwE)
The official OrangeFS github is [here](https://github.com/waltligon/orangefs/releases/tag/v.2.9.8).

```bash
scspkg create orangefs
cd `scspkg pkg src orangefs`
wget https://github.com/waltligon/orangefs/archive/refs/tags/v.2.9.8.tar.gz
tar -xvzf v.2.9.8.tar.gz
cd orangefs-v.2.9.8
autoreconf -iv
./configure --prefix=`scspkg pkg root orangefs` --enable-shared --enable-fuse
make -j8
make install
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

# 5.4. Setup Environment

```bash
```

# 5.3. Generating the Config File

```bash
cd ${HOME}
mkdir orangefs
cd orangefs
pvfs2-genconfig
```
