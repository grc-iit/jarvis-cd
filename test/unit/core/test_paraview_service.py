"""Generic ParaView HTTP/SSE service and artifact-boundary tests."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, cast

import pytest
from jarvis_cd.artifacts import ArtifactStore
from jarvis_cd.service_runtime import (
    DatasetDescriptor,
    DatasetMember,
    ServiceLifecycle,
    calculate_dataset_fingerprint,
)

# Match JARVIS's runtime repository loader: the directory containing the inner
# ``builtin`` package is the import root. Importing ``builtin.builtin`` from the
# checkout root would preload the outer namespace and make every later
# ``builtin.<package>`` dynamic import fail during full-suite collection.
_BUILTIN_REPOSITORY_ROOT = Path(__file__).resolve().parents[3] / "builtin"
sys.path.insert(0, str(_BUILTIN_REPOSITORY_ROOT))

from builtin.paraview import service as service_module  # noqa: E402
from builtin.paraview import pkg as package_module  # noqa: E402
from builtin.paraview import service_http as service_http_module  # noqa: E402
from builtin.paraview import service_supervisor as supervisor_module  # noqa: E402
from builtin.paraview.service_http import (  # noqa: E402
    COMMAND_RESULT_SCHEMA,
    COMMAND_SCHEMA,
    FRAME_SCHEMA,
    STATE_SCHEMA,
    CommandError,
    ServiceStateController,
    create_server,
)

_PNG = b"\x89PNG\r\n\x1a\nreal-test-frame"


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _read_sse_record(
    response: Any,
) -> tuple[str, int | None, dict[str, Any] | None]:
    """Read one complete event or heartbeat from a live HTTP response."""
    first_line = response.readline()
    if first_line == b": heartbeat\n":
        assert response.readline() == b"\n"
        return "heartbeat", None, None
    event = first_line.decode("ascii").strip().removeprefix("event: ")
    revision = int(response.readline().decode("ascii").strip().removeprefix("id: "))
    payload = json.loads(
        response.readline().decode("utf-8").strip().removeprefix("data: ")
    )
    assert response.readline() == b"\n"
    assert isinstance(payload, dict)
    return event, revision, payload


def _read_sse_event(response: Any) -> tuple[str, int, dict[str, Any]]:
    """Read one complete state or frame event from a live HTTP response."""
    event, revision, payload = _read_sse_record(response)
    assert event in {"state", "frame"}
    assert revision is not None
    assert payload is not None
    return event, revision, payload


def test_service_supervisor_rejects_shared_cluster_bind() -> None:
    """The staged supervisor cannot bypass the package's loopback boundary."""
    with pytest.raises(ValueError, match="loopback"):
        supervisor_module.main(
            [
                "--service-script",
                "service.py",
                "--descriptor",
                "descriptor.json",
                "--output-dir",
                "output",
                "--pvpython-bin",
                "pvpython",
                "--bind-host",
                "0.0.0.0",
                "--advertise-host",
                "compute-01.cluster.example",
                "--port",
                "0",
                "--startup-timeout",
                "30",
                "--service-instance-id",
                "service-test",
                "--authorization-file",
                "authorization.token",
            ]
        )


class _SupervisorProcess:
    """Controllable child process with real stream and termination surfaces."""

    def __init__(self) -> None:
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = -15

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            raise AssertionError("test child wait requires a terminal state")
        return self.returncode


class _HungSupervisorProcess(_SupervisorProcess):
    """Ignore SIGTERM until the supervisor escalates to kill."""

    def terminate(self) -> None:
        self.terminate_calls += 1

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(
                "pvpython",
                0.0 if timeout is None else timeout,
            )
        return self.returncode


class _RecordingReporter:
    """Record supervisor transitions and optionally fail selected reports."""

    execution_id = "exec-render"

    def __init__(self, *, fail_on: set[ServiceLifecycle] | None = None) -> None:
        self.fail_on = fail_on or set()
        self.lifecycle_calls: list[ServiceLifecycle] = []

    def report(
        self,
        lifecycle: ServiceLifecycle,
        *,
        message: str | None = None,
    ) -> None:
        del message
        self.lifecycle_calls.append(lifecycle)
        if lifecycle in self.fail_on:
            raise OSError(f"{lifecycle.value} report failed")


def _supervisor_descriptor(path: Path) -> Path:
    """Write one valid intrinsic descriptor for supervisor unit tests."""
    members = (DatasetMember(index=0, location="/cluster/input.vti"),)
    descriptor = DatasetDescriptor(
        dataset_id="supervisor-test",
        kind="volume",
        format="vtk-image-data",
        members=members,
        fingerprint=calculate_dataset_fingerprint(
            dataset_id="supervisor-test",
            kind="volume",
            format="vtk-image-data",
            members=members,
        ),
    )
    path.write_text(descriptor.to_json(), encoding="utf-8")
    return path


def _supervisor_arguments(descriptor: Path, output_dir: Path) -> list[str]:
    """Return a loopback supervisor command using no real subprocess."""
    authorization = descriptor.with_name("authorization.token")
    authorization.write_text("a" * 64 + "\n", encoding="ascii")
    if sys.platform != "win32":
        authorization.chmod(0o600)
    return [
        "--service-script",
        "service.py",
        "--descriptor",
        str(descriptor),
        "--output-dir",
        str(output_dir),
        "--pvpython-bin",
        "pvpython",
        "--bind-host",
        "127.0.0.1",
        "--advertise-host",
        "127.0.0.1",
        "--port",
        "18080",
        "--startup-timeout",
        "5",
        "--service-instance-id",
        "srv_0123456789abcdef0123456789abcdef",
        "--authorization-file",
        str(authorization.resolve()),
    ]


def test_supervisor_termination_escalates_after_bounded_grace() -> None:
    """A child that ignores terminate is killed after one finite timeout."""
    process = _HungSupervisorProcess()

    return_code, forced = supervisor_module._terminate_process(
        cast(Any, process),
        grace_seconds=0.01,
    )

    assert return_code == -9
    assert forced is True
    assert process.terminate_calls == 1
    assert process.kill_calls == 1


class _HealthResponse:
    """Context-managed HTTP response for exact health-contract tests."""

    status = 200

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_HealthResponse":
        return self

    def __exit__(
        self,
        _exception_type: object,
        _exception: object,
        _traceback: object,
    ) -> None:
        return

    def read(self, _limit: int) -> bytes:
        return self.payload


def test_supervisor_health_requires_exact_versioned_instance_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different process or malformed revision cannot satisfy readiness."""
    response_payload: dict[str, Any] = {}

    def fake_urlopen(request: object, *, timeout: float) -> _HealthResponse:
        assert timeout == 0.5
        assert isinstance(request, urllib.request.Request)
        assert request.get_header("Authorization") == "Bearer " + "a" * 64
        return _HealthResponse(response_payload)

    monkeypatch.setattr(supervisor_module.urllib.request, "urlopen", fake_urlopen)
    expected = {
        "schema_version": "jarvis.paraview.health.v1",
        "status": "ready",
        "service_instance_id": "srv_0123456789abcdef0123456789abcdef",
        "revision": 1,
    }
    response_payload.update(expected)

    assert supervisor_module._health_ready(
        "127.0.0.1",
        18080,
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
        authorization_token="a" * 64,
    )
    response_payload["revision"] = True
    assert not supervisor_module._health_ready(
        "127.0.0.1",
        18080,
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
        authorization_token="a" * 64,
    )
    response_payload.update(expected)
    response_payload["unexpected"] = "field"
    assert not supervisor_module._health_ready(
        "127.0.0.1",
        18080,
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
        authorization_token="a" * 64,
    )


def test_stop_reporting_failure_does_not_prevent_owned_child_cleanup() -> None:
    """Metadata I/O failure cannot orphan the supervised pvpython child."""
    process = _SupervisorProcess()
    reporter = _RecordingReporter(fail_on={ServiceLifecycle.STOPPING})

    result = supervisor_module._stop_after_request(
        cast(Any, process),
        cast(Any, reporter),
        ServiceLifecycle.READY,
    )

    assert result == 1
    assert process.terminate_calls == 1
    assert process.poll() == -15
    assert reporter.lifecycle_calls == [
        ServiceLifecycle.STOPPING,
        ServiceLifecycle.STOPPED,
    ]


def test_supervisor_cleans_child_when_initial_reporting_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Even STARTING persistence failure enters the unconditional cleanup path."""
    process = _SupervisorProcess()
    reporter = _RecordingReporter(fail_on={ServiceLifecycle.STARTING})
    descriptor = _supervisor_descriptor(tmp_path / "descriptor.json")

    def fake_popen(*_args: Any, **_kwargs: Any) -> Any:
        return process

    def reporter_from_environment(**_kwargs: Any) -> _RecordingReporter:
        return reporter

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        supervisor_module,
        "ServiceRuntimeReporter",
        SimpleNamespace(from_environment=reporter_from_environment),
    )

    result = supervisor_module.main(
        _supervisor_arguments(descriptor, tmp_path / "output")
    )

    assert result == 1
    assert process.terminate_calls == 1
    assert process.poll() == -15
    assert reporter.lifecycle_calls == [
        ServiceLifecycle.STARTING,
        ServiceLifecycle.FAILED,
    ]


