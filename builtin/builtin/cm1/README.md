CM1 is a simulation code.

# Dependencies

```bash
spack install intel-oneapi-compilers
spack load intel-oneapi-compilers
spack compilers add
spack install h5z-zfp%intel
```

# Compiling / Installing

```bash
git clone git@github.com:lukemartinlogan/cm1r19.8-LOFS.git
cd cm1r19.8-LOFS
# COREX * COREY is the number of cores you intend to use on the system
# They do not need to be 2 and 2 here, but this is how our configurations are compiled for now
COREX=2 COREY=2 bash buildCM1-spack.sh
export PATH=${PWD}/run:${PATH}
export CM1_PATH=${PWD}
```

# Usage

```bash
jarvis pipeline create cm1
jarvis pipeline append cm1 corex=2 corey=2
```