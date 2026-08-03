"""Pure result-contract tests for the WfCommons driver."""

from __future__ import annotations

import hashlib
import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType


def _load_driver() -> ModuleType:
    """Load the driver as a module without executing its CLI."""

    path = (
        Path(__file__).resolve().parents[3]
        / "builtin"
        / "builtin"
        / "wfcommons"
        / "run_wfbench.py"
    )
    spec = spec_from_file_location("test_builtin_wfcommons_driver", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load the WfCommons driver from {path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


driver = _load_driver()


def _manifest(path: Path) -> None:
    document = {
        "workflow": {
            "specification": {
                "tasks": [
                    {"id": "a", "parents": [], "children": ["b"]},
                    {"id": "b", "parents": ["a"], "children": []},
                ]
            }
        }
    }
    path.write_text(json.dumps(document), encoding="utf-8")


def test_workflow_identity_is_stable_and_counts_edges(tmp_path: Path) -> None:
    """Topology identity excludes file sizes and other footprint-only values."""

    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _manifest(first)
    raw = json.loads(first.read_text(encoding="utf-8"))
    raw["workflow"]["specification"]["files"] = [
        {"id": "data", "sizeInBytes": 99_000_000}
    ]
    second.write_text(json.dumps(raw), encoding="utf-8")

    assert driver.workflow_identity(first) == driver.workflow_identity(second)
    assert driver.workflow_identity(first)[:2] == (2, 1)


def test_result_document_binds_runtime_schema_and_closed_outputs(
    tmp_path: Path,
) -> None:
    """The generic result records exact inputs and successful process completion."""

    manifest = tmp_path / "workflow.json"
    log = tmp_path / "workflow.log"
    lock = tmp_path / "dependency-lock.txt"
    schema = tmp_path / "wfcommons-schema.json"
    _manifest(manifest)
    log.write_text("ok\n", encoding="utf-8")
    lock.write_text("wfcommons==1.4\n", encoding="utf-8")
    schema.write_text("{}\n", encoding="utf-8")

    result = driver.build_result_document(
        run_root=tmp_path,
        recipe="epigenomics",
        requested_task_count=100,
        data_footprint_mb=8,
        seed=424300,
        elapsed_seconds=1.25,
        return_code=0,
        workflow_path=manifest,
        workflow_log=log,
        dependency_lock=lock,
        schema_path=schema,
        wfcommons_version="1.4",
        python_version="3.12.8",
    )

    assert result["schema_version"] == "jarvis.wfcommons-result.v1"
    assert result["observed_task_count"] == 2
    assert result["dag_edge_count"] == 1
    assert result["return_code"] == 0
    assert (
        result["workflow_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    )
    assert result["schema_sha256"] == hashlib.sha256(schema.read_bytes()).hexdigest()
    assert result["workflow_manifest"] == "workflow.json"
    assert result["workflow_log"] == "workflow.log"
    assert result["dependency_lock"] == "dependency-lock.txt"
