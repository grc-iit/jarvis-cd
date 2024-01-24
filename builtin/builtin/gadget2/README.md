GADGET is a freely available code for cosmological N-body/SPH simulations on massively parallel computers with distributed memory. GADGET uses an explicit communication model that is implemented with the standardized MPI communication interface. The code can be run on essentially all supercomputer systems presently in use, including clusters of workstations or individual PCs.

GADGET computes gravitational forces with a hierarchical tree algorithm (optionally in combination with a particle-mesh scheme for long-range gravitational forces) and represents fluids by means of smoothed particle hydrodynamics (SPH). The code can be used for studies of isolated systems, or for simulations that include the cosmological expansion of space, both with or without periodic boundary conditions. In all these types of simulations, GADGET follows the evolution of a self-gravitating collisionless N-body system, and allows gas dynamics to be optionally included. Both the force computation and the time stepping of GADGET are fully adaptive, with a dynamic range which is, in principle, unlimited.

https://wwwmpa.mpa-garching.mpg.de/gadget/

# Installation

```bash
spack install hdf5@1.14.1 gsl@2.1 fftw@2
scspkg create gadget2
cd $(scspkg pkg src gadget2)
git clone https://github.com/lukemartinlogan/gadget2.git
export GADGET2_PATH=$(scspkg pkg src gadget2)/gadget2
export FFTW_PATH=$(spack find --format "{PREFIX}" fftw@2)
```

# Create environment

```bash
spack load hdf5@1.14.1 gsl@2.1 fftw@2
jarvis env build gadget2 +GADGET2_PATH +FFTW_PATH
```

# Gassphere Pipeline

```bash
jarvis pipeline create gassphere
jarvis pipeline env copy gadget2
jarvis pipeline append gadget2
jarvis pkg configure gadget2 \
test_case=gadget2 \
out=${HOME}/gadget2
jarvis pipeline run
```

# NGenIC Pipeline

```bash
jarvis pipeline create gassphere
jarvis pipeline env copy gadget2
jarvis pipeline append gadget2
jarvis pkg configure gadget2 \
test_case=gassphere-ngen \
out=${HOME}/gadget2 \
ic=hello
jarvis pipeline run
```