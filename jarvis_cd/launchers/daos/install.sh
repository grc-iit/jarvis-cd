#!/bin/bash

### MPIFILEUTILS


### IO500
scspkg create io500
cd `scspkg pkg-src io500`
scspkg add-deps io500 mpifileutils
scspkg set-env io500 MY_DAOS_INSTALL_PATH `scspkg pkg-root daos`
scspkg set-env io500 MY_MFU_INSTALL_PATH `scspkg pkg-root mpifileutils`
git clone https://github.com/IO500/io500.git -b io500-isc21
cd io500
module load io500

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
./prepare.sh
