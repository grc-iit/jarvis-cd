"""Bounded HTTP/SSE control plane for an authoritative ParaView backend.

This module intentionally uses only the Python standard library so it can run
inside the Python bundled with ParaView. Dataset-specific rendering choices do
not belong here; every visualization mutation arrives as a versioned command.
"""

from __future__ import annotations

import base64
import hmac
import io
import json
import math
import re
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple, cast

STATE_SCHEMA = "jarvis.paraview.service-state.v2"
COMMAND_SCHEMA = "jarvis.paraview.command.v2"
COMMAND_RESULT_SCHEMA = "jarvis.paraview.command-result.v2"
COMMAND_ERROR_SCHEMA = "jarvis.paraview.command-error.v1"
FRAME_SCHEMA = "jarvis.paraview.frame.v1"
MAX_COMMAND_BYTES = 64 * 1024
MAX_FRAME_BYTES = 32 * 1024 * 1024
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_COMMANDS_PER_SERVICE = 4096
MAX_IDEMPOTENCY_PAYLOAD_BYTES = 64 * 1024 * 1024
MAX_HTTP_CONNECTIONS = 32
MAX_SSE_SUBSCRIBERS = 8
HTTP_REQUEST_QUEUE_SIZE = 16
HTTP_HEADER_TIMEOUT_SECONDS = 5.0
HTTP_BODY_TIMEOUT_SECONDS = 10.0
HTTP_WRITE_TIMEOUT_SECONDS = 10.0
SSE_HEARTBEAT_SECONDS = 15.0
MAX_SCENE_NODES = 32
MAX_SCENE_REPRESENTATIONS = 32
MAX_SCENE_MEASUREMENTS = 32
MAX_MEASUREMENT_TIMESTEPS = 32
MAX_STORED_MEASUREMENT_SAMPLES = 128
MAX_SCENE_ARRAYS = 256
_COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_BEARER_TOKEN = re.compile(r"^[0-9a-f]{64}$")
_OPERATIONS = frozenset(
    {
        "set_timestep",
        "measure_field",
        "create_filter",
        "set_representation",
        "remove_scene_object",
        "fit_camera",
        "set_camera",
        "inspect_selection",
        "export_artifact",
        "export_scene",
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

    def begin_command(self) -> object:
        """Capture one reversible backend transaction checkpoint."""
        ...

    def commit_command(self, checkpoint: object) -> None:
        """Finalize a command after response validation succeeds."""
        ...

    def rollback_command(self, checkpoint: object) -> None:
        """Restore a command after any execute/frame/state failure."""
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


class _DeadlineSocketReader(io.RawIOBase):
    """Read one socket against an absolute, phase-specific deadline."""

    def __init__(self, connection: socket.socket) -> None:
        super().__init__()
        self._connection = connection
        self._deadline: Optional[float] = None

    def readable(self) -> bool:
        """Return true because this raw stream only supports reads."""
        return True

    def start_phase(self, timeout: float) -> None:
        """Start a new absolute read phase using monotonic time."""
        self._deadline = time.monotonic() + timeout

    def clear_phase(self) -> None:
        """Clear the active read phase after its bytes are complete."""
        self._deadline = None

    def readinto(self, buffer: Any) -> int:
        """Read bytes while reducing the socket timeout to time remaining."""
        if self.closed:
            raise ValueError("read from closed ParaView request stream")
        deadline = self._deadline
        if deadline is None:
            raise RuntimeError("ParaView request read has no active deadline")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("ParaView request phase deadline expired")
        self._connection.settimeout(remaining)
        try:
            return self._connection.recv_into(buffer)
        except socket.timeout as exc:
            raise TimeoutError("ParaView request phase deadline expired") from exc


class ServiceStateController:
    """Serialize commands, revisions, state, frames, and idempotent results."""

    def __init__(
        self,
        *,
        backend: VisualizationBackend,
        execution_id: str,
        package_name: str,
        package_id: str,
        service_instance_id: str,
        max_commands: int = MAX_COMMANDS_PER_SERVICE,
        max_idempotency_bytes: int = MAX_IDEMPOTENCY_PAYLOAD_BYTES,
    ) -> None:
        """Initialize from one real backend and produce the first frame."""
        if (
            isinstance(max_commands, bool)
            or not isinstance(max_commands, int)
            or max_commands < 1
        ):
            raise ValueError("max_commands must be a positive integer")
        if (
            isinstance(max_idempotency_bytes, bool)
            or not isinstance(max_idempotency_bytes, int)
            or max_idempotency_bytes < 1
        ):
            raise ValueError("max_idempotency_bytes must be a positive integer")
        if not all(_nonempty_text(value) for value in (package_name, package_id)):
            raise ValueError("package_name and package_id must be bounded text")
        self.backend = backend
        self.execution_id = execution_id
        self.package_name = package_name
        self.package_id = package_id
        self.service_instance_id = service_instance_id
        for attribute, expected in (
            ("execution_id", execution_id),
            ("package_name", package_name),
            ("package_id", package_id),
            ("service_instance_id", service_instance_id),
        ):
            actual = getattr(backend, attribute, expected)
            if actual != expected:
                raise ValueError(
                    f"backend {attribute} does not match the controller binding"
                )
        self.max_commands = max_commands
        self.max_idempotency_bytes = max_idempotency_bytes
        self._command_lock = threading.Lock()
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._revision = 1
        self._results: Dict[str, Tuple[bytes, bytes]] = {}
        self._idempotency_payload_bytes = 0
        self._frame = self._bounded_frame(backend.render_png())
        self._state = self._capture_state()
        self._state_sse_payload = _state_sse_payload(self._revision, self._state)
        self._frame_sse_payload = _frame_sse_payload(
            self.service_instance_id,
            self._revision,
            self._frame,
        )

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

    def wait_for_sse_change(
        self,
        revision: int,
        *,
        timeout: float,
    ) -> Tuple[int, bytes, bytes]:
        """Return shared immutable SSE payloads without per-subscriber encoding."""
        with self._changed:
            if self._revision <= revision:
                self._changed.wait(timeout=timeout)
            return (
                self._revision,
                self._state_sse_payload,
                self._frame_sse_payload,
            )

    def wake_waiters(self) -> bool:
        """Wake SSE waiters without blocking behind an executing command."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            self._changed.notify_all()
        finally:
            self._lock.release()
        return True

    def command(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        """Validate, de-duplicate, apply, and version one command."""
        canonical, command_id, operation, expected, arguments = _validate_command(
            payload
        )
        canonical_request = canonical.encode("utf-8")
        with self._command_lock:
            with self._changed:
                previous = self._results.get(command_id)
                if previous is not None:
                    previous_request, previous_response = previous
                    if previous_request != canonical_request:
                        raise CommandError(
                            "idempotency_conflict",
                            "command_id was already used for a different command",
                            status=HTTPStatus.CONFLICT,
                        )
                    decoded = json.loads(previous_response.decode("utf-8"))
                    if not isinstance(decoded, dict):
                        raise RuntimeError("cached ParaView response is not an object")
                    return decoded
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
                current_revision = self._revision
                current_frame = self._frame
                current_idempotency_bytes = self._idempotency_payload_bytes

            checkpoint = self.backend.begin_command()
            try:
                backend_arguments = dict(arguments)
                if operation == "export_scene":
                    backend_arguments["_final_revision"] = current_revision + 1
                result = self.backend.execute(
                    operation,
                    backend_arguments,
                    command_id,
                )
                if not isinstance(result, dict):
                    raise RuntimeError("ParaView backend returned a non-object result")
                # Export is already the exact current frame and publication is
                # deterministic. Every scene mutation must prove a new frame.
                candidate_frame = (
                    current_frame
                    if operation in {"export_artifact", "export_scene"}
                    else self._bounded_frame(self.backend.render_png())
                )
                candidate_revision = current_revision + 1
                candidate_state = self._capture_state(candidate_revision)
                candidate_state_sse_payload = _state_sse_payload(
                    candidate_revision,
                    candidate_state,
                )
                candidate_frame_sse_payload = _frame_sse_payload(
                    self.service_instance_id,
                    candidate_revision,
                    candidate_frame,
                )
                response = {
                    "schema_version": COMMAND_RESULT_SCHEMA,
                    "command_id": command_id,
                    "operation": operation,
                    "applied": True,
                    "state": candidate_state,
                    "result": result,
                }
                canonical_response = _canonical_json(response).encode("utf-8")
                if len(canonical_response) > MAX_RESPONSE_BYTES:
                    raise RuntimeError(
                        "ParaView command response exceeds the service size limit"
                    )
                candidate_idempotency_bytes = (
                    current_idempotency_bytes
                    + len(canonical_request)
                    + len(canonical_response)
                )
                if candidate_idempotency_bytes > self.max_idempotency_bytes:
                    raise CommandError(
                        "idempotency_payload_limit",
                        "the service idempotency payload budget was reached",
                        status=HTTPStatus.TOO_MANY_REQUESTS,
                        details={
                            "max_idempotency_bytes": self.max_idempotency_bytes,
                            "stored_idempotency_bytes": current_idempotency_bytes,
                        },
                    )
                self.backend.commit_command(checkpoint)
            except Exception as exc:
                try:
                    self.backend.rollback_command(checkpoint)
                except Exception:
                    raise RuntimeError(
                        "ParaView command failed and its backend transaction could "
                        "not be restored"
                    ) from None
                if isinstance(exc, CommandError):
                    raise
                raise CommandError(
                    "operation_failed",
                    "ParaView operation failed",
                    status=HTTPStatus.UNPROCESSABLE_ENTITY,
                ) from exc

            with self._changed:
                self._frame = candidate_frame
                self._revision = candidate_revision
                self._state = candidate_state
                self._state_sse_payload = candidate_state_sse_payload
                self._frame_sse_payload = candidate_frame_sse_payload
                self._results[command_id] = (canonical_request, canonical_response)
                self._idempotency_payload_bytes = candidate_idempotency_bytes
                self._changed.notify_all()
            return _json_copy(response)

    def _capture_state(self, revision: Optional[int] = None) -> Dict[str, Any]:
        state = {
            "schema_version": STATE_SCHEMA,
            "service_instance_id": self.service_instance_id,
            "revision": self._revision if revision is None else revision,
            "execution_id": self.execution_id,
            "dataset": self.backend.dataset_state(),
            "pipeline": self.backend.pipeline_state(),
        }
        _validate_state_shape(state)
        if len(_canonical_json(state).encode("utf-8")) > MAX_STATE_BYTES:
            raise RuntimeError("ParaView state exceeds the service size limit")
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


def _state_sse_payload(revision: int, state: Mapping[str, Any]) -> bytes:
    """Build one immutable state event shared by all admitted subscribers."""
    return _sse_payload("state", revision, _canonical_json(state).encode("utf-8"))


def _frame_sse_payload(
    service_instance_id: str,
    revision: int,
    frame: bytes,
) -> bytes:
    """Encode one frame once per revision, never once per subscriber."""
    value = {
        "schema_version": FRAME_SCHEMA,
        "service_instance_id": service_instance_id,
        "revision": revision,
        "media_type": "image/png",
        "encoding": "base64",
        "data": base64.b64encode(frame).decode("ascii"),
    }
    return _sse_payload("frame", revision, _canonical_json(value).encode("utf-8"))


def _sse_payload(event_name: str, revision: int, data: bytes) -> bytes:
    return b"".join(
        (
            b"event: ",
            event_name.encode("ascii"),
            b"\n",
            b"id: ",
            str(revision).encode("ascii"),
            b"\n",
            b"data: ",
            data,
            b"\n\n",
        )
    )


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = HTTP_REQUEST_QUEUE_SIZE

    def __init__(
        self,
        address: Tuple[str, int],
        controller: ServiceStateController,
        bearer_token: str,
        *,
        max_connections: int = MAX_HTTP_CONNECTIONS,
        max_sse_subscribers: int = MAX_SSE_SUBSCRIBERS,
        header_timeout: float = HTTP_HEADER_TIMEOUT_SECONDS,
        body_timeout: float = HTTP_BODY_TIMEOUT_SECONDS,
        write_timeout: float = HTTP_WRITE_TIMEOUT_SECONDS,
        heartbeat_interval: float = SSE_HEARTBEAT_SECONDS,
    ) -> None:
        if not _BEARER_TOKEN.fullmatch(bearer_token):
            raise ValueError("bearer_token must be 64 lowercase hexadecimal characters")
        if (
            isinstance(max_connections, bool)
            or not isinstance(max_connections, int)
            or max_connections < 1
            or isinstance(max_sse_subscribers, bool)
            or not isinstance(max_sse_subscribers, int)
            or not 1 <= max_sse_subscribers <= max_connections
        ):
            raise ValueError("HTTP and SSE limits must be bounded positive integers")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
            for value in (
                header_timeout,
                body_timeout,
                write_timeout,
                heartbeat_interval,
            )
        ):
            raise ValueError("HTTP timeouts must be finite positive seconds")
        self.controller = controller
        self.bearer_token = bearer_token
        self.header_timeout = float(header_timeout)
        self.body_timeout = float(body_timeout)
        self.write_timeout = float(write_timeout)
        self.heartbeat_interval = float(heartbeat_interval)
        self._connection_slots = threading.BoundedSemaphore(max_connections)
        self._subscriber_slots = threading.BoundedSemaphore(max_sse_subscribers)
        self._resource_lock = threading.Lock()
        self._active_connections: set[socket.socket] = set()
        self._active_subscribers = 0
        self._closing = threading.Event()
        super().__init__(address, _Handler)

    @property
    def active_connection_count(self) -> int:
        """Return the exact admitted connection count for lifecycle checks."""
        with self._resource_lock:
            return len(self._active_connections)

    @property
    def active_subscriber_count(self) -> int:
        """Return the exact admitted SSE subscriber count."""
        with self._resource_lock:
            return self._active_subscribers

    @property
    def closing(self) -> bool:
        """Return whether bounded server shutdown has begun."""
        return self._closing.is_set()

    def process_request(
        self,
        request: Any,
        client_address: Any,
    ) -> None:
        """Admit before thread creation so idle unauthenticated peers are bounded."""
        if self.closing or not self._connection_slots.acquire(blocking=False):
            self._reject_over_capacity(request)
            return
        try:
            request.settimeout(self.header_timeout)
            with self._resource_lock:
                self._active_connections.add(request)
            super().process_request(request, client_address)
        except BaseException:
            self._release_connection(request)
            self.shutdown_request(request)
            raise

    def process_request_thread(
        self,
        request: Any,
        client_address: Any,
    ) -> None:
        """Release every admitted connection regardless of handler outcome."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_connection(request)

    def acquire_subscriber(self) -> bool:
        """Admit one SSE stream without blocking a request thread."""
        if self.closing or not self._subscriber_slots.acquire(blocking=False):
            return False
        with self._resource_lock:
            self._active_subscribers += 1
        return True

    def release_subscriber(self) -> None:
        """Release one previously admitted SSE stream exactly once."""
        with self._resource_lock:
            if self._active_subscribers < 1:
                raise RuntimeError("SSE subscriber accounting underflow")
            self._active_subscribers -= 1
        self._subscriber_slots.release()

    def shutdown(self) -> None:
        """Stop accepting work and interrupt every connected request."""
        self._begin_shutdown()
        super().shutdown()

    def server_close(self) -> None:
        """Close active sockets before retiring the listening socket."""
        self._begin_shutdown()
        super().server_close()

    def _begin_shutdown(self) -> None:
        self._closing.set()
        with self._resource_lock:
            active = tuple(self._active_connections)
        for connection in active:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        self.controller.wake_waiters()

    def _release_connection(self, request: socket.socket) -> None:
        with self._resource_lock:
            admitted = request in self._active_connections
            self._active_connections.discard(request)
        if admitted:
            self._connection_slots.release()

    def _reject_over_capacity(self, request: socket.socket) -> None:
        payload = _canonical_json(
            {
                "schema_version": COMMAND_ERROR_SCHEMA,
                "error": {
                    "code": "connection_limit",
                    "message": "the ParaView service connection limit was reached",
                    "details": {},
                },
            }
        ).encode("utf-8")
        response = b"".join(
            (
                b"HTTP/1.1 503 Service Unavailable\r\n",
                b"Content-Type: application/json\r\n",
                b"Cache-Control: no-store\r\n",
                b"Connection: close\r\n",
                b"Content-Length: ",
                str(len(payload)).encode("ascii"),
                b"\r\n\r\n",
                payload,
            )
        )
        try:
            request.settimeout(self.write_timeout)
            request.sendall(response)
        except OSError:
            pass
        finally:
            self.shutdown_request(request)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "JarvisParaView/2"

    @property
    def controller(self) -> ServiceStateController:
        return self.runtime_server.controller

    @property
    def bearer_token(self) -> str:
        return self.runtime_server.bearer_token

    @property
    def runtime_server(self) -> _Server:
        """Return the bounded server that owns this request."""
        return cast(_Server, self.server)

    def setup(self) -> None:
        """Install a reader that enforces absolute header and body deadlines."""
        super().setup()
        original_reader = self.rfile
        self._deadline_reader = _DeadlineSocketReader(self.connection)
        self.rfile = io.BufferedReader(
            self._deadline_reader,
            buffer_size=io.DEFAULT_BUFFER_SIZE,
        )
        original_reader.close()

    def handle_one_request(self) -> None:
        """Bound every request-line/header phase, including trickle clients."""
        self._deadline_reader.start_phase(self.runtime_server.header_timeout)
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            self.close_connection = True
        finally:
            self._deadline_reader.clear_phase()

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        """Serve bounded health, state, frame, and event streams."""
        if not self._authorized():
            self._unauthorized()
            return
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
        if not self._authorized():
            self._unauthorized()
            return
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
        self._deadline_reader.start_phase(self.runtime_server.body_timeout)
        try:
            payload = self.rfile.read(length)
        except TimeoutError as exc:
            self.close_connection = True
            raise CommandError(
                "request_timeout",
                "command body was not received before the body timeout",
                status=HTTPStatus.REQUEST_TIMEOUT,
            ) from exc
        finally:
            self._deadline_reader.clear_phase()
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

    def _authorized(self) -> bool:
        value = self.headers.get("Authorization")
        if not isinstance(value, str) or not value.startswith("Bearer "):
            return False
        token = value.removeprefix("Bearer ")
        return bool(_BEARER_TOKEN.fullmatch(token)) and hmac.compare_digest(
            token,
            self.bearer_token,
        )

    def _unauthorized(self) -> None:
        self.close_connection = True
        payload = _canonical_json(
            {
                "schema_version": COMMAND_ERROR_SCHEMA,
                "error": {
                    "code": "unauthorized",
                    "message": "a valid execution-owned bearer capability is required",
                    "details": {},
                },
            }
        ).encode("utf-8")
        self._prepare_write()
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def _serve_sse(self, *, frames: bool) -> None:
        server = self.runtime_server
        if not server.acquire_subscriber():
            self.close_connection = True
            self._error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "subscriber_limit",
                "the ParaView service SSE subscriber limit was reached",
            )
            return
        revision = 0
        try:
            self._prepare_write()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            while not server.closing:
                current, state_payload, frame_payload = (
                    self.controller.wait_for_sse_change(
                        revision,
                        timeout=server.heartbeat_interval,
                    )
                )
                if server.closing:
                    return
                self._prepare_write()
                if current == revision:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(frame_payload if frames else state_payload)
                self.wfile.flush()
                revision = current
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return
        finally:
            self.close_connection = True
            server.release_subscriber()

    def _prepare_write(self) -> None:
        """Apply the bounded socket write timeout before response bytes."""
        self.connection.settimeout(self.runtime_server.write_timeout)

    def _json(self, status: HTTPStatus, value: Mapping[str, Any]) -> None:
        payload = _canonical_json(value).encode("utf-8")
        if len(payload) > MAX_RESPONSE_BYTES:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            payload = _canonical_json(
                {
                    "schema_version": COMMAND_ERROR_SCHEMA,
                    "error": {
                        "code": "response_too_large",
                        "message": "the ParaView service response exceeded its size limit",
                        "details": {},
                    },
                }
            ).encode("utf-8")
        self._prepare_write()
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
    bearer_token: str,
    *,
    max_connections: int = MAX_HTTP_CONNECTIONS,
    max_sse_subscribers: int = MAX_SSE_SUBSCRIBERS,
    header_timeout: float = HTTP_HEADER_TIMEOUT_SECONDS,
    body_timeout: float = HTTP_BODY_TIMEOUT_SECONDS,
    write_timeout: float = HTTP_WRITE_TIMEOUT_SECONDS,
    heartbeat_interval: float = SSE_HEARTBEAT_SECONDS,
) -> _Server:
    """Create a bounded service; callers control lifecycle and signal handling."""
    return _Server(
        (host, port),
        controller,
        bearer_token,
        max_connections=max_connections,
        max_sse_subscribers=max_sse_subscribers,
        header_timeout=header_timeout,
        body_timeout=body_timeout,
        write_timeout=write_timeout,
        heartbeat_interval=heartbeat_interval,
    )


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
            "command fields do not match the ParaView command schema",
        )
    command_schema = value.get("schema_version")
    if command_schema != COMMAND_SCHEMA:
        if command_schema == "jarvis.paraview.command.v1":
            raise CommandError(
                "unsupported_schema",
                "jarvis.paraview.command.v1 is not compatible with the v2 "
                "state/result contract; migrate the command to "
                "jarvis.paraview.command.v2",
            )
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
    if (
        value.get("schema_version") != STATE_SCHEMA
        or not _nonempty_text(value.get("service_instance_id"))
        or not _positive_int(value.get("revision"))
        or not _nonempty_text(value.get("execution_id"))
    ):
        raise RuntimeError("ParaView state identity is invalid")
    dataset = value.get("dataset")
    pipeline = value.get("pipeline")
    if not isinstance(dataset, dict) or set(dataset) != {"descriptor", "discovery"}:
        raise RuntimeError("ParaView dataset state is incomplete")
    if not isinstance(pipeline, dict) or set(pipeline) != {
        "timestep",
        "nodes",
        "representations",
        "measurements",
        "camera",
        "selection",
        "artifacts",
    }:
        raise RuntimeError("ParaView pipeline state is incomplete")
    timestep_values = _validate_dataset_discovery(dataset.get("discovery"))
    _validate_timestep(pipeline.get("timestep"), timestep_values)
    nodes = _validate_nodes(pipeline.get("nodes"))
    representations = _validate_representations(
        pipeline.get("representations"),
        nodes,
    )
    measurements = _validate_measurements(
        pipeline.get("measurements"),
        nodes,
        timestep_values,
    )
    _validate_measurement_references(representations, measurements)
    _validate_camera(pipeline.get("camera"))
    _validate_selection(pipeline.get("selection"), representations)
    _validate_artifacts(pipeline.get("artifacts"))


