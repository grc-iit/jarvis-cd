# Gadget2

The JARVIS Gadget2 package runs distributed-memory N-body and SPH simulations.
The agent-facing profile accepts a complete, digest-verified JARVIS input bundle
instead of selecting one of the repository's stock demonstrations. This lets a
caller supply a scientific parameter file, initial conditions, and any related
support files without giving the package authority over arbitrary host paths.

The Gadget2 project is documented at
<https://wwwmpa.mpa-garching.mpg.de/gadget/>.

## Native runtime

Provide an MPI-enabled `Gadget2` or `gadget2` executable through `PATH`. The
runtime must be built with the FFTW 2, GSL, MPI, and optional HDF5 dependencies
required by its selected compile-time options. JARVIS probes the executable and
reports the runtime readiness through the package deployment contract.

The benchmark and scientific profile do not install software or select an
arbitrary source repository at runtime. Site operators prepare and pin the
runtime independently.

## Scientific input bundle

The `input_bundle` must be a JARVIS input-bundle archive. Its manifest declares
every file, its SHA-256 digest, size, role, and one entrypoint. The entrypoint is
normally a `.param` file. `parameter_path` can select another declared `.param`
member, but cannot name an undeclared or escaping path.

For example, a bundle can contain:

```text
jarvis-input-manifest.json
galaxy/
  galaxy.param
  ICs/
    galaxy_littleendian.dat
```

The parameter file can use paths relative to its own directory, such as:

```text
InitCondFile  ICs/galaxy_littleendian.dat
OutputDir     output/
EnergyFile   energy.txt
InfoFile     info.txt
SnapshotFileBase snapshot
```

JARVIS verifies the archive, extracts it into package-owned storage, copies the
verified tree into the configured output root, and runs Gadget2 from the
parameter file's directory. The caller-owned archive is never modified.

## Pipeline example

```bash
jarvis ppl create galaxy-study
jarvis ppl append builtin.gadget2 galaxy \
  input_bundle=/data/inputs/galaxy-study.tar \
  parameter_path=galaxy/galaxy.param \
  out=galaxy-run \
  nprocs=8 \
  ppn=4
jarvis ppl run
```

Relative `out` paths resolve beneath the package shared directory. The package
reports the bounded result tree, energy and runtime tables, snapshot sets, and
restart sets through JARVIS artifact semantics. A zero process exit is not
sufficient for finalization: required energy, information, and snapshot products
must also exist.

## Retained stock profile

Existing pipelines that configure `gadget2_path`, `test_case`, and `output`
continue to use the historical stock-case profile. Those controls remain hidden
from agent-facing metadata so a generated scientific study cannot silently fall
back to the default `gassphere` example. New scientific workflows should use an
input bundle.
