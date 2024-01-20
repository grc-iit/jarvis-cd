GADGET is a freely available code for cosmological N-body/SPH simulations on massively parallel computers with distributed memory. GADGET uses an explicit communication model that is implemented with the standardized MPI communication interface. The code can be run on essentially all supercomputer systems presently in use, including clusters of workstations or individual PCs.

GADGET computes gravitational forces with a hierarchical tree algorithm (optionally in combination with a particle-mesh scheme for long-range gravitational forces) and represents fluids by means of smoothed particle hydrodynamics (SPH). The code can be used for studies of isolated systems, or for simulations that include the cosmological expansion of space, both with or without periodic boundary conditions. In all these types of simulations, GADGET follows the evolution of a self-gravitating collisionless N-body system, and allows gas dynamics to be optionally included. Both the force computation and the time stepping of GADGET are fully adaptive, with a dynamic range which is, in principle, unlimited.

https://wwwmpa.mpa-garching.mpg.de/gadget/

# Installation

Check the README for gadget2.

# Create Pipeline

```bash
jarvis pipeline create ngenic
jarvis pipeline env copy gadget2
jarvis pipeline append gadget2_df
jarvis pkg configure gadget2_df \
nparticles=100000 \
nprocs=4
jarvis pipeline run
```
