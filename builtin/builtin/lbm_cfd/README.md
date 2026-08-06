# LBM-CFD

`builtin.lbm_cfd` is a maintained native application package for the pinned
LBM-CFD-3D source study. It does not install or evaluate the Coeus system.

The required `input_bundle` follows `jarvis.package-input-bundle.v1` and carries
the license, build recipe, documentation, and complete source tree. JARVIS
verifies every member, copies it into an execution-owned working directory,
builds once with the loaded MPI C++ compiler, and runs the same bounded wake at
D3Q15, D3Q19, and D3Q27. Each run uses four MPI ranks by default, a 64 x 32 x 32
domain, and 501 time steps.

Success requires three distinct, finite, nonempty final VTK vorticity fields.
The package publishes those fields, input provenance, and a closed comparison
of the mean, RMS, maximum, and RMS ratio to D3Q19. Failed builds or simulations
publish no finalized study products. The runtime provider hint is `openmpi`.