def _validate_dataset_discovery(value: object) -> list[float]:
    """Validate immutable discovery and return its exact time axis."""
    if not isinstance(value, dict) or set(value) != {
        "arrays",
        "bounds",
        "timestep_values",
    }:
        raise RuntimeError("ParaView dataset discovery shape is invalid")
    arrays = value["arrays"]
    if not isinstance(arrays, list) or len(arrays) > MAX_SCENE_ARRAYS:
        raise RuntimeError("ParaView dataset discovery arrays are invalid")
    identities: set[Tuple[object, object]] = set()
    for array in arrays:
        _validate_field_identity(array)
        identity = (array["association"], array["name"])
        if identity in identities:
            raise RuntimeError("ParaView dataset discovery arrays are duplicated")
        identities.add(identity)
    bounds = value["bounds"]
    if bounds is not None and (
        not _finite_vector(bounds, 6)
        or any(bounds[index] > bounds[index + 1] for index in (0, 2, 4))
    ):
        raise RuntimeError("ParaView dataset discovery bounds are invalid")
    timesteps = value["timestep_values"]
    if (
        not isinstance(timesteps, list)
        or len(timesteps) > 512
        or not all(_finite_number_value(item) for item in timesteps)
    ):
        raise RuntimeError("ParaView dataset discovery timesteps are invalid")
    return [float(item) for item in timesteps]


