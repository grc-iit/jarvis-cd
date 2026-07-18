"""Tests for generic builtin ParaView progress and batch execution."""

from __future__ import annotations

import ast
import io
import os
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from jarvis_cd.progress.paraview import (
    ParaViewProgressAdapter,
    ParaViewProgressReporter,
    adapter_from_package,
)
from jarvis_cd.progress import ProgressStore


def _structured_lines() -> tuple[str, list[dict[str, Any]]]:
    stream = io.StringIO()
    reporter = ParaViewProgressReporter(
        package_name="builtin.paraview",
        package_id="asteroid_render",
        execution_id="exec_asteroid",
        stream=stream,
    )
    first = reporter.frame_completed(1, total_frames=2, timestep=0.0)
    second = reporter.frame_completed(
        2,
        total_frames=2,
        timestep=1.0,
        output_path="frame-0002.png",
    )
    assert first is not None and second is not None
    return stream.getvalue(), [first, second]


def test_standalone_reporter_is_python_310_stdlib() -> None:
    """Ares pvbatch can parse the helper without importing JARVIS Python."""
    path = (
        Path(__file__).resolve().parents[3]
        / "builtin"
        / "builtin"
        / "paraview"
        / "progress_reporter.py"
    )
    source = path.read_text(encoding="utf-8")

    ast.parse(source, filename=str(path), feature_version=(3, 10))
    assert "import jarvis" not in source


def test_pvbatch_reports_only_completed_units_with_real_optional_total() -> None:
    """Determinate progress exists only when the render script knows its total."""
    lines, events = _structured_lines()
    assert events[0]["determinate"] is True
    assert events[1]["state"] == "completed"
    assert events[1]["metadata"]["completed_after_render"] is True

    adapter = ParaViewProgressAdapter(
        package_id="asteroid_render",
        run_id="exec_asteroid",
    )
    framed = (
        "[builtin.paraview] [START] BEGIN\n"
        + lines
        + "[builtin.paraview] [START] END\n"
    )
    records = adapter.observe_jarvis_stdout(framed)

    assert [record["current"] for record in records] == [1.0, 2.0]
    assert [record["total"] for record in records] == [2.0, 2.0]
    assert adapter.acceptance_progress_valid(records[-1]["metadata"])

    stream = io.StringIO()
    reporter = ParaViewProgressReporter(
        package_name="builtin.paraview",
        package_id="open_ended",
        execution_id="exec_open",
        stream=stream,
    )
    event = reporter.timestep_completed(1, timestep=3.5)
    assert event is not None
    assert event["determinate"] is False
    assert "total" not in event
    assert event["metadata"]["completion_signal"] == "pipeline_update_returned"
    assert event["metadata"]["completed_after_update"] is True
    assert "completed_after_render" not in event["metadata"]


def test_pvbatch_nonroot_rank_does_not_duplicate_progress() -> None:
    """Only MPI rank zero emits the shared pvbatch progress stream."""
    stream = io.StringIO()
    reporter = ParaViewProgressReporter(
        package_name="builtin.paraview",
        package_id="parallel_render",
        execution_id="exec_parallel",
        stream=stream,
        rank=1,
    )

    assert reporter.frame_completed(1, total_frames=2) is None
    assert stream.getvalue() == ""


def test_pvserver_readiness_is_an_indeterminate_state() -> None:
    """A real server-ready marker is not presented as a percentage."""
    adapter = ParaViewProgressAdapter(package_id="server", run_id="exec_server")

    records = adapter.observe_jarvis_stdout(
        "[builtin.paraview] [START] BEGIN\n"
        "Waiting for client... Accepting connection(s): localhost:11111\n"
        "[builtin.paraview] [START] END\n"
    )

    assert len(records) == 1
    assert "total" not in records[0]
    assert records[0]["current"] == 0.0
    metadata = records[0]["metadata"]
    assert metadata["determinate"] is False
    assert metadata["compatibility_projection"] == "indeterminate_state_ordinal"
    assert metadata["readiness_signal"] == "pvserver_accepting_connections"
    assert adapter.acceptance_progress_valid(metadata)

    waiting_adapter = ParaViewProgressAdapter(
        package_id="waiting-server",
        run_id="exec_waiting",
    )
    waiting = waiting_adapter.observe_jarvis_stdout(
        "[builtin.paraview] [START] BEGIN\n"
        "Waiting for client on port 11111\n"
        "[builtin.paraview] [START] END\n"
    )
    assert len(waiting) == 1
    waiting_metadata = waiting[0]["metadata"]
    assert waiting_metadata["readiness_signal"] == "pvserver_waiting_for_client"
    assert waiting[0]["message"] == "ParaView server is waiting for a client"
    assert waiting_adapter.acceptance_progress_valid(waiting_metadata)