def test_supervisor_missing_configured_pvpython_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A configured launcher that does not exist is a recorded hard failure."""
    reporter = _RecordingReporter()
    descriptor = _supervisor_descriptor(tmp_path / "descriptor.json")
    missing_launcher = str(tmp_path / "missing-pvpython")
    arguments = _supervisor_arguments(descriptor, tmp_path / "output")
    arguments[arguments.index("--pvpython-bin") + 1] = missing_launcher

    def missing_popen(command: list[str], **_kwargs: Any) -> Any:
        assert command[0] == missing_launcher
        raise FileNotFoundError(f"configured launcher not found: {missing_launcher}")

    def reporter_from_environment(**_kwargs: Any) -> _RecordingReporter:
        return reporter

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", missing_popen)
    monkeypatch.setattr(
        supervisor_module,
        "ServiceRuntimeReporter",
        SimpleNamespace(from_environment=reporter_from_environment),
    )

    assert supervisor_module.main(arguments) == 1
    assert reporter.lifecycle_calls == [ServiceLifecycle.FAILED]
    diagnostic = capsys.readouterr().err
    assert "ParaView service supervisor failed" in diagnostic
    assert missing_launcher in diagnostic


def test_supervisor_accepts_hyphen_prefixed_pvpython_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Package-selected launcher flags remain values, not supervisor options."""
    reporter = _RecordingReporter()
    descriptor = _supervisor_descriptor(tmp_path / "descriptor.json")
    arguments = _supervisor_arguments(descriptor, tmp_path / "output")
    arguments.append("--pvpython-options=--mesa")
    observed: list[list[str]] = []

    def capture_popen(command: list[str], **_kwargs: Any) -> Any:
        observed.append(command)
        raise FileNotFoundError("stop after parsing the supervisor command")

    def reporter_from_environment(**_kwargs: Any) -> _RecordingReporter:
        return reporter

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", capture_popen)
    monkeypatch.setattr(
        supervisor_module,
        "ServiceRuntimeReporter",
        SimpleNamespace(from_environment=reporter_from_environment),
    )

    assert supervisor_module.main(arguments) == 1
    assert observed[0][:2] == ["pvpython", "--mesa"]
    assert reporter.lifecycle_calls == [ServiceLifecycle.FAILED]


def test_supervisor_reports_periodic_degradation_and_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Readiness remains a monitored lifecycle, not a one-time startup fact."""
    process = _SupervisorProcess()
    reporter = _RecordingReporter()
    descriptor = _supervisor_descriptor(tmp_path / "descriptor.json")
    probe_calls = 0

    def fake_popen(*_args: Any, **_kwargs: Any) -> Any:
        return process

    def reporter_from_environment(**_kwargs: Any) -> _RecordingReporter:
        return reporter

    def health_ready(
        _host: str,
        _port: int,
        *,
        service_instance_id: str,
        authorization_token: str,
    ) -> bool:
        nonlocal probe_calls
        assert service_instance_id == "srv_0123456789abcdef0123456789abcdef"
        assert authorization_token == "a" * 64
        probe_calls += 1
        healthy = probe_calls in {1, 5}
        if probe_calls == 5:
            process.returncode = 0
        return healthy

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        supervisor_module,
        "ServiceRuntimeReporter",
        SimpleNamespace(from_environment=reporter_from_environment),
    )
    monkeypatch.setattr(supervisor_module, "_health_ready", health_ready)
    monkeypatch.setattr(supervisor_module, "HEALTH_PROBE_INTERVAL_SECONDS", 0.001)

    result = supervisor_module.main(
        _supervisor_arguments(descriptor, tmp_path / "output")
    )

    assert result == 0
    assert probe_calls == 5
    assert reporter.lifecycle_calls == [
        ServiceLifecycle.STARTING,
        ServiceLifecycle.READY,
        ServiceLifecycle.DEGRADED,
        ServiceLifecycle.READY,
        ServiceLifecycle.STOPPED,
    ]


class _CapturedExec:
    """Capture a real package launch boundary without starting pvpython."""

    commands: list[tuple[str, Any]] = []
    help_text = "--mesa\n--force-offscreen-rendering\n"

    def __init__(self, command: str, exec_info: Any) -> None:
        self.command = command
        self.exec_info = exec_info
        self.exit_code = {"localhost": 0}
        self.stdout = {"localhost": ""}
        self.stderr = {"localhost": ""}
        if command.endswith(" --help"):
            self.stdout["localhost"] = self.help_text
        else:
            self.commands.append((command, exec_info))

    def run(self) -> "_CapturedExec":
        return self


class _ResolvedWhich:
    """Resolve package launchers without depending on host ParaView installs."""

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


class _MissingWhich(_ResolvedWhich):
    """Represent one absent mode-specific ParaView launcher."""

    def __init__(self, executable: str, exec_info: Any) -> None:
        super().__init__(executable, exec_info)
        self.exit_code = {"localhost": 1}
        self.stdout = {"localhost": ""}


class _Backend:
    """Deterministic real transport test backend; fakes stay in tests only."""

    def __init__(self) -> None:
        self.timestep = 0
        self.artifacts: list[dict[str, Any]] = []
        self.transaction_open = False

    def dataset_state(self) -> dict[str, Any]:
        return {
            "descriptor": {
                "schema_version": "jarvis.dataset-descriptor.v1",
                "dataset_id": "transport-test",
            },
            "discovery": {
                "arrays": [],
                "bounds": None,
                "timestep_values": [0.0, 1.0],
            },
        }

    def pipeline_state(self) -> dict[str, Any]:
        return {
            "timestep": {
                "index": self.timestep,
                "value": float(self.timestep),
                "count": 2,
            },
            "nodes": [
                {
                    "node_id": "node_root",
                    "kind": "reader",
                    "input_node_ids": [],
                    "filter": None,
                    "output": {
                        "topology": "unknown",
                        "raw_data_type": None,
                        "bounds": None,
                        "point_count": 0,
                        "cell_count": 0,
                        "arrays": [],
                    },
                }
            ],
            "representations": [
                {
                    "representation_id": "rep_root",
                    "node_id": "node_root",
                    "type": "surface",
                    "visible": True,
                    "opacity": 1.0,
                    "point_size_px": None,
                    "color": {"mode": "solid", "rgb": [0.8, 0.8, 0.8]},
                }
            ],
            "measurements": [],
            "camera": {
                "position": [1.0, 1.0, 1.0],
                "focal_point": [0.0, 0.0, 0.0],
                "view_up": [0.0, 1.0, 0.0],
                "parallel_scale": 1.0,
                "projection": "perspective",
                "view_angle": 30.0,
            },
            "selection": None,
            "artifacts": self.artifacts,
        }

    def execute(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        command_id: str,
    ) -> dict[str, Any]:
        del command_id
        if operation != "set_timestep" or set(arguments) != {"index"}:
            raise CommandError("unsupported", "test backend only accepts timestep")
        index = arguments["index"]
        if isinstance(index, bool) or not isinstance(index, int) or index not in {0, 1}:
            raise CommandError("out_of_range", "test timestep is out of range")
        self.timestep = index
        return {"timestep": {"index": index, "value": float(index)}}

    def render_png(self) -> bytes:
        return _PNG

    def begin_command(self) -> int:
        assert not self.transaction_open
        self.transaction_open = True
        return self.timestep

    def commit_command(self, checkpoint: object) -> None:
        del checkpoint
        assert self.transaction_open
        self.transaction_open = False

    def rollback_command(self, checkpoint: object) -> None:
        assert self.transaction_open
        self.timestep = cast(int, checkpoint)
        self.transaction_open = False


class _RevisionFrameBackend(_Backend):
    """Return revision-distinct PNG bytes so publication cannot hide a stale frame."""

    def __init__(self) -> None:
        super().__init__()
        self.render_calls = 0

    def render_png(self) -> bytes:
        self.render_calls += 1
        return _PNG + f"-{self.render_calls}".encode("ascii")


class _BlockingRevisionFrameBackend(_RevisionFrameBackend):
    """Hold one mutation open while live SSE streams prove their liveness."""

    def __init__(self) -> None:
        super().__init__()
        self.execute_started = threading.Event()
        self.release_execute = threading.Event()

    def execute(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        command_id: str,
    ) -> dict[str, Any]:
        self.execute_started.set()
        if not self.release_execute.wait(timeout=2):
            raise RuntimeError("test command release timed out")
        return super().execute(operation, arguments, command_id)


class _SelectionCollection:
    """Small vtkCollection stand-in used only at the unit-test boundary."""

    def __init__(self) -> None:
        self.items: list[Any] = []

    def GetNumberOfItems(self) -> int:
        return len(self.items)

    def GetItemAsObject(self, index: int) -> Any:
        return self.items[index]


class _SelectionSource:
    def __init__(self, xml_name: str, ids: list[int]) -> None:
        self.xml_name = xml_name
        self.proxy = SimpleNamespace(IDs=ids)

    def GetXMLName(self) -> str:
        return self.xml_name


class _InputProperty:
    def __init__(self, source: Any) -> None:
        self.source = source

    def GetProxy(self, _index: int) -> Any:
        return self.source


class _SelectionRepresentation:
    def __init__(self, source: Any) -> None:
        self.source = source

    def GetProperty(self, name: str) -> _InputProperty:
        assert name == "Input"
        return _InputProperty(self.source)


class _SelectionServerManager:
    @staticmethod
    def _getPyProxy(source: _SelectionSource) -> Any:
        return source.proxy


class _SelectionView:
    def __init__(self, source: Any, ids: list[int]) -> None:
        self.source = source
        self.ids = ids
        self.representation = _SelectionRepresentation(source)
        self.pixel_rectangle: list[int] | None = None

    def SelectSurfaceCells(
        self,
        pixel_rectangle: list[int],
        representations: _SelectionCollection,
        selections: _SelectionCollection,
        _modifier: int,
    ) -> None:
        self.pixel_rectangle = pixel_rectangle
        representations.items.append(self.representation)
        selections.items.append(_SelectionSource("IDSelectionSource", self.ids))


class _SelectionSimple:
    def __init__(self, *, fail_clear: bool = False) -> None:
        self.surface_calls: list[tuple[list[int], Any]] = []
        self.cleared_sources: list[Any] = []
        self.render_calls = 0
        self.fail_clear = fail_clear

    def Render(self, _view: Any) -> None:
        self.render_calls += 1

    def SelectSurfaceCells(self, *, Rectangle: list[int], View: Any) -> None:
        self.surface_calls.append((Rectangle, View))

    def ClearSelection(self, source: Any) -> None:
        self.cleared_sources.append(source)
        if self.fail_clear:
            raise RuntimeError("selection clear failed")


def _command(
    *,
    command_id: str = "cmd-1",
    expected_revision: int | None = 1,
    index: int = 1,
) -> dict[str, Any]:
    return {
        "schema_version": COMMAND_SCHEMA,
        "command_id": command_id,
        "operation": "set_timestep",
        "expected_revision": expected_revision,
        "arguments": {"index": index},
    }


def test_controller_returns_authoritative_state_and_idempotent_result() -> None:
    """Semantic commands mutate backend state once and return that exact state."""
    controller = ServiceStateController(
        backend=_Backend(),
        execution_id="exec-render",
        package_name="builtin.paraview",
        package_id="viewer",
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
    )

    initial = controller.state()
    first = controller.command(_command())
    replay = controller.command(_command())

    assert initial["schema_version"] == STATE_SCHEMA
    assert initial["revision"] == 1
    assert first == replay
    assert first["schema_version"] == COMMAND_RESULT_SCHEMA
    assert first["state"]["revision"] == 2
    assert first["state"]["pipeline"]["timestep"]["index"] == 1
    with pytest.raises(CommandError, match="different command"):
        controller.command(_command(index=0))


def test_v2_contract_versions_and_explicit_v1_migration_failure() -> None:
    """Mixed v1 input and v2 output is rejected instead of feigning compatibility."""
    assert STATE_SCHEMA == "jarvis.paraview.service-state.v2"
    assert COMMAND_SCHEMA == "jarvis.paraview.command.v2"
    assert COMMAND_RESULT_SCHEMA == "jarvis.paraview.command-result.v2"
    legacy = {
        "schema_version": "jarvis.paraview.command.v1",
        "command_id": "legacy-colormap",
        "operation": "set_colormap",
        "expected_revision": 1,
        "arguments": {"preset": "Viridis (matplotlib)", "invert": False},
    }

    with pytest.raises(CommandError, match="migrate the command") as captured:
        service_http_module._validate_command(legacy)

    assert captured.value.code == "unsupported_schema"


def test_controller_rejects_stale_revision_before_backend_mutation() -> None:
    """Concurrent agents receive an explicit conflict instead of lost updates."""
    controller = ServiceStateController(
        backend=_Backend(),
        execution_id="exec-render",
        package_name="builtin.paraview",
        package_id="viewer",
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
    )
    controller.command(_command())

    with pytest.raises(CommandError) as captured:
        controller.command(_command(command_id="cmd-2", expected_revision=1, index=0))

    assert captured.value.code == "revision_conflict"
    assert captured.value.details == {"expected_revision": 1, "actual_revision": 2}


def test_controller_retains_ids_until_explicit_lifetime_limit() -> None:
    """Old command IDs remain replayable after the bounded service fills."""
    controller = ServiceStateController(
        backend=_Backend(),
        execution_id="exec-render",
        package_name="builtin.paraview",
        package_id="viewer",
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
        max_commands=2,
    )
    first_command = _command(command_id="cmd-1", expected_revision=1, index=1)
    first = controller.command(first_command)
    controller.command(_command(command_id="cmd-2", expected_revision=2, index=0))

    with pytest.raises(CommandError) as captured:
        controller.command(_command(command_id="cmd-3", expected_revision=3, index=1))

    assert captured.value.code == "command_limit"
    assert captured.value.status is HTTPStatus.TOO_MANY_REQUESTS
    assert captured.value.details == {"max_commands": 2}
    assert controller.command(first_command) == first


class _ScreenshotSimple:
    """Write deterministic screenshot bytes at the real filesystem boundary."""

    def __init__(self, payload: bytes = _PNG) -> None:
        self.payload = payload
        self.calls: list[tuple[Path, list[int]]] = []

    def SaveScreenshot(
        self,
        path: str,
        _view: object,
        *,
        ImageResolution: list[int],
    ) -> None:
        target = Path(path)
        self.calls.append((target, ImageResolution))
        target.write_bytes(self.payload)


def test_png_staging_is_private_and_rejects_invalid_output(
    tmp_path: Path,
) -> None:
    """Staging validates PNG bytes without publishing a final filename."""
    simple = _ScreenshotSimple()
    staged_path, payload = service_module._stage_png(
        simple=simple,
        view=object(),
        output_dir=tmp_path,
        width=640,
        height=480,
    )

    assert payload == _PNG
    assert staged_path.read_bytes() == _PNG
    assert staged_path.name.startswith(".paraview-artifact.")
    assert len(simple.calls) == 1
    staged_path.unlink()

    with pytest.raises(RuntimeError, match="did not produce a PNG"):
        service_module._stage_png(
            simple=_ScreenshotSimple(b"not-a-png"),
            view=object(),
            output_dir=tmp_path,
            width=640,
            height=480,
        )
    assert not list(tmp_path.glob(".paraview-artifact.*"))


def test_staged_export_recovers_link_created_before_sidecar_append(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A marker makes link-before-ledger failure attributable and recoverable."""
    sidecar = (tmp_path / "artifacts.jsonl").resolve()
    output = (tmp_path / "frame.png").resolve()
    monkeypatch.setenv("JARVIS_ARTIFACT_PATH", str(sidecar))
    monkeypatch.setenv("JARVIS_EXECUTION_ID", "exec-render")
    monkeypatch.setenv("JARVIS_PACKAGE_NAME", "paraview")
    monkeypatch.setenv("JARVIS_PACKAGE_ID", "builtin.paraview")
    staged_path, payload = service_module._stage_png(
        simple=_ScreenshotSimple(),
        view=object(),
        output_dir=tmp_path,
        width=640,
        height=480,
    )
    event = service_module._prepare_artifact_event(
        artifact_id="art_test",
        logical_name="frame.png",
        path=output,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        service_instance_id="srv_test",
        command_id="cmd-export",
        representation_ids=["rep_root"],
        scene_digest="sha256:" + "0" * 64,
        cluster_location="/cluster/frame.png",
    )

    def fail_open(_path: Path) -> int:
        raise OSError("sidecar open failed")

    original_open = service_module._open_private_append
    monkeypatch.setattr(service_module, "_open_private_append", fail_open)
    with pytest.raises(OSError, match="sidecar open failed"):
        service_module._commit_staged_artifacts(
            {
                "art_test": {
                    "event": event,
                    "staged_path": staged_path,
                    "output_path": output,
                }
            }
        )

    assert staged_path.is_file()
    assert output.read_bytes() == _PNG
    assert not sidecar.exists()
    markers = list(tmp_path.glob(".artifacts.jsonl.paraview-transaction-*.json"))
    assert len(markers) == 1

    monkeypatch.setattr(service_module, "_open_private_append", original_open)
    service_module._recover_artifact_transactions()

    published = service_module._read_artifact_lines(sidecar)
    assert published == [event]
    assert not markers[0].exists()
    assert not staged_path.exists()
    assert output.read_bytes() == _PNG