def _validate_timestep(value: object, timestep_values: Sequence[float]) -> None:
    if not isinstance(value, dict) or set(value) != {"index", "value", "count"}:
        raise RuntimeError("ParaView timestep state is invalid")
    if (
        not _nonnegative_int(value["index"])
        or not _nonnegative_int(value["count"])
        or (value["value"] is not None and not _finite_number_value(value["value"]))
        or value["count"] > 0
        and value["index"] >= value["count"]
        or value["count"] == 0
        and value["index"] != 0
        or value["count"] == 0
        and value["value"] is not None
    ):
        raise RuntimeError("ParaView timestep values are invalid")
    if timestep_values:
        if (
            value["count"] != len(timestep_values)
            or value["value"] != timestep_values[value["index"]]
        ):
            raise RuntimeError(
                "ParaView timestep state disagrees with dataset discovery"
            )
    elif value != {"index": 0, "value": None, "count": 0}:
        raise RuntimeError("ParaView static timestep state is inconsistent")


def _validate_nodes(value: object) -> Dict[str, Mapping[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_SCENE_NODES:
        raise RuntimeError("ParaView scene nodes are invalid")
    nodes: Dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(value):
        if not isinstance(raw, dict) or set(raw) != {
            "node_id",
            "kind",
            "input_node_ids",
            "filter",
            "output",
        }:
            raise RuntimeError("ParaView scene node shape is invalid")
        node_id = raw["node_id"]
        kind = raw["kind"]
        inputs = raw["input_node_ids"]
        if (
            not _nonempty_text(node_id)
            or node_id in nodes
            or kind not in {"reader", "slice", "clip", "threshold", "contour"}
            or not isinstance(inputs, list)
            or any(not _nonempty_text(item) for item in inputs)
            or len(set(inputs)) != len(inputs)
        ):
            raise RuntimeError("ParaView scene node values are invalid")
        if index == 0:
            if (
                node_id != "node_root"
                or kind != "reader"
                or inputs != []
                or raw["filter"] is not None
            ):
                raise RuntimeError("ParaView root node is invalid")
        else:
            if len(inputs) != 1 or inputs[0] not in nodes:
                raise RuntimeError("ParaView node dependencies are invalid")
            _validate_filter_record(raw["filter"], kind)
        _validate_output_summary(raw["output"])
        if index > 0:
            parent = nodes[cast(str, inputs[0])]
            filter_record = cast(Mapping[str, Any], raw["filter"])
            parameters = cast(Mapping[str, Any], filter_record["parameters"])
            if kind in {"threshold", "contour"}:
                matches = [
                    array
                    for array in parent["output"]["arrays"]
                    if array["name"] == parameters["name"]
                    and array["association"] == parameters["association"]
                ]
                if len(matches) != 1 or matches[0]["components"] != 1:
                    raise RuntimeError(
                        "ParaView filter field is not a scalar input-node array"
                    )
            expected_topology = (
                "surface"
                if kind in {"slice", "contour"}
                else parent["output"]["topology"]
            )
            if raw["output"]["topology"] != expected_topology:
                raise RuntimeError("ParaView filter output topology is inconsistent")
        nodes[cast(str, node_id)] = raw
    return nodes


def _validate_filter_record(value: object, kind: object) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {"type", "parameters"}
        or value["type"] != kind
        or not isinstance(value["parameters"], dict)
    ):
        raise RuntimeError("ParaView filter record is invalid")
    parameters = value["parameters"]
    if kind in {"slice", "clip"}:
        if (
            set(parameters) != {"origin", "normal"}
            or not _finite_vector(parameters["origin"], 3)
            or not _finite_vector(parameters["normal"], 3)
            or all(float(item) == 0.0 for item in parameters["normal"])
        ):
            raise RuntimeError("ParaView plane filter record is invalid")
        return
    if kind == "threshold":
        if (
            set(parameters) != {"name", "association", "lower", "upper"}
            or not _nonempty_text(parameters["name"])
            or parameters["association"] not in {"point", "cell"}
            or not _finite_number_value(parameters["lower"])
            or not _finite_number_value(parameters["upper"])
            or parameters["lower"] > parameters["upper"]
        ):
            raise RuntimeError("ParaView threshold record is invalid")
        return
    if kind == "contour":
        values = parameters.get("isovalues")
        if (
            set(parameters) != {"name", "association", "isovalues"}
            or not _nonempty_text(parameters["name"])
            or parameters["association"] != "point"
            or not isinstance(values, list)
            or not 1 <= len(values) <= 64
            or not all(_finite_number_value(item) for item in values)
            or len(set(values)) != len(values)
        ):
            raise RuntimeError("ParaView contour record is invalid")
        return
    raise RuntimeError("ParaView filter record kind is invalid")


def _validate_output_summary(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {
        "topology",
        "raw_data_type",
        "bounds",
        "point_count",
        "cell_count",
        "arrays",
    }:
        raise RuntimeError("ParaView node output summary shape is invalid")
    if (
        value["topology"] not in {"points", "surface", "volume", "composite", "unknown"}
        or not (
            value["raw_data_type"] is None or _nonempty_text(value["raw_data_type"])
        )
        or not _nullable_nonnegative_int(value["point_count"])
        or not _nullable_nonnegative_int(value["cell_count"])
    ):
        raise RuntimeError("ParaView node output summary values are invalid")
    bounds = value["bounds"]
    if bounds is not None and (
        not _finite_vector(bounds, 6)
        or any(bounds[index] > bounds[index + 1] for index in (0, 2, 4))
    ):
        raise RuntimeError("ParaView node output bounds are invalid")
    arrays = value["arrays"]
    if not isinstance(arrays, list) or len(arrays) > MAX_SCENE_ARRAYS:
        raise RuntimeError("ParaView node output arrays are invalid")
    identities: set[Tuple[object, object]] = set()
    for array in arrays:
        _validate_field_identity(array)
        identity = (array["association"], array["name"])
        if identity in identities:
            raise RuntimeError("ParaView node output arrays are duplicated")
        identities.add(identity)


def _validate_field_identity(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {
        "name",
        "association",
        "components",
        "units",
    }:
        raise RuntimeError("ParaView field identity shape is invalid")
    if (
        not _nonempty_text(value["name"])
        or value["association"] not in {"point", "cell"}
        or not _positive_int(value["components"])
        or value["components"] > 256
        or not (value["units"] is None or _nonempty_text(value["units"]))
    ):
        raise RuntimeError("ParaView field identity values are invalid")


def _validate_representations(
    value: object,
    nodes: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Mapping[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= (
        MAX_SCENE_REPRESENTATIONS
    ):
        raise RuntimeError("ParaView representations are invalid")
    representations: Dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(value):
        if not isinstance(raw, dict) or set(raw) != {
            "representation_id",
            "node_id",
            "type",
            "visible",
            "opacity",
            "point_size_px",
            "color",
        }:
            raise RuntimeError("ParaView representation shape is invalid")
        representation_id = raw["representation_id"]
        node_id = raw["node_id"]
        representation_type = raw["type"]
        point_size = raw["point_size_px"]
        if (
            not _nonempty_text(representation_id)
            or representation_id in representations
            or node_id not in nodes
            or representation_type not in {"surface", "points"}
            or not isinstance(raw["visible"], bool)
            or not _finite_number_value(raw["opacity"])
            or not 0 <= raw["opacity"] <= 1
            or representation_type == "surface"
            and point_size is not None
            or representation_type == "points"
            and (
                isinstance(point_size, bool)
                or not isinstance(point_size, (int, float))
                or not math.isfinite(float(point_size))
                or not 1 <= float(point_size) <= 64
            )
        ):
            raise RuntimeError("ParaView representation values are invalid")
        if index == 0 and (representation_id != "rep_root" or node_id != "node_root"):
            raise RuntimeError("ParaView root representation is invalid")
        _validate_representation_color(
            raw["color"],
            nodes[cast(str, node_id)],
            visible=raw["visible"],
        )
        representations[cast(str, representation_id)] = raw
    return representations


def _validate_representation_color(
    value: object,
    node: Mapping[str, Any],
    *,
    visible: bool,
) -> None:
    if not isinstance(value, dict):
        raise RuntimeError("ParaView representation color is invalid")
    if value.get("mode") == "solid":
        if (
            set(value) != {"mode", "rgb"}
            or not _finite_vector(value["rgb"], 3)
            or any(not 0 <= item <= 1 for item in value["rgb"])
        ):
            raise RuntimeError("ParaView solid color is invalid")
        return
    if (
        set(value)
        != {
            "mode",
            "field",
            "observation",
            "preset",
            "invert",
            "scale",
            "range_policy",
            "transfer_range",
            "scalar_bar",
            "supported_scales",
        }
        or value.get("mode") != "field"
    ):
        raise RuntimeError("ParaView field color shape is invalid")
    _validate_field_identity(value["field"])
    _validate_node_field_identity(value["field"], node)
    observation = value["observation"]
    if (
        not isinstance(observation, dict)
        or set(observation) != {"observed_range", "tuple_count", "value_mode"}
        or not _finite_pair(observation["observed_range"], increasing=False)
        or not _nullable_nonnegative_int(observation["tuple_count"])
        or observation["value_mode"]
        != ("scalar" if value["field"]["components"] == 1 else "magnitude")
    ):
        raise RuntimeError("ParaView field observation is invalid")
    if (
        not _nonempty_text(value["preset"])
        or not isinstance(value["invert"], bool)
        or value["scale"] not in ({"mode": "linear"}, {"mode": "log"})
        or not _finite_pair(value["transfer_range"], increasing=False)
        or not isinstance(value["scalar_bar"], dict)
        or set(value["scalar_bar"]) != {"visible", "embedded_in_frame"}
        or not isinstance(value["scalar_bar"]["visible"], bool)
        or value["scalar_bar"]["embedded_in_frame"] != value["scalar_bar"]["visible"]
        or not visible
        and (value["scalar_bar"]["visible"] or value["scalar_bar"]["embedded_in_frame"])
        or value["supported_scales"] != ["linear", "log"]
    ):
        raise RuntimeError("ParaView field color values are invalid")
    _validate_range_policy(value["range_policy"], value["transfer_range"], observation)
    if value["scale"]["mode"] == "log" and not (
        0 < value["transfer_range"][0] < value["transfer_range"][1]
    ):
        raise RuntimeError("ParaView log color range is invalid")


def _validate_range_policy(
    policy: object,
    transfer_range: object,
    observation: Mapping[str, Any],
) -> None:
    if not isinstance(policy, dict):
        raise RuntimeError("ParaView range policy is invalid")
    mode = policy.get("mode")
    if mode == "full":
        if (
            policy != {"mode": "full", "timestep_behavior": "recompute"}
            or transfer_range != observation["observed_range"]
        ):
            raise RuntimeError("ParaView full range policy is invalid")
        return
    if mode == "fixed":
        if (
            set(policy) != {"mode", "range", "timestep_behavior"}
            or policy["timestep_behavior"] != "freeze"
            or not _finite_pair(policy["range"], increasing=True)
            or transfer_range != policy["range"]
        ):
            raise RuntimeError("ParaView fixed range policy is invalid")
        return
    if mode == "measurement_percentile":
        if (
            set(policy)
            != {
                "mode",
                "measurement_id",
                "lower_percentile",
                "upper_percentile",
                "timestep_behavior",
            }
            or not _nonempty_text(policy["measurement_id"])
            or not _finite_number_value(policy["lower_percentile"])
            or not _finite_number_value(policy["upper_percentile"])
            or not 0 <= policy["lower_percentile"] < policy["upper_percentile"] <= 100
            or policy["timestep_behavior"] != "freeze"
            or not _finite_pair(transfer_range, increasing=True)
        ):
            raise RuntimeError("ParaView measurement range policy is invalid")
        return
    raise RuntimeError("ParaView range policy mode is invalid")


def _validate_measurements(
    value: object,
    nodes: Mapping[str, Mapping[str, Any]],
    timestep_values: Sequence[float],
) -> Dict[str, Mapping[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_SCENE_MEASUREMENTS:
        raise RuntimeError("ParaView measurements are invalid")
    measurements: Dict[str, Mapping[str, Any]] = {}
    stored_sample_count = 0
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {
            "measurement_id",
            "node_id",
            "field",
            "value_mode",
            "timestep_indices",
            "samples",
            "aggregate",
        }:
            raise RuntimeError("ParaView measurement shape is invalid")
        measurement_id = raw["measurement_id"]
        indices = raw["timestep_indices"]
        samples = raw["samples"]
        if (
            not _nonempty_text(measurement_id)
            or measurement_id in measurements
            or raw["node_id"] not in nodes
            or not isinstance(indices, list)
            or not 1 <= len(indices) <= MAX_MEASUREMENT_TIMESTEPS
            or not all(_nonnegative_int(index) for index in indices)
            or len(set(indices)) != len(indices)
            or timestep_values
            and any(index >= len(timestep_values) for index in indices)
            or not timestep_values
            and indices != [0]
            or not isinstance(samples, list)
            or len(samples) != len(indices)
        ):
            raise RuntimeError("ParaView measurement values are invalid")
        _validate_field_identity(raw["field"])
        if raw["value_mode"] != (
            "scalar" if raw["field"]["components"] == 1 else "magnitude"
        ):
            raise RuntimeError("ParaView measurement value mode is invalid")
        _validate_node_field_identity(raw["field"], nodes[cast(str, raw["node_id"])])
        stored_sample_count += len(samples)
        if stored_sample_count > MAX_STORED_MEASUREMENT_SAMPLES:
            raise RuntimeError(
                "ParaView cumulative measurement samples exceed the state limit"
            )
        for expected_index, sample in zip(indices, samples):
            expected_value = (
                timestep_values[expected_index] if timestep_values else None
            )
            _validate_measurement_sample(sample, expected_index, expected_value)
        _validate_measurement_aggregate(raw["aggregate"], samples)
        measurements[cast(str, measurement_id)] = raw
    return measurements


def _validate_node_field_identity(
    field: object,
    node: Mapping[str, Any],
) -> None:
    """Require an authoritative field to match one exact node output array."""
    output = node.get("output")
    arrays = output.get("arrays") if isinstance(output, dict) else None
    if (
        not isinstance(field, dict)
        or not isinstance(arrays, list)
        or field not in arrays
    ):
        raise RuntimeError("ParaView field is not an exact node output array")


def _validate_measurement_sample(
    value: object,
    expected_index: object,
    expected_value: object,
) -> None:
    if not isinstance(value, dict) or set(value) != {
        "timestep_index",
        "timestep_value",
        "observed_range",
        "tuple_count",
        "distribution",
    }:
        raise RuntimeError("ParaView measurement sample shape is invalid")
    if (
        value["timestep_index"] != expected_index
        or value["timestep_value"] != expected_value
        or not (
            value["timestep_value"] is None
            or _finite_number_value(value["timestep_value"])
        )
        or not _finite_pair(value["observed_range"], increasing=False)
        or not _nullable_nonnegative_int(value["tuple_count"])
    ):
        raise RuntimeError("ParaView measurement sample values are invalid")
    _validate_distribution(
        value["distribution"],
        observed_range=value["observed_range"],
        methods={"paraview.histogram-filter"},
        estimators={"uniform-within-bin"},
    )


def _validate_measurement_aggregate(
    value: object,
    samples: Sequence[Mapping[str, Any]],
) -> None:
    if not isinstance(value, dict) or set(value) != {
        "observed_range",
        "tuple_count",
        "distribution",
    }:
        raise RuntimeError("ParaView measurement aggregate shape is invalid")
    expected_range = [
        min(float(sample["observed_range"][0]) for sample in samples),
        max(float(sample["observed_range"][1]) for sample in samples),
    ]
    counts = [sample["tuple_count"] for sample in samples]
    expected_count = (
        sum(cast(Sequence[int], counts))
        if all(_nonnegative_int(count) for count in counts)
        else None
    )
    if (
        value["observed_range"] != expected_range
        or value["tuple_count"] != expected_count
    ):
        raise RuntimeError("ParaView measurement aggregate values are invalid")
    _validate_distribution(
        value["distribution"],
        observed_range=value["observed_range"],
        methods={"aggregate-of-paraview-histogram-filter"},
        estimators={"uniform-within-source-and-aggregate-bins"},
    )
    sample_available = all(
        sample["distribution"].get("status") == "available" for sample in samples
    )
    if sample_available != (value["distribution"].get("status") == "available"):
        raise RuntimeError("ParaView aggregate distribution availability is invalid")


def _validate_distribution(
    value: object,
    *,
    observed_range: Sequence[float],
    methods: set[str],
    estimators: set[str],
) -> None:
    if not isinstance(value, dict):
        raise RuntimeError("ParaView distribution is invalid")
    if value.get("status") == "unavailable":
        if set(value) != {"status", "reason"} or not _nonempty_text(
            value.get("reason")
        ):
            raise RuntimeError("ParaView unavailable distribution is invalid")
        return
    expected = {
        "status",
        "method",
        "bin_count",
        "finite_count",
        "nonfinite_count",
        "estimator",
        "histogram",
        "percentiles",
        "log_scale_eligible",
    }
    if set(value) != expected or value.get("status") != "available":
        raise RuntimeError("ParaView available distribution shape is invalid")
    bin_count = value["bin_count"]
    finite_count = value["finite_count"]
    if (
        value["method"] not in methods
        or value["estimator"] not in estimators
        or not _positive_int(bin_count)
        or bin_count > 128
        or not _nonnegative_finite_number(finite_count)
        or finite_count <= 0
        or not _nullable_nonnegative_int(value["nonfinite_count"])
        or not isinstance(value["log_scale_eligible"], bool)
        or value["log_scale_eligible"]
        != (observed_range[0] > 0 and observed_range[0] < observed_range[1])
    ):
        raise RuntimeError("ParaView available distribution values are invalid")
    histogram = value["histogram"]
    if not isinstance(histogram, dict) or set(histogram) != {"bin_edges", "counts"}:
        raise RuntimeError("ParaView histogram shape is invalid")
    edges = histogram["bin_edges"]
    counts = histogram["counts"]
    if (
        not isinstance(edges, list)
        or len(edges) != bin_count + 1
        or not all(_finite_number_value(item) for item in edges)
        or any(left > right for left, right in zip(edges, edges[1:]))
        or edges[0] != observed_range[0]
        or edges[-1] != observed_range[1]
        or not isinstance(counts, list)
        or len(counts) != bin_count
        or not all(_nonnegative_finite_number(item) for item in counts)
        or not math.isclose(
            sum(float(item) for item in counts),
            float(finite_count),
            rel_tol=1e-9,
            abs_tol=1e-9,
        )
    ):
        raise RuntimeError("ParaView histogram values are invalid")
    percentiles = value["percentiles"]
    expected_percentiles = [0.0, 1.0, 5.0, 50.0, 95.0, 99.0, 100.0]
    if (
        not isinstance(percentiles, list)
        or len(percentiles) != len(expected_percentiles)
        or any(
            not isinstance(item, dict)
            or set(item) != {"percentile", "value"}
            or item["percentile"] != expected
            or not _finite_number_value(item["value"])
            or not observed_range[0] <= item["value"] <= observed_range[1]
            for item, expected in zip(percentiles, expected_percentiles)
        )
    ):
        raise RuntimeError("ParaView distribution percentiles are invalid")


def _validate_measurement_references(
    representations: Mapping[str, Mapping[str, Any]],
    measurements: Mapping[str, Mapping[str, Any]],
) -> None:
    for representation in representations.values():
        color = representation["color"]
        if color.get("mode") != "field":
            continue
        policy = color["range_policy"]
        measurement_id = policy.get("measurement_id")
        if measurement_id is None:
            continue
        measurement = measurements.get(measurement_id)
        if measurement is None:
            raise RuntimeError("ParaView range policy measurement is missing")
        if (
            measurement["node_id"] != representation["node_id"]
            or measurement["field"] != color["field"]
        ):
            raise RuntimeError("ParaView range measurement identity is invalid")


def _validate_camera(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {
        "position",
        "focal_point",
        "view_up",
        "parallel_scale",
        "projection",
        "view_angle",
    }:
        raise RuntimeError("ParaView camera state is invalid")
    if (
        not _finite_vector(value["position"], 3)
        or not _finite_vector(value["focal_point"], 3)
        or not _finite_vector(value["view_up"], 3)
        or not _finite_number_value(value["parallel_scale"])
        or value["parallel_scale"] <= 0
        or value["projection"] not in {"perspective", "parallel"}
        or not _finite_number_value(value["view_angle"])
        or not 0 < value["view_angle"] < 180
    ):
        raise RuntimeError("ParaView camera values are invalid")
    direction = [
        float(value["focal_point"][index]) - float(value["position"][index])
        for index in range(3)
    ]
    direction_length = math.sqrt(sum(component * component for component in direction))
    view_up = [float(component) for component in value["view_up"]]
    up_length = math.sqrt(sum(component * component for component in view_up))
    cross = [
        direction[1] * view_up[2] - direction[2] * view_up[1],
        direction[2] * view_up[0] - direction[0] * view_up[2],
        direction[0] * view_up[1] - direction[1] * view_up[0],
    ]
    if (
        direction_length <= 1e-12
        or up_length <= 1e-12
        or math.sqrt(sum(component * component for component in cross))
        <= 1e-12 * direction_length * up_length
    ):
        raise RuntimeError("ParaView camera geometry is invalid")


def _validate_selection(
    value: object,
    representations: Mapping[str, Mapping[str, Any]],
) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise RuntimeError("ParaView selection state is invalid")
    representation_id = value.get("representation_id")
    representation = (
        representations.get(representation_id)
        if isinstance(representation_id, str)
        else None
    )
    if (
        representation is None
        or value.get("node_id") != representation["node_id"]
        or value.get("association") not in {"point", "cell"}
    ):
        raise RuntimeError("ParaView selection target is invalid")
    common = {
        "selector",
        "representation_id",
        "node_id",
        "status",
        "association",
        "selected_count",
        "returned_count",
        "truncated",
        "ids",
        "reason",
    }
    if value.get("selector") == "element":
        if set(value) != common | {"index", "element_count"}:
            raise RuntimeError("ParaView element selection shape is invalid")
        if (
            value["status"] != "selected"
            or not _nonnegative_int(value["index"])
            or not _positive_int(value["element_count"])
            or value["index"] >= value["element_count"]
            or value["selected_count"] != 1
            or value["returned_count"] != 1
            or value["truncated"] is not False
            or value["reason"] is not None
        ):
            raise RuntimeError("ParaView element selection values are invalid")
    elif value.get("selector") == "viewport":
        if set(value) != common | {"viewport", "pixel_rectangle"}:
            raise RuntimeError("ParaView viewport selection shape is invalid")
        if (
            value["status"] not in {"selected", "empty", "unsupported"}
            or not _normalized_viewport(value["viewport"])
            or not isinstance(value["pixel_rectangle"], list)
            or len(value["pixel_rectangle"]) != 4
            or not all(_nonnegative_int(item) for item in value["pixel_rectangle"])
            or not _nullable_nonnegative_int(value["selected_count"])
            or not _nonnegative_int(value["returned_count"])
            or not isinstance(value["truncated"], bool)
        ):
            raise RuntimeError("ParaView viewport selection values are invalid")
        if value["status"] == "unsupported" and not (
            value["selected_count"] is None
            and value["returned_count"] == 0
            and value["truncated"] is False
            and _nonempty_text(value["reason"])
        ):
            raise RuntimeError("ParaView unsupported selection is invalid")
        if value["status"] == "empty" and not (
            value["selected_count"] == 0
            and value["returned_count"] == 0
            and value["truncated"] is False
            and value["reason"] is None
        ):
            raise RuntimeError("ParaView empty selection is invalid")
        if value["status"] == "selected" and not (
            _positive_int(value["selected_count"])
            and value["returned_count"] > 0
            and value["reason"] is None
        ):
            raise RuntimeError("ParaView selected viewport is invalid")
    else:
        raise RuntimeError("ParaView selection selector is invalid")
    ids = value["ids"]
    if (
        not isinstance(ids, list)
        or len(ids) != value["returned_count"]
        or len(ids) > 256
    ):
        raise RuntimeError("ParaView selection IDs are invalid")
    for item in ids:
        if (
            not isinstance(item, dict)
            or set(item)
            not in (
                {"process_id", "element_id"},
                {"block_index", "process_id", "element_id"},
                {"level", "hierarchy_index", "element_id"},
            )
            or not all(_nonnegative_int(number) for number in item.values())
        ):
            raise RuntimeError("ParaView selection ID shape is invalid")


def _validate_artifacts(value: object) -> None:
    if not isinstance(value, list) or len(value) > 128:
        raise RuntimeError("ParaView artifacts are invalid")
    seen: set[str] = set()
    for artifact in value:
        if not isinstance(artifact, dict) or not _nonempty_text(
            artifact.get("artifact_id")
        ):
            raise RuntimeError("ParaView artifact record is invalid")
        artifact_id = artifact["artifact_id"]
        metadata = artifact.get("metadata")
        if artifact_id in seen or not isinstance(metadata, dict):
            raise RuntimeError("ParaView artifact identity is invalid")
        representation_ids = metadata.get("representation_ids")
        scene_digest = metadata.get("scene_digest")
        if (
            not isinstance(representation_ids, list)
            or not representation_ids
            or representation_ids != sorted(representation_ids)
            or len(set(representation_ids)) != len(representation_ids)
            or not all(_nonempty_text(item) for item in representation_ids)
            or not isinstance(scene_digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", scene_digest) is None
        ):
            raise RuntimeError("ParaView artifact scene provenance is invalid")
        seen.add(cast(str, artifact_id))


def _normalized_viewport(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"x0", "y0", "x1", "y1"}
        and all(_finite_number_value(item) for item in value.values())
        and all(0 <= item <= 1 for item in value.values())
        and value["x0"] < value["x1"]
        and value["y0"] < value["y1"]
    )


def _finite_vector(value: object, length: int) -> bool:
    return (
        isinstance(value, list)
        and len(value) == length
        and all(_finite_number_value(item) for item in value)
    )


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and len(value.encode("utf-8")) <= 1024


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _nullable_nonnegative_int(value: object) -> bool:
    return value is None or _nonnegative_int(value)


def _finite_pair(value: object, *, increasing: bool) -> bool:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(_finite_number_value(item) for item in value)
    ):
        return False
    return value[0] < value[1] if increasing else value[0] <= value[1]


def _finite_number_value(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _nonnegative_finite_number(value: object) -> bool:
    return _finite_number_value(value) and float(cast(float, value)) >= 0


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
