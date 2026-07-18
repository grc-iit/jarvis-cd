"""Generic ParaView HTTP/SSE service and artifact-boundary tests."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, cast

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

    def fake_urlopen(_request: object, *, timeout: float) -> _HealthResponse:
        assert timeout == 0.5
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
    )
    response_payload["revision"] = True
    assert not supervisor_module._health_ready(
        "127.0.0.1",
        18080,
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
    )
    response_payload.update(expected)
    response_payload["unexpected"] = "field"
    assert not supervisor_module._health_ready(
        "127.0.0.1",
        18080,
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
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
    ) -> bool:
        nonlocal probe_calls
        assert service_instance_id == "srv_0123456789abcdef0123456789abcdef"
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
            "active_field": None,
            "filters": [],
            "colormap": None,
            "camera": {
                "position": [1.0, 1.0, 1.0],
                "focal_point": [0.0, 0.0, 0.0],
                "view_up": [0.0, 1.0, 0.0],
                "parallel_scale": 1.0,
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
        self.pixel_rectangle: list[int] | None = None

    def SelectSurfaceCells(
        self,
        pixel_rectangle: list[int],
        representations: _SelectionCollection,
        selections: _SelectionCollection,
        _modifier: int,
    ) -> None:
        self.pixel_rectangle = pixel_rectangle
        representations.items.append(_SelectionRepresentation(self.source))
        selections.items.append(_SelectionSource("IDSelectionSource", self.ids))


class _SelectionSimple:
    def __init__(self) -> None:
        self.surface_calls: list[tuple[list[int], Any]] = []

    def Render(self, _view: Any) -> None:
        return

    def SelectSurfaceCells(self, *, Rectangle: list[int], View: Any) -> None:
        self.surface_calls.append((Rectangle, View))


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


def test_controller_rejects_stale_revision_before_backend_mutation() -> None:
    """Concurrent agents receive an explicit conflict instead of lost updates."""
    controller = ServiceStateController(
        backend=_Backend(),
        execution_id="exec-render",
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


def test_unique_png_export_never_overwrites_and_rejects_invalid_output(
    tmp_path: Path,
) -> None:
    """Publishing validates PNG bytes and uses create-if-absent semantics."""
    simple = _ScreenshotSimple()
    output = tmp_path / "frame.png"

    payload = service_module._write_unique_png(
        simple=simple,
        view=object(),
        output_path=output,
        width=640,
        height=480,
    )

    assert payload == _PNG
    assert output.read_bytes() == _PNG
    with pytest.raises(CommandError) as captured:
        service_module._write_unique_png(
            simple=simple,
            view=object(),
            output_path=output,
            width=640,
            height=480,
        )
    assert captured.value.code == "artifact_exists"
    assert captured.value.status is HTTPStatus.CONFLICT
    assert output.read_bytes() == _PNG
    assert len(simple.calls) == 1

    invalid_output = tmp_path / "invalid.png"
    with pytest.raises(RuntimeError, match="did not produce a PNG"):
        service_module._write_unique_png(
            simple=_ScreenshotSimple(b"not-a-png"),
            view=object(),
            output_path=invalid_output,
            width=640,
            height=480,
        )
    assert not invalid_output.exists()


def test_export_rolls_back_png_when_manifest_publication_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed artifact record cannot leave an untracked exported PNG."""
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    backend.output_dir = tmp_path.resolve()
    backend.simple = _ScreenshotSimple()
    backend.view = object()
    backend._artifacts = []
    backend.service_instance_id = "srv_0123456789abcdef0123456789abcdef"

    def fail_manifest(**_kwargs: Any) -> dict[str, Any]:
        raise OSError("sidecar fsync failed")

    monkeypatch.setattr(service_module, "_append_artifact", fail_manifest)

    with pytest.raises(OSError, match="sidecar fsync failed"):
        backend._export_artifact(
            {"filename": "nested/frame.png", "width": 640, "height": 480},
            "cmd-export",
        )

    assert not (tmp_path / "nested" / "frame.png").exists()
    assert backend._artifacts == []


class _CameraView:
    """Mutable camera properties matching ParaView's render view surface."""

    def __init__(self) -> None:
        self.CameraPosition = [4.0, 3.0, 2.0]
        self.CameraFocalPoint = [0.0, 0.0, 0.0]
        self.CameraViewUp = [0.0, 1.0, 0.0]
        self.CameraParallelScale = 5.0


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
    with pytest.raises(RuntimeError, match="render failed"):
        backend._set_camera(
            {"position": [8.0, 3.0, 2.0], "parallel_scale": 8.0},
            "cmd-failed-camera",
        )

    assert backend._camera_state() == original
    assert backend.simple.render_calls == 2


