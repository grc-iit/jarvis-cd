"""Tests for the package-owned BioBB MD-setup driver."""

from __future__ import annotations

import json
import sys
import zipfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load_driver() -> ModuleType:
    """Load the standalone driver without requiring BioBB at test time."""

    path = (
        Path(__file__).resolve().parents[3]
        / "builtin"
        / "builtin"
        / "biobb_wf_md_setup"
        / "run_md_setup.py"
    )
    name = "test_biobb_md_setup_driver_module"
    spec = spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load BioBB driver from {path}")
    module = module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


driver = _load_driver()


def _gro(path: Path, atom_count: int, box: str = "2.0 3.0 4.0") -> None:
    lines = ["generated", str(atom_count)]
    lines.extend(
        f"    1SOL     OW{index:5d}   0.000   0.000   0.000"
        for index in range(1, atom_count + 1)
    )
    lines.append(box)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _topology(path: Path, *, solvent: int) -> None:
    with zipfile.ZipFile(path, mode="w") as archive:
        archive.writestr(
            "topol.top",
            f"[ system ]\nProtein\n\n[ molecules ]\nProtein 1\nSOL {solvent}\n",
        )


def _functions(calls: dict[str, dict[str, Any]]) -> Any:
    def fix_side_chain(**kwargs: Any) -> int:
        calls["fix_side_chain"] = kwargs
        Path(kwargs["output_pdb_path"]).write_text(
            Path(kwargs["input_pdb_path"]).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        return 0

    def pdb2gmx(**kwargs: Any) -> int:
        calls["pdb2gmx"] = kwargs
        _gro(Path(kwargs["output_gro_path"]), 2)
        _topology(Path(kwargs["output_top_zip_path"]), solvent=0)
        return 0

    def editconf(**kwargs: Any) -> int:
        calls["editconf"] = kwargs
        _gro(Path(kwargs["output_gro_path"]), 2)
        return 0

    def solvate(**kwargs: Any) -> int:
        calls["solvate"] = kwargs
        _gro(Path(kwargs["output_gro_path"]), 8)
        _topology(Path(kwargs["output_top_zip_path"]), solvent=2)
        return 0

    return driver.BioBBFunctions(fix_side_chain, pdb2gmx, editconf, solvate)


def _config(root: Path) -> Any:
    input_path = root / "input.pdb"
    input_path.write_text(
        "ATOM      1  N   ALA A   1\nHETATM    2  O   HOH A   2\nEND\n",
        encoding="utf-8",
    )
    return driver.RunConfiguration(
        input_pdb=input_path,
        output_dir=root,
        force_field="charmm27",
        water_type="spce",
        box_type="dodecahedron",
        distance_to_molecule=1.25,
        ignore_input_hydrogens=True,
        merge_chains=False,
        gmx="gmx",
    )


def test_successful_driver_records_semantics_metrics_and_exact_outputs(
    tmp_path: Path,
) -> None:
    """A real five-stage cell closes with scientific values and file hashes."""

    calls: dict[str, dict[str, Any]] = {}

    return_code = driver.run(_config(tmp_path), functions=_functions(calls))

    assert return_code == 0
    result = json.loads((tmp_path / driver.RESULT_NAME).read_text(encoding="utf-8"))
    assert result["schema_version"] == driver.RESULT_SCHEMA
    assert result["status"] == "completed"
    assert result["parameters"] == {
        "box_type": "dodecahedron",
        "distance_to_molecule": 1.25,
        "force_field": "charmm27",
        "ignore_input_hydrogens": True,
        "merge_chains": False,
        "water_type": "spce",
    }
    assert result["metrics"]["input_pdb_atom_count"] == 2
    assert result["metrics"]["boxed_volume_nm3"] == pytest.approx(24.0)
    assert result["metrics"]["solvent_molecule_count"] == 2
    assert [stage["name"] for stage in result["stages"]] == [
        "stage_input",
        "fix_side_chain",
        "pdb2gmx",
        "editconf",
        "solvate",
    ]
    assert len(result["artifacts"]) == 6
    assert all(len(item["sha256"]) == 64 for item in result["artifacts"])
    assert calls["pdb2gmx"]["properties"] == {
        "force_field": "charmm27",
        "water_type": "spce",
        "ignh": True,
        "merge": False,
        "gmx_path": "gmx",
    }
    assert calls["editconf"]["properties"]["box_type"] == "dodecahedron"


def test_failed_stage_writes_a_nonzero_closed_partial_result(tmp_path: Path) -> None:
    """A BioBB exception remains inspectable without becoming a successful run."""

    calls: dict[str, dict[str, Any]] = {}
    functions = _functions(calls)

    def fail_pdb2gmx(**kwargs: Any) -> int:
        del kwargs
        return 9

    failed = driver.BioBBFunctions(
        functions.fix_side_chain,
        fail_pdb2gmx,
        functions.editconf,
        functions.solvate,
    )

    return_code = driver.run(_config(tmp_path), functions=failed)

    assert return_code == 1
    result = json.loads((tmp_path / driver.RESULT_NAME).read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["return_code"] == 1
    assert result["error"]["type"] == "RuntimeError"
    assert [item["path"] for item in result["artifacts"]] == ["fixed.pdb"]


def test_malformed_scientific_output_is_a_recorded_failure(tmp_path: Path) -> None:
    """A nominal block return cannot bypass semantic output validation."""

    calls: dict[str, dict[str, Any]] = {}
    functions = _functions(calls)

    def malformed_solvate(**kwargs: Any) -> int:
        Path(kwargs["output_gro_path"]).write_text("not a GRO\n", encoding="utf-8")
        _topology(Path(kwargs["output_top_zip_path"]), solvent=2)
        return 0

    malformed = driver.BioBBFunctions(
        functions.fix_side_chain,
        functions.pdb2gmx,
        functions.editconf,
        malformed_solvate,
    )

    return_code = driver.run(_config(tmp_path), functions=malformed)

    assert return_code == 1
    result = json.loads((tmp_path / driver.RESULT_NAME).read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["stages"][-1]["name"] == "validate_outputs"
    assert result["error"]["message"].startswith("GRO output is truncated")


def test_gro_parser_handles_triclinic_box_vectors(tmp_path: Path) -> None:
    """Nine-value GROMACS boxes use the documented vector ordering."""

    path = tmp_path / "triclinic.gro"
    _gro(path, 1, "2.0 3.0 4.0 0.0 0.0 0.5 0.0 0.0 0.25")

    metrics = driver._gro_metrics(path)

    assert metrics["atom_count"] == 1
    assert metrics["box_volume_nm3"] == pytest.approx(24.0)


def test_input_must_be_inside_the_package_owned_output(tmp_path: Path) -> None:
    """The driver cannot claim or mutate a caller-owned source path."""

    root = tmp_path / "out"
    root.mkdir()
    outside = tmp_path / "outside.pdb"
    outside.write_text("ATOM\n", encoding="utf-8")
    config = driver.RunConfiguration(
        input_pdb=outside,
        output_dir=root,
        force_field="amber99sb-ildn",
        water_type="tip3p",
        box_type="cubic",
        distance_to_molecule=1.0,
        ignore_input_hydrogens=True,
        merge_chains=False,
        gmx="gmx",
    )

    with pytest.raises(ValueError, match="output directory"):
        driver.run(config, functions=_functions({}))