class _CameraView:
    """Mutable camera properties matching ParaView's render view surface."""

    def __init__(self) -> None:
        self.CameraPosition = [4.0, 3.0, 2.0]
        self.CameraFocalPoint = [0.0, 0.0, 0.0]
        self.CameraViewUp = [0.0, 1.0, 0.0]
        self.CameraParallelScale = 5.0
        self.CameraParallelProjection = 0
        self.CameraViewAngle = 30.0
        self.ViewTime: float | None = None


class _RenderSimple:
    """Fail the selected render call without mutating the camera itself."""

    def __init__(self, *, fail_on: set[int] | None = None) -> None:
        self.fail_on = fail_on or set()
        self.render_calls = 0

    def Render(self, _view: object) -> None:
        self.render_calls += 1
        if self.render_calls in self.fail_on:
            raise RuntimeError("render failed")


def test_camera_validates_before_mutation_and_rolls_back_render_failure() -> None:
    """A rejected or failed camera command leaves the prior camera exact."""
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    backend.view = _CameraView()
    backend.simple = _RenderSimple()
    original = backend._camera_state()

    with pytest.raises(CommandError, match="must be distinct"):
        backend._set_camera(
            {"position": [0.0, 0.0, 0.0]},
            "cmd-invalid-camera",
        )

    assert backend._camera_state() == original
    assert backend.simple.render_calls == 0

    backend.simple = _RenderSimple(fail_on={1})
    backend._nodes = {"node_root": {"node_id": "node_root"}}
    backend._node_proxies = {
        "node_root": SimpleNamespace(UpdatePipeline=lambda *_args: None)
    }
    backend._representations = {}
    backend._representation_displays = {}
    backend._representation_transfer_proxies = {}
    backend._measurements = {}
    backend._selection = None
    backend._artifacts = []
    backend._transaction_open = False
    backend._pending_deletes = []
    backend._retired_proxies = []
    backend._staged_artifacts = {}
    backend._reader_timesteps = []
    backend._timesteps = []
    backend._timestep_index = 0
    with pytest.raises(RuntimeError, match="render failed"):
        backend.execute(
            "set_camera",
            {"position": [8.0, 3.0, 2.0], "parallel_scale": 8.0},
            "cmd-failed-camera",
        )

    assert backend._camera_state() == original
    assert backend.simple.render_calls == 2


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"focal_point": [4.0, 3.0, 2.0]}, "must be distinct"),
        ({"view_up": [-4.0, -3.0, -2.0]}, "cannot be parallel"),
        ({"view_up": [0.0, 0.0, 0.0]}, "zero vector"),
        ({"projection": "orthographic"}, "perspective or parallel"),
        ({"view_angle": 0.0}, "between 0 and 180"),
        ({"view_angle": 180.0}, "between 0 and 180"),
    ],
)
def test_camera_partial_update_validates_complete_geometry_before_mutation(
    arguments: dict[str, Any],
    message: str,
) -> None:
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    backend.view = _CameraView()
    backend.simple = _RenderSimple()
    original = backend._camera_state()

    with pytest.raises(CommandError, match=message):
        backend._set_camera(arguments, "invalid-partial-camera")

    assert backend._camera_state() == original
    assert backend.simple.render_calls == 0