class _FilterProxy:
    """Minimal slice proxy used to exercise mutation ordering."""

    def __init__(self) -> None:
        self.SliceType = SimpleNamespace(Origin=None, Normal=None)
        self.updated = False

    def UpdatePipeline(self) -> None:
        self.updated = True


class _FilterSimple:
    """ParaView surface intentionally omitting ResetCamera."""

    def __init__(self) -> None:
        self.slice_calls: list[object] = []
        self.hidden: list[object] = []
        self.proxy = _FilterProxy()

    def Slice(self, *, Input: object) -> _FilterProxy:
        self.slice_calls.append(Input)
        return self.proxy

    def Show(self, source: object, _view: object) -> object:
        return SimpleNamespace(source=source)

    def Hide(self, source: object, _view: object) -> None:
        self.hidden.append(source)

    def Delete(self, _source: object) -> None:
        return

    def Render(self, _view: object) -> None:
        return


def test_filter_validates_atomically_and_preserves_explicit_camera() -> None:
    """Filters have no hidden reset and commit only after full validation."""
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    previous_source = object()
    backend.active_source = previous_source
    backend.display = object()
    backend.view = _CameraView()
    backend.simple = _FilterSimple()
    backend._filters = []
    backend._arrays = []
    backend._active_field = {"name": "pressure", "association": "point"}
    backend._colormap = {"preset": "Viridis", "invert": False}
    backend._selection = {"status": "selected"}
    backend._discover_arrays = lambda _source=None: []
    backend._discover_bounds = lambda _source=None: (0.0, 1.0, 0.0, 1.0, 0.0, 1.0)
    original_camera = backend._camera_state()

    with pytest.raises(CommandError, match="zero vector"):
        backend._apply_filter(
            {
                "type": "slice",
                "parameters": {
                    "origin": [0.0, 0.0, 0.0],
                    "normal": [0.0, 0.0, 0.0],
                },
            },
            "cmd-invalid-filter",
        )

    assert backend.simple.slice_calls == []
    result = backend._apply_filter(
        {
            "type": "slice",
            "parameters": {
                "origin": [0.0, 0.0, 0.0],
                "normal": [0.0, 0.0, 1.0],
            },
        },
        "cmd-valid-filter",
    )

    assert result["filter"]["type"] == "slice"
    assert backend.active_source is backend.simple.proxy
    assert backend.simple.proxy.updated is True
    assert backend._camera_state() == original_camera
    assert backend._active_field is None
    assert backend._colormap is None
    assert backend._selection is None


class _FieldDisplay:
    """Record transfer-function rescaling for active-field tests."""

    def __init__(self) -> None:
        self.rescale_calls = 0

    def RescaleTransferFunctionToDataRange(
        self,
        _extend: bool,
        _force: bool,
    ) -> None:
        self.rescale_calls += 1


class _FieldSimple(_RenderSimple):
    """Record the actual ColorBy choice applied to a display."""

    def __init__(self) -> None:
        super().__init__()
        self.color_by: tuple[str, str] | None = None

    def ColorBy(self, _display: object, field: tuple[str, str]) -> None:
        self.color_by = field


def test_active_field_change_clears_stale_colormap_semantics() -> None:
    """A transfer preset is never claimed for a newly selected array."""
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    backend._arrays = [{"name": "temperature", "association": "point", "components": 1}]
    backend._active_field = {
        "name": "pressure",
        "association": "point",
        "components": 1,
    }
    backend._colormap = {"preset": "Viridis", "invert": False}
    backend.display = _FieldDisplay()
    backend.simple = _FieldSimple()
    backend.view = object()

    result = backend._set_active_field(
        {"name": "temperature", "association": "point"},
        "cmd-field",
    )

    assert result["active_field"]["name"] == "temperature"
    assert backend.simple.color_by == ("POINTS", "temperature")
    assert backend._colormap is None


