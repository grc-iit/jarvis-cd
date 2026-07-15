"""Bounded HTTP/SSE control plane for an authoritative ParaView backend.

This module intentionally uses only the Python standard library so it can run
inside the Python bundled with ParaView. Dataset-specific rendering choices do
not belong here; every visualization mutation arrives as a versioned command.
"""

from __future__ import annotations

import base64
import json
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Mapping, Optional, Protocol, Tuple, cast

STATE_SCHEMA = "jarvis.paraview.service-state.v1"
COMMAND_SCHEMA = "jarvis.paraview.command.v1"
COMMAND_RESULT_SCHEMA = "jarvis.paraview.command-result.v1"
COMMAND_ERROR_SCHEMA = "jarvis.paraview.command-error.v1"
FRAME_SCHEMA = "jarvis.paraview.frame.v1"
MAX_COMMAND_BYTES = 64 * 1024
MAX_FRAME_BYTES = 32 * 1024 * 1024
MAX_COMMANDS_PER_SERVICE = 4096
_COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_OPERATIONS = frozenset(
    {
        "set_timestep",
        "set_active_field",
        "set_camera",
        "apply_filter",
        "set_colormap",
        "inspect_selection",
        "export_artifact",
    }
)


class VisualizationBackend(Protocol):
    """Real renderer contract consumed by the transport layer."""

    def dataset_state(self) -> Dict[str, Any]:
        """Return descriptor plus real bounded discovery facts."""
        ...

    def pipeline_state(self) -> Dict[str, Any]:
        """Return the complete authoritative visualization state."""
        ...

    def execute(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        command_id: str,
    ) -> Dict[str, Any]:
        """Apply one semantic operation and return its real result."""
        ...

    def render_png(self) -> bytes:
        """Render the current view and return a PNG frame."""
        ...


