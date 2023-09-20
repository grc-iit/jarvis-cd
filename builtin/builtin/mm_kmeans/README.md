
MegaMmap is a software distributed shared memory. This is the KMeans
benchmark.

# 5.1. Install

```
spack install hermes@dev-1.1
spack load hermes@dev-1.1
scspkg create mega_mmap
cd `scspkg pkg src mega_mmap`
git clone https://github.com/lukemartinlogan/mega_mmap.git
cd mega_mmap
export MM_PATH=${PWD}
mkdir build
cd build
cmake ../ -DCMAKE_INSTALL_PREFIX=`scspkg pkg src mega_mmap`
make -j8

scspkg env set MM_PATH ${MM_PATH}
scspkg env prepend PATH ${MM_PATH}/build/bin
scspkg env prepend LD_LIBRARY_PATH ${MM_PATH}/build/bin
```

# 5.2. Run KMeans DF

```
module load mega_mmap
jarvis pipeline create kmeans
jarvis pipeline env build +MM_PATH +SPARK_SCRIPTS
jarvis pipeline append spark_cluster
jarvis pipeline append mm_kmeans path=${HOME}/mm_data memory=40g
jarvis pipeline run
```
