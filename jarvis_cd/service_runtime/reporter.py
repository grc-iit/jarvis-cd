"""Package-facing reporter for JARVIS-owned service-runtime metadata."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Mapping
from uuid import uuid4

from .schema import (
    DatasetDescriptor,
    ServiceLifecycle,
    ServiceProtocol,
    ServiceRuntimeReport,
)
from .store import ServiceRuntimeStore


class ServiceRuntimeReporter:
    """Persist service lifecycle without parsing application stdout."""

    def __init__(
        self,
        *,
        execution_id: str,
        package_name: str,
        package_id: str,
        path: str | os.PathLike[str],
        service_instance_id: str,
        host: str,
        port: int,
        protocol: ServiceProtocol = ServiceProtocol.HTTP,
        health_path: str = "/healthz",
        live_data_path: str = "/live-data",
        events_path: str = "/events",
        state_path: str = "/state",
        command_path: str = "/commands",
        dataset_descriptor: DatasetDescriptor,
    ) -> None:
        """Bind immutable service identity to an owned sidecar."""
        self.execution_id = execution_id
        self.package_name = package_name
        self.package_id = package_id
        self.path = Path(path)
        self.service_instance_id = service_instance_id
        self.host = host
        self.port = port
        self.protocol = protocol
        self.health_path = health_path
        self.live_data_path = live_data_path
        self.events_path = events_path
        self.state_path = state_path
        self.command_path = command_path
        self.dataset_descriptor = dataset_descriptor
        if not self.path.is_absolute():
            raise ValueError("service runtime sidecar path must be absolute")
        # Validate all immutable values before the first storage mutation.
        self._build_report(1, ServiceLifecycle.STARTING, None, time.time())

    @classmethod
    def from_environment(
        cls,
        *,
        service_instance_id: str,
        host: str,
        port: int,
        dataset_descriptor: DatasetDescriptor,
        protocol: ServiceProtocol = ServiceProtocol.HTTP,
        environ: Mapping[str, str] | None = None,
    ) -> "ServiceRuntimeReporter":
        """Construct a reporter from authoritative JARVIS package bindings."""
        values = os.environ if environ is None else environ
        required = {
            name: values.get(name)
            for name in (
                "JARVIS_EXECUTION_ID",
                "JARVIS_PACKAGE_NAME",
                "JARVIS_PACKAGE_ID",
                "JARVIS_SERVICE_RUNTIME_PATH",
            )
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(
                "missing JARVIS service runtime bindings: " + ", ".join(missing)
            )
        return cls(
            execution_id=required["JARVIS_EXECUTION_ID"] or "",
            package_name=required["JARVIS_PACKAGE_NAME"] or "",
            package_id=required["JARVIS_PACKAGE_ID"] or "",
            path=required["JARVIS_SERVICE_RUNTIME_PATH"] or "",
            service_instance_id=service_instance_id,
            host=host,
            port=port,
            protocol=protocol,
            dataset_descriptor=dataset_descriptor,
        )

    @staticmethod
    def new_service_instance_id() -> str:
        """Return a portable opaque identity for one concrete service process."""
        return f"srv_{uuid4().hex}"

    def report(
        self,
        lifecycle: ServiceLifecycle,
        *,
        message: str | None = None,
    ) -> ServiceRuntimeReport:
        """Append the next revision for this exact service instance."""
        observed_at = time.time()

        def build(
            history: tuple[ServiceRuntimeReport, ...],
        ) -> ServiceRuntimeReport:
            previous = next(
                (
                    report
                    for report in reversed(history)
                    if report.service_instance_id == self.service_instance_id
                ),
                None,
            )
            revision = previous.revision + 1 if previous is not None else 1
            return self._build_report(
                revision,
                lifecycle,
                message,
                observed_at,
            )

        return ServiceRuntimeStore(self.path).append_next(build)

    def _build_report(
        self,
        revision: int,
        lifecycle: ServiceLifecycle,
        message: str | None,
        observed_at: float,
    ) -> ServiceRuntimeReport:
        return ServiceRuntimeReport(
            execution_id=self.execution_id,
            package_name=self.package_name,
            package_id=self.package_id,
            service_instance_id=self.service_instance_id,
            revision=revision,
            lifecycle=lifecycle,
            host=self.host,
            port=self.port,
            protocol=self.protocol,
            health_path=self.health_path,
            live_data_path=self.live_data_path,
            events_path=self.events_path,
            state_path=self.state_path,
            command_path=self.command_path,
            dataset_descriptor=self.dataset_descriptor,
            message=message,
            observed_at_epoch=observed_at,
        )


__all__ = ["ServiceRuntimeReporter"]
