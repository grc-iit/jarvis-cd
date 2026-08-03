#!/usr/bin/env python3
"""Run and record one five-stage BioBB molecular-dynamics setup cell."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import time
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

RESULT_SCHEMA = "jarvis.biobb-md-setup-result.v1"
RESULT_NAME = "biobb-result.json"


@dataclass(frozen=True, slots=True)
class RunConfiguration:
    """Closed scientific configuration for one BioBB MD-setup execution."""

    input_pdb: Path
    output_dir: Path
    force_field: str
    water_type: str
    box_type: str
    distance_to_molecule: float
    ignore_input_hydrogens: bool
    merge_chains: bool
    gmx: str


@dataclass(frozen=True, slots=True)
class BioBBFunctions:
    """Loaded BioBB entrypoints used by the package-owned driver."""

    fix_side_chain: Callable[..., Any]
    pdb2gmx: Callable[..., Any]
    editconf: Callable[..., Any]
    solvate: Callable[..., Any]


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest of one bounded file."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _require_file(path: Path, description: str) -> None:
    """Require one nonempty regular, non-symlink stage product."""

    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError(f"BioBB did not produce {description}: {path.name}")


def _pdb_atom_count(path: Path) -> int:
    """Count coordinate records in a PDB file."""

    with path.open("r", encoding="utf-8", errors="replace") as stream:
        return sum(line.startswith(("ATOM  ", "HETATM")) for line in stream)


def _gro_metrics(path: Path) -> dict[str, Any]:
    """Read the atom count and periodic-box volume from a GROMACS GRO file."""

    lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    if len(lines) < 3:
        raise RuntimeError(f"GRO output is truncated: {path.name}")
    try:
        atom_count = int(lines[1].strip())
        values = [float(value) for value in lines[-1].split()]
    except ValueError as exc:
        raise RuntimeError(f"GRO output is malformed: {path.name}") from exc
    if atom_count <= 0 or len(lines) < atom_count + 3 or len(values) not in {3, 9}:
        raise RuntimeError(f"GRO output is malformed: {path.name}")
    if len(values) == 3:
        volume = values[0] * values[1] * values[2]
    else:
        first = (values[0], values[3], values[4])
        second = (values[5], values[1], values[6])
        third = (values[7], values[8], values[2])
        volume = abs(
            first[0] * (second[1] * third[2] - second[2] * third[1])
            - first[1] * (second[0] * third[2] - second[2] * third[0])
            + first[2] * (second[0] * third[1] - second[1] * third[0])
        )
    if not math.isfinite(volume) or volume <= 0:
        raise RuntimeError(f"GRO output has an invalid box: {path.name}")
    return {
        "atom_count": atom_count,
        "box_values_nm": values,
        "box_volume_nm3": volume,
    }


def _topology_molecule_counts(path: Path) -> dict[str, int]:
    """Read the closed ``[ molecules ]`` table from a topology ZIP archive."""

    counts: dict[str, int] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            candidates = sorted(
                name
                for name in archive.namelist()
                if not name.endswith("/") and name.casefold().endswith(".top")
            )
            if not candidates:
                raise RuntimeError("topology archive has no .top member")
            text = archive.read(candidates[0]).decode("utf-8", errors="strict")
    except (OSError, UnicodeError, zipfile.BadZipFile, KeyError) as exc:
        raise RuntimeError(f"cannot read topology archive: {path.name}") from exc
    in_molecules = False
    for raw_line in text.splitlines():
        line = raw_line.split(";", maxsplit=1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            in_molecules = line[1:-1].strip().casefold() == "molecules"
            continue
        if not in_molecules:
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        try:
            count = int(fields[-1])
        except ValueError as exc:
            raise RuntimeError("topology molecules table is malformed") from exc
        if count < 0:
            raise RuntimeError("topology molecule count cannot be negative")
        name = fields[0]
        counts[name] = counts.get(name, 0) + count
    if not counts:
        raise RuntimeError("topology archive has no molecule counts")
    return counts


def _load_functions() -> BioBBFunctions:
    """Import BioBB lazily so package inspection needs no scientific runtime."""

    from biobb_gromacs.gromacs.editconf import (  # pyright: ignore[reportMissingImports]
        editconf,
    )
    from biobb_gromacs.gromacs.pdb2gmx import (  # pyright: ignore[reportMissingImports]
        pdb2gmx,
    )
    from biobb_gromacs.gromacs.solvate import (  # pyright: ignore[reportMissingImports]
        solvate,
    )
    from biobb_model.model.fix_side_chain import (  # pyright: ignore[reportMissingImports]
        fix_side_chain,
    )

    return BioBBFunctions(fix_side_chain, pdb2gmx, editconf, solvate)


def _invoke(name: str, function: Callable[..., Any], **kwargs: Any) -> dict[str, Any]:
    """Execute one BioBB block and return a closed successful stage record."""

    started = time.monotonic()
    return_code = function(**kwargs)
    if isinstance(return_code, int) and not isinstance(return_code, bool):
        if return_code != 0:
            raise RuntimeError(f"BioBB stage {name} returned {return_code}")
    return {
        "name": name,
        "status": "completed",
        "elapsed_seconds": round(time.monotonic() - started, 6),
    }


def _artifact(root: Path, path: Path, *, format_name: str) -> dict[str, Any]:
    """Describe one exact output relative to the owned result root."""

    resolved_root = root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_relative_to(resolved_root):
        raise RuntimeError(f"BioBB output escaped its owned root: {path}")
    size = resolved.stat().st_size
    if not resolved.is_file() or size <= 0:
        raise RuntimeError(f"BioBB output is missing: {path.name}")
    return {
        "path": resolved.relative_to(resolved_root).as_posix(),
        "format": format_name,
        "size_bytes": size,
        "sha256": _sha256(resolved),
    }


def run(
    config: RunConfiguration,
    *,
    functions: BioBBFunctions | None = None,
) -> int:
    """Execute the BioBB workflow and write a machine-readable result document."""

    root = config.output_dir.resolve(strict=True)
    input_path = config.input_pdb.resolve(strict=True)
    if (
        config.input_pdb.is_symlink()
        or not input_path.is_file()
        or not input_path.is_relative_to(root)
    ):
        raise ValueError("input PDB must be a regular file in the output directory")
    loaded = functions or _load_functions()
    fixed = root / "fixed.pdb"
    processed = root / "processed.gro"
    processed_topology = root / "processed_topology.zip"
    boxed = root / "boxed.gro"
    solvated = root / "solvated.gro"
    solvated_topology = root / "solvated_topology.zip"
    stages: list[dict[str, Any]] = [
        {"name": "stage_input", "status": "completed", "elapsed_seconds": 0.0}
    ]
    output_specs: list[tuple[Path, str]] = []
    status = "completed"
    error: dict[str, str] | None = None
    started = time.monotonic()
    try:
        stages.append(
            _invoke(
                "fix_side_chain",
                loaded.fix_side_chain,
                input_pdb_path=str(input_path),
                output_pdb_path=str(fixed),
            )
        )
        _require_file(fixed, "side-chain-repaired PDB")
        output_specs.append((fixed, "pdb"))
        stages.append(
            _invoke(
                "pdb2gmx",
                loaded.pdb2gmx,
                input_pdb_path=str(fixed),
                output_gro_path=str(processed),
                output_top_zip_path=str(processed_topology),
                properties={
                    "force_field": config.force_field,
                    "water_type": config.water_type,
                    "ignh": config.ignore_input_hydrogens,
                    "merge": config.merge_chains,
                    "gmx_path": config.gmx,
                },
            )
        )
        _require_file(processed, "pdb2gmx coordinates")
        _require_file(processed_topology, "pdb2gmx topology")
        output_specs.extend(
            ((processed, "gromacs-gro"), (processed_topology, "gromacs-topology-zip"))
        )
        stages.append(
            _invoke(
                "editconf",
                loaded.editconf,
                input_gro_path=str(processed),
                output_gro_path=str(boxed),
                properties={
                    "box_type": config.box_type,
                    "distance_to_molecule": config.distance_to_molecule,
                    "center_molecule": True,
                    "gmx_path": config.gmx,
                },
            )
        )
        _require_file(boxed, "boxed coordinates")
        output_specs.append((boxed, "gromacs-gro"))
        stages.append(
            _invoke(
                "solvate",
                loaded.solvate,
                input_solute_gro_path=str(boxed),
                output_gro_path=str(solvated),
                input_top_zip_path=str(processed_topology),
                output_top_zip_path=str(solvated_topology),
                properties={"gmx_path": config.gmx},
            )
        )
        _require_file(solvated, "solvated coordinates")
        _require_file(solvated_topology, "solvated topology")
        output_specs.extend(
            ((solvated, "gromacs-gro"), (solvated_topology, "gromacs-topology-zip"))
        )
    except Exception as exc:
        status = "failed"
        error = {"type": type(exc).__name__, "message": str(exc)[:2048]}
        stages.append(
            {
                "name": "workflow",
                "status": "failed",
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "error": error,
            }
        )

    metrics: dict[str, Any] = {"input_pdb_atom_count": _pdb_atom_count(input_path)}
    if status == "completed":
        try:
            boxed_metrics = _gro_metrics(boxed)
            solvated_metrics = _gro_metrics(solvated)
            molecule_counts = _topology_molecule_counts(solvated_topology)
            metrics.update(
                {
                    "boxed_atom_count": boxed_metrics["atom_count"],
                    "boxed_volume_nm3": boxed_metrics["box_volume_nm3"],
                    "solvated_atom_count": solvated_metrics["atom_count"],
                    "solvated_volume_nm3": solvated_metrics["box_volume_nm3"],
                    "molecule_counts": molecule_counts,
                    "solvent_molecule_count": molecule_counts.get(
                        "SOL", molecule_counts.get("WAT")
                    ),
                }
            )
        except Exception as exc:
            status = "failed"
            error = {"type": type(exc).__name__, "message": str(exc)[:2048]}
            stages.append(
                {
                    "name": "validate_outputs",
                    "status": "failed",
                    "elapsed_seconds": round(time.monotonic() - started, 6),
                    "error": error,
                }
            )
    artifacts = [
        _artifact(root, path, format_name=format_name)
        for path, format_name in output_specs
        if path.exists() and not path.is_symlink() and path.is_file()
    ]
    result: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA,
        "status": status,
        "return_code": 0 if status == "completed" else 1,
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "parameters": {
            "force_field": config.force_field,
            "water_type": config.water_type,
            "box_type": config.box_type,
            "distance_to_molecule": config.distance_to_molecule,
            "ignore_input_hydrogens": config.ignore_input_hydrogens,
            "merge_chains": config.merge_chains,
        },
        "input": {
            "path": input_path.relative_to(root).as_posix(),
            "size_bytes": input_path.stat().st_size,
            "sha256": _sha256(input_path),
        },
        "stages": stages,
        "metrics": metrics,
        "artifacts": artifacts,
    }
    if error is not None:
        result["error"] = error
    (root / RESULT_NAME).write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return int(result["return_code"])


def _parse_args(argv: Sequence[str] | None = None) -> RunConfiguration:
    """Parse one closed command-line execution request."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_pdb", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--force-field", default="amber99sb-ildn")
    parser.add_argument("--water-type", default="tip3p")
    parser.add_argument("--box-type", default="cubic")
    parser.add_argument("--distance-to-molecule", default=1.0, type=float)
    parser.add_argument("--ignore-input-hydrogens", action="store_true")
    parser.add_argument("--merge-chains", action="store_true")
    parser.add_argument("--gmx", default="gmx")
    args = parser.parse_args(argv)
    return RunConfiguration(
        input_pdb=args.input_pdb,
        output_dir=args.output_dir,
        force_field=args.force_field,
        water_type=args.water_type,
        box_type=args.box_type,
        distance_to_molecule=args.distance_to_molecule,
        ignore_input_hydrogens=args.ignore_input_hydrogens,
        merge_chains=args.merge_chains,
        gmx=args.gmx,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command-line BioBB MD-setup cell."""

    config = _parse_args(argv)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    root = config.output_dir.resolve(strict=True)
    source = config.input_pdb.resolve(strict=True)
    if not source.is_relative_to(root):
        if config.input_pdb.is_symlink() or not source.is_file():
            raise ValueError("input PDB must be a regular file")
        staged = root / "input.pdb"
        if staged.exists() or staged.is_symlink():
            raise ValueError("staged input already exists")
        shutil.copyfile(source, staged, follow_symlinks=False)
        os.chmod(staged, 0o600)
        config = replace(config, input_pdb=staged, output_dir=root)
    return run(config)


if __name__ == "__main__":
    raise SystemExit(main())
