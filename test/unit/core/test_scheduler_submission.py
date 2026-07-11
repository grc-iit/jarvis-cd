"""Structured scheduler-submission contract tests."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from jarvis_cd.core.pipeline import Pipeline
from jarvis_cd.core.scheduler import SlurmScheduler


def test_slurm_submission_uses_and_parses_parsable_identity(tmp_path: Path) -> None:
    scheduler = SlurmScheduler(
        {"name": "slurm"},
        tmp_path,
        pipeline_name="example",
    )

    assert scheduler.submit_command() == [
        "sbatch",
        "--parsable",
        str(tmp_path / "submit.slurm"),
    ]
    assert scheduler.submit_command(wait=True) == [
        "sbatch",
        "--parsable",
        "--wait",
        str(tmp_path / "submit.slurm"),
    ]
    assert scheduler.parse_submission_output("12345;ares\n") == {
        "provider": "slurm",
        "scheduler_job_id": "12345",
        "scheduler_cluster": "ares",
        "identity_source": "scheduler_submit_api",
    }


@pytest.mark.parametrize(
    "stdout",
    ["Submitted batch job 12345\n", "12345\napplication output\n", "abc;ares\n"],
)
def test_slurm_submission_rejects_human_or_ambiguous_output(
    tmp_path: Path,
    stdout: str,
) -> None:
    scheduler = SlurmScheduler({"name": "slurm"}, tmp_path)

    with pytest.raises(ValueError, match="parsable"):
        scheduler.parse_submission_output(stdout)


def test_pipeline_persists_provider_owned_submission_identity(tmp_path: Path) -> None:
    script_path = tmp_path / "submit.slurm"
    script_path.write_text("#!/bin/bash\n", encoding="utf-8")
    scheduler = SimpleNamespace(
        NAME="slurm",
        hostfile=str(tmp_path / "hostfile.txt"),
        write_script=Mock(return_value=script_path),
        submit_command=Mock(return_value=["sbatch", "--parsable", str(script_path)]),
        parse_submission_output=Mock(
            return_value={
                "provider": "slurm",
                "scheduler_job_id": "24680",
                "scheduler_cluster": "ares",
                "identity_source": "scheduler_submit_api",
            }
        ),
    )
    execution = SimpleNamespace(
        exit_code={"localhost": 0},
        stdout={"localhost": "24680;ares\n"},
    )
    executor = Mock()
    executor.run.return_value = execution
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.scheduler = {"name": "slurm"}
    pipeline.name = "example"
    pipeline.last_loaded_file = None
    pipeline.jarvis = SimpleNamespace(get_pipeline_shared_dir=lambda _name: tmp_path)
    pipeline.save = Mock()

    with patch("jarvis_cd.core.scheduler.make_scheduler", return_value=scheduler):
        with patch("jarvis_cd.shell.Exec", return_value=executor):
            returned = pipeline.submit(submit=True, wait=False)

    assert returned == script_path
    assert pipeline.last_submission == {
        "schema_version": "jarvis.scheduler.submission.v1",
        "provider": "slurm",
        "script_path": str(script_path),
        "scheduler_job_id": "24680",
        "scheduler_cluster": "ares",
        "identity_source": "scheduler_submit_api",
        "state": "submitted",
        "submitted": True,
        "wait": False,
        "terminal": False,
        "scheduler_stderr": None,
        "submission_returncode": 0,
        "terminal_returncode": None,
    }
    scheduler.parse_submission_output.assert_called_once_with("24680;ares\n")
    pipeline.save.assert_called_once_with()


def test_pipeline_never_accepts_unstructured_submission_stdout(tmp_path: Path) -> None:
    script_path = tmp_path / "submit.slurm"
    scheduler = SimpleNamespace(
        NAME="slurm",
        hostfile=str(tmp_path / "hostfile.txt"),
        write_script=Mock(return_value=script_path),
        submit_command=Mock(return_value=["sbatch", "--parsable", str(script_path)]),
        parse_submission_output=Mock(side_effect=ValueError("not parsable")),
    )
    execution = SimpleNamespace(
        exit_code={"localhost": 0},
        stdout={"localhost": "Submitted batch job 13579\n"},
    )
    executor = Mock()
    executor.run.return_value = execution
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.scheduler = {"name": "slurm"}
    pipeline.name = "example"
    pipeline.last_loaded_file = None
    pipeline.jarvis = SimpleNamespace(get_pipeline_shared_dir=lambda _name: tmp_path)
    pipeline.save = Mock()

    with patch("jarvis_cd.core.scheduler.make_scheduler", return_value=scheduler):
        with patch("jarvis_cd.shell.Exec", return_value=executor):
            with pytest.raises(RuntimeError, match="structured job identity"):
                pipeline.submit(submit=True, wait=False)

    assert pipeline.last_submission["scheduler_job_id"] is None
    assert pipeline.last_submission["state"] == "identity_failed"
    pipeline.save.assert_called_once_with()


def test_pipeline_preserves_waited_job_identity_when_workload_fails(
    tmp_path: Path,
) -> None:
    script_path = tmp_path / "submit.slurm"
    scheduler = SimpleNamespace(
        NAME="slurm",
        hostfile=str(tmp_path / "hostfile.txt"),
        write_script=Mock(return_value=script_path),
        submit_command=Mock(
            return_value=["sbatch", "--parsable", "--wait", str(script_path)]
        ),
        parse_submission_output=Mock(
            return_value={
                "provider": "slurm",
                "scheduler_job_id": "97531",
                "scheduler_cluster": "ares",
                "identity_source": "scheduler_submit_api",
            }
        ),
    )
    execution = SimpleNamespace(
        exit_code={"localhost": 9},
        stdout={"localhost": "97531;ares\n"},
    )
    executor = Mock()
    executor.run.return_value = execution
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.scheduler = {"name": "slurm"}
    pipeline.name = "example"
    pipeline.last_loaded_file = None
    pipeline.jarvis = SimpleNamespace(get_pipeline_shared_dir=lambda _name: tmp_path)
    pipeline.save = Mock()

    with patch("jarvis_cd.core.scheduler.make_scheduler", return_value=scheduler):
        with patch("jarvis_cd.shell.Exec", return_value=executor):
            with pytest.raises(RuntimeError, match="accepted, but the workload failed"):
                pipeline.submit(submit=True, wait=True)

    scheduler.parse_submission_output.assert_called_once_with("97531;ares\n")
    assert pipeline.last_submission["scheduler_job_id"] == "97531"
    assert pipeline.last_submission["scheduler_cluster"] == "ares"
    assert pipeline.last_submission["identity_source"] == "scheduler_submit_api"
    assert pipeline.last_submission["state"] == "workload_failed"
    assert pipeline.last_submission["submitted"] is True
    assert pipeline.last_submission["terminal"] is True
    assert pipeline.last_submission["submission_returncode"] == 9
    assert pipeline.last_submission["terminal_returncode"] == 9
    pipeline.save.assert_called_once_with()


def test_pipeline_records_true_submission_failure_without_job_identity(
    tmp_path: Path,
) -> None:
    script_path = tmp_path / "submit.slurm"
    scheduler = SimpleNamespace(
        NAME="slurm",
        hostfile=str(tmp_path / "hostfile.txt"),
        write_script=Mock(return_value=script_path),
        submit_command=Mock(return_value=["sbatch", "--parsable", str(script_path)]),
        parse_submission_output=Mock(side_effect=ValueError("not parsable")),
    )
    execution = SimpleNamespace(
        exit_code={"localhost": 1},
        stderr={"localhost": "sbatch: error: invalid account\n"},
        stdout={"localhost": ""},
    )
    executor = Mock()
    executor.run.return_value = execution
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.scheduler = {"name": "slurm"}
    pipeline.name = "example"
    pipeline.last_loaded_file = None
    pipeline.jarvis = SimpleNamespace(get_pipeline_shared_dir=lambda _name: tmp_path)
    pipeline.save = Mock()

    with patch("jarvis_cd.core.scheduler.make_scheduler", return_value=scheduler):
        with patch("jarvis_cd.shell.Exec", return_value=executor):
            with pytest.raises(
                RuntimeError,
                match="Scheduler submission failed.*invalid account",
            ):
                pipeline.submit(submit=True, wait=False)

    assert pipeline.last_submission["scheduler_job_id"] is None
    assert pipeline.last_submission["state"] == "submission_failed"
    assert pipeline.last_submission["submitted"] is False
    assert pipeline.last_submission["terminal"] is False
    assert pipeline.last_submission["scheduler_stderr"] == (
        "sbatch: error: invalid account"
    )
    assert pipeline.last_submission["submission_returncode"] == 1
    assert pipeline.last_submission["terminal_returncode"] is None
    pipeline.save.assert_called_once_with()
