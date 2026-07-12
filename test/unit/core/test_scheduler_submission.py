"""Structured scheduler-submission contract tests."""

import multiprocessing
import os
import shlex
import subprocess
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest
import yaml

from jarvis_cd.core.config import Jarvis
from jarvis_cd.core.pipeline import (
    Pipeline,
    _atomic_yaml_dump,
    _remove_execution_tree,
    _unlock_execution_input_for_cleanup,
)
from jarvis_cd.core.scheduler import SlurmScheduler


def _prepare_snapshot_submission(pipeline: Pipeline, tmp_path: Path) -> None:
    """Give a low-level Pipeline test double an isolated snapshot result."""
    pipeline.last_submission = None
    pipeline._execution_snapshot_dir = None
    pipeline._write_execution_snapshot = Mock(
        return_value=(
            tmp_path / "runtime",
            tmp_path / "input",
            "a" * 64,
        )
    )


def _real_pipeline(tmp_path: Path) -> Pipeline:
    """Create a real Pipeline whose config/shared/private roots are test-local."""
    repository_root = Path(__file__).parents[3]
    jarvis = SimpleNamespace(
        hostfile=None,
        get_pipeline_dir=lambda name: tmp_path / "config" / "pipelines" / name,
        get_pipeline_shared_dir=lambda name: tmp_path / "shared" / name,
        get_pipeline_private_dir=lambda name: tmp_path / "private" / name,
        get_builtin_repo_path=lambda: repository_root / "builtin",
        set_current_pipeline=lambda _name: None,
    )
    with patch("jarvis_cd.core.pipeline.Jarvis.get_instance", return_value=jarvis):
        pipeline = Pipeline()
    pipeline.create("example")
    pipeline.scheduler = {"name": "slurm", "nodes": 1}
    pipeline.packages = [
        {
            "pkg_type": "builtin.echo",
            "pkg_id": "echo",
            "pkg_name": "echo",
            "global_id": "example.echo",
            "config": {},
        }
    ]
    pipeline.env = {"PATH": "/spack/a/bin", "SPACK_ROOT": "/opt/spack"}
    pipeline.last_loaded_file = "/operator/source.yaml"
    pipeline.save()
    return pipeline


def _load_real_scheduler_snapshot(
    jarvis_root: str,
    runtime_dir: str,
    result_queue: Any,
) -> None:
    """Load one execution snapshot in an independent real Jarvis process."""
    Jarvis._instance = None
    Jarvis.get_instance(jarvis_root)
    os.environ["JARVIS_PIPELINE_SNAPSHOT_DIR"] = runtime_dir
    pipeline = Pipeline()
    pipeline.load("yaml", str(Path(runtime_dir) / "pipeline.yaml"))
    result_queue.put(pipeline._execution_id)


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
    "spec",
    [
        {"name": "slurm", "job_name": "safe\n#SBATCH --nodes=999"},
        {"name": "slurm", "hostfile": "/tmp/hosts\nrm -rf /"},
        {"name": "slurm", "bad-key": "value"},
        {"name": "slurm", "sbatch_args": ["--nodes=1\n#SBATCH --exclusive"]},
        {"name": "slurm", "sbatch_args": ["nodes=1"]},
        {"name": "slurm", "suffix": "-40g\nattacker"},
        {"name": "slurm", "pre_cmds": ["echo safe\necho injected"]},
    ],
)
def test_slurm_renderer_rejects_structural_injection(
    tmp_path: Path,
    spec: dict[str, object],
) -> None:
    """Untrusted scheduler values cannot create directives or script lines."""
    with pytest.raises(ValueError):
        SlurmScheduler(spec, tmp_path)


