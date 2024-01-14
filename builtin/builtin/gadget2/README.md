Gray-Scott is a 3D 7-Point stencil code

# Installation

```bash
spack install hdf5@1.14.1 gsl@2.1 fftw@3.3.8
git clone https://github.com/lukemartinlogan/gadget2.git
cd gadget2
export GADGET2_PATH=${PWD}
```

# Create environment

```bash
spack load hdf5@1.14.1 gsl@2.1 fftw@3.3.8
jarvis env build gadget +GADGET2_PATH
```
