
MegaMmap is a software distributed shared memory.

# 5.1. Install

```
spack install hermes@dev-1.1
spack load hermes@dev-1.1
scspkg create mega_mmap
cd `scspkg pkg src mega_mmap`
git clone https://github.com/lukemartinlogan/mega_mmap.git
cd mega_mmap
mkdir build
cd build
cmake ../ -DCMAKE_INSTALL_PREFIX=`scspkg pkg src mega_mmap`
make -j8
cd ../

export MM_PATH=${PWD}
scspkg env set mega_mmap MM_PATH ${MM_PATH}
scspkg env prepend mega_mmap PATH ${MM_PATH}/build/bin
scspkg env prepend mega_mmap PATH ${MM_PATH}/cmake-build-debug/bin
scspkg env prepend mega_mmap LD_LIBRARY_PATH ${MM_PATH}/build/bin
scspkg env prepend mega_mmap LD_LIBRARY_PATH ${MM_PATH}/cmake-build-debug/bin
```

# 5.2. Run KMeans Sort

```
spack load --only dependencies hermes@dev-1.1
module load hermes_run
module load mega_mmap
module load spark
jarvis pipeline create mm_sort
jarvis pipeline env build +MM_PATH
jarvis pipeline append mm_sort path=${HOME}/mm_data
jarvis pipeline run
```
