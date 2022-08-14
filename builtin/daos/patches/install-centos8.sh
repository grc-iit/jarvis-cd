#Install berkeley-db
scspkg create berkeley-db
cd `scspkg pkg-src berkeley-db`
wget https://download.oracle.com/berkeley-db/db-18.1.40.tar.gz
tar -zxf db-18.1.40.tar.gz
cd db-18.1.40/build_unix
../dist/configure --disable-static --enable-dbm --enable-compat185 --with-repmgr-ssl=no --prefix=`scspkg pkg-root berkeley-db`
make -j8
make install
module load berkeley-db

#Install orangefs
scspkg create orangefs
cd `scspkg pkg-src orangefs`
wget http://download.orangefs.org/current/source/orangefs-2.9.8.tar.gz
tar -xvzf orangefs-2.9.8.tar.gz
cd orangefs-v.2.9.8
./configure --prefix=`scspkg pkg-root orangefs` --with-kernel=/lib/modules/`uname -r`/build --enable-shared --enable-fuse
make -j8
make install
scspkg add-deps orangefs berkeley-db
module load orangefs

#Install mpich
scspkg create orangefs-mpich
scspkg add-deps orangefs-mpich orangefs
cd `scspkg pkg-src orangefs-mpich`
wget http://www.mpich.org/static/downloads/3.2/mpich-3.2.tar.gz --no-check-certificate
tar -xzf mpich-3.2.tar.gz
cd mpich-3.2
./configure --prefix=`scspkg pkg-root orangefs-mpich` --enable-fast=O3 --enable-romio --enable-shared --with-pvfs2=`scspkg pkg-root orangefs` --with-file-system=pvfs2
make -j8
make install
module load orangefs-mpich

#Install DAOS
python3 -m pip install defusedxml distro junit_xml pyxattr tabulate scons pyyaml pyelftools
scspkg create daos-2.1
scspkg add-deps daos-2.1 orangefs-mpich
cd `scspkg pkg-src daos-2.1`
git clone --recurse-submodules https://github.com/daos-stack/daos.git -b v2.1.104-tb
cd daos
jarvis ssh exec "./utils/scripts/install-el8.sh" --sudo
scons PREFIX=`scspkg pkg-root daos-2.1` --config=force --build-deps=yes install
module load daos-2.1

#Install IO500 (orangefs)
scspkg create orangefs-io500
scspkg add-deps orangefs-io500 orangefs orangefs-mpich
cd `scspkg pkg-src orangefs-io500`
git clone https://github.com/IO500/io500.git -b io500-isc22
cd io500
./prepare.sh
make
cp -r bin `scspkg pkg-root orangefs-io500`
cp io500 `scspkg pkg-root orangefs-io500`/bin

#Install IO500 (DAOS)

## IO500-libcircle
scspkg create libcircle
cd `scspkg pkg-src libcircle`
wget https://github.com/hpc/libcircle/releases/download/v0.3/libcircle-0.3.0.tar.gz
tar -zxf libcircle-0.3.0.tar.gz
cd libcircle-0.3.0
cat << 'EOF' > libcircle_opt.patch
--- a/libcircle/token.c
+++ b/libcircle/token.c
@@ -1307,6 +1307,12 @@

         LOG(CIRCLE_LOG_DBG, "Sending work request to %d...", source);

+        /* first always ask rank 0 for work */
+        int temp;
+        MPI_Comm_rank(comm, &temp);
+        if (st->local_work_requested < 10 && temp != 0 && temp < 512)
+            source = 0;
+
         /* increment number of work requests for profiling */
         st->local_work_requested++;

EOF
patch -p1 < libcircle_opt.patch
./configure --prefix=`scspkg pkg-root libcircle`
make install
module load libcircle

## IO500 - lwgrp
scspkg create lwgrp
cd `scspkg pkg-src lwgrp`
wget https://github.com/llnl/lwgrp/releases/download/v1.0.2/lwgrp-1.0.2.tar.gz
tar -zxf lwgrp-1.0.2.tar.gz
cd lwgrp-1.0.2
./configure --prefix=`scspkg pkg-root lwgrp`
make install
module load lwgrp

## IO500 - dtcmp
scspkg create dtcmp
cd `scspkg pkg-src dtcmp`
wget https://github.com/llnl/dtcmp/releases/download/v1.1.0/dtcmp-1.1.0.tar.gz
tar -zxf dtcmp-1.1.0.tar.gz
cd dtcmp-1.1.0
./configure --prefix=`scspkg pkg-root dtcmp` --with-lwgrp=`scspkg pkg-root lwgrp`
make install
module load dtcmp

## IO500 -- LibArchive
jarvis ssh exec "yum -y install libarchive-devel bzip2-devel" --sudo -C $JARVIS_ROOT

## IO500 -- MFU
scspkg create mfu
scspkg add-deps mfu libcircle lwgrp dtcmp daos-2.1
cd `scspkg pkg-src mfu`
git clone https://github.com/mchaarawi/mpifileutils -b pfind_integration
cd mpifileutils
mkdir build
cd build
CFLAGS="-I `scspkg pkg-root daos-2.1`/include" \
LDFLAGS="-L `scspkg pkg-root daos-2.1`/lib64 -luuid -ldaos -ldfs -ldaos_common -lgurt -lpthread" \
cmake ../ \
  -DENABLE_XATTRS=OFF \
  -DWITH_DTCMP_PREFIX=`scspkg pkg-root dtcmp` \
  -DWITH_LibCircle_PREFIX=`scspkg pkg-root libcircle` \
  -DCMAKE_INSTALL_PREFIX=`scspkg pkg-root mfu`
make install
module load mfu

## IO500
scspkg create daos-io500
scspkg add-deps daos-2.1 mfu orangefs-mpich
export MY_DAOS_INSTALL_PATH=`scspkg pkg-root daos-2.1`
export MY_MFU_INSTALL_PATH=`scspkg pkg-root mfu`
export MY_MFU_SOURCE_PATH=`scspkg pkg-root mfu`/src/mpifileutils
export MY_MFU_BUILD_PATH=`scspkg pkg-root mfu`/src/mpifileutils/build
export MY_IO500_PATH=`scspkg pkg-root daos-io500`/io500

cd `scspkg pkg-src daos-io500`
git clone https://github.com/IO500/io500.git -b io500-isc22
cd io500
git apply ${JARVIS_ROOT}/builtin/daos/patches/io500.patch

./prepare.sh
cp -r bin `scspkg pkg-root daos-io500`
cp io500 `scspkg pkg-root daos-io500`/bin
