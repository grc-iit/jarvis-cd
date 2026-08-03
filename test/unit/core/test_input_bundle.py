"""Security and identity tests for package input-bundle materialization."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest

from jarvis_cd.input_bundle import (
    INPUT_BUNDLE_MANIFEST_NAME,
    INPUT_BUNDLE_SCHEMA_VERSION,
    InputBundleError,
    extract_input_bundle,
    stage_input_bundle,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_bundle(
    destination: Path,
    files: dict[str, bytes],
    *,
    entrypoint: str,
    mutate_manifest: object | None = None,
    extra_member: tuple[tarfile.TarInfo, bytes] | None = None,
) -> Path:
    manifest: object = {
        "schema_version": INPUT_BUNDLE_SCHEMA_VERSION,
        "entrypoint": entrypoint,
        "files": [
            {
                "path": name,
                "role": "package_input",
                "sha256": _sha256(payload),
                "size_bytes": len(payload),
            }
            for name, payload in sorted(files.items())
        ],
    }
    if mutate_manifest is not None:
        manifest = mutate_manifest
    manifest_payload = json.dumps(manifest, sort_keys=True).encode("utf-8")
    with tarfile.open(destination, mode="w") as archive:
        manifest_info = tarfile.TarInfo(INPUT_BUNDLE_MANIFEST_NAME)
        manifest_info.size = len(manifest_payload)
        archive.addfile(manifest_info, io.BytesIO(manifest_payload))
        for name, payload in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        if extra_member is not None:
            info, payload = extra_member
            archive.addfile(info, io.BytesIO(payload))
    return destination


def test_extract_input_bundle_is_digest_bound_and_idempotent(tmp_path: Path) -> None:
    archive = _write_bundle(
        tmp_path / "inputs.tar",
        {
            "in/run.lmp": b"include support/potential.eam\n",
            "support/potential.eam": b"EAM",
        },
        entrypoint="in/run.lmp",
    )

    first = extract_input_bundle(archive, tmp_path / "materialized")
    second = extract_input_bundle(archive, tmp_path / "materialized")

    assert first == second
    assert first.root.name == _sha256(archive.read_bytes())
    assert first.entrypoint.read_bytes() == b"include support/potential.eam\n"
    assert (first.root / "support" / "potential.eam").read_bytes() == b"EAM"
    assert first.manifest.entrypoint == "in/run.lmp"


def test_extract_input_bundle_rejects_links_and_path_traversal(tmp_path: Path) -> None:
    link = tarfile.TarInfo("linked")
    link.type = tarfile.SYMTYPE
    link.linkname = "entrypoint"
    archive = _write_bundle(
        tmp_path / "link.tar",
        {"entrypoint": b"run"},
        entrypoint="entrypoint",
        extra_member=(link, b""),
    )
    with pytest.raises(InputBundleError, match="not a regular file"):
        extract_input_bundle(archive, tmp_path / "linked-output")

    traversal = tarfile.TarInfo("../escape")
    traversal.size = 1
    archive = _write_bundle(
        tmp_path / "traversal.tar",
        {"entrypoint": b"run"},
        entrypoint="entrypoint",
        extra_member=(traversal, b"x"),
    )
    with pytest.raises(InputBundleError, match="confined relative path"):
        extract_input_bundle(archive, tmp_path / "traversal-output")


def test_extract_input_bundle_rejects_manifest_and_payload_mismatch(
    tmp_path: Path,
) -> None:
    malformed = {
        "schema_version": INPUT_BUNDLE_SCHEMA_VERSION,
        "entrypoint": "entrypoint",
        "files": [
            {
                "path": "entrypoint",
                "role": "package_input",
                "sha256": "0" * 64,
                "size_bytes": 3,
            }
        ],
    }
    archive = _write_bundle(
        tmp_path / "mismatch.tar",
        {"entrypoint": b"run"},
        entrypoint="entrypoint",
        mutate_manifest=malformed,
    )

    with pytest.raises(InputBundleError, match="digest or size mismatch"):
        extract_input_bundle(archive, tmp_path / "output")


def test_extract_input_bundle_detects_changed_materialization(tmp_path: Path) -> None:
    archive = _write_bundle(
        tmp_path / "inputs.tar",
        {"entrypoint": b"run"},
        entrypoint="entrypoint",
    )
    materialized = extract_input_bundle(archive, tmp_path / "output")
    materialized.entrypoint.write_bytes(b"changed")

    with pytest.raises(InputBundleError, match="member changed"):
        extract_input_bundle(archive, tmp_path / "output")


def test_extract_input_bundle_rejects_coordinated_materialization_rewrite(
    tmp_path: Path,
) -> None:
    archive = _write_bundle(
        tmp_path / "inputs.tar",
        {"entrypoint": b"run"},
        entrypoint="entrypoint",
    )
    materialized = extract_input_bundle(archive, tmp_path / "output")
    changed = b"changed"
    materialized.entrypoint.write_bytes(changed)
    rewritten_manifest = {
        "schema_version": INPUT_BUNDLE_SCHEMA_VERSION,
        "entrypoint": "entrypoint",
        "files": [
            {
                "path": "entrypoint",
                "role": "package_input",
                "sha256": _sha256(changed),
                "size_bytes": len(changed),
            }
        ],
    }
    (materialized.root / INPUT_BUNDLE_MANIFEST_NAME).write_text(
        json.dumps(rewritten_manifest, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(InputBundleError, match="differs from the source archive"):
        extract_input_bundle(archive, tmp_path / "output")


def test_extract_input_bundle_reapplies_bounds_when_reusing(tmp_path: Path) -> None:
    archive = _write_bundle(
        tmp_path / "inputs.tar",
        {"entrypoint": b"run"},
        entrypoint="entrypoint",
    )
    extract_input_bundle(archive, tmp_path / "output")

    with pytest.raises(InputBundleError, match="payload exceeds"):
        extract_input_bundle(archive, tmp_path / "output", max_total_bytes=1)


def test_extract_input_bundle_reuses_concurrent_atomic_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    archive = _write_bundle(
        tmp_path / "inputs.tar",
        {"entrypoint": b"run"},
        entrypoint="entrypoint",
    )

    def publish_competitor(source: Path, destination: Path) -> None:
        shutil.copytree(source, destination)
        raise FileExistsError("concurrent publication")

    monkeypatch.setattr("jarvis_cd.input_bundle.os.replace", publish_competitor)

    materialized = extract_input_bundle(archive, tmp_path / "output")

    assert materialized.entrypoint.read_bytes() == b"run"
    assert not any(
        path.name.startswith(f".{materialized.bundle_sha256}.")
        for path in (tmp_path / "output").iterdir()
    )


def test_stage_input_bundle_copies_payload_without_overwriting(tmp_path: Path) -> None:
    archive = _write_bundle(
        tmp_path / "inputs.tar",
        {"in/run.lmp": b"run 1\n", "potential.eam": b"EAM"},
        entrypoint="in/run.lmp",
    )
    materialized = extract_input_bundle(archive, tmp_path / "materialized")

    entrypoint = stage_input_bundle(materialized, tmp_path / "run")

    assert entrypoint == tmp_path / "run" / "in" / "run.lmp"
    assert entrypoint.read_bytes() == b"run 1\n"
    assert (tmp_path / "run" / "potential.eam").read_bytes() == b"EAM"
    with pytest.raises(InputBundleError, match="already exists"):
        stage_input_bundle(materialized, tmp_path / "run")


def test_extract_input_bundle_enforces_declared_bounds(tmp_path: Path) -> None:
    archive = _write_bundle(
        tmp_path / "inputs.tar",
        {"entrypoint": b"run", "second": b"file"},
        entrypoint="entrypoint",
    )

    with pytest.raises(InputBundleError, match="member count"):
        extract_input_bundle(archive, tmp_path / "file-bound", max_files=1)
    with pytest.raises(InputBundleError, match="payload exceeds"):
        extract_input_bundle(archive, tmp_path / "byte-bound", max_total_bytes=1)
    with pytest.raises(InputBundleError, match="archive size or type"):
        extract_input_bundle(archive, tmp_path / "archive-bound", max_archive_bytes=1)
