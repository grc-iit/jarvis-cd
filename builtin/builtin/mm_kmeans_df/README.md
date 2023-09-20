
MegaMmap is a software distributed shared memory.

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
jarvis pipeline create kmeans_df
jarvis pipeline env build
jarvis pipeline append mm_kmeans_df path=${HOME}/mm_data df_size=256g window_size=64m nprocs=256
jarvis pipeline run
```
