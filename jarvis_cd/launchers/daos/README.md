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
SCAFFOLD=`pwd`
#Generate security certificates (copy to all nodes)
${DAOS_ROOT}/lib64/daos/certgen/gen_certificates.sh ${SCAFFOLD}
#Start DAOS server (per-node)
sudo ${DAOS_ROOT}/bin/daos_server start -o ${SCAFFOLD}/daos_server.yaml -d ${SCAFFOLD}
#Start DAOS agents (per-node)
sudo ${DAOS_ROOT}/bin/daos_agent start -o ${SCAFFOLD}/daos_agent.yaml -s ${SCAFFOLD}
#Format DAOS storage (per-node)
sudo ${DAOS_ROOT}/bin/dmg storage format -o ${SCAFFOLD}/daos_control.yaml 
#Check if DAOS has started (per-node)
sudo ${DAOS_ROOT}/bin/dmg -o ${SCAFFOLD}/daos_control.yaml system query -v
#Check status (per-node)
cat "/tmp/daos_agent.log"
```

## IO500

### LibArchive
```bash
#Ubuntu
sudo apt install libarchive-dev libbz2-dev
#Red hat
sudo apt install libarchive-devel libbz2-devel
```

### LIBCIRCLE
```bash
scspkg create libcircle
cd `scspkg pkg-src libcircle` 
wget https://github.com/hpc/libcircle/releases/download/v0.3/libcircle-0.3.0.tar.gz
tar -zxf libcircle-0.3.0.tar.gz
cd libcircle-0.3.0
./configure --prefix=`scspkg pkg-root libcircle`
make -j8
make install
module load libcircle
```

### LWGRP
```bash
scspkg create lwgrp
cd `scspkg pkg-src lwgrp`
wget https://github.com/llnl/lwgrp/releases/download/v1.0.2/lwgrp-1.0.2.tar.gz
tar -zxf lwgrp-1.0.2.tar.gz
cd lwgrp-1.0.2
./configure --prefix=`scspkg pkg-root lwgrp`
make -j8
make install
module load lwgrp
```

### DTCMP
```bash
scspkg create dtcmp
scspkg add-deps lwgrp
cd `scspkg pkg-src dtcmp`
wget https://github.com/llnl/dtcmp/releases/download/v1.1.0/dtcmp-1.1.0.tar.gz
tar -zxf dtcmp-1.1.0.tar.gz
cd dtcmp-1.1.0
./configure --prefix=`scspkg pkg-root dtcmp` --with-lwgrp=`scspkg pkg-root lwgrp`
make -j8
make install
module load dtcmp
```

### MPIFILEUTILS
```bash
scspkg create mpifileutils
scspkg add-deps mpifileutils libcircle dtcmp daos
cd `scspkg pkg-src mpifileutils`
git clone https://github.com/mchaarawi/mpifileutils -b pfind_integration
cd mpifileutils
```

```
cat << EOF > cmake.patch
--- CMakeLists.txt	2022-06-03 00:05:20.147138999 +0000
+++ CMakeLists.txt	2022-06-03 00:05:47.251030826 +0000
@@ -1,6 +1,7 @@
 PROJECT(MFU)
 
 CMAKE_MINIMUM_REQUIRED(VERSION 3.1)
+link_libraries("-luuid -ldaos -ldfs -ldaos_common -lgurt -lpthread")
 
 IF(POLICY CMP0042)
   CMAKE_POLICY(SET CMP0042 NEW)
EOF
git apply cmake.patch
```

```
mkdir build
cd build
cmake ../ \
  -DENABLE_XATTRS=OFF \
  -DWITH_DTCMP_PREFIX=`scspkg pkg-root dtcmp` \
  -DWITH_LibCircle_PREFIX=`scspkg pkg-root libcircle` \
  -DCMAKE_INSTALL_PREFIX=`scspkg pkg-root mpifileutils`
