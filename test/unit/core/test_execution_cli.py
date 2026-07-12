"""Machine-readable execution CLI tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from jarvis_cd.core.cli import JarvisCLI
from jarvis_cd.core.execution import ExecutionHandle, ExecutionStore


def _completed_record(tmp_path: Path):
    """Create one completed record for CLI projection tests."""
    store = ExecutionStore(tmp_path / "executions", "example")
    store.create("run-1", mode="direct")
    store.update("run-1", state="running")
    return store.update("run-1", state="completed", terminal=True, return_code=0)


def test_execution_get_json_is_one_clean_record(
    tmp_path: Path,
    capsys,
) -> None:
    """The query command emits a directly parseable JSON document."""
    record = _completed_record(tmp_path)
    pipeline = SimpleNamespace(get_execution=Mock(return_value=record))
    cli = JarvisCLI()
    cli.kwargs = {"execution_id": "run-1", "json": True}
    cli._ensure_initialized = Mock()
    cli._require_current_pipeline = Mock(return_value=pipeline)

    cli.execution_get()

    assert json.loads(capsys.readouterr().out) == record.to_dict()
    pipeline.get_execution.assert_called_once_with("run-1")


def test_execution_list_can_query_noncurrent_pipeline(
    tmp_path: Path,
    capsys,
) -> None:
    """A handle remains queryable after the operator changes pipelines."""
    record = _completed_record(tmp_path)
    pipeline = SimpleNamespace(
        name="example", list_executions=Mock(return_value=[record])
    )
    cli = JarvisCLI()
    cli.kwargs = {"pipeline_id": "example", "json": True}
    cli._ensure_initialized = Mock()

    with patch("jarvis_cd.core.cli.Pipeline", return_value=pipeline) as constructor:
        cli.execution_list()

    document = json.loads(capsys.readouterr().out)
    assert document["schema_version"] == "jarvis.execution.list.v1"
    assert document["pipeline_id"] == "example"
    assert document["executions"] == [record.to_dict()]
    constructor.assert_called_once_with("example")


def test_execution_progress_json_can_query_noncurrent_pipeline(
    tmp_path: Path,
    capsys,
) -> None:
    """Agents can poll a handle after the current pipeline changes."""
    store = ExecutionStore(tmp_path / "executions", "example")
    record = store.create("run-1", mode="direct")
    snapshot = store.progress("run-1")
    pipeline = SimpleNamespace(
        get_execution_progress=Mock(return_value=snapshot),
    )
    cli = JarvisCLI()
    cli.kwargs = {
        "execution_id": record.execution_id,
        "pipeline_id": "example",
        "json": True,
    }
    cli._ensure_initialized = Mock()

    with patch("jarvis_cd.core.cli.Pipeline", return_value=pipeline) as constructor:
        cli.execution_progress()

    assert json.loads(capsys.readouterr().out) == snapshot.to_dict()
    pipeline.get_execution_progress.assert_called_once_with("run-1")
    constructor.assert_called_once_with("example")


def test_json_handle_is_emitted_as_one_final_line(capsys) -> None:
    """Run and submit handlers share the exact handle serialization."""
    handle = ExecutionHandle(
        execution_id="run-1",
        pipeline_id="example",
        mode="scheduler",
        scheduler_provider="slurm",
        scheduler_native_id="17",
        cluster="ares",
    )

    JarvisCLI._emit_execution_handle(handle, json_output=True)

    assert json.loads(capsys.readouterr().out) == handle.to_dict()


@pytest.mark.parametrize("option", ["--execution-id", "--execution_id"])
def test_submit_parser_accepts_execution_id_spellings(option: str) -> None:
    """The documented hyphenated flag and legacy underscore flag are aliases."""
    cli = JarvisCLI()
    cli.define_options()
    cli.ppl_submit = Mock()

    parsed = cli.parse(["ppl", "submit", "pipeline.yaml", option, "run-1", "+json"])

    assert parsed["pipeline_file"] == "pipeline.yaml"
    assert parsed["execution_id"] == "run-1"
    assert parsed["json"] is True
    cli.ppl_submit.assert_called_once_with()


@pytest.mark.parametrize(
    ("command", "method_name"),
    [
        (["execution", "get", "run-1"], "execution_get"),
        (["execution", "list"], "execution_list"),
        (["execution", "progress", "run-1"], "execution_progress"),
    ],
)
@pytest.mark.parametrize("option", ["--pipeline-id", "--pipeline_id"])
def test_execution_query_parser_accepts_pipeline_id_spellings(
    command: list[str],
    method_name: str,
    option: str,
) -> None:
    """All record queries accept the documented pipeline identity option."""
    cli = JarvisCLI()
    cli.define_options()
    handler = Mock()
    setattr(cli, method_name, handler)

    parsed = cli.parse([*command, option, "visualization", "+json"])

    assert parsed["pipeline_id"] == "visualization"
    assert parsed["json"] is True
    handler.assert_called_once_with()
