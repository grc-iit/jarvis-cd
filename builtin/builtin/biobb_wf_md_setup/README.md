# BioBB molecular-dynamics setup

`builtin.biobb_wf_md_setup` prepares one caller-supplied PDB structure with
BioExcel Building Blocks and GROMACS. The native profile performs five real
stages:

1. copy the immutable caller input into package-owned storage;
2. repair missing side chains with `biobb_model`;
3. construct coordinates and topology with `pdb2gmx`;
4. center the solute and construct the selected periodic box with `editconf`;
5. solvate the system and update its topology with `solvate`.

The package fails if any BioBB block, product check, or semantic output parser
fails. A successful process exit is the authoritative completion signal.

## Native runtime

Native execution requires:

- `python3` with `biobb-model` and `biobb-gromacs` importable; and
- `gmx` available through `PATH`.

The deployment descriptor reports these as separate runtime requirements. It
also provides provider-neutral hints for Python distributions and a `gromacs`
Spack spec. No container is required for the native profile.

The scientific configuration is:

| Parameter | Meaning | Default |
|---|---|---|
| `pdb_file` | Required caller-owned regular PDB input | none |
| `out` | Package-owned result directory | `run` |
| `force_field` | GROMACS force field passed to `pdb2gmx` | `amber99sb-ildn` |
| `water_type` | Water model passed to `pdb2gmx` | `tip3p` |
| `box_type` | `cubic`, `triclinic`, `dodecahedron`, or `octahedron` | `cubic` |
| `distance_to_molecule` | Solute-to-box clearance in nanometers | `1.0` |
| `ignore_input_hydrogens` | Reconstruct input hydrogens | `true` |
| `merge_chains` | Merge input chains into one molecule | `false` |

Example package configuration:

```yaml
- pkg_type: builtin.biobb_wf_md_setup
  pkg_name: lysozyme_dodecahedron
  pdb_file: /shared/inputs/1aki.pdb
  out: run
  force_field: amber99sb-ildn
  water_type: tip3p
  box_type: dodecahedron
  distance_to_molecule: 1.0
  ignore_input_hydrogens: true
  merge_chains: false
```

JARVIS copies `pdb_file` to `out/input.pdb` before execution. Existing files,
links, empty files, non-PDB inputs, and inputs larger than 32 MiB fail locally
before a scientific process starts.

## Outputs

The native workflow produces these exact files beneath `out`:

| File | Meaning |
|---|---|
| `biobb-result.json` | Closed result, parameters, stage states, metrics, and product hashes |
| `fixed.pdb` | Side-chain-repaired structure |
| `processed.gro` | Coordinates produced by `pdb2gmx` |
| `processed_topology.zip` | Initial GROMACS topology bundle |
| `boxed.gro` | Centered solute in the selected periodic box |
| `solvated.gro` | Solvated coordinates |
| `solvated_topology.zip` | Solvated topology bundle |

The result uses schema `jarvis.biobb-md-setup-result.v1`. It records the input
hash, scientific parameters, per-stage outcome, coordinate atom counts, box
volume, topology molecule counts, solvent count, and the size and SHA-256 hash
of every declared product. The artifact adapter checks those paths, sizes, and
hashes before finalizing them. A nonzero process or driver result leaves every
available product explicitly incomplete.

## Legacy container benchmarking

The prior container and Apptainer batch modes remain available for existing
storage experiments. Their replication, parallel scratch, OpenMP, MD extension,
and base-image controls are deliberately hidden from generic agent catalogs:
they describe a synthetic container benchmark, not the ordinary scientific
cell above. Native qualification and agent-facing use do not build or require
Docker images.
