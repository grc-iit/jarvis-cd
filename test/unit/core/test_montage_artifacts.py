"""Generated-artifact tests for the builtin Montage package."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest

from jarvis_cd.artifacts import (
    ArtifactRole,
    ArtifactState,
    ArtifactStructure,
    load_artifacts_module,
)


def _module() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[3]
        / "builtin"
        / "builtin"
        / "montage"
        / "artifacts.py"
    )
    return load_artifacts_module(path)


def _products(root: Path) -> None:
    for band in ("j", "h", "k"):
        (root / f"montage-{band}.fits").write_bytes(b"SIMPLE  " + bytes(4096))
    (root / "montage-jhk.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(2048))
    (root / "montage-result.json").write_text(
        json.dumps({"schema_version": "jarvis.montage-result.v1"}) + "\n",
        encoding="utf-8",
    )


def test_montage_success_finalizes_only_declared_products(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The provider reports FITS, composite, and result products without crawling."""
    _products(tmp_path)
    (tmp_path / "unrelated.txt").write_text("ignore\n", encoding="utf-8")
    module = _module()
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.montage",
            "out": "/execution/shared/montage",
            "j_bundle": "/inputs/j.tar",
            "h_bundle": "/inputs/h.tar",
            "k_bundle": "/inputs/k.tar",
            "region": "M31",
        }
    )
    monkeypatch.setattr(module, "Path", lambda _value: tmp_path)

    assert adapter is not None
    observations = adapter.finalize_artifacts_for_exit(0)

    assert adapter.finalize_artifacts_for_exit(0) == []
    assert {item.logical_name for item in observations} == {
        "montage-j-mosaic",
        "montage-h-mosaic",
        "montage-k-mosaic",
        "montage-three-band-composite",
        "montage-result",
    }
    assert {item.state for item in observations} == {ArtifactState.FINALIZED}
    assert sum(item.role is ArtifactRole.OUTPUT for item in observations) == 4
    result = next(
        item for item in observations if item.logical_name == "montage-result"
    )
    assert result.role is ArtifactRole.VALIDATION
    assert result.structure is ArtifactStructure.FILE


def test_montage_missing_product_is_incomplete_even_after_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Process exit alone cannot claim a complete three-band product set."""
    _products(tmp_path)
    (tmp_path / "montage-k.fits").unlink()
    module = _module()
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.montage",
            "out": "/execution/shared/montage",
            "j_bundle": "/inputs/j.tar",
            "h_bundle": "/inputs/h.tar",
            "k_bundle": "/inputs/k.tar",
            "region": "M31",
        }
    )
    monkeypatch.setattr(module, "Path", lambda _value: tmp_path)

    assert adapter is not None
    observations = adapter.finalize_artifacts_for_exit(0)

    missing = next(
        item for item in observations if item.logical_name == "montage-k-mosaic"
    )
    assert missing.state is ArtifactState.INCOMPLETE
    assert missing.location is None


def test_montage_factory_confines_relative_output_to_shared_root() -> None:
    """Artifacts cannot escape the package-owned shared output namespace."""
    module = _module()
    adapter = module.adapter_from_package(
        {
            "pkg_type": "builtin.montage",
            "out": ".",
            "shared_dir": "/execution/shared/montage",
            "j_bundle": "/inputs/j.tar",
            "h_bundle": "/inputs/h.tar",
            "k_bundle": "/inputs/k.tar",
        }
    )

    assert adapter is not None
    assert adapter.output_dir.as_posix() == "/execution/shared/montage"
    assert module.adapter_from_package({"pkg_type": "builtin.lammps"}) is None
