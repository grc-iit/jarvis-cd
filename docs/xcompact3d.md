# Xcompact3D Package

`builtin.xcompact3d` launches one caller-defined incompressible-flow simulation under
JARVIS-owned MPI execution. It deliberately does not encode a particular scientific study,
axis comparison, or benchmark. Multiple package instances can select distinct inputs and a
separate analysis step can compare their outputs.

## Input profiles

| Profile | Settings | Behavior |
|---|---|---|
| Single input | `inputs` | Copies one bounded regular `.i3d` file into package-owned storage and launches it there. |
| Input bundle | `input_bundle`, optional `input_path` | Verifies and stages every manifest member, then launches the selected member. An empty `input_path` selects the manifest entrypoint. |

`inputs` and `input_bundle` are mutually exclusive, and one is required. `input_path` is
accepted only with an input bundle. It must be a confined relative path that exactly names a
manifest member. A bundle is the appropriate profile when an input depends on files such as
`adios2_config.xml`, geometry, restart data, or other solver resources.

## Settings

| Setting | Default | Meaning |
|---|---:|---|
| `nprocs` | `4` | MPI process count. |
| `ppn` | `4` | MPI processes per node. |
| `inputs` | empty | Single caller-supplied Xcompact3D input file. |
| `input_bundle` | empty | `jarvis.package-input-bundle.v1` archive. |
| `input_path` | empty | Selected manifest member, or the entrypoint when empty. |
| `out` | `run` | Output root; relative paths resolve below package shared storage. |
| `base_image` | `ubuntu:24.04` | Base image for the optional package-owned container build. |

The native profile resolves `xcompact3d` or `incompact3d` from the pipeline environment.
Runtime installation remains an operator or installation-manager responsibility; an agent
does not provide executable paths.

## Outputs and completion

The selected input and all support files are copied into an execution-owned working tree.
The solver stream is written to `xcompact3d.log` beside the selected input. A nonzero result
from any MPI rank fails the package.

At process completion, the package performs a bounded output walk without following links.
It reports the complete owned result tree and stable high-confidence products for the solver
log, checkpoint, restart metadata, field-data collection, statistics collection, and ADIOS2
BP collections. Exact staged inputs are excluded. A nonzero process exit or bounded-walk
truncation marks discovered products incomplete.

## Multi-input example

```yaml
name: channel-orientation-study
pkgs:
  - pkg_type: builtin.xcompact3d
    pkg_name: channel_x
    input_bundle: /data/channel-cases.tar
    input_path: channel/input_test_x.i3d
    nprocs: 4
    ppn: 4
    out: channel-x
  - pkg_type: builtin.xcompact3d
    pkg_name: channel_z
    input_bundle: /data/channel-cases.tar
    input_path: channel/input_test_z.i3d
    nprocs: 4
    ppn: 4
    out: channel-z
```

This pipeline runs two ordinary application instances. Any scientific comparison belongs in
an explicit downstream package or in the caller's validation layer rather than in the
Xcompact3D launcher.