def test_slurm_renderer_atomically_publishes_allocation_hostfile(
    tmp_path: Path,
) -> None:
    """The rendered script stages, flushes, and renames its hostfile."""
    hostfile = tmp_path / "allocation hosts.txt"
    script = SlurmScheduler(
        {"name": "slurm", "hostfile": str(hostfile)},
        tmp_path,
    ).render()

    assert f"jarvis_hostfile={shlex.quote(str(hostfile))}" in script
    assert "jarvis_hostfile_tmp=$(mktemp -- " in script
    assert "if ! { scontrol show hostnames" in script
    assert 'if [ ! -s "$jarvis_hostfile_tmp" ]; then' in script
    assert "os.fsync(stream.fileno())" in script
    assert "os.replace(temporary_path, final_path)" in script
    assert "os.fsync(directory)" in script
    assert '> "$jarvis_hostfile_tmp"' in script
    assert f"> {shlex.quote(str(hostfile))}" not in script


def test_slurm_hostfile_generation_failure_preserves_previous_file(
    tmp_path: Path,
) -> None:
    """A failed scontrol command cannot truncate a prior valid hostfile."""
    hostfile = tmp_path / "hostfile.txt"
    hostfile.write_text("known-good-node\n", encoding="utf-8")
    script = SlurmScheduler(
        {"name": "slurm", "hostfile": str(hostfile)},
        tmp_path,
    ).render()

    if os.name == "nt":
        assert 'if ! { scontrol show hostnames "$SLURM_JOB_NODELIST"; }' in script
        assert '> "$jarvis_hostfile_tmp"' in script
        return

    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    scontrol = executable_dir / "scontrol"
    scontrol.write_text(
        "#!/bin/sh\nprintf 'partial-node\\n'\nexit 23\n",
        encoding="utf-8",
    )
    scontrol.chmod(0o700)
    environment = dict(os.environ)
    environment["PATH"] = f"{executable_dir}{os.pathsep}{environment['PATH']}"
    environment["SLURM_JOB_NODELIST"] = "ignored"

    completed = subprocess.run(
        ["bash", "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    assert completed.returncode != 0
    assert "Could not expand the SLURM allocation hostfile" in completed.stderr
    assert hostfile.read_text(encoding="utf-8") == "known-good-node\n"
    assert list(tmp_path.glob(".hostfile.txt.jarvis.*")) == []


def test_atomic_yaml_dump_preserves_existing_document_when_serialization_fails(
    tmp_path: Path,
) -> None:
    target = tmp_path / "pipeline.yaml"
    target.write_text("state: previous\n", encoding="utf-8")

    def fail_after_partial_write(value, stream, *, default_flow_style):
        del value, default_flow_style
        stream.write("state: partial\n")
        raise RuntimeError("serialization interrupted")

    with patch(
        "jarvis_cd.core.pipeline.yaml.dump", side_effect=fail_after_partial_write
    ):
        with pytest.raises(RuntimeError, match="serialization interrupted"):
            _atomic_yaml_dump(target, {"state": "new"})

    assert target.read_text(encoding="utf-8") == "state: previous\n"
    assert list(tmp_path.glob(".pipeline.yaml.*.tmp")) == []


def test_scheduler_script_fdopen_failure_closes_mkstemp_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scheduler script setup failures do not leak the raw mkstemp handle."""
    import jarvis_cd.core.scheduler as scheduler_module

    descriptors: list[int] = []
    real_mkstemp = scheduler_module.tempfile.mkstemp

    def capture_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        descriptor, name = real_mkstemp(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor, name

    monkeypatch.setattr(scheduler_module.tempfile, "mkstemp", capture_mkstemp)
    monkeypatch.setattr(
        scheduler_module.os,
        "fdopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("fdopen failed")),
    )
    scheduler = SlurmScheduler({"name": "slurm"}, tmp_path)

    with pytest.raises(OSError, match="fdopen failed"):
        scheduler.write_script()

    assert len(descriptors) == 1
    with pytest.raises(OSError):
        scheduler_module.os.fstat(descriptors[0])
    assert list(tmp_path.glob(".submit.slurm.*.tmp")) == []


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
    pipeline.scheduler = {"name": "slurm", "job_name": "clio-relay-unit-marker"}
    pipeline.name = "example"
    pipeline.last_loaded_file = None
    pipeline.jarvis = SimpleNamespace(get_pipeline_shared_dir=lambda _name: tmp_path)
    _prepare_snapshot_submission(pipeline, tmp_path)
    saved_submissions: list[dict[str, object]] = []
    pipeline.save = Mock(
        side_effect=lambda: saved_submissions.append(deepcopy(pipeline.last_submission))
    )

    with patch("jarvis_cd.core.scheduler.make_scheduler", return_value=scheduler):
        with patch("jarvis_cd.shell.Exec", return_value=executor):
            returned = pipeline.submit(submit=True, wait=False)

    assert returned == script_path
    assert pipeline.last_submission == {
        "schema_version": "jarvis.scheduler.submission.v1",
        "execution_id": pipeline.last_submission["execution_id"],
        "provider": "slurm",
        "script_path": str(script_path),
        "hostfile_path": str(tmp_path / "hostfile.txt"),
        "pipeline_snapshot_path": str(tmp_path / "runtime"),
        "pipeline_input_path": str(tmp_path / "input"),
        "execution_root_path": pipeline.last_submission["execution_root_path"],
        "pipeline_snapshot_sha256": "a" * 64,
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
        "reconciliation_marker": "clio-relay-unit-marker",
    }
    scheduler.parse_submission_output.assert_called_once_with("24680;ares\n")
    assert pipeline.save.call_count == 4
    assert saved_submissions[0] is None
    assert saved_submissions[1]["state"] == "scripted"
    assert saved_submissions[1]["scheduler_job_id"] is None
    assert saved_submissions[1]["reconciliation_marker"] == "clio-relay-unit-marker"
    assert saved_submissions[2]["scheduler_job_id"] == "24680"
    assert saved_submissions[2]["submitted"] is True
    assert saved_submissions[3]["state"] == "submitted"


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
    _prepare_snapshot_submission(pipeline, tmp_path)
    pipeline.save = Mock()

    with patch("jarvis_cd.core.scheduler.make_scheduler", return_value=scheduler):
        with patch("jarvis_cd.shell.Exec", return_value=executor):
            with pytest.raises(RuntimeError, match="structured job identity"):
                pipeline.submit(submit=True, wait=False)

    assert pipeline.last_submission["scheduler_job_id"] is None
    assert pipeline.last_submission["state"] == "identity_failed"
    assert pipeline.save.call_count == 3


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
    _prepare_snapshot_submission(pipeline, tmp_path)
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
    assert pipeline.save.call_count == 4


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
    _prepare_snapshot_submission(pipeline, tmp_path)
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
    assert pipeline.last_submission["terminal"] is True
    assert pipeline.last_submission["scheduler_stderr"] == (
        "sbatch: error: invalid account"
    )
    assert pipeline.last_submission["submission_returncode"] == 1
    assert pipeline.last_submission["terminal_returncode"] is None
    assert pipeline.save.call_count == 3


def test_scheduler_submission_uses_isolated_pipeline_environment_and_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued job references only its immutable execution-scoped snapshot."""
    pipeline = _real_pipeline(tmp_path)

    script_a = pipeline.submit(submit=False, execution_id="execution-a")
    submission_a = dict(pipeline.last_submission)
    runtime_a = Path(submission_a["pipeline_snapshot_path"])
    input_a = Path(submission_a["pipeline_input_path"])
    environment_a = yaml.safe_load((runtime_a / "environment.yaml").read_text())

    pipeline.env = {"PATH": "/spack/b/bin", "SPACK_ROOT": "/opt/spack"}
    pipeline.save()
    script_b = pipeline.submit(submit=False, execution_id="execution-b")
    submission_b = dict(pipeline.last_submission)
    runtime_b = Path(submission_b["pipeline_snapshot_path"])

    assert script_a != script_b
    assert script_a.parent.name == "execution-a"
    assert script_b.parent.name == "execution-b"
    assert submission_a["hostfile_path"] != submission_b["hostfile_path"]
    assert yaml.safe_load((runtime_a / "environment.yaml").read_text()) == environment_a
    assert yaml.safe_load((runtime_b / "environment.yaml").read_text())["PATH"] == (
        "/spack/b/bin"
    )
    script_text = script_a.read_text(encoding="utf-8")
    assert "JARVIS_PIPELINE_SNAPSHOT_DIR" in script_text
    assert str(runtime_a) in script_text
    assert "jarvis cd example" not in script_text
    assert len(str(submission_a["pipeline_snapshot_sha256"])) == 64
    assert (input_a / "pipeline.yaml").is_file()

    monkeypatch.setenv("JARVIS_PIPELINE_SNAPSHOT_DIR", str(runtime_a))
    with patch(
        "jarvis_cd.core.pipeline.Jarvis.get_instance",
        return_value=pipeline.jarvis,
    ):
        loaded = Pipeline()
    loaded.load("yaml", str(runtime_a / "pipeline.yaml"))
    assert loaded.env == environment_a
    loaded.env["PATH"] = "/runtime-only/bin"
    loaded.save()
    canonical_env = yaml.safe_load(
        (pipeline.jarvis.get_pipeline_dir("example") / "environment.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert canonical_env["PATH"] == "/spack/b/bin"
    assert yaml.safe_load((runtime_a / "environment.yaml").read_text())["PATH"] == (
        "/runtime-only/bin"
    )


def test_queued_snapshot_round_trips_container_gpu_and_expanded_tmp_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queued execution snapshots retain the effective container runtime state."""
    pipeline = _real_pipeline(tmp_path)
    node_tmp = tmp_path / "node-local-tmp"
    monkeypatch.setenv("JARVIS_NODE_TMP", str(node_tmp))
    pipeline.container_gpu = True
    pipeline.tmp_bind_root = os.path.expandvars("$JARVIS_NODE_TMP")
    pipeline.save()

    pipeline.submit(submit=False, execution_id="container-runtime")
    runtime = Path(pipeline.last_submission["pipeline_snapshot_path"])
    snapshot = yaml.safe_load((runtime / "pipeline.yaml").read_text(encoding="utf-8"))

    assert snapshot["container_gpu"] is True
    assert snapshot["tmp_bind_root"] == str(node_tmp)

    monkeypatch.setenv("JARVIS_PIPELINE_SNAPSHOT_DIR", str(runtime))
    with patch(
        "jarvis_cd.core.pipeline.Jarvis.get_instance",
        return_value=pipeline.jarvis,
    ):
        loaded = Pipeline()
    loaded.load("yaml", str(runtime / "pipeline.yaml"))

    assert loaded.container_gpu is True
    assert loaded.tmp_bind_root == str(node_tmp)


def test_concurrent_real_snapshot_loads_do_not_change_operator_selection(
    tmp_path: Path,
) -> None:
    """Independent queued jobs cannot race on the real global Jarvis config."""
    original_instance = Jarvis._instance
    processes: list[multiprocessing.Process] = []
    result_queue: Any = None
    try:
        Jarvis._instance = None
        jarvis_root = tmp_path / "jarvis-root"
        jarvis = Jarvis.get_instance(str(jarvis_root))
        jarvis.initialize(
            str(tmp_path / "config"),
            str(tmp_path / "private"),
            str(tmp_path / "shared"),
            force=False,
        )

        runtimes: list[Path] = []
        for name in ("alpha", "beta"):
            pipeline = Pipeline()
            pipeline.create(name)
            pipeline.scheduler = {"name": "slurm", "nodes": 1}
            pipeline.packages = [
                {
                    "pkg_type": "builtin.echo",
                    "pkg_id": "echo",
                    "pkg_name": "echo",
                    "global_id": f"{name}.echo",
                    "config": {},
                }
            ]
            pipeline.env = {"PATH": os.environ.get("PATH", "")}
            pipeline.save()
            pipeline.submit(submit=False, execution_id=f"{name}-queued")
            runtimes.append(Path(pipeline.last_submission["pipeline_snapshot_path"]))

        jarvis.set_current_pipeline("operator-selection")
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        processes = [
            context.Process(
                target=_load_real_scheduler_snapshot,
                args=(str(jarvis_root), str(runtime), result_queue),
            )
            for runtime in runtimes
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=30)
            assert not process.is_alive()
            assert process.exitcode == 0

        loaded_ids = {result_queue.get(timeout=5) for _process in processes}
        assert loaded_ids == {"alpha-queued", "beta-queued"}
        persisted_config = yaml.safe_load(
            jarvis.config_file.read_text(encoding="utf-8")
        )
        assert persisted_config["current_pipeline"] == "operator-selection"
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        if result_queue is not None:
            result_queue.close()
            result_queue.join_thread()
        Jarvis._instance = original_instance


def test_scheduler_submission_rejects_invalid_or_reused_execution_ids(
    tmp_path: Path,
) -> None:
    """Execution identities cannot escape or overwrite an existing snapshot."""
    pipeline = _real_pipeline(tmp_path)

    with pytest.raises(ValueError, match="execution_id"):
        pipeline.submit(submit=False, execution_id="../outside")

    script = pipeline.submit(submit=False, execution_id="stable-id")
    original = script.read_bytes()
    with pytest.raises(FileExistsError):
        pipeline.submit(submit=False, execution_id="stable-id")

    assert script.read_bytes() == original
    assert not (tmp_path / "shared" / "outside").exists()


def test_scheduler_submission_removes_incomplete_execution_snapshot(
    tmp_path: Path,
) -> None:
    """A pre-submit snapshot failure leaves no execution-shaped residue."""
    pipeline = _real_pipeline(tmp_path)
    pipeline._write_execution_snapshot = Mock(
        side_effect=RuntimeError("snapshot failed")
    )

    with pytest.raises(RuntimeError, match="snapshot failed"):
        pipeline.submit(submit=False, execution_id="incomplete")

    assert not (tmp_path / "shared" / "example" / "executions" / "incomplete").exists()


def test_real_packages_use_execution_scoped_config_shared_and_private_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two queued copies of a real package never share mutable directories."""
    pipeline = _real_pipeline(tmp_path)
    pipeline.submit(submit=False, execution_id="package-a")
    runtime_a = Path(pipeline.last_submission["pipeline_snapshot_path"])
    pipeline.submit(submit=False, execution_id="package-b")
    runtime_b = Path(pipeline.last_submission["pipeline_snapshot_path"])

    loaded: list[Pipeline] = []
    for runtime in (runtime_a, runtime_b):
        monkeypatch.setenv("JARVIS_PIPELINE_SNAPSHOT_DIR", str(runtime))
        with patch(
            "jarvis_cd.core.pipeline.Jarvis.get_instance",
            return_value=pipeline.jarvis,
        ):
            execution = Pipeline()
        execution.load("yaml", str(runtime / "pipeline.yaml"))
        loaded.append(execution)
    monkeypatch.delenv("JARVIS_PIPELINE_SNAPSHOT_DIR")

    packages = [
        execution._load_package_instance(execution.packages[0], execution.env)
        for execution in loaded
    ]
    roots = []
    for package in packages:
        roots.append(
            (
                Path(package.config_dir),
                Path(package.shared_dir),
                Path(package.private_dir),
            )
        )
    assert roots[0] != roots[1]
    for index, execution in enumerate(loaded):
        execution_root = Path(execution._execution_root)
        assert roots[index] == (
            execution_root / "runtime" / "packages" / "echo",
            execution_root / "shared" / "echo",
            execution_root / "private" / "echo",
        )
    assert loaded[0].execution_container_name != loaded[1].execution_container_name
    assert packages[0].deploy_image_name() != packages[1].deploy_image_name()

    def write_package_state(index: int) -> None:
        for directory in roots[index]:
            (directory / "owner.txt").write_text(str(index), encoding="utf-8")

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(write_package_state, (0, 1)))

    for index, directories in enumerate(roots):
        for directory in directories:
            assert (directory / "owner.txt").read_text(encoding="utf-8") == str(index)
    named_config = pipeline.jarvis.get_pipeline_dir("example") / "packages" / "echo"
    named_shared = pipeline.jarvis.get_pipeline_shared_dir("example") / "echo"
    named_private = pipeline.jarvis.get_pipeline_private_dir("example") / "echo"
    assert not named_config.exists()
    assert not named_shared.exists()
    assert not named_private.exists()


def test_queued_execution_blocks_destroy_and_requires_verified_exact_cleanup(
    tmp_path: Path,
) -> None:
    """Destroy cannot erase an async scheduler snapshot still potentially active."""
    pipeline = _real_pipeline(tmp_path)
    execution = SimpleNamespace(
        exit_code={"localhost": 0},
        stdout={"localhost": "31415;cluster\n"},
        stderr={"localhost": ""},
    )
    executor = Mock()
    executor.run.return_value = execution
    with patch("jarvis_cd.shell.Exec", return_value=executor):
        pipeline.submit(submit=True, wait=False, execution_id="queued-job")

    execution_root = tmp_path / "shared" / "example" / "executions" / "queued-job"
    with pytest.raises(RuntimeError, match="execution snapshots"):
        pipeline.destroy()
    assert execution_root.is_dir()
    assert pipeline.jarvis.get_pipeline_dir("example").is_dir()
    with pytest.raises(RuntimeError, match="not terminal"):
        pipeline.cleanup_executions(["queued-job"])
    with pytest.raises(ValueError, match="execution_id"):
        pipeline.cleanup_executions(["../queued-job"], force=True)

    assert pipeline.cleanup_executions(
        ["queued-job"], terminal_verified=["queued-job"]
    ) == ["queued-job"]
    assert not execution_root.exists()


def test_script_only_execution_is_explicitly_cleanup_eligible(tmp_path: Path) -> None:
    """A script-only root is terminal but still removed only by exact ID."""
    pipeline = _real_pipeline(tmp_path)
    pipeline.submit(submit=False, execution_id="script-only")

    removed = pipeline.cleanup_executions(["script-only"])

    assert removed == ["script-only"]
    assert not (tmp_path / "shared" / "example" / "executions" / "script-only").exists()


def test_failed_execution_cleanup_leaves_resumable_tombstone(
    tmp_path: Path,
) -> None:
    """A deletion failure never restores a possibly damaged live execution."""
    pipeline = _real_pipeline(tmp_path)
    pipeline.submit(submit=False, execution_id="cleanup-rollback")
    execution_root = tmp_path / "shared" / "example" / "executions" / "cleanup-rollback"
    quarantine = execution_root.parent / ".remove-cleanup-rollback"

    with (
        patch(
            "jarvis_cd.core.pipeline._unlock_execution_input_for_cleanup",
        ),
        patch(
            "jarvis_cd.core.pipeline._remove_execution_tree",
            side_effect=PermissionError("injected deletion failure"),
        ),
        pytest.raises(PermissionError, match="injected deletion failure"),
    ):
        pipeline.cleanup_executions(["cleanup-rollback"])

    assert not execution_root.exists()
    assert quarantine.is_dir()
    assert pipeline.cleanup_executions(["cleanup-rollback"]) == ["cleanup-rollback"]
    assert not quarantine.exists()


def test_partial_execution_cleanup_never_restores_damaged_root(tmp_path: Path) -> None:
    """A retry resumes the tombstone after an injected partial tree deletion."""
    pipeline = _real_pipeline(tmp_path)
    pipeline.submit(submit=False, execution_id="partial-cleanup")
    execution_root = tmp_path / "shared" / "example" / "executions" / "partial-cleanup"
    quarantine = execution_root.parent / ".remove-partial-cleanup"

    def delete_one_file_then_fail(*_args: object, **_kwargs: object) -> None:
        (quarantine / "input" / "pipeline.yaml").unlink()
        raise OSError("injected partial deletion")

    with (
        patch(
            "jarvis_cd.core.pipeline._remove_execution_tree",
            side_effect=delete_one_file_then_fail,
        ),
        pytest.raises(OSError, match="injected partial deletion"),
    ):
        pipeline.cleanup_executions(["partial-cleanup"])

    assert not execution_root.exists()
    assert quarantine.is_dir()
    assert not (quarantine / "input" / "pipeline.yaml").exists()
    assert pipeline.cleanup_executions(["partial-cleanup"]) == ["partial-cleanup"]
    assert not quarantine.exists()


def test_unlock_failure_leaves_execution_under_cleanup_tombstone(
    tmp_path: Path,
) -> None:
    """A post-fchmod failure cannot return an unsealed execution to service."""
    pipeline = _real_pipeline(tmp_path)
    pipeline.submit(submit=False, execution_id="unlock-failure")
    execution_root = tmp_path / "shared" / "example" / "executions" / "unlock-failure"
    quarantine = execution_root.parent / ".remove-unlock-failure"

    def fail_after_mode_change(
        _execution_root: Path,
        *,
        root_descriptor: int | None = None,
    ) -> None:
        if os.name != "nt":
            assert root_descriptor is not None
            descriptor = os.open(
                "input",
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                dir_fd=root_descriptor,
            )
            try:
                os.fchmod(descriptor, 0o700)
            finally:
                os.close(descriptor)
        raise OSError("injected unlock durability failure")

    with (
        patch(
            "jarvis_cd.core.pipeline._unlock_execution_input_for_cleanup",
            side_effect=fail_after_mode_change,
        ),
        pytest.raises(OSError, match="injected unlock durability failure"),
    ):
        pipeline.cleanup_executions(["unlock-failure"])

    assert not execution_root.exists()
    assert quarantine.is_dir()
    if os.name != "nt":
        assert ((quarantine / "input").stat().st_mode & 0o777) == 0o700
    assert pipeline.cleanup_executions(["unlock-failure"]) == ["unlock-failure"]
    assert not quarantine.exists()


def test_cleanup_receipt_finalizes_after_tree_was_already_removed(
    tmp_path: Path,
) -> None:
    """A crash after tree deletion leaves a receipt-only retry state."""
    pipeline = _real_pipeline(tmp_path)
    pipeline.submit(submit=False, execution_id="receipt-only")
    execution_root = tmp_path / "shared" / "example" / "executions" / "receipt-only"
    quarantine = execution_root.parent / ".remove-receipt-only"

    def remove_tree_then_fail(
        path: Path,
        *,
        executions_descriptor: int | None,
        root_descriptor: int | None,
        windows_root_handle: int | None,
        root_identity: tuple[int, int],
    ) -> None:
        _remove_execution_tree(
            path,
            executions_descriptor=executions_descriptor,
            root_descriptor=root_descriptor,
            windows_root_handle=windows_root_handle,
            root_identity=root_identity,
        )
        raise OSError("injected post-tree crash")

    with (
        patch(
            "jarvis_cd.core.pipeline._remove_execution_tree",
            side_effect=remove_tree_then_fail,
        ),
        pytest.raises(OSError, match="injected post-tree crash"),
    ):
        pipeline.cleanup_executions(["receipt-only"])

    assert not execution_root.exists()
    assert not quarantine.exists()
    receipts = list(execution_root.parent.glob(".remove-receipt-only-*.json"))
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.is_file()
    assert pipeline.cleanup_executions(["receipt-only"]) == ["receipt-only"]
    assert not receipt.exists()


def test_cleanup_unlock_never_follows_execution_root_symlink(tmp_path: Path) -> None:
    """POSIX cleanup opens both the root and input relative to trusted handles."""
    if os.name == "nt":
        _unlock_execution_input_for_cleanup(tmp_path / "missing")
        return

    outside = tmp_path / "outside"
    outside_input = outside / "input"
    outside_input.mkdir(parents=True)
    outside_input.chmod(0o500)
    quarantine = tmp_path / ".remove-symlink"
    quarantine.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        _unlock_execution_input_for_cleanup(quarantine)

    assert (outside_input.stat().st_mode & 0o777) == 0o500


def test_cleanup_never_deletes_root_replacement_after_validation(
    tmp_path: Path,
) -> None:
    """A validated tombstone swap fails closed without deleting the replacement."""
    pipeline = _real_pipeline(tmp_path)
    pipeline.submit(submit=False, execution_id="race-delete")
    executions = tmp_path / "shared" / "example" / "executions"
    quarantine = executions / ".remove-race-delete"
    stolen = executions / ".stolen-owned"
    victim = executions / ".victim-source"
    victim.mkdir()
    (victim / "sentinel.txt").write_text("keep", encoding="utf-8")

    def unlock_then_swap(
        execution_root: Path,
        *,
        root_descriptor: int | None = None,
    ) -> None:
        _unlock_execution_input_for_cleanup(
            execution_root,
            root_descriptor=root_descriptor,
        )
        os.replace(quarantine, stolen)
        os.replace(victim, quarantine)

    with (
        patch(
            "jarvis_cd.core.pipeline._unlock_execution_input_for_cleanup",
            side_effect=unlock_then_swap,
        ),
        pytest.raises(PermissionError if os.name == "nt" else RuntimeError),
    ):
        pipeline.cleanup_executions(["race-delete"])

    if os.name == "nt":
        assert (victim / "sentinel.txt").read_text(encoding="utf-8") == "keep"
        assert quarantine.is_dir()
        assert not stolen.exists()
    else:
        assert (quarantine / "sentinel.txt").read_text(encoding="utf-8") == "keep"
        assert stolen.is_dir()


def test_live_cleanup_revalidates_identity_after_rename(tmp_path: Path) -> None:
    """A candidate replacement in the detach window is never accepted."""
    pipeline = _real_pipeline(tmp_path)
    pipeline.submit(submit=False, execution_id="race-live")
    executions = tmp_path / "shared" / "example" / "executions"
    candidate = executions / "race-live"
    quarantine = executions / ".remove-race-live"
    stolen = executions / ".stolen-live"
    victim = executions / ".victim-live"
    victim.mkdir()
    (victim / "sentinel.txt").write_text("keep", encoding="utf-8")
    real_replace = os.replace
    swapped = False

    def replace_after_swap(
        source: object,
        destination: object,
        **kwargs: object,
    ) -> None:
        nonlocal swapped
        if not swapped and Path(str(source)).name == "race-live":
            swapped = True
            real_replace(candidate, stolen)
            real_replace(victim, candidate)
        real_replace(source, destination, **kwargs)

    with (
        patch("jarvis_cd.core.pipeline.os.replace", side_effect=replace_after_swap),
        pytest.raises(RuntimeError, match="execution changed before cleanup"),
    ):
        pipeline.cleanup_executions(["race-live"])

    assert swapped is True
    assert (quarantine / "sentinel.txt").read_text(encoding="utf-8") == "keep"
    assert stolen.is_dir()
