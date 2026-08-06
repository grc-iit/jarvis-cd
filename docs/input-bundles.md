# Package Input Bundles

JARVIS package input bundles carry a caller-supplied multi-file application input as one
digest-verifiable regular file. They are intended for applications whose entrypoint refers
to neighboring support files, such as a LAMMPS input script and potential, an OpenFOAM case
tree, or a WRF namelist and sounding.

The bundle is a tar-compatible archive containing only regular files. It must include
`jarvis-input-manifest.json` at its root:

```json
{
  "schema_version": "jarvis.package-input-bundle.v1",
  "entrypoint": "in.copper",
  "files": [
    {
      "path": "in.copper",
      "role": "lammps_input",
      "sha256": "<64 lowercase hexadecimal characters>",
      "size_bytes": 1280
    },
    {
      "path": "Cu.eam",
      "role": "potential",
      "sha256": "<64 lowercase hexadecimal characters>",
      "size_bytes": 36588
    }
  ]
}
```

The manifest is closed: it has exactly `schema_version`, `entrypoint`, and `files`.
Each file has exactly `path`, `role`, `sha256`, and `size_bytes`. Paths use relative POSIX
syntax and cannot contain `.` or `..` components. The entrypoint must name one declared
file. Roles are package-defined semantic labels and may repeat.

JARVIS rejects links, devices, duplicate paths, undeclared archive members, size or digest
mismatches, unsupported schemas, and configured safety-bound violations. A verified archive
is extracted atomically below a content-addressed package directory. Applications that need
a mutable working tree use `stage_input_bundle`; it copies every verified payload without
overwriting existing paths.

Packages expose a bundle through an ordinary configuration setting with a
`jarvis.configuration-input-binding.v1` local regular-file descriptor. This lets clients
materialize the archive itself before the package verifies and expands its contents.

## Python API

```python
from pathlib import Path

from jarvis_cd.input_bundle import extract_input_bundle, stage_input_bundle

materialized = extract_input_bundle(
    "/local-or-materialized/inputs.tar",
    Path(package.shared_dir) / "input-bundles",
)
entrypoint = stage_input_bundle(materialized, package_output_directory)
```

`extract_input_bundle` is idempotent for an unchanged archive and unchanged materialized
tree. `stage_input_bundle` is intentionally not idempotent: an existing destination file is
a collision and fails closed rather than reusing or overwriting a prior run.