class CommandError(ValueError):
    """A bounded client command failed validation or execution."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: HTTPStatus = HTTPStatus.BAD_REQUEST,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}


class ServiceStateController:
    """Serialize commands, revisions, state, frames, and idempotent results."""

    def __init__(
        self,
        *,
        backend: VisualizationBackend,
        execution_id: str,
        service_instance_id: str,
        max_commands: int = MAX_COMMANDS_PER_SERVICE,
    ) -> None:
        """Initialize from one real backend and produce the first frame."""
        if (
            isinstance(max_commands, bool)
            or not isinstance(max_commands, int)
            or max_commands < 1
        ):
            raise ValueError("max_commands must be a positive integer")
        self.backend = backend
        self.execution_id = execution_id
        self.service_instance_id = service_instance_id
        self.max_commands = max_commands
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._revision = 1
        self._results: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        self._frame = self._bounded_frame(backend.render_png())
        self._state = self._capture_state()

    def state(self) -> Dict[str, Any]:
        """Return a JSON-safe copy of current authoritative state."""
        with self._lock:
            return _json_copy(self._state)

    def frame(self) -> Tuple[int, bytes]:
        """Return current state revision and immutable PNG bytes."""
        with self._lock:
            return self._revision, self._frame

    def wait_for_change(
        self,
        revision: int,
        *,
        timeout: float,
    ) -> Tuple[int, Dict[str, Any], bytes]:
        """Wait for a newer state or a heartbeat timeout."""
        with self._changed:
            if self._revision <= revision:
                self._changed.wait(timeout=timeout)
            return self._revision, _json_copy(self._state), self._frame

    def command(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        """Validate, de-duplicate, apply, and version one command."""
        canonical, command_id, operation, expected, arguments = _validate_command(
            payload
        )
        with self._changed:
            previous = self._results.get(command_id)
            if previous is not None:
                previous_request, previous_response = previous
                if previous_request != canonical:
                    raise CommandError(
                        "idempotency_conflict",
                        "command_id was already used for a different command",
                        status=HTTPStatus.CONFLICT,
                    )
                return _json_copy(previous_response)
            if len(self._results) >= self.max_commands:
                raise CommandError(
                    "command_limit",
                    "the service lifetime command limit was reached",
                    status=HTTPStatus.TOO_MANY_REQUESTS,
                    details={"max_commands": self.max_commands},
                )
            if expected is not None and expected != self._revision:
                raise CommandError(
                    "revision_conflict",
                    "expected_revision does not match authoritative state",
                    status=HTTPStatus.CONFLICT,
                    details={
                        "expected_revision": expected,
                        "actual_revision": self._revision,
                    },
                )
            try:
                result = self.backend.execute(operation, arguments, command_id)
            except CommandError:
                raise
            except Exception as exc:
                raise CommandError(
                    "operation_failed",
                    f"ParaView operation failed: {exc}",
                    status=HTTPStatus.UNPROCESSABLE_ENTITY,
                ) from exc
            if not isinstance(result, dict):
                raise RuntimeError("ParaView backend returned a non-object result")
            # Exporting a screenshot does not change the visible view. Reusing the
            # already validated frame avoids introducing a failure after the
            # durable artifact has been published.
            if operation != "export_artifact":
                self._frame = self._bounded_frame(self.backend.render_png())
            self._revision += 1
            self._state = self._capture_state()
            response = {
                "schema_version": COMMAND_RESULT_SCHEMA,
                "command_id": command_id,
                "operation": operation,
                "applied": True,
                "state": self._state,
                "result": result,
            }
            _canonical_json(response)
            self._results[command_id] = (canonical, _json_copy(response))
            self._changed.notify_all()
            return _json_copy(response)

    def _capture_state(self) -> Dict[str, Any]:
        state = {
            "schema_version": STATE_SCHEMA,
            "service_instance_id": self.service_instance_id,
            "revision": self._revision,
            "execution_id": self.execution_id,
            "dataset": self.backend.dataset_state(),
            "pipeline": self.backend.pipeline_state(),
        }
        _validate_state_shape(state)
        _canonical_json(state)
        return state

    @staticmethod
    def _bounded_frame(value: bytes) -> bytes:
        if not isinstance(value, bytes) or not value:
            raise RuntimeError("ParaView backend returned an empty non-byte frame")
        if len(value) > MAX_FRAME_BYTES:
            raise RuntimeError("ParaView frame exceeds the service size limit")
        if not value.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("ParaView backend did not return a PNG frame")
        return value


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        address: Tuple[str, int],
        controller: ServiceStateController,
    ) -> None:
        self.controller = controller
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "JarvisParaView/1"

    @property
    def controller(self) -> ServiceStateController:
        return cast(_Server, self.server).controller

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        """Serve bounded health, state, frame, and event streams."""
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._json(
                HTTPStatus.OK,
                {
                    "schema_version": "jarvis.paraview.health.v1",
                    "status": "ready",
                    "service_instance_id": self.controller.service_instance_id,
                    "revision": self.controller.state()["revision"],
                },
            )
            return
        if path == "/state":
            self._json(HTTPStatus.OK, self.controller.state())
            return
        if path == "/live-data":
            self._serve_sse(frames=True)
            return
        if path == "/events":
            self._serve_sse(frames=False)
            return
        self._error(HTTPStatus.NOT_FOUND, "not_found", "endpoint does not exist")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        """Apply one authoritative semantic command."""
        if self.path.split("?", 1)[0] != "/commands":
            self._error(HTTPStatus.NOT_FOUND, "not_found", "endpoint does not exist")
            return
        try:
            payload = self._read_json()
            response = self.controller.command(payload)
        except CommandError as exc:
            self._error(exc.status, exc.code, str(exc), details=exc.details)
            return
        self._json(HTTPStatus.OK, response)

    def _read_json(self) -> Dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type != "application/json":
            raise CommandError(
                "unsupported_media_type",
                "commands require Content-Type application/json",
                status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            )
        rendered_length = self.headers.get("Content-Length")
        try:
            length = int(rendered_length or "")
        except ValueError as exc:
            raise CommandError("invalid_length", "Content-Length is required") from exc
        if not 1 <= length <= MAX_COMMAND_BYTES:
            raise CommandError(
                "invalid_length",
                f"command body must be 1-{MAX_COMMAND_BYTES} bytes",
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        payload = self.rfile.read(length)
        if len(payload) != length:
            raise CommandError("incomplete_body", "command body was incomplete")
        try:
            value = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise CommandError(
                "invalid_json", "command body is not valid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise CommandError("invalid_command", "command body must be an object")
        return cast(Dict[str, Any], value)

    def _serve_sse(self, *, frames: bool) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        revision = 0
        try:
            while True:
                current, state, frame = self.controller.wait_for_change(
                    revision,
                    timeout=15.0,
                )
                if current == revision:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    continue
                if frames:
                    value: Dict[str, Any] = {
                        "schema_version": FRAME_SCHEMA,
                        "service_instance_id": self.controller.service_instance_id,
                        "revision": current,
                        "media_type": "image/png",
                        "encoding": "base64",
                        "data": base64.b64encode(frame).decode("ascii"),
                    }
                    event_name = "frame"
                else:
                    value = state
                    event_name = "state"
                payload = _canonical_json(value).encode("utf-8")
                self.wfile.write(b"event: " + event_name.encode("ascii") + b"\n")
                self.wfile.write(b"id: " + str(current).encode("ascii") + b"\n")
                self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
                revision = current
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    def _json(self, status: HTTPStatus, value: Mapping[str, Any]) -> None:
        payload = _canonical_json(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _error(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
        *,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._json(
            status,
            {
                "schema_version": COMMAND_ERROR_SCHEMA,
                "error": {
                    "code": code,
                    "message": message,
                    "details": details or {},
                },
            },
        )

    def log_message(self, format: str, *args: object) -> None:
        """Emit bounded request logs without command payloads."""
        print(
            "%s - - [%s] %s"
            % (self.address_string(), self.log_date_time_string(), format % args),
            flush=True,
        )


def create_server(
    host: str,
    port: int,
    controller: ServiceStateController,
) -> ThreadingHTTPServer:
    """Create a bound service; callers control lifecycle and signal handling."""
    return _Server((host, port), controller)


def _validate_command(
    value: Mapping[str, Any],
) -> Tuple[str, str, str, Optional[int], Mapping[str, Any]]:
    expected_fields = {
        "schema_version",
        "command_id",
        "operation",
        "expected_revision",
        "arguments",
    }
    if set(value) != expected_fields:
        raise CommandError(
            "invalid_command",
            "command fields do not match jarvis.paraview.command.v1",
        )
    if value.get("schema_version") != COMMAND_SCHEMA:
        raise CommandError("invalid_schema", "unsupported command schema_version")
    command_id = value.get("command_id")
    if not isinstance(command_id, str) or _COMMAND_ID.fullmatch(command_id) is None:
        raise CommandError("invalid_command_id", "command_id is invalid")
    operation = value.get("operation")
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        raise CommandError("unsupported_operation", "operation is not supported")
    expected = value.get("expected_revision")
    if expected is not None and (
        isinstance(expected, bool) or not isinstance(expected, int) or expected < 1
    ):
        raise CommandError(
            "invalid_revision",
            "expected_revision must be a positive integer or null",
        )
    arguments = value.get("arguments")
    if not isinstance(arguments, dict):
        raise CommandError("invalid_arguments", "arguments must be an object")
    canonical = _canonical_json(value)
    return canonical, command_id, operation, expected, arguments


def _validate_state_shape(value: Mapping[str, Any]) -> None:
    if set(value) != {
        "schema_version",
        "service_instance_id",
        "revision",
        "execution_id",
        "dataset",
        "pipeline",
    }:
        raise RuntimeError("ParaView state fields do not match the stable schema")
    dataset = value.get("dataset")
    pipeline = value.get("pipeline")
    if not isinstance(dataset, dict) or set(dataset) != {"descriptor", "discovery"}:
        raise RuntimeError("ParaView dataset state is incomplete")
    if not isinstance(pipeline, dict) or set(pipeline) != {
        "timestep",
        "active_field",
        "filters",
        "colormap",
        "camera",
        "selection",
        "artifacts",
    }:
        raise RuntimeError("ParaView pipeline state is incomplete")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("service payload must contain finite JSON values") from exc


def _json_copy(value: Mapping[str, Any]) -> Dict[str, Any]:
    return cast(Dict[str, Any], json.loads(_canonical_json(value)))


def _reject_duplicate_keys(pairs: list[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key: %s" % key)
        value[key] = item
    return value


__all__ = [
    "COMMAND_ERROR_SCHEMA",
    "COMMAND_RESULT_SCHEMA",
    "COMMAND_SCHEMA",
    "FRAME_SCHEMA",
    "STATE_SCHEMA",
    "CommandError",
    "ServiceStateController",
    "VisualizationBackend",
    "create_server",
]
