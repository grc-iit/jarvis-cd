"""Generated-artifact tests for the builtin WfCommons package."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
from types import ModuleType

import pytest

from jarvis_cd.artifacts import load_artifacts_module


def _load_artifacts() -> ModuleType:
    """Load WfCommons artifact semantics directly from the builtin package."""

    path = (
        Path(__file__).resolve().parents[3]
        / "builtin"
        / "builtin"
        / "wfcommons"
        / "artifacts.py"
    )
    return load_artifacts_module(path)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _closed_run(root: Path, *, return_code: int = 0) -> None:
    workflow = root / "workflow.json"
    log = root / "workflow.log"
    lock = root / "dependency-lock.txt"
    schema = root / "wfcommons-schema.json"
    workflow.write_text('{"workflow": {}}\n', encoding="utf-8")
    log.write_text("complete\n", encoding="utf-8")
    lock.write_text("wfcommons==1.4\n", encoding="utf-8")
    schema.write_text("{}\n", encoding="utf-8")
    result = {
        "schema_version": "jarvis.wfcommons-result.v1",
        "recipe": "epigenomics",
        "requested_task_count": 100,
        "observed_task_count": 97,
        "data_footprint_mb": 8,
        "seed": 424300,
        "elapsed_seconds": 1.2,
        "return_code": return_code,
        "workflow_manifest": workflow.name,
        "workflow_sha256": _sha(workflow),
        "workflow_log": log.name,
        "workflow_log_sha256": _sha(log),
        "dependency_lock": lock.name,
        "dependency_lock_sha256": _sha(lock),
        "schema_file": schema.name,
        "schema_sha256": _sha(schema),
    }
    (root / "wfcommons-result.json").write_text(json.dumps(result), encoding="utf-8")


def test_finalized_run_reports_result_workflow_log_and_provenance(
    tmp_path: Path,
) -> None:
    """A successful cell exposes all closed products with exact hashes."""

    module = _load_artifacts()
    _closed_run(tmp_path)
    adapter = module.adapter_from_package(
        {"pkg_type": "builtin.wfcommons", "out": "/execution/shared/wfcommons"}
    )
    assert adapter is not None
    adapter._local_root = tmp_path

    observations = adapter.finalize_artifacts_for_exit(0)

    assert [item.logical_name for item in observations] == [
        "wfcommons-result",
        "wfcommons-workflow",
        "wfcommons-workflow-log",
        "wfcommons-dependency-lock",
        "wfcommons-schema",
    ]
    assert all(item.state.value == "finalized" for item in observations)
    assert all(
        item.checksum and item.checksum.startswith("sha256:") for item in observations
    )
    assert adapter.finalize_artifacts_for_exit(0) == []


def test_failed_process_never_finalizes_outputs(tmp_path: Path) -> None:
    """A nonzero driver result leaves available products explicitly incomplete."""

    module = _load_artifacts()
    _closed_run(tmp_path, return_code=9)
    adapter = module.WfcommonsArtifactAdapter(
        PurePosixPath("/execution/shared/wfcommons"), tmp_path
    )

    observations = adapter.finalize_artifacts_for_exit(9)

    assert observations
    assert all(item.state.value == "incomplete" for item in observations)


def test_result_cannot_escape_the_owned_output_root(tmp_path: Path) -> None:
    """Artifact paths from the result document are confined to the output root."""

    module = _load_artifacts()
    _closed_run(tmp_path)
    result = json.loads((tmp_path / "wfcommons-result.json").read_text())
    result["workflow_manifest"] = "../outside.json"
    (tmp_path / "wfcommons-result.json").write_text(json.dumps(result))

    adapter = module.WfcommonsArtifactAdapter(
        PurePosixPath("/execution/shared/wfcommons"), tmp_path
    )
    with pytest.raises(RuntimeError, match="escaped"):
        adapter.finalize_artifacts_for_exit(0)


def test_unrelated_package_has_no_wfcommons_artifact_adapter() -> None:
    """Artifact semantics remain package-specific."""

    module = _load_artifacts()
    assert module.adapter_from_package({"pkg_type": "builtin.ior"}) is None