class _FilterProxy:
    """Minimal slice proxy used to exercise mutation ordering."""

    def __init__(self) -> None:
        self.SliceType = SimpleNamespace(Origin=None, Normal=None)
        self.updated = False
        self.update_times: list[float | None] = []

    def UpdatePipeline(self, value: float | None = None) -> None:
        self.updated = True
        self.update_times.append(value)


class _FilterSimple:
    """ParaView surface intentionally omitting ResetCamera."""

    def __init__(self) -> None:
        self.slice_calls: list[object] = []
        self.hidden: list[object] = []
        self.proxy = _FilterProxy()
        self.active_source: object | None = object()

    def Slice(self, *, Input: object) -> _FilterProxy:
        self.slice_calls.append(Input)
        return self.proxy

    def Show(self, source: object, _view: object) -> object:
        return SimpleNamespace(source=source)

    def Hide(self, source: object, _view: object) -> None:
        self.hidden.append(source)

    def Delete(self, _source: object) -> None:
        return

    def GetActiveSource(self) -> object | None:
        return self.active_source

    def SetActiveSource(self, source: object | None) -> None:
        self.active_source = source

    def Render(self, _view: object) -> None:
        return


def test_filter_preserves_dataset_discovery_and_explicit_camera() -> None:
    """A branch filter adds only a topology node and preserves source context."""
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    backend.view = _CameraView()
    backend.simple = _FilterSimple()
    previous_source = backend.simple.active_source
    root_output = {
        "topology": "volume",
        "raw_data_type": "vtkImageData",
        "bounds": [-10.0, 10.0, -20.0, 20.0, -30.0, 30.0],
        "point_count": 100,
        "cell_count": 50,
        "arrays": [
            {
                "name": "pressure",
                "association": "point",
                "components": 1,
                "units": None,
            }
        ],
    }
    backend._nodes = {
        "node_root": {
            "node_id": "node_root",
            "kind": "reader",
            "input_node_ids": [],
            "filter": None,
            "output": root_output,
        }
    }
    backend._node_proxies = {"node_root": object()}
    backend._timesteps = [0.0, 1.0]
    backend._reader_timesteps = [0.0, 1.0]
    backend._dataset_arrays = list(root_output["arrays"])
    backend._dataset_bounds = tuple(root_output["bounds"])
    backend._dataset_timesteps = list(backend._timesteps)
    backend._timestep_index = 1
    backend.descriptor = {
        "schema_version": "jarvis.dataset-descriptor.v1",
        "dataset_id": "source-volume",
    }
    backend._selection = {"status": "selected"}
    backend._output_summary = lambda _proxy, *, topology: {
        "topology": topology,
        "raw_data_type": "vtkPolyData",
        "bounds": [0.0, 1.0, 0.0, 1.0, 0.0, 1.0],
        "point_count": 12,
        "cell_count": 8,
        "arrays": list(root_output["arrays"]),
    }
    original_camera = backend._camera_state()
    original_discovery = backend.dataset_state()

    with pytest.raises(CommandError, match="zero vector"):
        backend._create_filter(
            {
                "input_node_id": "node_root",
                "type": "slice",
                "parameters": {
                    "origin": [0.0, 0.0, 0.0],
                    "normal": [0.0, 0.0, 0.0],
                },
            },
            "cmd-invalid-filter",
        )

    assert backend.simple.slice_calls == []
    result = backend._create_filter(
        {
            "input_node_id": "node_root",
            "type": "slice",
            "parameters": {
                "origin": [0.0, 0.0, 0.0],
                "normal": [0.0, 0.0, 1.0],
            },
        },
        "cmd-valid-filter",
    )

    assert result["node"]["filter"]["type"] == "slice"
    assert backend.simple.active_source is previous_source
    assert backend.simple.proxy.updated is True
    assert backend.simple.proxy.update_times == [1.0]
    assert backend._camera_state() == original_camera
    assert backend.dataset_state() == original_discovery
    assert result["node"]["node_id"] in backend._nodes
    assert result["node"]["output"]["topology"] == "surface"
    assert backend.simple.hidden == []
    assert backend._selection == {"status": "selected"}


