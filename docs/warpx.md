# WarpX Package

`builtin.warpx` launches one three-dimensional WarpX particle-in-cell simulation under
JARVIS-owned MPI execution. The package supports an installed example, one caller-supplied
input file, or one selected member of a digest-verified package input bundle.

## Input profiles

| Profile | Settings | Behavior |
|---|---|---|
| Installed example | `example` | Runs the selected example from the installed WarpX tree and applies the configured bounds. |
| Single input | `inputs` | Copies the regular input file into the package output root and launches it there. |
| Input bundle | `input_bundle`, optional `input_path` | Verifies and stages every manifest member, then launches the selected member. An empty `input_path` selects the manifest entrypoint. |

`inputs` and `input_bundle` are mutually exclusive. `input_path` is accepted only with an
input bundle, must be a confined relative path, and must name a manifest-declared regular
file. The special `custom` example requires one of the two caller-input forms.

By default, JARVIS does not replace scientific values in caller inputs. Set
`override_input_parameters=true` to apply `max_step`, `n_cell`, `plot_int`, and the
package-owned plotfile prefix as WarpX command-line overrides. Installed examples always
use those bounds.

## Settings

| Setting | Default | Meaning |
|---|---:|---|
| `nprocs` | `2` | MPI process count. |
| `ppn` | `2` | MPI processes per node. |
| `inputs` | empty | Single caller-supplied WarpX input file. |
| `input_bundle` | empty | `jarvis.package-input-bundle.v1` archive. |
| `input_path` | empty | Selected manifest member, or the entrypoint when empty. |
| `example` | `laser_acceleration` | Installed example used without caller input. |
| `override_input_parameters` | `false` | Authorize command-line overrides for caller input. |
| `max_step` | `50` | Step bound for examples or authorized overrides. |
| `n_cell` | `64 64 128` | Three positive base-grid dimensions. |
| `out` | `run` | Output root; relative paths resolve below package shared storage. |
| `plot_int` | `10` | Plot interval; `-1` disables plots. |
| `use_gpu` | `false` | Select the CUDA container build and launch profile. |
| `cuda_arch` | `80` | CUDA architecture used by the container build. |
| `base_image` | `sci-hpc-base` | Base image used by package-owned container builds. |

The native profile resolves a supported WarpX executable through the activated pipeline
`PATH`. Its deployment contract advertises a provider-neutral `warpx` Spack query. Sites or
callers may constrain the actual installation through ordinary JARVIS environment and
installation configuration.

## Outputs and completion

The default `out=run` resolves to a dedicated directory below the execution package's
durable shared directory. Relative
diagnostic paths in supplied WarpX inputs therefore remain execution-owned. A nonzero exit
from any MPI rank fails the package.

At process completion, the package performs a bounded walk without following links. It
reports the owned WarpX result tree plus high-confidence plotfile, checkpoint, ADIOS2, HDF5,
JSON, CSV, and text diagnostics. Exact staged input files are excluded from the output
manifest. A nonzero process exit or a discovery-bound truncation marks reported products
incomplete.

## Example bundle pipeline

```yaml
name: plasma-study
pkgs:
  - pkg_type: builtin.warpx
    pkg_name: density_high
    input_bundle: /data/langmuir-cases.tar
    input_path: density-high/inputs
    nprocs: 4
    ppn: 4
    out: run
```

Multiple package instances can select different manifest members without introducing a
study-specific WarpX package. Downstream analysis remains a separate application or
pipeline step.