make -j8 install
module load mpifileutils
```

### IO500
```bash
scspkg create io500
cd `scspkg pkg-src io500`
scspkg add-deps io500 mpifileutils
scspkg set-env io500 MY_DAOS_INSTALL_PATH `scspkg pkg-root daos`
scspkg set-env io500 MY_MFU_INSTALL_PATH `scspkg pkg-root mpifileutils`
git clone https://github.com/IO500/io500.git -b io500-isc21
cd io500
```

```bash  
cat << EOF > io500_prepare.patch
diff --git a/prepare.sh b/prepare.sh
index f8908d7..19d4aa6 100755
--- a/prepare.sh
+++ b/prepare.sh
@@ -8,7 +8,7 @@ echo It will output OK at the end if builds succeed
 echo
 
 IOR_HASH=0410a38e985e0862a9fd9abec017abffc4c5fc43
-PFIND_HASH=62c3a7e31
+PFIND_HASH=mfu_integration
 
 INSTALL_DIR=\$PWD
 BIN=\$INSTALL_DIR/bin
@@ -59,7 +59,7 @@ function get_ior {
 
 function get_pfind {
   echo "Preparing parallel find"
-  git_co https://github.com/VI4IO/pfind.git pfind \$PFIND_HASH
+  git_co https://github.com/mchaarawi/pfind pfind \$PFIND_HASH
 }
 
 function get_schema_tools {
@@ -73,7 +73,7 @@ function build_ior {
   pushd \$BUILD/ior
   ./bootstrap
   # Add here extra flags
-  ./configure --prefix=\$INSTALL_DIR
+  ./configure --prefix=\$INSTALL_DIR --with-daos=${MY_DAOS_INSTALL_PATH}
   cd src
   \$MAKE clean
   \$MAKE install
EOF
git apply io500_prepare.patch
```

```bash
cat << EOF > io500_Makefile.patch
diff --git a/Makefile b/Makefile
index 2975471..5dce307 100644
--- a/Makefile
+++ b/Makefile
@@ -1,10 +1,13 @@
 CC = mpicc
 CFLAGS += -std=gnu99 -Wall -Wempty-body -Werror -Wstrict-prototypes -Werror=maybe-uninitialized -Warray-bounds
+CFLAGS += -I${MY_DAOS_INSTALL_PATH}/include -I${MY_MFU_INSTALL_PATH}/include
 
 IORCFLAGS = \$(shell grep CFLAGS ./build/ior/src/build.conf | cut -d "=" -f 2-)
 CFLAGS += -g3 -lefence -I./include/ -I./src/ -I./build/pfind/src/ -I./build/ior/src/
 IORLIBS = \$(shell grep LDFLAGS ./build/ior/src/build.conf | cut -d "=" -f 2-)
 LDFLAGS += -lm \$(IORCFLAGS) \$(IORLIBS) # -lgpfs # may need some additional flags as provided to IOR
+LDFLAGS += -L${MY_DAOS_INSTALL_PATH}/lib64 -ldaos -ldaos_common -ldfs -lgurt -luuid
+LDFLAGS += -L${MY_MFU_INSTALL_PATH}/lib64 -lmfu_dfind -lmfu
 
 VERSION_GIT=\$(shell git describe --always --abbrev=12)
 VERSION_TREE=\$(shell git diff src | wc -l | sed -e 's/   *//g' -e 's/^0//' | sed "s/\([0-9]\)/-\1/")
EOF
git apply io500_Makefile.patch
```

https://daosio.atlassian.net/wiki/spaces/DC/pages/4874571083/IO-500+ISC21
```bash
./prepare.sh
```

```bash
${DAOS_ROOT}/bin/dmg -o daos_control.yaml pool create -z 100G --label io500_pool
${DAOS_ROOT}/bin/dmg -o daos_control.yaml pool create -z 500M --label io500_pool
daos container create --type POSIX --pool io500_pool
```

```bash
mpssh "dfuse --pool=$DAOS_POOL --container=$DAOS_CONT -m $DAOS_FUSE"
```