def test_http_health_state_command_conflict_and_frame_sse() -> None:
    """The real stdlib server exposes all bounded transport behaviors."""
    controller = ServiceStateController(
        backend=_Backend(),
        execution_id="exec-render",
        package_name="builtin.paraview",
        package_id="viewer",
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
    )
    token = "a" * 64
    server = create_server("127.0.0.1", 0, controller, token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError) as missing_auth:
            urllib.request.urlopen(origin + "/healthz", timeout=2)
        wrong_request = urllib.request.Request(
            origin + "/healthz",
            headers={"Authorization": "Bearer " + "b" * 64},
        )
        with pytest.raises(urllib.error.HTTPError) as wrong_auth:
            urllib.request.urlopen(wrong_request, timeout=2)
        headers = {"Authorization": f"Bearer {token}"}
        with urllib.request.urlopen(
            urllib.request.Request(origin + "/healthz", headers=headers),
            timeout=2,
        ) as response:
            health = json.load(response)
        with urllib.request.urlopen(
            urllib.request.Request(origin + "/state", headers=headers),
            timeout=2,
        ) as response:
            state = json.load(response)
        request = urllib.request.Request(
            origin + "/commands",
            data=json.dumps(_command()).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            result = json.load(response)
        stale = urllib.request.Request(
            origin + "/commands",
            data=json.dumps(
                _command(command_id="cmd-2", expected_revision=1, index=0)
            ).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(stale, timeout=2)
        conflict = json.load(captured.value)
        with urllib.request.urlopen(
            urllib.request.Request(origin + "/live-data", headers=headers),
            timeout=2,
        ) as response:
            event = response.readline().decode("ascii").strip()
            event_id = response.readline().decode("ascii").strip()
            data = response.readline().decode("utf-8").strip()

        assert health["status"] == "ready"
        assert missing_auth.value.code == 401
        assert missing_auth.value.headers["Connection"] == "close"
        assert wrong_auth.value.code == 401
        assert wrong_auth.value.headers["Connection"] == "close"
        assert state["schema_version"] == STATE_SCHEMA
        assert result["state"]["revision"] == 2
        assert captured.value.code == 409
        assert conflict["error"]["code"] == "revision_conflict"
        assert event == "event: frame"
        assert event_id == "id: 2"
        assert json.loads(data.removeprefix("data: "))["schema_version"] == FRAME_SCHEMA
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_sse_publishes_every_mutation_and_replays_current_revision() -> None:
    """Live streams and reconnects receive the PNG and state for each committed revision."""
    backend = _RevisionFrameBackend()
    controller = ServiceStateController(
        backend=backend,
        execution_id="exec-render",
        package_name="builtin.paraview",
        package_id="viewer",
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
    )
    token = "a" * 64
    server = create_server(
        "127.0.0.1",
        0,
        controller,
        token,
        heartbeat_interval=0.05,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    headers = {"Authorization": f"Bearer {token}"}
    frames = urllib.request.urlopen(
        urllib.request.Request(origin + "/live-data", headers=headers),
        timeout=2,
    )
    states = urllib.request.urlopen(
        urllib.request.Request(origin + "/events", headers=headers),
        timeout=2,
    )
    try:
        frame_event, frame_revision, frame = _read_sse_event(frames)
        state_event, state_revision, state = _read_sse_event(states)
        assert (frame_event, frame_revision) == ("frame", 1)
        assert (state_event, state_revision) == ("state", 1)
        assert state["revision"] == 1
        assert base64.b64decode(frame["data"]) == _PNG + b"-1"

        for revision, index in ((2, 1), (3, 0)):
            request = urllib.request.Request(
                origin + "/commands",
                data=json.dumps(
                    _command(
                        command_id=f"cmd-{revision}",
                        expected_revision=revision - 1,
                        index=index,
                    )
                ).encode("utf-8"),
                headers={"Content-Type": "application/json", **headers},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                result = json.load(response)
            frame_event, frame_revision, frame = _read_sse_event(frames)
            state_event, state_revision, state = _read_sse_event(states)

            assert result["state"]["revision"] == revision
            assert (frame_event, frame_revision) == ("frame", revision)
            assert (state_event, state_revision) == ("state", revision)
            assert state == result["state"]
            assert base64.b64decode(frame["data"]) == _PNG + f"-{revision}".encode(
                "ascii"
            )

        frames.close()
        states.close()
        request = urllib.request.Request(
            origin + "/commands",
            data=json.dumps(
                _command(command_id="cmd-4", expected_revision=3, index=1)
            ).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            result = json.load(response)
        reconnect_headers = {**headers, "Last-Event-ID": "3"}
        with urllib.request.urlopen(
            urllib.request.Request(origin + "/live-data", headers=reconnect_headers),
            timeout=2,
        ) as reconnected_frames:
            frame_event, frame_revision, frame = _read_sse_event(reconnected_frames)
        with urllib.request.urlopen(
            urllib.request.Request(origin + "/events", headers=reconnect_headers),
            timeout=2,
        ) as reconnected_states:
            state_event, state_revision, state = _read_sse_event(reconnected_states)

        assert result["state"]["revision"] == 4
        assert (frame_event, frame_revision) == ("frame", 4)
        assert (state_event, state_revision) == ("state", 4)
        assert state == result["state"]
        assert base64.b64decode(frame["data"]) == _PNG + b"-4"
        assert backend.render_calls == 4
    finally:
        frames.close()
        states.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_sse_heartbeats_continue_during_a_blocked_command() -> None:
    """Long mutations preserve stream liveness and publish one matched revision."""
    backend = _BlockingRevisionFrameBackend()
    controller = ServiceStateController(
        backend=backend,
        execution_id="exec-render",
        package_name="builtin.paraview",
        package_id="viewer",
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
    )
    token = "a" * 64
    server = create_server(
        "127.0.0.1",
        0,
        controller,
        token,
        heartbeat_interval=0.02,
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    headers = {"Authorization": f"Bearer {token}"}
    frames = urllib.request.urlopen(
        urllib.request.Request(origin + "/live-data", headers=headers),
        timeout=2,
    )
    states = urllib.request.urlopen(
        urllib.request.Request(origin + "/events", headers=headers),
        timeout=2,
    )
    command_results: list[dict[str, Any]] = []
    command_errors: list[BaseException] = []
    records: dict[str, tuple[str, int | None, dict[str, Any] | None]] = {}
    reader_errors: list[BaseException] = []
    snapshots: list[tuple[dict[str, Any], tuple[int, bytes]]] = []

    def invoke_command() -> None:
        try:
            command_results.append(
                controller.command(
                    _command(
                        command_id="cmd-blocked",
                        expected_revision=1,
                        index=1,
                    )
                )
            )
        except BaseException as exc:
            command_errors.append(exc)

    def read_record(name: str, response: Any) -> None:
        try:
            records[name] = _read_sse_record(response)
        except BaseException as exc:
            reader_errors.append(exc)

    def read_committed_snapshot() -> None:
        try:
            snapshots.append((controller.state(), controller.frame()))
        except BaseException as exc:
            reader_errors.append(exc)

    command_thread = threading.Thread(target=invoke_command, daemon=True)
    readers = [
        threading.Thread(target=read_record, args=("frame", frames), daemon=True),
        threading.Thread(target=read_record, args=("state", states), daemon=True),
        threading.Thread(target=read_committed_snapshot, daemon=True),
    ]
    try:
        assert _read_sse_event(frames)[:2] == ("frame", 1)
        assert _read_sse_event(states)[:2] == ("state", 1)

        command_thread.start()
        assert backend.execute_started.wait(timeout=1)
        for reader in readers:
            reader.start()
        for reader in readers:
            reader.join(timeout=0.5)

        assert not any(reader.is_alive() for reader in readers)
        assert not reader_errors
        assert records == {
            "frame": ("heartbeat", None, None),
            "state": ("heartbeat", None, None),
        }
        assert snapshots[0][0]["revision"] == 1
        assert snapshots[0][1] == (1, _PNG + b"-1")

        backend.release_execute.set()
        command_thread.join(timeout=2)
        assert not command_thread.is_alive()
        assert not command_errors
        assert len(command_results) == 1

        frame_event, frame_revision, frame = _read_sse_event(frames)
        state_event, state_revision, state = _read_sse_event(states)
        result = command_results[0]
        assert (frame_event, frame_revision) == ("frame", 2)
        assert (state_event, state_revision) == ("state", 2)
        assert state == result["state"]
        assert base64.b64decode(frame["data"]) == _PNG + b"-2"
        assert backend.render_calls == 2

        assert _read_sse_record(frames) == ("heartbeat", None, None)
        assert _read_sse_record(states) == ("heartbeat", None, None)
    finally:
        backend.release_execute.set()
        command_thread.join(timeout=2)
        for reader in readers:
            reader.join(timeout=2)
        frames.close()
        states.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_sse_frame_payload_is_encoded_once_per_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent subscribers share one immutable frame encoding per revision."""
    calls = 0
    original = service_http_module.base64.b64encode

    def counted(value: bytes) -> bytes:
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(service_http_module.base64, "b64encode", counted)
    controller = ServiceStateController(
        backend=_Backend(),
        execution_id="exec-render",
        package_name="builtin.paraview",
        package_id="viewer",
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
    )

    revision, state_payload, frame_payload = controller.wait_for_sse_change(
        0,
        timeout=0.01,
    )
    repeated = controller.wait_for_sse_change(0, timeout=0.01)

    assert revision == 1
    assert repeated[1] is state_payload
    assert repeated[2] is frame_payload
    assert calls == 1

    controller.command(_command())
    changed = controller.wait_for_sse_change(revision, timeout=0.01)

    assert changed[0] == 2
    assert changed[2] is not frame_payload
    assert calls == 2


def test_http_connection_and_body_timeouts_reclaim_slots() -> None:
    """Idle headers and partial command bodies cannot retain connection slots."""
    controller = ServiceStateController(
        backend=_Backend(),
        execution_id="exec-render",
        package_name="builtin.paraview",
        package_id="viewer",
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
    )
    token = "a" * 64
    server = create_server(
        "127.0.0.1",
        0,
        controller,
        token,
        max_connections=1,
        max_sse_subscribers=1,
        header_timeout=0.1,
        body_timeout=0.1,
        write_timeout=0.5,
        heartbeat_interval=0.1,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = cast(tuple[str, int], server.server_address)
    idle = socket.create_connection(address, timeout=1)
    rejected = socket.create_connection(address, timeout=1)
    try:
        assert _wait_until(lambda: server.active_connection_count == 1)
        rejection = rejected.recv(4096)
        assert b"503 Service Unavailable" in rejection
        assert b'"code":"connection_limit"' in rejection
        assert _wait_until(lambda: server.active_connection_count == 0)

        partial = socket.create_connection(address, timeout=1)
        partial.sendall(
            (
                "POST /commands HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                f"Authorization: Bearer {token}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 20\r\n\r\n"
                "{}"
            ).encode("ascii")
        )
        response = b""
        while b'"code":"request_timeout"' not in response:
            chunk = partial.recv(4096)
            if not chunk:
                break
            response += chunk
        partial.close()

        assert b"408 Request Timeout" in response
        assert b'"code":"request_timeout"' in response
        assert _wait_until(lambda: server.active_connection_count == 0)
    finally:
        idle.close()
        rejected.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_error_response_is_finally_capped_and_secret_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even oversized structured backend errors become one fixed envelope."""
    backend = _Backend()
    secret = "private-capability-" + "b" * 64

    def fail_execute(
        _operation: str,
        _arguments: Mapping[str, Any],
        _command_id: str,
    ) -> dict[str, Any]:
        raise CommandError(
            "backend_rejected",
            secret,
            details={"diagnostic": secret + "X" * 2048},
        )

    monkeypatch.setattr(backend, "execute", fail_execute)
    monkeypatch.setattr(service_http_module, "MAX_RESPONSE_BYTES", 256)
    controller = ServiceStateController(
        backend=backend,
        execution_id="exec-render",
        package_name="builtin.paraview",
        package_id="viewer",
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
    )
    token = "a" * 64
    server = create_server("127.0.0.1", 0, controller, token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    request = urllib.request.Request(
        origin + "/commands",
        data=json.dumps(_command()).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(request, timeout=2)
        payload = captured.value.read()
        document = json.loads(payload)

        assert captured.value.code == HTTPStatus.INTERNAL_SERVER_ERROR
        assert len(payload) <= service_http_module.MAX_RESPONSE_BYTES
        assert document == {
            "schema_version": service_http_module.COMMAND_ERROR_SCHEMA,
            "error": {
                "code": "response_too_large",
                "message": "the ParaView service response exceeded its size limit",
                "details": {},
            },
        }
        assert secret.encode("ascii") not in payload
        assert backend.transaction_open is False
        assert backend.timestep == 0
        assert controller.state()["revision"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_absolute_read_deadlines_reject_trickle_clients() -> None:
    """Bytes arriving below inactivity timeouts cannot retain the sole slot."""
    controller = ServiceStateController(
        backend=_Backend(),
        execution_id="exec-render",
        package_name="builtin.paraview",
        package_id="viewer",
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
    )
    token = "a" * 64
    server = create_server(
        "127.0.0.1",
        0,
        controller,
        token,
        max_connections=1,
        max_sse_subscribers=1,
        header_timeout=0.12,
        body_timeout=0.12,
        write_timeout=0.5,
        heartbeat_interval=0.1,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = cast(tuple[str, int], server.server_address)
    origin = f"http://127.0.0.1:{address[1]}"
    headers = {"Authorization": f"Bearer {token}"}
    sockets: list[socket.socket] = []
    drippers: list[tuple[threading.Thread, threading.Event]] = []

    def start_drip(connection: socket.socket, payload: bytes) -> None:
        stop = threading.Event()

        def drip() -> None:
            for value in payload:
                if stop.is_set():
                    return
                try:
                    connection.sendall(bytes((value,)))
                except OSError:
                    return
                if stop.wait(0.03):
                    return

        worker = threading.Thread(target=drip, daemon=True)
        drippers.append((worker, stop))
        worker.start()

    def prove_slot_reusable() -> None:
        with urllib.request.urlopen(
            urllib.request.Request(origin + "/healthz", headers=headers),
            timeout=2,
        ) as response:
            assert json.load(response)["status"] == "ready"

    try:
        slow_header = socket.create_connection(address, timeout=1)
        sockets.append(slow_header)
        start_drip(
            slow_header,
            b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n" + b"X" * 128,
        )
        assert _wait_until(lambda: server.active_connection_count == 1)
        assert _wait_until(
            lambda: server.active_connection_count == 0,
            timeout=0.6,
        )
        drippers[-1][1].set()
        drippers[-1][0].join(timeout=1)
        prove_slot_reusable()

        slow_body = socket.create_connection(address, timeout=1)
        sockets.append(slow_body)
        slow_body.sendall(
            (
                "POST /commands HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                f"Authorization: Bearer {token}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 256\r\n\r\n"
            ).encode("ascii")
        )
        start_drip(slow_body, b"{" + b" " * 254 + b"}")
        assert _wait_until(lambda: server.active_connection_count == 1)
        assert _wait_until(
            lambda: server.active_connection_count == 0,
            timeout=0.6,
        )
        drippers[-1][1].set()
        drippers[-1][0].join(timeout=1)
        prove_slot_reusable()
    finally:
        for _worker, stop in drippers:
            stop.set()
        for connection in sockets:
            connection.close()
        for worker, _stop in drippers:
            worker.join(timeout=1)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_shutdown_does_not_wait_for_an_executing_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing intent and sockets never block behind the controller lock."""
    backend = _Backend()
    original_execute = backend.execute
    entered = threading.Event()
    release = threading.Event()

    def blocked_execute(
        operation: str,
        arguments: Mapping[str, Any],
        command_id: str,
    ) -> dict[str, Any]:
        entered.set()
        if not release.wait(timeout=2):
            raise RuntimeError("test command release timed out")
        return original_execute(operation, arguments, command_id)

    monkeypatch.setattr(backend, "execute", blocked_execute)
    controller = ServiceStateController(
        backend=backend,
        execution_id="exec-render",
        package_name="builtin.paraview",
        package_id="viewer",
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
    )
    token = "a" * 64
    server = create_server("127.0.0.1", 0, controller, token)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01),
        daemon=True,
    )
    thread.start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    request = urllib.request.Request(
        origin + "/commands",
        data=json.dumps(_command()).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    def invoke_command() -> None:
        try:
            urllib.request.urlopen(request, timeout=2).close()
        except (OSError, urllib.error.URLError):
            pass

    caller = threading.Thread(target=invoke_command, daemon=True)
    caller.start()
    shutdown_thread = threading.Thread(target=server.shutdown, daemon=True)
    try:
        assert entered.wait(timeout=1)
        shutdown_thread.start()
        shutdown_thread.join(timeout=0.3)
        assert not shutdown_thread.is_alive()
        server.server_close()
    finally:
        release.set()
        if shutdown_thread.is_alive():
            shutdown_thread.join(timeout=1)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        caller.join(timeout=2)


def test_sse_subscriber_limit_reclaims_slot_and_shutdown_interrupts_stream() -> None:
    """SSE admission is bounded, reclaimable, and interruptible on shutdown."""
    controller = ServiceStateController(
        backend=_Backend(),
        execution_id="exec-render",
        package_name="builtin.paraview",
        package_id="viewer",
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
    )
    token = "a" * 64
    server = create_server(
        "127.0.0.1",
        0,
        controller,
        token,
        max_connections=3,
        max_sse_subscribers=1,
        header_timeout=0.2,
        body_timeout=0.2,
        write_timeout=0.2,
        heartbeat_interval=0.02,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    headers = {"Authorization": f"Bearer {token}"}
    first = urllib.request.urlopen(
        urllib.request.Request(origin + "/live-data", headers=headers),
        timeout=2,
    )
    try:
        assert first.readline() == b"event: frame\n"
        assert _wait_until(lambda: server.active_subscriber_count == 1)
        with pytest.raises(urllib.error.HTTPError) as limited:
            urllib.request.urlopen(
                urllib.request.Request(origin + "/events", headers=headers),
                timeout=2,
            )
        assert limited.value.code == HTTPStatus.SERVICE_UNAVAILABLE
        assert json.load(limited.value)["error"]["code"] == "subscriber_limit"

        first.close()
        assert _wait_until(lambda: server.active_subscriber_count == 0)

        replacement = urllib.request.urlopen(
            urllib.request.Request(origin + "/events", headers=headers),
            timeout=2,
        )
        assert replacement.readline() == b"event: state\n"
        assert _wait_until(lambda: server.active_subscriber_count == 1)
        idle = socket.create_connection(
            cast(tuple[str, int], server.server_address),
            timeout=1,
        )
        assert _wait_until(lambda: server.active_connection_count == 2)

        shutdown_thread = threading.Thread(target=server.shutdown)
        shutdown_thread.start()
        shutdown_thread.join(timeout=2)
        assert not shutdown_thread.is_alive()
        assert _wait_until(lambda: server.active_connection_count == 0)
        assert _wait_until(lambda: server.active_subscriber_count == 0)
        idle.close()
        replacement.close()
    finally:
        first.close()
        if not server.closing:
            server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_missing_paraview_runtime_fails_explicitly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A normal Python interpreter never degrades into a pretend renderer."""

    def missing(_name: str) -> Any:
        raise ImportError("paraview is not installed")

    monkeypatch.setattr(service_module.importlib, "import_module", missing)

    with pytest.raises(RuntimeError, match="requires pvpython"):
        service_module.ParaViewBackend(
            descriptor={},
            output_dir=tmp_path,
            service_instance_id="srv_0123456789abcdef0123456789abcdef",
            execution_id="exec-render",
            package_name="builtin.paraview",
            package_id="viewer",
        )


def test_service_startup_rejects_cli_and_environment_execution_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("JARVIS_EXECUTION_ID", "exec-bound")
    monkeypatch.setenv("JARVIS_PACKAGE_NAME", "paraview")
    monkeypatch.setenv("JARVIS_PACKAGE_ID", "builtin.paraview")

    with pytest.raises(RuntimeError, match="does not match JARVIS_EXECUTION_ID"):
        service_module.main(
            [
                "--descriptor",
                str(tmp_path / "descriptor.json"),
                "--output-dir",
                str(tmp_path / "output"),
                "--bind-host",
                "127.0.0.1",
                "--port",
                "18080",
                "--execution-id",
                "exec-cli",
                "--service-instance-id",
                "srv-test",
                "--authorization-file",
                str(tmp_path / "authorization.token"),
            ]
        )


class _TimedPipelineProxy:
    """Capture the exact internal ParaView clock used for a timestep mutation."""

    def __init__(self) -> None:
        self.update_times: list[float] = []

    def UpdatePipeline(self, value: float) -> None:
        self.update_times.append(value)


def test_descriptor_physical_timesteps_override_synthetic_reader_labels() -> None:
    """Public state keeps catalog times while ParaView advances on its internal clock."""

    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    physical = [0.0, 1141.0, 2286.0, 3429.0, 4565.0]
    backend.descriptor = {
        "members": [
            {
                "index": index,
                "location": f"/cluster/frame-{index}.vti",
                "timestep": value,
            }
            for index, value in enumerate(physical)
        ]
    }
    reader_clock = [0.0, 1.0, 2.0, 3.0, 4.0]

    assert backend._resolve_timesteps(reader_clock) == physical

    backend._reader_timesteps = reader_clock
    backend._timesteps = physical
    backend._timestep_index = 0
    backend._selection = None
    backend._artifacts = []
    backend._measurements = {}
    backend._representations = {}
    backend.view = _CameraView()
    backend.view.ViewTime = None
    backend.reader = _TimedPipelineProxy()
    backend.active_source = _TimedPipelineProxy()
    backend._node_proxies = {
        "node_root": backend.reader,
        "node_branch": backend.active_source,
    }
    backend._nodes = {
        "node_root": {"node_id": "node_root"},
        "node_branch": {"node_id": "node_branch"},
    }
    backend._refreshed_node_records = lambda: dict(backend._nodes)
    backend._refreshed_representation_records = lambda _nodes: {}
    backend.simple = SimpleNamespace(Render=lambda _view: None)

    result = backend._set_timestep({"index": 4}, "cmd-final")

    assert result == {"timestep": {"index": 4, "value": 4565.0}}
    assert backend.pipeline_state()["timestep"] == {
        "index": 4,
        "value": 4565.0,
        "count": 5,
    }
    assert backend.view.ViewTime == 4.0
    assert backend.reader.update_times == [4.0]
    assert backend.active_source.update_times == [4.0]


def test_descriptor_timestep_mapping_rejects_partial_or_mismatched_series() -> None:
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    backend.descriptor = {
        "members": [
            {"index": 0, "location": "/cluster/frame-0.vti", "timestep": 0.0},
            {"index": 1, "location": "/cluster/frame-1.vti"},
        ]
    }
    with pytest.raises(RuntimeError, match="mix timed and untimed"):
        backend._resolve_timesteps([0.0, 1.0])

    backend.descriptor["members"][1]["timestep"] = 1141.0
    with pytest.raises(RuntimeError, match="count differs"):
        backend._resolve_timesteps([0.0])
    with pytest.raises(RuntimeError, match="count differs"):
        backend._resolve_timesteps([])


@pytest.mark.parametrize("invalid", [True, float("nan"), float("inf")])
def test_package_descriptor_rejects_invalid_member_timestep(invalid: object) -> None:
    members = (DatasetMember(index=0, location="/cluster/input.vti"),)
    descriptor = DatasetDescriptor(
        dataset_id="invalid-time",
        kind="volume",
        format="vtk-image-data",
        members=members,
        fingerprint=calculate_dataset_fingerprint(
            dataset_id="invalid-time",
            kind="volume",
            format="vtk-image-data",
            members=members,
        ),
    ).to_dict()
    descriptor["members"][0]["timestep"] = invalid

    with pytest.raises(ValueError, match="finite number"):
        service_module._validate_descriptor(descriptor)


def test_normalized_viewport_maps_to_real_paraview_pixels() -> None:
    """Browser drag boxes use top-left coordinates; ParaView uses bottom-left."""
    viewport = service_module._viewport({"x0": 0.1, "y0": 0.2, "x1": 0.9, "y1": 0.6})

    assert service_module._viewport_pixel_rectangle(viewport, (960, 540)) == [
        95,
        215,
        864,
        432,
    ]
    with pytest.raises(CommandError, match="positive width"):
        service_module._viewport({"x0": 0.5, "y0": 0.2, "x1": 0.5, "y1": 0.6})
    with pytest.raises(CommandError, match="normalized"):
        service_module._viewport({"x0": -0.1, "y0": 0.2, "x1": 0.5, "y1": 0.6})


def test_viewport_inspection_uses_backend_selection_and_exact_result_shape() -> None:
    """Viewport inspection delegates to ParaView and never maps pixels to world data."""
    source = SimpleNamespace()
    view = _SelectionView(source, [0, 41, 0, 73])
    simple = _SelectionSimple()
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    backend.active_source = SimpleNamespace(
        SMProxy=source,
        GetDataInformation=lambda: SimpleNamespace(
            GetNumberOfPoints=lambda: 100,
            GetNumberOfCells=lambda: 100,
        ),
    )
    backend.view = view
    backend.simple = simple
    backend.servermanager = _SelectionServerManager()
    backend.vtk = SimpleNamespace(vtkCollection=_SelectionCollection)
    backend._selection = None
    backend._representations = {
        "rep_root": {"node_id": "node_root", "visible": True, "type": "surface"}
    }
    backend._representation_displays = {"rep_root": view.representation}
    backend._node_proxies = {"node_root": backend.active_source}

    result = backend._inspect_selection(
        {
            "representation_id": "rep_root",
            "viewport": {"x0": 0.25, "y0": 0.1, "x1": 0.75, "y1": 0.9},
        },
        "cmd-select",
    )

    selection = result["selection"]
    assert selection == {
        "selector": "viewport",
        "representation_id": "rep_root",
        "node_id": "node_root",
        "status": "selected",
        "association": "cell",
        "viewport": {"x0": 0.25, "y0": 0.1, "x1": 0.75, "y1": 0.9},
        "pixel_rectangle": [239, 53, 720, 486],
        "selected_count": 2,
        "returned_count": 2,
        "truncated": False,
        "ids": [
            {"process_id": 0, "element_id": 41},
            {"process_id": 0, "element_id": 73},
        ],
        "reason": None,
    }
    assert view.pixel_rectangle == selection["pixel_rectangle"]
    assert simple.surface_calls == []
    assert simple.cleared_sources == [backend.active_source]
    assert simple.render_calls == 2


def test_viewport_inspection_reports_unsupported_without_approximation() -> None:
    """A missing backend picker is visible to callers instead of being guessed."""
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    backend.active_source = SimpleNamespace(
        SMProxy=object(),
        GetDataInformation=lambda: SimpleNamespace(
            GetNumberOfPoints=lambda: 100,
            GetNumberOfCells=lambda: 100,
        ),
    )
    backend.view = SimpleNamespace()
    backend.simple = _SelectionSimple()
    backend.servermanager = _SelectionServerManager()
    backend.vtk = SimpleNamespace(vtkCollection=_SelectionCollection)
    backend._selection = None
    backend._representations = {
        "rep_root": {"node_id": "node_root", "visible": True, "type": "surface"}
    }
    backend._representation_displays = {"rep_root": object()}
    backend._node_proxies = {"node_root": backend.active_source}

    result = backend._inspect_selection(
        {
            "representation_id": "rep_root",
            "viewport": {"x0": 0.1, "y0": 0.1, "x1": 0.2, "y1": 0.2},
        },
        "cmd-select",
    )

    selection = result["selection"]
    assert selection["status"] == "unsupported"
    assert selection["selected_count"] is None
    assert selection["ids"] == []
    assert selection["reason"] == "paraview_surface_selection_unavailable"
    assert backend.simple.cleared_sources == [backend.active_source]


def test_viewport_inspection_fails_if_selection_highlight_cannot_be_cleared() -> None:
    """A successful ID query cannot leave an obscuring ParaView highlight behind."""
    source = SimpleNamespace()
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    backend.active_source = SimpleNamespace(
        SMProxy=source,
        GetDataInformation=lambda: SimpleNamespace(
            GetNumberOfPoints=lambda: 100,
            GetNumberOfCells=lambda: 100,
        ),
    )
    backend.view = _SelectionView(source, [0, 41])
    backend.simple = _SelectionSimple(fail_clear=True)
    backend.servermanager = _SelectionServerManager()
    backend.vtk = SimpleNamespace(vtkCollection=_SelectionCollection)
    backend._selection = None
    backend._representations = {
        "rep_root": {"node_id": "node_root", "visible": True, "type": "surface"}
    }
    backend._representation_displays = {"rep_root": backend.view.representation}
    backend._node_proxies = {"node_root": backend.active_source}

    with pytest.raises(RuntimeError, match="could not clear"):
        backend._inspect_selection(
            {
                "representation_id": "rep_root",
                "viewport": {"x0": 0.1, "y0": 0.1, "x1": 0.2, "y1": 0.2},
            },
            "cmd-select",
        )

    assert backend.simple.cleared_sources == [backend.active_source]
    assert backend._selection is None


def test_package_service_mode_stages_generic_runtime_and_owned_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The package launches a supervisor with no dataset-specific scene input."""
    member_values = [
        {
            "index": index,
            "location": f"/cluster/asteroid/frame-{index:04d}.vti",
            "timestep": float(index),
        }
        for index in range(5)
    ]
    members = tuple(DatasetMember.from_dict(value) for value in member_values)
    fingerprint = calculate_dataset_fingerprint(
        dataset_id="asteroid-subset",
        kind="temporal-volume-series",
        format="vtk-image-data",
        members=members,
    )
    descriptor = {
        "schema_version": "jarvis.dataset-descriptor.v1",
        "dataset_id": "asteroid-subset",
        "kind": "temporal-volume-series",
        "format": "vtk-image-data",
        "members": member_values,
        "arrays": [],
        "bounds": None,
        "fingerprint": {"algorithm": "sha256", "digest": fingerprint},
        "source_artifact": None,
    }
    package = cast(Any, object.__new__(package_module.Paraview))
    package.pkg_dir = str(Path(package_module.__file__).resolve().parent)
    package.pkg_id = "viewer"
    package.pkg_type = "builtin.paraview"
    package.global_id = "visualization.viewer"
    package.shared_dir = str((tmp_path / "shared" / "viewer").resolve())
    package.private_dir = str((tmp_path / "private" / "viewer").resolve())
    package.config = {
        "mode": "service",
        "nprocs": 1,
        "ppn": 1,
        "cwd": "",
        "dataset_descriptor": json.dumps(descriptor),
        "force_offscreen_rendering": True,
        "service_bind_host": "127.0.0.1",
        "service_advertise_host": "127.0.0.1",
        "service_port": 0,
    }
    package.pipeline = SimpleNamespace(get_hostfile=lambda: object())
    package.env = {}
    package.mod_env = {
        "PATH": "/runtime/paraview/bin",
        "JARVIS_EXECUTION_ID": "exec-render",
        "JARVIS_PACKAGE_NAME": "builtin.paraview",
        "JARVIS_PACKAGE_ID": "viewer",
        "JARVIS_PROGRESS_PATH": str((tmp_path / "progress.jsonl").resolve()),
        "JARVIS_PROGRESS_TRANSPORT": "sidecar",
        "JARVIS_ARTIFACT_PATH": str((tmp_path / "artifacts.jsonl").resolve()),
        "JARVIS_ARTIFACT_TRANSPORT": "sidecar",
        "JARVIS_SERVICE_RUNTIME_PATH": str(
            (tmp_path / "service-runtimes.jsonl").resolve()
        ),
    }
    _CapturedExec.commands = []
    _ResolvedWhich.calls = []
    monkeypatch.setattr(package_module, "Exec", _CapturedExec)
    monkeypatch.setattr(package_module, "Which", _ResolvedWhich)

    package.start()

    command, exec_info = _CapturedExec.commands[0]
    assert "service_supervisor.py" in command
    assert "/runtime/paraview/bin/pvpython" in command
    assert "--pvpython-options=--mesa" in command
    assert "--force-offscreen-rendering" not in command
    assert "--bind-host 127.0.0.1" in command
    assert "--advertise-host 127.0.0.1" in command
    assert "--startup-timeout 600" in command
    assert exec_info.env["JARVIS_SERVICE_RUNTIME_PATH"].endswith(
        "service-runtimes.jsonl"
    )
    assert exec_info.env["JARVIS_ARTIFACT_TRANSPORT"] == "sidecar"
    service_roots = list(
        (Path(package.shared_dir) / ".jarvis-service" / "exec-render").iterdir()
    )
    assert len(service_roots) == 1
    service_root = service_roots[0]
    assert (service_root / "service.py").is_file()
    assert (service_root / "service_http.py").is_file()
    assert (service_root / "service_supervisor.py").is_file()
    assert (service_root / "output").is_dir()
    authorization_file = service_root / "authorization.token"
    token = authorization_file.read_text(encoding="ascii").strip()
    assert len(token) == 64
    assert all(character in "0123456789abcdef" for character in token)
    assert token not in command
    assert "--authorization-file" in command
    if sys.platform != "win32":
        assert authorization_file.stat().st_mode & 0o077 == 0
    staged_descriptor = json.loads(
        (service_root / "dataset-descriptor.json").read_text(encoding="utf-8")
    )
    assert staged_descriptor == descriptor

    package.config["service_startup_timeout"] = 30
    package._start_service(dict(package.mod_env))
    deduplicated_command, _ = _CapturedExec.commands[1]
    assert deduplicated_command.count("--mesa") == 1
    assert "--force-offscreen-rendering" not in deduplicated_command
    assert "--startup-timeout 30" in deduplicated_command
    assert [call[0] for call in _ResolvedWhich.calls] == ["pvpython", "pvpython"]
    assert _ResolvedWhich.calls[0][1].env["PATH"] == "/runtime/paraview/bin"

    package.config["service_bind_host"] = "0.0.0.0"
    with pytest.raises(ValueError, match="loopback-only"):
        package._start_service(dict(package.mod_env))


def test_package_validates_dataset_descriptor_during_configuration(
    tmp_path: Path,
) -> None:
    """A malformed catalog copy cannot persist until an execution is launched."""
    package = cast(Any, object.__new__(package_module.Paraview))
    package.config = {
        "mode": "service",
        "dataset_descriptor": json.dumps(
            {
                "schema_version": "jarvis.dataset-descriptor.v1",
                "dataset_id": "asteroid-subset",
                "kind": "temporal-volume-series",
                "format": "vtk-image-data",
                "members": [
                    {
                        "index": 0,
                        "location": "/cluster/asteroid/frame-0000.vti",
                        "timestep": 0.0,
                    }
                ],
                "arrays": [],
                "bounds": None,
            }
        ),
    }

    with pytest.raises(ValueError, match="fingerprint must be an object"):
        package._configure()

    package.config["dataset_descriptor"] = str(
        _supervisor_descriptor(tmp_path / "valid-descriptor.json")
    )
    package._configure()


@pytest.mark.parametrize("mode", ["server", "batch"])
def test_package_requires_service_mode_for_live_dataset_descriptor(mode: str) -> None:
    """A live-view descriptor cannot silently launch a non-service mode."""
    package = cast(Any, object.__new__(package_module.Paraview))
    package.config = {
        "mode": mode,
        "dataset_descriptor": '{"dataset_id":"asteroid-subset"}',
    }

    with pytest.raises(ValueError) as error:
        package._configure()

    assert "mode='service'" in str(error.value)
    assert "live dataset viewing" in str(error.value)

    package.config["dataset_descriptor"] = ""
    package._configure()


def test_package_rejects_unknown_mode_during_configuration() -> None:
    """An invalid mode cannot survive configuration until package startup."""
    package = cast(Any, object.__new__(package_module.Paraview))
    package.config = {"mode": "site-wrapper", "dataset_descriptor": ""}

    with pytest.raises(ValueError, match="Unsupported ParaView mode"):
        package._configure()


def test_package_parameters_describe_live_view_service_mode() -> None:
    """Agent-facing parameter help explains the live-view mode contract."""
    package = cast(Any, object.__new__(package_module.Paraview))
    parameters = {
        parameter["name"]: parameter for parameter in package._configure_menu()
    }

    assert "service for a live dataset view" in parameters["mode"]["msg"]
    assert "requires mode=service" in parameters["dataset_descriptor"]["msg"]
    assert (
        "service mode is always headless"
        in parameters["force_offscreen_rendering"]["msg"]
    )
    assert "pvpython_bin" not in parameters
    assert "pvpython_options" not in parameters
    assert "pvbatch_bin" not in parameters
    assert "pvbatch_options" not in parameters


def test_package_selects_one_deterministic_headless_backend() -> None:
    """Mesa is preferred when available, with force-offscreen as fallback."""
    both = package_module._ParaViewRuntime(
        executable="/runtime/bin/pvpython",
        capabilities=frozenset({"--force-offscreen-rendering", "--mesa"}),
    )
    fallback = package_module._ParaViewRuntime(
        executable="/runtime/bin/pvpython",
        capabilities=frozenset({"--force-offscreen-rendering"}),
    )

    assert both.arguments(force_offscreen=True) == ("--mesa",)
    assert fallback.arguments(force_offscreen=True) == ("--force-offscreen-rendering",)
    assert both.arguments(force_offscreen=False) == ()


@pytest.mark.parametrize(
    ("mode", "executable"),
    [
        ("service", "pvpython"),
        ("batch", "pvbatch"),
        ("server", "pvserver"),
    ],
)
def test_package_fails_clearly_when_mode_runtime_is_absent(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    executable: str,
) -> None:
    """Each mode checks its own dependency in the JARVIS execution PATH."""
    package = cast(Any, object.__new__(package_module.Paraview))
    package.config = {"cwd": ""}
    package.pipeline = SimpleNamespace(get_hostfile=lambda: object())
    _MissingWhich.calls = []
    monkeypatch.setattr(package_module, "Which", _MissingWhich)

    with pytest.raises(RuntimeError) as error:
        package._resolve_runtime(mode, {"PATH": "/jarvis/environment/bin"})

    message = str(error.value)
    assert f"mode={mode!r}" in message
    assert repr(executable) in message
    assert "JARVIS execution environment" in message
    assert _MissingWhich.calls[0][1].env == {"PATH": "/jarvis/environment/bin"}


def test_package_rejects_legacy_launcher_overrides() -> None:
    """Executable paths and raw launcher flags are not semantic parameters."""
    package = cast(Any, object.__new__(package_module.Paraview))
    package.config = {
        "mode": "server",
        "dataset_descriptor": "",
        "pvpython_bin": "/site/ParaView/bin/pvpython",
    }

    with pytest.raises(ValueError, match="execution environment PATH"):
        package._configure()

    package.config = {
        "mode": "server",
        "dataset_descriptor": "",
        "pvpython_bin": "pvpython",
        "pvpython_options": "--mesa",
    }
    with pytest.raises(ValueError, match="detects supported headless arguments"):
        package._configure()


def test_package_exec_failure_preserves_bounded_stderr() -> None:
    """A failed remote launch reports its actionable process diagnostic."""
    result = SimpleNamespace(
        exit_code={"localhost": 127},
        stderr={"localhost": "/bin/sh: pvserver: command not found\n"},
    )

    with pytest.raises(RuntimeError) as error:
        package_module.Paraview._raise_for_exec_failure(
            result,
            operation="ParaView server",
        )

    message = str(error.value)
    assert "localhost=127" in message
    assert "pvserver: command not found" in message


def test_package_exec_failure_truncates_oversized_stderr() -> None:
    """Remote stderr cannot make the raised execution error unbounded."""
    result = SimpleNamespace(
        exit_code={"localhost": 1},
        stderr={"localhost": "actionable-prefix " + ("x" * 5000)},
    )

    with pytest.raises(RuntimeError) as error:
        package_module.Paraview._raise_for_exec_failure(
            result,
            operation="ParaView service",
        )

    message = str(error.value)
    assert "actionable-prefix" in message
    assert message.endswith("...")
    assert len(message) < 4200


def test_service_export_is_immediately_queryable_through_artifact_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Export results are fsynced to the existing artifact API before return."""
    sidecar = (tmp_path / "execution" / "artifacts" / "viewer.jsonl").resolve()
    output = (tmp_path / "shared" / "output" / "frame.png").resolve()
    output.parent.mkdir(parents=True)
    monkeypatch.setenv("JARVIS_ARTIFACT_PATH", str(sidecar))
    monkeypatch.setenv("JARVIS_EXECUTION_ID", "exec-render")
    monkeypatch.setenv("JARVIS_PACKAGE_NAME", "builtin.paraview")
    monkeypatch.setenv("JARVIS_PACKAGE_ID", "viewer")

    staged_path, payload = service_module._stage_png(
        simple=_ScreenshotSimple(),
        view=object(),
        output_dir=output.parent,
        width=640,
        height=480,
    )
    returned = service_module._prepare_artifact_event(
        artifact_id="art_0123456789abcdefghijklmn",
        logical_name="frame.png",
        path=output,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
        command_id="cmd-export",
        representation_ids=["rep_root"],
        scene_digest="sha256:" + "0" * 64,
        cluster_location="/cluster/output/frame.png",
    )
    service_module._commit_staged_artifacts(
        {
            returned["artifact_id"]: {
                "event": returned,
                "staged_path": staged_path,
                "output_path": output,
            }
        }
    )
    persisted = ArtifactStore(sidecar).latest(returned["artifact_id"])

    assert persisted is not None
    assert persisted.artifact_id == returned["artifact_id"]
    assert persisted.location is not None
    assert persisted.location.value == "/cluster/output/frame.png"
    assert persisted.checksum == "sha256:" + hashlib.sha256(payload).hexdigest()
