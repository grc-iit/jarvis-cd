"""Supervise a pvpython service and report real lifecycle to JARVIS."""

from __future__ import annotations

import argparse
import json
import shlex
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO, Optional, Sequence

from jarvis_cd.service_runtime import (
    DatasetDescriptor,
    ServiceLifecycle,
    ServiceRuntimeAuthority,
    ServiceRuntimeReporter,
)

HEALTH_PROBE_INTERVAL_SECONDS = 2.0
HEALTH_FAILURE_THRESHOLD = 3
TERMINATION_GRACE_SECONDS = 10.0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Launch pvpython, health-probe it, and persist lifecycle revisions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-script", required=True)
    parser.add_argument("--descriptor", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--pvpython-bin", required=True)
    parser.add_argument("--pvpython-options", default="")
    parser.add_argument("--bind-host", required=True)
    parser.add_argument("--advertise-host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--startup-timeout", type=float, required=True)
    parser.add_argument("--service-instance-id", required=True)
    parser.add_argument("--authorization-file", required=True)
    parser.add_argument("--initial-scene")
    args = parser.parse_args(argv)

    if not 1 <= args.startup_timeout <= 600:
        raise ValueError("startup timeout must be between 1 and 600 seconds")
    if args.bind_host != "127.0.0.1" or args.advertise_host != "127.0.0.1":
        raise ValueError(
            "ParaView service supervisor requires loopback bind and advertisement"
        )
    descriptor = DatasetDescriptor.from_json(
        Path(args.descriptor).read_text(encoding="utf-8")
    )
    authorization_path = Path(args.authorization_file)
    authorization_token = _read_authorization_token(authorization_path)
    port = args.port or _available_port(args.bind_host)
    if not 1 <= port <= 65535:
        raise ValueError("service port must be zero or between 1 and 65535")
    reporter = ServiceRuntimeReporter.from_environment(
        service_instance_id=args.service_instance_id,
        host=args.advertise_host,
        port=port,
        dataset_descriptor=descriptor,
        authority=ServiceRuntimeAuthority(
            scheme="bearer",
            token=authorization_token,
        ),
    )
    command = [
        args.pvpython_bin,
        *shlex.split(args.pvpython_options),
        args.service_script,
        "--descriptor",
        args.descriptor,
        "--output-dir",
        args.output_dir,
        "--bind-host",
        args.bind_host,
        "--port",
        str(port),
        "--execution-id",
        reporter.execution_id,
        "--service-instance-id",
        args.service_instance_id,
        "--authorization-file",
        str(authorization_path),
    ]
    if args.initial_scene:
        command.extend(["--initial-scene", args.initial_scene])
    stopping = threading.Event()
    lifecycle: Optional[ServiceLifecycle] = None
    process: Optional[subprocess.Popen[str]] = None
    forwarders: list[threading.Thread] = []

    def request_stop(_signum: int, _frame: object) -> None:
        # Signal handlers only communicate intent. Metadata I/O and process
        # termination happen in the supervised control loop, where failures
        # cannot prevent cleanup.
        stopping.set()

    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None and process.stderr is not None
        forwarders = [
            threading.Thread(
                target=_forward,
                args=(process.stdout, sys.stdout),
                daemon=True,
            ),
            threading.Thread(
                target=_forward,
                args=(process.stderr, sys.stderr),
                daemon=True,
            ),
        ]
        for forwarder in forwarders:
            forwarder.start()
        reporter.report(
            ServiceLifecycle.STARTING,
            message="ParaView service process started; waiting for health readiness",
        )
        lifecycle = ServiceLifecycle.STARTING

        deadline = time.monotonic() + args.startup_timeout
        probe_host = _probe_host(args.bind_host)
        ready = False
        while time.monotonic() < deadline and process.poll() is None:
            if stopping.is_set():
                return _stop_after_request(process, reporter, lifecycle)
            if _health_ready(
                probe_host,
                port,
                service_instance_id=args.service_instance_id,
                authorization_token=authorization_token,
            ):
                reporter.report(
                    ServiceLifecycle.READY,
                    message="ParaView HTTP service passed its health probe",
                )
                lifecycle = ServiceLifecycle.READY
                ready = True
                break
            stopping.wait(0.1)
        if not ready:
            return_code = process.poll()
            if return_code is None:
                _terminate_process(process)
                diagnostic = "ParaView HTTP service did not become ready before timeout"
            else:
                diagnostic = (
                    "ParaView service process exited before readiness with code "
                    f"{return_code}"
                )
            reporter.report(ServiceLifecycle.FAILED, message=diagnostic)
            lifecycle = ServiceLifecycle.FAILED
            print(diagnostic, file=sys.stderr, flush=True)
            return 1

        health_failures = 0
        while process.poll() is None:
            if stopping.is_set():
                return _stop_after_request(process, reporter, lifecycle)
            healthy = _health_ready(
                probe_host,
                port,
                service_instance_id=args.service_instance_id,
                authorization_token=authorization_token,
            )
            if healthy:
                health_failures = 0
                if lifecycle is ServiceLifecycle.DEGRADED:
                    reporter.report(
                        ServiceLifecycle.READY,
                        message="ParaView HTTP service health recovered",
                    )
                    lifecycle = ServiceLifecycle.READY
            else:
                health_failures += 1
                if (
                    health_failures >= HEALTH_FAILURE_THRESHOLD
                    and lifecycle is ServiceLifecycle.READY
                ):
                    reporter.report(
                        ServiceLifecycle.DEGRADED,
                        message=(
                            "ParaView HTTP service failed consecutive health probes"
                        ),
                    )
                    lifecycle = ServiceLifecycle.DEGRADED
            stopping.wait(HEALTH_PROBE_INTERVAL_SECONDS)

        return_code = process.wait()
        if return_code == 0:
            reporter.report(
                ServiceLifecycle.STOPPED,
                message="ParaView service exited cleanly",
            )
            lifecycle = ServiceLifecycle.STOPPED
            return 0
        reporter.report(
            ServiceLifecycle.FAILED,
            message=f"ParaView service process exited with code {return_code}",
        )
        lifecycle = ServiceLifecycle.FAILED
        return return_code if 0 < return_code <= 255 else 1
    except Exception as exc:
        diagnostic = f"ParaView service supervisor failed: {exc}"
        if process is not None and process.poll() is None:
            try:
                _terminate_process(process)
            except Exception as cleanup_error:
                diagnostic += f"; child cleanup failed: {cleanup_error}"
        if lifecycle not in {ServiceLifecycle.STOPPED, ServiceLifecycle.FAILED}:
            try:
                reporter.report(ServiceLifecycle.FAILED, message=diagnostic)
            except Exception as report_error:
                diagnostic += f"; failure reporting failed: {report_error}"
        print(diagnostic, file=sys.stderr, flush=True)
        return 1
    finally:
        try:
            if process is not None and process.poll() is None:
                _terminate_process(process)
        finally:
            for forwarder in forwarders:
                forwarder.join(timeout=2)
            signal.signal(signal.SIGTERM, previous_sigterm)
            signal.signal(signal.SIGINT, previous_sigint)


def _stop_after_request(
    process: subprocess.Popen[str],
    reporter: ServiceRuntimeReporter,
    lifecycle: ServiceLifecycle,
) -> int:
    """Stop the child even when lifecycle persistence fails."""
    report_errors: list[str] = []
    if lifecycle not in {ServiceLifecycle.STOPPED, ServiceLifecycle.FAILED}:
        try:
            reporter.report(
                ServiceLifecycle.STOPPING,
                message="ParaView service received an owned termination request",
            )
        except Exception as exc:
            report_errors.append(f"stopping report failed: {exc}")
    try:
        _, forced = _terminate_process(process)
    except Exception as exc:
        report_errors.append(f"child cleanup failed: {exc}")
        try:
            reporter.report(
                ServiceLifecycle.FAILED,
                message="ParaView service could not be stopped after an owned request",
            )
        except Exception as report_error:
            report_errors.append(f"cleanup failure report failed: {report_error}")
        print("; ".join(report_errors), file=sys.stderr, flush=True)
        return 1
    try:
        reporter.report(
            ServiceLifecycle.STOPPED,
            message=(
                "ParaView service was forcibly stopped after an owned termination request"
                if forced
                else "ParaView service stopped after an owned termination request"
            ),
        )
    except Exception as exc:
        report_errors.append(f"stopped report failed: {exc}")
    if report_errors:
        print("; ".join(report_errors), file=sys.stderr, flush=True)
        return 1
    return 0


def _terminate_process(
    process: subprocess.Popen[str],
    *,
    grace_seconds: float = TERMINATION_GRACE_SECONDS,
) -> tuple[int, bool]:
    """Terminate one owned child with a bounded kill escalation."""
    if grace_seconds <= 0:
        raise ValueError("termination grace_seconds must be positive")
    current = process.poll()
    if current is not None:
        return current, False
    process.terminate()
    try:
        return process.wait(timeout=grace_seconds), False
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(timeout=grace_seconds), True


def _read_authorization_token(path: Path) -> str:
    """Read one private capability file without placing its value in argv or logs."""
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValueError("authorization file must be a regular absolute file")
    if path.stat().st_size > 128:
        raise ValueError("authorization file exceeds its bounded size")
    if sys.platform != "win32" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("authorization file must not grant group or other access")
    token = path.read_text(encoding="ascii").strip()
    ServiceRuntimeAuthority(scheme="bearer", token=token)
    return token


def _available_port(bind_host: str) -> int:
    """Reserve and release an ephemeral port immediately before child launch."""
    family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as candidate:
        candidate.bind((bind_host, 0))
        return int(candidate.getsockname()[1])


def _probe_host(bind_host: str) -> str:
    if bind_host == "0.0.0.0":
        return "127.0.0.1"
    if bind_host == "::":
        return "::1"
    return bind_host


def _health_ready(
    host: str,
    port: int,
    *,
    service_instance_id: str,
    authorization_token: str,
) -> bool:
    """Accept readiness only from the versioned service health response."""
    rendered_host = f"[{host}]" if ":" in host else host
    request = urllib.request.Request(
        f"http://{rendered_host}:{port}/healthz",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {authorization_token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=0.5) as response:
            if response.status != 200:
                return False
            payload = response.read(16 * 1024 + 1)
    except (OSError, urllib.error.URLError, TimeoutError):
        return False
    if len(payload) > 16 * 1024:
        return False
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    revision = value.get("revision") if isinstance(value, dict) else None
    return (
        isinstance(value, dict)
        and value
        == {
            "schema_version": "jarvis.paraview.health.v1",
            "status": "ready",
            "service_instance_id": service_instance_id,
            "revision": revision,
        }
        and not isinstance(revision, bool)
        and isinstance(revision, int)
        and revision >= 1
    )


def _forward(source: IO[str], destination: IO[str]) -> None:
    """Forward child diagnostics without treating them as metadata."""
    try:
        for line in source:
            destination.write(line)
            destination.flush()
    finally:
        source.close()


if __name__ == "__main__":
    raise SystemExit(main())
