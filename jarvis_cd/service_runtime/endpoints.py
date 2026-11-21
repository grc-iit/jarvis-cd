"""JARVIS-owned records of where a ready service actually listens.

A service package may announce readiness only through its progress stream,
for example ``clio-pvserver ready port=59629`` parsed by a package progress
provider into a ``ready`` progress observation. Port 0 asks the operating
system for a free port, so the configured port says nothing; only the ready
event knows the real one. When JARVIS persists a ``ready`` observation it
extracts the announced endpoints and appends one ``jarvis.service-endpoint.v1``
record per port, stamped with the node that observed it and the ready time.
``jarvis execution service-runtimes +json`` returns them as
``service_endpoints``, next to endpoints derived from service runtime reports.
"""

from __future__ import annotations

import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

SERVICE_ENDPOINT_SCHEMA_VERSION = "jarvis.service-endpoint.v1"
_WILDCARD_HOSTS = {"", "0.0.0.0", "::", "*"}
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_MAX_ENDPOINTS = 32


def endpoint_path_for(runtime_path: str | os.PathLike[str]) -> Path:
    """Return the endpoint sidecar that sits beside a service runtime sidecar."""
    path = Path(runtime_path)
    return path.with_name(f"{path.stem}.endpoints.jsonl")


def _port(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and 0 < value < 65536:
        return value
    return None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _entry(raw: Mapping[str, Any], index: int) -> dict[str, Any] | None:
    port = _port(raw.get("port"))
    if port is None:
        return None
    return {
        "name": _text(raw.get("name")) or f"port{index}",
        "protocol": (_text(raw.get("protocol")) or "tcp").lower(),
        "port": port,
        "bind_host": _text(raw.get("host"))
        or _text(raw.get("bind"))
        or _text(raw.get("bind_host")),
    }


def _ports_from(container: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """Read one of the accepted ready-event shapes from ``container``."""
    explicit = container.get("service_endpoints")
    if isinstance(explicit, list):
        return [
            item
            for index, raw in enumerate(explicit)
            if isinstance(raw, Mapping) and (item := _entry(raw, index))
        ]
    service = container.get("service")
    if isinstance(service, Mapping) and isinstance(service.get("ports"), list):
        found = [
            item
            for index, raw in enumerate(service["ports"])
            if isinstance(raw, Mapping) and (item := _entry(raw, index))
        ]
        fields = container.get("fields")
        # The ready line's own port wins over a configured port of 0.
        if not found and isinstance(fields, Mapping) and _port(fields.get("port")):
            first = service["ports"][0] if service["ports"] else {}
            merged = dict(first) if isinstance(first, Mapping) else {}
            merged["port"] = fields["port"]
            item = _entry(merged, 0)
            found = [item] if item else []
        return found
    fields = container.get("fields")
    for source in (container, fields if isinstance(fields, Mapping) else {}):
        if _port(source.get("port")) is not None:
            item = _entry(source, 0)
            return [item] if item else []
    return None


def extract_endpoints(metadata: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Return the endpoints a ``ready`` progress observation announces.

    Accepted shapes, in order: ``service_endpoints: [{name, protocol, host,
    port}]``; ``service: {ports: [{name, protocol, bind, port}]}`` with an
    optional ``fields.port`` from the ready line; or a bare ``port`` (also
    under ``fields``). Each shape is read at the top of ``metadata`` or one
    level down (a package namespace such as ``metadata.clio``).
    """
    if not isinstance(metadata, Mapping):
        return []
    candidates: list[Mapping[str, Any]] = [metadata]
    candidates += [value for value in metadata.values() if isinstance(value, Mapping)]
    for container in candidates:
        found = _ports_from(container)
        if found:
            return found[:_MAX_ENDPOINTS]
    return []


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def client_host(bind_host: str | None, node: str | None) -> str | None:
    """Return the address a client on the cluster should dial."""
    if bind_host and bind_host not in _WILDCARD_HOSTS:
        return bind_host
    return node


class ServiceEndpointStore:
    """Append-only, locked JSONL of ready endpoints for one package."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def append(self, records: Iterable[Mapping[str, Any]]) -> None:
        lines = "".join(
            json.dumps(dict(record), sort_keys=True, separators=(",", ":")) + "\n"
            for record in records
        )
        if not lines:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        )
        try:
            try:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - non-POSIX hosts
                pass
            os.write(descriptor, lines.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.is_file() or self.path.is_symlink():
            return []
        records = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue  # a torn final line from a crash
            if (
                isinstance(value, dict)
                and value.get("schema_version") == SERVICE_ENDPOINT_SCHEMA_VERSION
            ):
                records.append(value)
        return records

    def current(self) -> list[dict[str, Any]]:
        """Latest record per endpoint name, keeping the first ready time."""
        latest: dict[str, dict[str, Any]] = {}
        for record in self.read_all():
            name = str(record.get("name"))
            previous = latest.get(name)
            if previous is not None and previous.get("port") == record.get("port"):
                record = {**record, "ready_at": previous["ready_at"],
                          "ready_at_epoch": previous["ready_at_epoch"]}
            latest[name] = record
        return list(latest.values())


def ready_endpoint_records(
    *,
    execution_id: str,
    package_id: str,
    package_name: str,
    metadata: Mapping[str, Any] | None,
    progress_sequence: int | None = None,
    observed_at_epoch: float | None = None,
    node: str | None = None,
) -> list[dict[str, Any]]:
    """Build endpoint records for one ``ready`` progress observation."""
    endpoints = extract_endpoints(metadata)
    if not endpoints:
        return []
    epoch = time.time() if observed_at_epoch is None else float(observed_at_epoch)
    node = node or socket.gethostname()
    records = []
    for endpoint in endpoints:
        bind_host = endpoint["bind_host"]
        host = client_host(bind_host, node)
        records.append(
            {
                "schema_version": SERVICE_ENDPOINT_SCHEMA_VERSION,
                "execution_id": execution_id,
                "package_id": package_id,
                "package_name": package_name,
                "name": endpoint["name"],
                "protocol": endpoint["protocol"],
                "port": endpoint["port"],
                "bind_host": bind_host,
                "node": node,
                "host": host,
                "loopback_only": bind_host in _LOOPBACK_HOSTS,
                "ready_at": _iso(epoch),
                "ready_at_epoch": epoch,
                "source": "progress_ready",
                "progress_sequence": progress_sequence,
            }
        )
    return records


def endpoint_from_runtime_report(report: Any, ready_at_epoch: float | None) -> dict[str, Any]:
    """Project a service runtime report onto the endpoint record shape."""
    bind_host = getattr(report, "host", None)
    return {
        "schema_version": SERVICE_ENDPOINT_SCHEMA_VERSION,
        "execution_id": report.execution_id,
        "package_id": report.package_id,
        "package_name": report.package_name,
        "name": report.service_instance_id,
        "protocol": report.protocol.value,
        "port": report.port,
        "bind_host": bind_host,
        "node": None,
        "host": bind_host,
        "loopback_only": bind_host in _LOOPBACK_HOSTS,
        "ready_at": _iso(ready_at_epoch) if ready_at_epoch is not None else None,
        "ready_at_epoch": ready_at_epoch,
        "source": "service_runtime",
        "lifecycle": report.lifecycle.value,
        "service_instance_id": report.service_instance_id,
        "revision": report.revision,
    }
