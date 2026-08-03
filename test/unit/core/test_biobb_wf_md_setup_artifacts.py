"""Generated-artifact tests for the builtin BioBB MD-setup package."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import ModuleType

import pytest

from jarvis_cd.artifacts import ArtifactRole, ArtifactState, load_artifacts_module


def _load_artifacts() -> ModuleType:
    """Load BioBB artifact semantics directly from the builtin package."""

    path = (
        Path(__file__).resolve().parents[3]
        / "builtin"
        / "builtin"
        / "biobb_wf_md_setup"
        / "artifacts.py"
    )
    return load_artifacts_module(path)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _closed_result(root: Path, *, status: str = "completed") -> None:
    products = [
        ("fixed.pdb", "pdb"),
        ("processed.gro", "gromacs-gro"),
        ("processed_topology.zip", "gromacs-topology-zip"),
        ("boxed.gro", "gromacs-gro"),
        ("solvated.gro", "gromacs-gro"),
        ("solvated_topology.zip", "gromacs-topology-zip"),
    ]
    artifacts = []
    for name, format_name in products:
        path = root / name
        path.write_bytes(f"product:{name}\n".encode())
        artifacts.append(
            {
                "path": name,
                "format": format_name,
                "size_bytes": path.stat().st_size,
                "sha256": _sha(path),
            }
        )
    result = {
        "schema_version": "jarvis.biobb-md-setup-result.v1",
        "status": status,
        "return_code": 0 if status == "completed" else 1,
        "parameters": {
            "force_field": "amber99sb-ildn",
            "water_type": "tip3p",
            "box_type": "cubic",
            "distance_to_molecule": 1.0,
            "ignore_input_hydrogens": True,
            "merge_chains": False,
        },
        "input": {"path": "input.pdb", "size_bytes": 5, "sha256": "0" * 64},
        "stages": [{"name": "solvate", "status": status}],
        "metrics": {
            "boxed_volume_nm3": 24.0,
            "solvated_atom_count": 8,
            "solvent_molecule_count": 2,
        },
        "artifacts": artifacts,
    }
    (root / "biobb-result.json").write_text(json.dumps(result), encoding="utf-8")


def test_success_reports_result_and_all_exact_products(tmp_path: Path) -> None:
    """A closed successful cell exposes hashes and semantic metrics."""

    _closed_result(tmp_path)
    module = _load_artifacts()
    adapter = module.BiobbMdSetupArtifactAdapter(
        module.PurePosixPath("/execution/shared/biobb"), tmp_path
    )

    observations = adapter.finalize_artifacts_for_exit(0)

    assert [item.logical_name for item in observations] == [
        "biobb-md-setup-result",
        "biobb-fixed-structure",
        "biobb-processed-coordinates",
        "biobb-processed-topology",
        "biobb-boxed-coordinates",
        "biobb-solvated-coordinates",
        "biobb-solvated-topology",
    ]
    assert all(item.state is ArtifactState.FINALIZED for item in observations)
    assert observations[0].role is ArtifactRole.OUTPUT
    assert observations[0].metadata["solvent_molecule_count"] == 2
    assert adapter.finalize_artifacts_for_exit(0) == []


def test_nonzero_process_or_failed_document_never_finalizes(tmp_path: Path) -> None:
    """Filesystem presence cannot conceal process or driver failure."""

    _closed_result(tmp_path, status="failed")
    module = _load_artifacts()
    adapter = module.BiobbMdSetupArtifactAdapter(
        module.PurePosixPath("/execution/shared/biobb"), tmp_path
    )

    observations = adapter.finalize_artifacts_for_exit(7)

    assert observations
    assert all(item.state is ArtifactState.INCOMPLETE for item in observations)


def test_declared_product_hash_and_root_confinement_are_enforced(
    tmp_path: Path,
) -> None:
    """A result cannot redirect artifact ownership or silently change a file."""

    _closed_result(tmp_path)
    module = _load_artifacts()
    document = json.loads((tmp_path / "biobb-result.json").read_text())
    document["artifacts"][0]["sha256"] = "f" * 64
    (tmp_path / "biobb-result.json").write_text(json.dumps(document))
    adapter = module.BiobbMdSetupArtifactAdapter(
        module.PurePosixPath("/execution/shared/biobb"), tmp_path
    )
    with pytest.raises(RuntimeError, match="hash differs"):
        adapter.finalize_artifacts_for_exit(0)

    _closed_result(tmp_path)
    document = json.loads((tmp_path / "biobb-result.json").read_text())
    document["artifacts"][0]["path"] = "../outside.pdb"
    (tmp_path / "biobb-result.json").write_text(json.dumps(document))
    adapter = module.BiobbMdSetupArtifactAdapter(
        module.PurePosixPath("/execution/shared/biobb"), tmp_path
    )
    with pytest.raises(RuntimeError, match="escaped"):
        adapter.finalize_artifacts_for_exit(0)


def test_factory_resolves_relative_output_and_ignores_unrelated_packages() -> None:
    """Artifact discovery uses the same package-owned root as native launch."""

    module = _load_artifacts()
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.biobb_wf_md_setup",
            "out": "run",
            "shared_dir": "/execution/shared/biobb",
            "runtime_cwd": "/execution/runtime",
        }
    )

    assert adapter is not None
    assert adapter.output_dir.as_posix() == "/execution/shared/biobb/run"
    assert module.adapter_from_package({"pkg_type": "builtin.ior"}) is None


def test_container_private_output_is_not_claimed() -> None:
    """A host cannot report artifacts from an unmounted container path."""

    module = _load_artifacts()
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.biobb_wf_md_setup",
            "out": "/tmp/biobb",
            "effective_deploy_mode": "container",
            "shared_dir": "/execution/shared",
            "private_dir": "/execution/private",
            "runtime_cwd": "/execution/runtime",
        }
    )

    assert adapter is None
