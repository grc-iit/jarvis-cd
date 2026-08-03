# Montage

`builtin.montage` runs NASA/IPAC Montage mosaics through two explicit profiles.

## Offline three-band profile

Supply `j_bundle`, `h_bundle`, and `k_bundle` together. Each value is a
digest-verified `jarvis.package-input-bundle.v1` archive whose manifest:

- selects a `mosaic_header` entrypoint;
- declares one or more `fits_source` files under a single directory; and
- contains no undeclared files or links.

The package stages each bundle into the execution-owned shared directory,
runs `mExec` and `mExamine` independently for J, H, and K, renders a J/H/K PNG
with `mViewer`, and validates every product before writing
`montage-result.json`. It finalizes exactly these durable artifacts:

- `montage-j.fits`, `montage-h.fits`, and `montage-k.fits`;
- `montage-jhk.png`; and
- `montage-result.json` using schema `jarvis.montage-result.v1`.

Every command failure is propagated. The profile performs no runtime Internet
discovery or acquisition and supports agent-free replay from the same bundle
digests.

## Legacy archive profile

When all three bundles are empty, Montage retains the earlier single-band
archive workflow. `region`, `band`, and `size` control the archive request. A
native execution uses the package-owned `run_mosaic.sh`; a container execution
uses the built image. If data are not already staged, this profile may access
IRSA at runtime and is therefore not equivalent to the offline profile.

Relative `out` paths resolve below the JARVIS package shared directory. The
default `.` keeps outputs in the durable execution-owned package root.