def test_paraview_factory_is_package_local_and_path_is_not_config_owned(
    tmp_path: Path,
) -> None:
    """An arbitrary YAML path cannot become the authoritative sidecar."""
    assert adapter_from_package({"pkg_type": "builtin.lammps"}) is None
    adapter = adapter_from_package(
        {
            "pkg_type": "builtin.paraview",
            "pkg_id": "render_a",
            "progress": {
                "log_visibility": "shared",
                "path": str(tmp_path / "untrusted.jsonl"),
            },
        }
    )
    assert isinstance(adapter, ParaViewProgressAdapter)
    assert adapter.package_id == "render_a"
    assert adapter.progress_log_paths() == []


def _load_paraview_package() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[3]
        / "builtin"
        / "builtin"
        / "paraview"
        / "pkg.py"
    )
    spec = spec_from_file_location("test_paraview_runtime_package", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load ParaView package: {path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _CapturedExec:
    commands: list[tuple[str, Any]] = []
    exit_codes: dict[str, int] = {"localhost": 0}
    help_text = "--mesa\n--force-offscreen-rendering\n"

    def __init__(self, command: str, exec_info: Any) -> None:
        self.command = command
        self.exec_info = exec_info
        self.stdout = {"localhost": ""}
        self.stderr = {"localhost": ""}
        if command.endswith(" --help"):
            self.exit_code = {"localhost": 0}
            self.stdout["localhost"] = self.help_text
        else:
            self.exit_code = dict(self.exit_codes)
            self.commands.append((command, exec_info))

    def run(self) -> "_CapturedExec":
        return self


class _ResolvedWhich:
    """Resolve the requested launcher inside a deterministic package PATH."""

    calls: list[tuple[str, Any]] = []

    def __init__(self, executable: str, exec_info: Any) -> None:
        self.executable = executable
        self.exec_info = exec_info
        self.exit_code = {"localhost": 0}
        self.stdout = {"localhost": f"/runtime/paraview/bin/{executable}\n"}
        self.stderr = {"localhost": ""}
        self.calls.append((executable, exec_info))

    def run(self) -> "_ResolvedWhich":
        return self


def test_generic_paraview_batch_resolves_runtime_and_hostfile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The package resolves pvbatch and headless flags in its execution context."""
    module = _load_paraview_package()
    package = object.__new__(module.Paraview)
    assert module.__file__ is not None
    package.pkg_dir = str(Path(module.__file__).resolve().parent)
    hostfile = object()
    package.pkg_id = "asteroid_render"
    package.pkg_type = "builtin.paraview"
    package.global_id = "paraview_progress.asteroid_render"
    package.shared_dir = str(tmp_path / "shared" / "asteroid_render")
    package.private_dir = str(tmp_path / "private" / "asteroid_render")
    package.config = {
        "cwd": str(tmp_path),
        "force_offscreen_rendering": True,
        "mode": "batch",
        "nprocs": 2,
        "ppn": 2,
        "script": str(tmp_path / "asteroid script.py"),
        "script_args": "--frames 2",
    }
    package.pipeline = SimpleNamespace(get_hostfile=lambda: hostfile)
    package.env = {}
    package.mod_env = {
        "JARVIS_EXECUTION_ID": "exec_asteroid",
        "JARVIS_PACKAGE_ID": "asteroid_render",
        "JARVIS_PROGRESS_PATH": str(tmp_path / "progress.jsonl"),
    }
    _CapturedExec.commands = []
    _CapturedExec.exit_codes = {"localhost": 0}
    _ResolvedWhich.calls = []
    monkeypatch.setattr(module, "Exec", _CapturedExec)
    monkeypatch.setattr(module, "Which", _ResolvedWhich)

    package.start()

    command, exec_info = _CapturedExec.commands[0]
    assert command.startswith("/runtime/paraview/bin/pvbatch --mesa")
    assert "--force-offscreen-rendering" not in command
    assert exec_info.hostfile is hostfile
    assert exec_info.env["JARVIS_PACKAGE_NAME"] == "builtin.paraview"
    assert exec_info.env["JARVIS_PACKAGE_ID"] == "asteroid_render"
    assert exec_info.env["JARVIS_PROGRESS_TRANSPORT"] == "stdout"
    assert [call[0] for call in _ResolvedWhich.calls] == ["pvbatch"]
    assert _ResolvedWhich.calls[0][1].hostfile is hostfile
    reporter_path = Path(exec_info.env["JARVIS_PARAVIEW_REPORTER"])
    assert reporter_path.parent == Path(package.shared_dir) / ".jarvis-progress"
    assert module.__file__ is not None
    assert (
        reporter_path.read_bytes()
        == (
            Path(module.__file__).resolve().parent / "progress_reporter.py"
        ).read_bytes()
    )

    lines, _ = _structured_lines()
    for line in lines.splitlines(keepends=True):
        exec_info.line_callback("stdout", line)
    events = ProgressStore(tmp_path / "progress.jsonl").read_all()
    assert [event.current for event in events] == [1.0, 2.0]


def test_single_process_container_stages_reporter_and_uses_local_exec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A mounted package path carries the reporter into a one-rank container."""
    module = _load_paraview_package()
    package = object.__new__(module.Paraview)
    assert module.__file__ is not None
    package.pkg_dir = str(Path(module.__file__).resolve().parent)
    hostfile = object()
    package.pkg_id = "container_render"
    package.pkg_type = "builtin.paraview"
    package.global_id = "paraview_progress.container_render"
    package.shared_dir = str(tmp_path / "shared" / "container_render")
    package.private_dir = str(tmp_path / "private" / "container_render")
    package.config = {
        "cwd": str(tmp_path),
        "deploy_mode": "container",
        "force_offscreen_rendering": True,
        "mode": "batch",
        "nprocs": 1,
        "ppn": 1,
        "script": str(tmp_path / "render.py"),
        "script_args": "--frames 2",
    }
    package.pipeline = SimpleNamespace(
        container_engine="apptainer",
        execution_container_name="paraview-progress-exec",
        get_hostfile=lambda: hostfile,
        name="paraview_progress",
        _has_containerized_packages=lambda: True,
    )
    package.env = {}
    package.mod_env = {
        "JARVIS_EXECUTION_ID": "exec_container",
        "JARVIS_PACKAGE_ID": "container_render",
        "JARVIS_PROGRESS_PATH": str(tmp_path / "progress.jsonl"),
    }
    _CapturedExec.commands = []
    _CapturedExec.exit_codes = {"localhost": 0}
    _ResolvedWhich.calls = []
    monkeypatch.setattr(module, "Exec", _CapturedExec)
    monkeypatch.setattr(module, "Which", _ResolvedWhich)

    package.start()

    _, exec_info = _CapturedExec.commands[0]
    assert isinstance(exec_info, module.LocalExecInfo)
    assert exec_info.container == "apptainer"
    assert exec_info.container_image == "paraview-progress-exec"
    assert exec_info.shared_dir == package.shared_dir
    assert exec_info.bind_mounts == [
        f"{package.shared_dir}:{package.shared_dir}",
        f"{package.private_dir}:{package.private_dir}",
    ]
    reporter_path = Path(exec_info.env["JARVIS_PARAVIEW_REPORTER"])
    assert reporter_path.is_file()
    assert reporter_path.parent == Path(package.shared_dir) / ".jarvis-progress"
    assert exec_info.env["PYTHONPATH"].split(os.pathsep)[0] == str(reporter_path.parent)
    probe_info = _ResolvedWhich.calls[0][1]
    assert probe_info.container == "apptainer"
    assert probe_info.container_image == "paraview-progress-exec"


@pytest.mark.parametrize(
    ("mode", "operation"),
    [("batch", "ParaView batch"), ("server", "ParaView server")],
)
def test_paraview_exit_failure_propagates_to_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    operation: str,
) -> None:
    """A failed pvbatch or pvserver process cannot become a completed run."""
    module = _load_paraview_package()
    package = object.__new__(module.Paraview)
    package.pkg_id = "failed_render"
    package.pkg_type = "builtin.paraview"
    package.shared_dir = str(tmp_path / "shared" / "failed_render")
    package.private_dir = str(tmp_path / "private" / "failed_render")
    package.config = {
        "cwd": str(tmp_path),
        "force_offscreen_rendering": True,
        "mode": mode,
        "nprocs": 1,
        "ppn": 1,
        "port_id": 11111,
        "script": str(tmp_path / "render.py"),
        "script_args": "",
        "time_out": 10,
    }
    package.pipeline = SimpleNamespace(get_hostfile=lambda: object())
    package.env = {}
    package.mod_env = {}
    _CapturedExec.commands = []
    _CapturedExec.exit_codes = {"compute-1": 7}
    _ResolvedWhich.calls = []
    monkeypatch.setattr(module, "Exec", _CapturedExec)
    monkeypatch.setattr(module, "Which", _ResolvedWhich)

    with pytest.raises(RuntimeError, match=rf"{operation} failed.*compute-1=7"):
        package.start()
