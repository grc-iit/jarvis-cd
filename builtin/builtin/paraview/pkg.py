"""Launch generic ParaView servers, services, or user-supplied batch scripts."""

import os
import secrets
import shlex
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, ExecInfo, LocalExecInfo, MpiExecInfo, Which

_EXEC_FAILURE_STDERR_LIMIT = 4096
_MODE_EXECUTABLES = {
    "batch": "pvbatch",
    "server": "pvserver",
    "service": "pvpython",
}
_HEADLESS_ARGUMENTS = ("--mesa", "--force-offscreen-rendering")


@dataclass(frozen=True)
class _ParaViewRuntime:
    """One ParaView launcher resolved inside the package execution environment."""

    executable: str
    capabilities: frozenset[str]

    def arguments(self, *, force_offscreen: bool) -> tuple[str, ...]:
        """Return package-selected launcher arguments for semantic headless mode."""
        if not force_offscreen:
            return ()
        for argument in _HEADLESS_ARGUMENTS:
            if argument in self.capabilities:
                return (argument,)
        raise RuntimeError(
            f"ParaView launcher {self.executable!r} does not advertise a supported "
            "headless rendering capability (--mesa or --force-offscreen-rendering)"
        )


class Paraview(Application):
    """
    This class provides methods to launch the Paraview application.
    """

    def _init(self):
        """
        Initialize paths
        """
        pass

    def _configure_menu(self):
        """
        Create a CLI menu for the configurator method.
        For thorough documentation of these parameters, view:
        https://github.com/scs-lab/jarvis-util/wiki/3.-Argument-Parsing

        :return: List(dict)
        """
        return [
            {
                "name": "mode",
                "msg": (
                    "ParaView mode: service for a live dataset view, server for "
                    "a plain pvserver, or batch for a script"
                ),
                "type": str,
                "default": "server",
            },
            {
                "name": "nprocs",
                "msg": "Number of processes",
                "type": int,
                "default": 1,
            },
            {
                "name": "ppn",
                "msg": "The number of processes per node",
                "type": int,
                "default": 16,
            },
            {
                "name": "time_out",
                "msg": "Set a timeout period for idle client sessions",
                "type": int,
                "default": 10000,
            },
            {
                "name": "force_offscreen_rendering",
                "msg": (
                    "Use a detected headless backend in server or batch mode; "
                    "service mode is always headless"
                ),
                "type": bool,
                "default": False,
            },
            {
                "name": "port_id",
                "msg": "Set the port the server listens on",
                "type": int,
                "default": 11111,
            },
            {
                "name": "script",
                "msg": "Generic Python script passed to pvbatch in batch mode",
                "type": str,
                "default": "",
            },
            {
                "name": "script_args",
                "msg": "Arguments passed unchanged to the pvbatch script",
                "type": str,
                "default": "",
            },
            {
                "name": "cwd",
                "msg": "Working directory for pvbatch (empty uses current directory)",
                "type": str,
                "default": "",
            },
            {
                "name": "dataset_descriptor",
                "msg": (
                    "Live-view dataset descriptor JSON or JSON file path; "
                    "requires mode=service"
                ),
                "type": str,
                "default": "",
            },
            {
                "name": "service_bind_host",
                "msg": "Loopback interface on which the private service listens",
                "type": str,
                "default": "127.0.0.1",
            },
            {
                "name": "service_advertise_host",
                "msg": "Loopback host recorded for the colocated relay connector",
                "type": str,
                "default": "127.0.0.1",
            },
            {
                "name": "service_port",
                "msg": "HTTP service port (zero selects an ephemeral port)",
                "type": int,
                "default": 0,
            },
            {
                "name": "service_startup_timeout",
                "msg": "Seconds allowed for the real HTTP health probe",
                "type": int,
                "default": 120,
            },
        ]

    def _configure(self, **kwargs: Any) -> None:
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param kwargs: Configuration parameters for this pkg.
        :return: None
        """
        del kwargs
        self._validate_legacy_runtime_configuration()
        mode = self.config.get("mode", "server")
        if mode not in _MODE_EXECUTABLES:
            raise ValueError(f"Unsupported ParaView mode: {mode!r}")
        configured = self.config.get("dataset_descriptor")
        if mode != "service":
            if configured not in (None, ""):
                raise ValueError(
                    "ParaView dataset_descriptor requires mode='service' for "
                    "live dataset viewing"
                )
            return

        from jarvis_cd.service_runtime import DatasetDescriptor

        if not isinstance(configured, str):
            raise ValueError(
                "ParaView dataset_descriptor must be JSON text or a file path"
            )
        self._load_dataset_descriptor(configured, DatasetDescriptor)

    def start(self):
        """
        Launch an application. E.g., OrangeFS will launch the servers, clients,
        and metadata services on all necessary pkgs.

        :return: None
        """
        self._validate_legacy_runtime_configuration()
        mode = self.config.get("mode", "server")
        environment = dict(self.mod_env)
        environment.setdefault("JARVIS_PACKAGE_NAME", "builtin.paraview")
        environment.setdefault("JARVIS_PACKAGE_ID", str(self.pkg_id or "paraview"))
        if mode == "service":
            self._start_service(environment)
            return
        reporter_path = self._stage_progress_reporter()
        reporter_dir = str(reporter_path.parent)
        current_pythonpath = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = (
            reporter_dir
            if not current_pythonpath
            else reporter_dir + os.pathsep + current_pythonpath
        )
        environment["JARVIS_PARAVIEW_REPORTER"] = str(reporter_path)
        environment["JARVIS_PROGRESS_TRANSPORT"] = "stdout"
        self.mod_env.update(
            {
                "JARVIS_PACKAGE_NAME": environment["JARVIS_PACKAGE_NAME"],
                "JARVIS_PACKAGE_ID": environment["JARVIS_PACKAGE_ID"],
                "JARVIS_PROGRESS_TRANSPORT": "stdout",
            }
        )
        line_callback = self.progress_line_callback()
        exec_info = self._execution_info(environment, line_callback)
        if mode == "batch":
            script = os.path.expandvars(cast(str, self.config.get("script") or ""))
            if not script:
                raise ValueError("ParaView batch mode requires a script")
            runtime = self._resolve_runtime("batch", environment)
            command = [shlex.quote(runtime.executable)]
            command.extend(
                shlex.quote(item)
                for item in runtime.arguments(
                    force_offscreen=bool(
                        self.config.get("force_offscreen_rendering", False)
                    )
                )
            )
            command.append(shlex.quote(script))
            script_args = cast(str, self.config.get("script_args") or "")
            if script_args:
                command.extend(shlex.quote(item) for item in shlex.split(script_args))
            result = Exec(" ".join(command), exec_info).run()
            self._raise_for_exec_failure(result, operation="ParaView batch")
            return
        if mode != "server":
            raise ValueError(f"Unsupported ParaView mode: {mode!r}")

        runtime = self._resolve_runtime("server", environment)
        port_id = self.config["port_id"]
        time_out = self.config["time_out"]
        command = [
            shlex.quote(runtime.executable),
            f"--server-port={port_id}",
            f"--timeout={time_out}",
        ]
        command.extend(
            shlex.quote(item)
            for item in runtime.arguments(
                force_offscreen=bool(
                    self.config.get("force_offscreen_rendering", False)
                )
            )
        )
        result = Exec(
            " ".join(command),
            exec_info,
        ).run()
        self._raise_for_exec_failure(result, operation="ParaView server")

    def _start_service(self, environment: dict[str, str]) -> None:
        """Launch a durable HTTP/SSE service through the JARVIS supervisor."""
        from jarvis_cd.service_runtime import (
            DatasetDescriptor,
            ServiceRuntimeReporter,
        )

        if self._configured_int("nprocs", 1) != 1:
            raise ValueError("ParaView service mode currently requires nprocs=1")
        advertise_host = cast(
            str,
            self.config.get("service_advertise_host") or "127.0.0.1",
        )
        bind_host = cast(
            str,
            self.config.get("service_bind_host") or "127.0.0.1",
        )
        if bind_host != "127.0.0.1" or advertise_host != "127.0.0.1":
            raise ValueError(
                "ParaView service mode is loopback-only; the relay connector "
                "must run inside the owned execution allocation"
            )
        execution_id = environment.get("JARVIS_EXECUTION_ID")
        runtime_path = environment.get("JARVIS_SERVICE_RUNTIME_PATH")
        artifact_path = environment.get("JARVIS_ARTIFACT_PATH")
        if not execution_id or not runtime_path or not artifact_path:
            raise RuntimeError(
                "ParaView service mode requires a durable JARVIS execution binding"
            )
        runtime = self._resolve_runtime("service", environment)
        pvpython_arguments = runtime.arguments(force_offscreen=True)
        pvpython_options = shlex.join(pvpython_arguments)
        descriptor_value = cast(
            str,
            self.config.get("dataset_descriptor") or "",
        )
        descriptor = self._load_dataset_descriptor(
            descriptor_value,
            DatasetDescriptor,
        )
        if not self.shared_dir:
            raise RuntimeError("ParaView package has no shared directory")
        service_instance_id = ServiceRuntimeReporter.new_service_instance_id()
        service_root = (
            Path(self.shared_dir)
            / ".jarvis-service"
            / execution_id
            / service_instance_id
        )
        service_root.mkdir(parents=True, exist_ok=False, mode=0o700)
        if os.name != "nt":
            service_root.chmod(0o700)
        package_root = Path(__file__).resolve().parent
        service_script = self._stage_payload(
            package_root / "service.py",
            service_root / "service.py",
        )
        self._stage_payload(
            package_root / "service_http.py",
            service_root / "service_http.py",
        )
        supervisor = self._stage_payload(
            package_root / "service_supervisor.py",
            service_root / "service_supervisor.py",
        )
        descriptor_path = service_root / "dataset-descriptor.json"
        self._write_private_payload(
            descriptor_path,
            (descriptor.to_json() + "\n").encode("utf-8"),
        )
        output_dir = service_root / "output"
        output_dir.mkdir(mode=0o700)
        authorization_path = service_root / "authorization.token"
        self._write_private_payload(
            authorization_path,
            (secrets.token_hex(32) + "\n").encode("ascii"),
        )
        service_port = self._configured_int("service_port", 0)
        if not 0 <= service_port <= 65535:
            raise ValueError("service_port must be between 0 and 65535")
        startup_timeout = self._configured_int("service_startup_timeout", 120)
        command = [
            sys.executable,
            str(supervisor),
            "--service-script",
            str(service_script),
            "--descriptor",
            str(descriptor_path),
            "--output-dir",
            str(output_dir),
            "--pvpython-bin",
            runtime.executable,
            f"--pvpython-options={pvpython_options}",
            "--bind-host",
            bind_host,
            "--advertise-host",
            advertise_host,
            "--port",
            str(service_port),
            "--startup-timeout",
            str(startup_timeout),
            "--service-instance-id",
            service_instance_id,
            "--authorization-file",
            str(authorization_path),
        ]
        environment.update(
            {
                "JARVIS_ARTIFACT_TRANSPORT": "sidecar",
                "JARVIS_PROGRESS_TRANSPORT": "sidecar",
                "JARVIS_SERVICE_INSTANCE_ID": service_instance_id,
            }
        )
        self.mod_env.update(environment)
        line_callback = self.runtime_line_callback()
        result = Exec(
            " ".join(shlex.quote(item) for item in command),
            self._execution_info(environment, line_callback),
        ).run()
        self._raise_for_exec_failure(result, operation="ParaView service")

    def _validate_legacy_runtime_configuration(self) -> None:
        """Reject implementation overrides while accepting old no-op defaults."""
        for field, executable in (
            ("pvbatch_bin", "pvbatch"),
            ("pvpython_bin", "pvpython"),
        ):
            configured = self.config.get(field)
            if configured in (None, "", executable):
                continue
            raise ValueError(
                f"ParaView {field} is no longer configurable; make {executable} "
                "available through the JARVIS pipeline execution environment PATH"
            )
        for field, executable in (
            ("pvbatch_options", "pvbatch"),
            ("pvpython_options", "pvpython"),
        ):
            configured = self.config.get(field)
            if configured in (None, ""):
                continue
            raise ValueError(
                f"ParaView {field} is no longer configurable; builtin.paraview "
                f"detects supported headless arguments from {executable}"
            )

    def _resolve_runtime(
        self,
        mode: str,
        environment: dict[str, str],
    ) -> _ParaViewRuntime:
        """Resolve and probe the mode-specific launcher from ``self.mod_env``."""
        executable_name = _MODE_EXECUTABLES.get(mode)
        if executable_name is None:
            raise ValueError(f"Unsupported ParaView mode: {mode!r}")
        exec_info = self._runtime_probe_info(environment)
        resolver = Which(executable_name, exec_info)
        resolver.run()
        failures = self._failed_exit_codes(resolver)
        resolved = self._first_output_line(resolver.stdout)
        if failures or not resolved:
            path = environment.get("PATH")
            path_context = "the configured PATH" if path else "PATH"
            raise RuntimeError(
                f"builtin.paraview mode={mode!r} requires {executable_name!r} "
                f"in the JARVIS execution environment {path_context}; install "
                "ParaView or select a pipeline environment that provides it"
            )

        help_result = Exec(
            f"{shlex.quote(resolved)} --help",
            exec_info,
        ).run()
        help_failures = self._failed_exit_codes(help_result)
        if help_failures:
            details = ", ".join(f"{host}={code!r}" for host, code in help_failures)
            diagnostic = self._bounded_stderr(help_result, help_failures)
            raise RuntimeError(
                f"ParaView capability probe failed for {resolved!r}: {details}"
                f"{diagnostic}"
            )
        help_text = "\n".join(
            value
            for output in (help_result.stdout, help_result.stderr)
            if isinstance(output, dict)
            for value in output.values()
            if isinstance(value, str)
        )
        capabilities = frozenset(
            argument for argument in _HEADLESS_ARGUMENTS if argument in help_text
        )
        return _ParaViewRuntime(
            executable=resolved,
            capabilities=capabilities,
        )

    def _runtime_probe_info(self, environment: dict[str, str]) -> LocalExecInfo:
        """Use the same host, container, environment, and cwd as package launch."""
        common: dict[str, Any] = {
            "env": environment,
            "cwd": os.path.expandvars(cast(str, self.config.get("cwd") or "")) or None,
            "hostfile": self.hostfile,
            "hide_output": True,
            "timeout": 30,
        }
        if self.config.get("deploy_mode") == "container":
            common.update(
                {
                    "container": self._container_engine,
                    "container_image": self.deploy_image_name(),
                    "shared_dir": self.shared_dir,
                    "private_dir": self.private_dir,
                    "bind_mounts": self.container_mounts,
                }
            )
        return LocalExecInfo(**common)

    @staticmethod
    def _first_output_line(output: Any) -> str:
        """Return the first printable non-empty line from executor output."""
        if not isinstance(output, dict):
            return ""
        for host in sorted(output, key=str):
            value = output[host]
            if not isinstance(value, str):
                continue
            for line in value.splitlines():
                candidate = line.strip()
                if candidate and all(ord(character) >= 32 for character in candidate):
                    return candidate
        return ""

    @staticmethod
    def _failed_exit_codes(result: Any) -> list[tuple[Any, Any]]:
        """Return deterministic nonzero or malformed executor statuses."""
        exit_codes = getattr(result, "exit_code", None)
        if not isinstance(exit_codes, dict) or not exit_codes:
            return [("runtime", "missing")]
        return sorted(
            (
                (host, code)
                for host, code in exit_codes.items()
                if isinstance(code, bool) or not isinstance(code, int) or code != 0
            ),
            key=lambda item: str(item[0]),
        )

    @staticmethod
    def _bounded_stderr(
        result: Any,
        failures: list[tuple[Any, Any]],
    ) -> str:
        """Return bounded stderr for a failed runtime capability probe."""
        stderr_by_host = getattr(result, "stderr", None)
        if not isinstance(stderr_by_host, dict):
            return ""
        messages = []
        for host, _code in failures:
            stderr = stderr_by_host.get(host)
            if stderr is None:
                stderr = stderr_by_host.get(str(host))
            if isinstance(stderr, str) and stderr.strip():
                messages.append(f"{host}: {' '.join(stderr.split())}")
        if not messages:
            return ""
        bounded = "; ".join(messages)
        if len(bounded) > _EXEC_FAILURE_STDERR_LIMIT:
            bounded = bounded[: _EXEC_FAILURE_STDERR_LIMIT - 3] + "..."
        return f"; stderr: {bounded}"

    @staticmethod
    def _load_dataset_descriptor(
        configured: str,
        descriptor_type: type[Any],
    ) -> Any:
        """Load one strict intrinsic descriptor from JSON or a JSON file."""
        rendered = os.path.expandvars(configured).strip()
        if not rendered:
            raise ValueError("ParaView service mode requires dataset_descriptor")
        if rendered.startswith("{"):
            payload = rendered
        else:
            path = Path(rendered).expanduser()
            if not path.is_file() or path.stat().st_size > 256 * 1024:
                raise ValueError("dataset_descriptor file is missing or too large")
            payload = path.read_text(encoding="utf-8")
        return descriptor_type.from_json(payload)

    def _stage_progress_reporter(self) -> Path:
        """Copy the standalone reporter into the package's mounted shared path."""
        if not self.shared_dir:
            raise RuntimeError("ParaView package has no shared directory")
        source = Path(__file__).resolve().parent / "progress_reporter.py"
        payload = source.read_bytes()
        reporter_dir = Path(self.shared_dir) / ".jarvis-progress"
        reporter_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = reporter_dir / "progress_reporter.py"
        descriptor, temporary_name = tempfile.mkstemp(
            dir=reporter_dir,
            prefix=".progress_reporter.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        descriptor_open = True
        try:
            try:
                stream = os.fdopen(descriptor, "wb")
            except BaseException:
                os.close(descriptor)
                descriptor_open = False
                raise
            descriptor_open = False
            with stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, target)
            if os.name != "nt":
                target.chmod(0o600)
        finally:
            if descriptor_open:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
        return target

    @classmethod
    def _stage_payload(cls, source: Path, target: Path) -> Path:
        """Atomically stage one package-owned runtime module."""
        cls._write_private_payload(target, source.read_bytes())
        return target

    @staticmethod
    def _write_private_payload(target: Path, payload: bytes) -> None:
        """Durably replace a private package-owned service file."""
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        descriptor_open = True
        try:
            try:
                stream = os.fdopen(descriptor, "wb")
            except BaseException:
                os.close(descriptor)
                descriptor_open = False
                raise
            descriptor_open = False
            with stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, target)
            if os.name != "nt":
                target.chmod(0o600)
        finally:
            if descriptor_open:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def _execution_info(
        self,
        environment: dict[str, str],
        line_callback: Callable[[str, str], None] | None,
    ) -> ExecInfo:
        """Select direct execution for one rank and MPI for multiple ranks."""
        nprocs = self._configured_int("nprocs", 1)
        common: dict[str, Any] = {
            "env": environment,
            "cwd": os.path.expandvars(cast(str, self.config.get("cwd") or "")) or None,
            "hostfile": self.hostfile,
            "line_callback": line_callback,
        }
        if self.config.get("deploy_mode") == "container":
            common.update(
                {
                    "container": self._container_engine,
                    "container_image": self.deploy_image_name(),
                    "shared_dir": self.shared_dir,
                    "private_dir": self.private_dir,
                    "bind_mounts": self.container_mounts,
                }
            )
        if nprocs == 1:
            return LocalExecInfo(**common)
        return MpiExecInfo(
            nprocs=nprocs,
            ppn=self.config["ppn"],
            port=(
                self.ssh_port if self.config.get("deploy_mode") == "container" else 22
            ),
            **common,
        )

    def _configured_int(self, name: str, default: int) -> int:
        """Return one package integer without accepting booleans or objects."""
        value = self.config.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError(f"ParaView {name} must be an integer")
        try:
            return int(value)
        except ValueError as exc:
            raise ValueError(f"ParaView {name} must be an integer") from exc

    @staticmethod
    def _raise_for_exec_failure(result: Any, *, operation: str) -> None:
        """Raise with bounded stderr when an execution exits unsuccessfully."""
        exit_codes = getattr(result, "exit_code", None)
        if not isinstance(exit_codes, dict) or not exit_codes:
            raise RuntimeError(f"{operation} returned no process exit status")
        failures = [
            (host, code)
            for host, code in exit_codes.items()
            if isinstance(code, bool) or not isinstance(code, int) or code != 0
        ]
        if failures:
            details = ", ".join(
                f"{host}={code!r}"
                for host, code in sorted(failures, key=lambda item: str(item[0]))
            )
            diagnostic = ""
            stderr_by_host = getattr(result, "stderr", None)
            if isinstance(stderr_by_host, dict):
                messages: list[str] = []
                for host, _code in sorted(failures, key=lambda item: str(item[0])):
                    stderr = stderr_by_host.get(host)
                    if stderr is None:
                        stderr = stderr_by_host.get(str(host))
                    if isinstance(stderr, str) and stderr.strip():
                        messages.append(f"{host}: {' '.join(stderr.split())}")
                if messages:
                    bounded = "; ".join(messages)
                    if len(bounded) > _EXEC_FAILURE_STDERR_LIMIT:
                        bounded = bounded[: _EXEC_FAILURE_STDERR_LIMIT - 3] + "..."
                    diagnostic = f"; stderr: {bounded}"
            raise RuntimeError(
                f"{operation} failed with exit status: {details}{diagnostic}"
            )

    def stop(self):
        """
        Stop a running application. E.g., OrangeFS will terminate the servers,
        clients, and metadata services.

        :return: None
        """
        pass

    def clean(self):
        """
        Destroy all data for an application. E.g., OrangeFS will delete all
        metadata and data directories in addition to the orangefs.xml file.

        :return: None
        """
        pass