def test_http_health_state_command_conflict_and_frame_sse() -> None:
    """The real stdlib server exposes all bounded transport behaviors."""
    controller = ServiceStateController(
        backend=_Backend(),
        execution_id="exec-render",
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
    )
    server = create_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(origin + "/healthz", timeout=2) as response:
            health = json.load(response)
        with urllib.request.urlopen(origin + "/state", timeout=2) as response:
            state = json.load(response)
        request = urllib.request.Request(
            origin + "/commands",
            data=json.dumps(_command()).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            result = json.load(response)
        stale = urllib.request.Request(
            origin + "/commands",
            data=json.dumps(
                _command(command_id="cmd-2", expected_revision=1, index=0)
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(stale, timeout=2)
        conflict = json.load(captured.value)
        with urllib.request.urlopen(origin + "/live-data", timeout=2) as response:
            event = response.readline().decode("ascii").strip()
            event_id = response.readline().decode("ascii").strip()
            data = response.readline().decode("utf-8").strip()

        assert health["status"] == "ready"
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
        )


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


def test_surface_selection_returns_real_ids_with_bounded_result() -> None:
    """The response caps IDs while preserving ParaView's exact selected count."""
    source = SimpleNamespace()
    representations = _SelectionCollection()
    representations.items.append(_SelectionRepresentation(source))
    selections = _SelectionCollection()
    selections.items.append(
        _SelectionSource(
            "IDSelectionSource",
            [value for index in range(300) for value in (0, index)],
        )
    )

    ids, count, reason = service_module._surface_selection_ids(
        selected_representations=representations,
        selection_sources=selections,
        active_source=SimpleNamespace(SMProxy=source),
        servermanager=_SelectionServerManager(),
        limit=256,
    )

    assert reason is None
    assert count == 300
    assert len(ids) == 256
    assert ids[0] == {"process_id": 0, "element_id": 0}
    assert ids[-1] == {"process_id": 0, "element_id": 255}


def test_viewport_inspection_uses_backend_selection_and_exact_result_shape() -> None:
    """Viewport inspection delegates to ParaView and never maps pixels to world data."""
    source = SimpleNamespace()
    view = _SelectionView(source, [0, 41, 0, 73])
    simple = _SelectionSimple()
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    backend.active_source = SimpleNamespace(SMProxy=source)
    backend.view = view
    backend.simple = simple
    backend.servermanager = _SelectionServerManager()
    backend.vtk = SimpleNamespace(vtkCollection=_SelectionCollection)
    backend._selection = None

    result = backend._inspect_selection(
        {"viewport": {"x0": 0.25, "y0": 0.1, "x1": 0.75, "y1": 0.9}},
        "cmd-select",
    )

    selection = result["selection"]
    assert selection == {
        "selector": "viewport",
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
    assert simple.surface_calls == [(selection["pixel_rectangle"], view)]


def test_viewport_inspection_reports_unsupported_without_approximation() -> None:
    """A missing backend picker is visible to callers instead of being guessed."""
    backend = cast(Any, object.__new__(service_module.ParaViewBackend))
    backend.active_source = SimpleNamespace(SMProxy=object())
    backend.view = SimpleNamespace()
    backend.simple = _SelectionSimple()
    backend.servermanager = _SelectionServerManager()
    backend.vtk = SimpleNamespace(vtkCollection=_SelectionCollection)
    backend._selection = None

    result = backend._inspect_selection(
        {"viewport": {"x0": 0.1, "y0": 0.1, "x1": 0.2, "y1": 0.2}},
        "cmd-select",
    )

    selection = result["selection"]
    assert selection["status"] == "unsupported"
    assert selection["selected_count"] is None
    assert selection["ids"] == []
    assert selection["reason"] == "paraview_surface_selection_unavailable"


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
        "service_startup_timeout": 30,
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
    staged_descriptor = json.loads(
        (service_root / "dataset-descriptor.json").read_text(encoding="utf-8")
    )
    assert staged_descriptor == descriptor

    package._start_service(dict(package.mod_env))
    deduplicated_command, _ = _CapturedExec.commands[1]
    assert deduplicated_command.count("--mesa") == 1
    assert "--force-offscreen-rendering" not in deduplicated_command
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
    output.write_bytes(_PNG)
    monkeypatch.setenv("JARVIS_ARTIFACT_PATH", str(sidecar))
    monkeypatch.setenv("JARVIS_EXECUTION_ID", "exec-render")
    monkeypatch.setenv("JARVIS_PACKAGE_NAME", "builtin.paraview")
    monkeypatch.setenv("JARVIS_PACKAGE_ID", "viewer")

    returned = service_module._append_artifact(
        artifact_id="art_0123456789abcdefghijklmn",
        logical_name="frame.png",
        path=output,
        size_bytes=len(_PNG),
        sha256="a" * 64,
        service_instance_id="srv_0123456789abcdef0123456789abcdef",
        command_id="cmd-export",
        cluster_location="/cluster/output/frame.png",
    )
    persisted = ArtifactStore(sidecar).latest(returned["artifact_id"])

    assert persisted is not None
    assert persisted.artifact_id == returned["artifact_id"]
    assert persisted.location is not None
    assert persisted.location.value == "/cluster/output/frame.png"
    assert persisted.checksum == "sha256:" + "a" * 64
