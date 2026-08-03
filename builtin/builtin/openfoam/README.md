# OpenFOAM

`builtin.openfoam` retains the legacy script and container launcher and adds a
maintained native supplied-study profile. The supplied profile is selected only
when `input_bundle` is nonempty.

The archive must implement `jarvis.package-input-bundle.v1` and contain the
three digest-bound `angle-00`, `angle-06`, and `angle-12` NACA 0012 case trees.
JARVIS stages the immutable archive into an execution-owned working directory,
runs `decomposePar`, `simpleFoam -parallel`, and `reconstructPar` for each case,
and fails on any nonzero process exit. The profile requires exactly four MPI
ranks because the supplied decomposition is fixed at four subdomains.

The successful profile publishes the closed incidence comparison, input
provenance, and all three native force-coefficient series. Failed runs publish
no finalized study products. The runtime may be supplied by the operator or
resolved through the `openfoam@2312` provider hint.

The legacy `script_location`, `script`, `base_image`, and container settings
remain available when `input_bundle` is empty.
