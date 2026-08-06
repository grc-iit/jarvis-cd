"""Artifact-contract tests for the builtin Gadget2 package."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from jarvis_cd.artifacts import (
    ArtifactState,
    ArtifactStructure,
    load_artifacts_module,
)
from jarvis_cd.input_bundle import (
    INPUT_BUNDLE_MANIFEST_NAME,
    INPUT_BUNDLE_SCHEMA_VERSION,
)


def _load_artifacts() -> ModuleType:
    """Load the package-local artifact module."""

    path = (
        Path(__file__).resolve().parents[3]
        / "builtin"
        / "builtin"
        / "gadget2"
        / "artifacts.py"
    )
    return load_artifacts_module(path)


def _write_bundle(destination: Path) -> Path:
    files = {
        "galaxy/galaxy.param": (
            b"InitCondFile ICs/galaxy.dat\nOutputDir output/\n"
            b"EnergyFile energy.txt\nInfoFile info.txt\nCpuFile cpu.txt\n"
            b"TimingsFile timings.txt\nSnapshotFileBase snapshot\n"
        ),
        "galaxy/ICs/galaxy.dat": b"initial-condition\x00\x01",
    }
    manifest = {
        "schema_version": INPUT_BUNDLE_SCHEMA_VERSION,
        "entrypoint": "galaxy/galaxy.param",
        "files": [
            {
                "path": name,
                "role": "gadget2_input",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
            for name, payload in files.items()
        ],
    }
    encoded = json.dumps(manifest, sort_keys=True).encode()
    with tarfile.open(destination, "w") as archive:
        info = tarfile.TarInfo(INPUT_BUNDLE_MANIFEST_NAME)
        info.size = len(encoded)
        archive.addfile(info, io.BytesIO(encoded))
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return destination


def _adapter(module: ModuleType, tmp_path: Path) -> Any:
    declared = module.parse_gadget2_output_contract(
        (
            "OutputDir output/\nEnergyFile energy.txt\nInfoFile info.txt\n"
            "CpuFile cpu.txt\nTimingsFile timings.txt\n"
            "SnapshotFileBase snapshot\n"
        ),
        parameter_path=module.PurePosixPath("galaxy/galaxy.param"),
    )
    adapter = module.Gadget2ArtifactAdapter(
        module.PurePosixPath("/scratch/gadget2/run"),
        frozenset(
            {
                module.PurePosixPath("galaxy/galaxy.param"),
                module.PurePosixPath("galaxy/ICs/galaxy.dat"),
            }
        ),
        declared,
    )
    adapter._local_root = tmp_path / "run"
    return adapter


def _write_products(root: Path) -> None:
    (root / "galaxy" / "ICs").mkdir(parents=True)
    (root / "galaxy" / "galaxy.param").write_text(
        (
            "InitCondFile ICs/galaxy.dat\nOutputDir output/\n"
            "EnergyFile energy.txt\nInfoFile info.txt\nCpuFile cpu.txt\n"
            "TimingsFile timings.txt\nSnapshotFileBase snapshot\n"
        ),
        encoding="utf-8",
    )
    (root / "galaxy" / "ICs" / "galaxy.dat").write_bytes(b"initial-condition\x00\x01")
    products = root / "galaxy" / "output"
    products.mkdir()
    (products / "energy.txt").write_text(
        "0 0 -10 4\n0.1 0 -9.99 3.99\n", encoding="utf-8"
    )
    (products / "info.txt").write_text("Begin Step 1\n", encoding="utf-8")
    (products / "cpu.txt").write_text("Step 1\n", encoding="utf-8")
    (products / "timings.txt").write_text("Step=1\n", encoding="utf-8")
    (products / "snapshot_000").write_bytes(b"snapshot-zero")
    (products / "snapshot_001").write_bytes(b"snapshot-one")
    (products / "restart.0").write_bytes(b"rank-zero")
    (products / "restart.1").write_bytes(b"rank-one")


def test_success_finalizes_closed_logs_snapshots_and_restart_sets(
    tmp_path: Path,
) -> None:
    """A successful run reports outputs but never republishes staged inputs."""

    module = _load_artifacts()
    adapter = _adapter(module, tmp_path)
    _write_products(tmp_path / "run")

    observations = adapter.finalize_artifacts_for_exit(0)

    by_name = {item.logical_name: item for item in observations}
    assert set(by_name) == {
        "gadget2-results",
        "gadget2-energy",
        "gadget2-info",
        "gadget2-cpu",
        "gadget2-timings",
        "gadget2-snapshots",
        "gadget2-restarts",
    }
    assert all(item.state is ArtifactState.FINALIZED for item in observations)
    assert by_name["gadget2-snapshots"].structure is ArtifactStructure.COLLECTION
    assert by_name["gadget2-restarts"].structure is ArtifactStructure.COLLECTION
    members = by_name["gadget2-results"].metadata["member_names"]
    assert "galaxy/galaxy.param" not in members
    assert "galaxy/ICs/galaxy.dat" not in members
    assert "galaxy/output/energy.txt" in members
    assert adapter.finalize_artifacts_for_exit(0) == []


def test_missing_scientific_products_cannot_be_finalized(tmp_path: Path) -> None:
    """Process exit alone is weaker than a Gadget2 result contract."""

    module = _load_artifacts()
    adapter = _adapter(module, tmp_path)
    root = tmp_path / "run" / "galaxy" / "output"
    root.mkdir(parents=True)
    (root / "info.txt").write_text("started\n", encoding="utf-8")

    observations = adapter.finalize_artifacts_for_exit(0)

    assert observations
    assert all(item.state is ArtifactState.INCOMPLETE for item in observations)
    assert "missing" in observations[0].message.casefold()


def test_zero_byte_declared_products_cannot_be_finalized(tmp_path: Path) -> None:
    """Names alone cannot turn an aborted zero-status run into valid artifacts."""

    module = _load_artifacts()
    adapter = _adapter(module, tmp_path)
    root = tmp_path / "run" / "galaxy" / "output"
    root.mkdir(parents=True)
    (root / "energy.txt").write_bytes(b"")
    (root / "info.txt").write_bytes(b"")
    (root / "snapshot_000").write_bytes(b"")

    observations = adapter.finalize_artifacts_for_exit(0)

    assert observations
    assert all(item.state is ArtifactState.INCOMPLETE for item in observations)


def test_parameter_declared_names_drive_artifact_discovery(tmp_path: Path) -> None:
    """Custom scientific filenames are discovered without stock-name guesses."""

    module = _load_artifacts()
    declared = module.parse_gadget2_output_contract(
        (
            "OutputDir products/\nEnergyFile conserved.dat\n"
            "InfoFile progress.log\nSnapshotFileBase states/galaxy\n"
        ),
        parameter_path=module.PurePosixPath("galaxy/custom.param"),
    )
    adapter = module.Gadget2ArtifactAdapter(
        module.PurePosixPath("/scratch/gadget2/run"),
        frozenset(),
        declared,
    )
    adapter._local_root = tmp_path / "run"
    products = tmp_path / "run" / "galaxy" / "products"
    (products / "states").mkdir(parents=True)
    (products / "conserved.dat").write_text("0 -1\n", encoding="utf-8")
    (products / "progress.log").write_text("Step 1\n", encoding="utf-8")
    (products / "states" / "galaxy_000").write_bytes(b"snapshot")

    observations = adapter.finalize_artifacts_for_exit(0)

    by_name = {item.logical_name: item for item in observations}
    assert all(item.state is ArtifactState.FINALIZED for item in observations)
    assert by_name["gadget2-energy"].location.value.endswith("conserved.dat")
    assert by_name["gadget2-info"].location.value.endswith("progress.log")
    assert by_name["gadget2-snapshots"].metadata["member_names"] == [
        "galaxy/products/states/galaxy_000"
    ]


def test_nonzero_exit_marks_observed_products_incomplete(tmp_path: Path) -> None:
    """Native files cannot conceal an authoritative process failure."""

    module = _load_artifacts()
    adapter = _adapter(module, tmp_path)
    _write_products(tmp_path / "run")

    observations = adapter.finalize_artifacts_for_exit(7)

    assert observations
    assert all(item.state is ArtifactState.INCOMPLETE for item in observations)


def test_factory_resolves_relative_output_and_ignores_unrelated_packages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Artifact authority remains exact to one package-owned output root."""

    module = _load_artifacts()
    bundle = _write_bundle(tmp_path / "galaxy.tar")
    materialized = module.extract_input_bundle(
        bundle,
        tmp_path / "materialized-input",
    )
    monkeypatch.setattr(
        module,
        "extract_input_bundle",
        lambda *_args, **_kwargs: materialized,
    )
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.gadget2",
            "input_bundle": str(bundle),
            "out": "run",
            "shared_dir": "/execution/shared/gadget2",
        }
    )

    assert adapter is not None
    assert adapter.output_dir.as_posix() == "/execution/shared/gadget2/run"
    assert module.adapter_from_package({"pkg_type": "builtin.ior"}) is None


def test_container_private_output_is_not_claimed(tmp_path: Path) -> None:
    """A host cannot report files that exist only in a private container path."""

    module = _load_artifacts()
    bundle = _write_bundle(tmp_path / "galaxy.tar")
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.gadget2",
            "input_bundle": str(bundle),
            "out": "/tmp/gadget2",
            "effective_deploy_mode": "container",
            "shared_dir": "/execution/shared",
            "private_dir": "/execution/private",
        }
    )

    assert adapter is None
