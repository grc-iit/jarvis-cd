"""Launch generic ParaView servers or user-supplied pvbatch scripts."""

import os
import shlex
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, ExecInfo, LocalExecInfo, MpiExecInfo


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
                "msg": "ParaView mode: server or batch",
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
                "msg": "Useful for headless environments (no display)",
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
                "name": "pvbatch_bin",
                "msg": "Path or command used to launch pvbatch",
                "type": str,
                "default": "pvbatch",
            },
            {
                "name": "pvbatch_options",
                "msg": "Options passed to pvbatch (for example --mesa)",
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
        ]

    def _configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param kwargs: Configuration parameters for this pkg.
        :return: None
        """

        pass

    def start(self):
        """
        Launch an application. E.g., OrangeFS will launch the servers, clients,
        and metadata services on all necessary pkgs.

        :return: None
        """
        mode = self.config.get("mode", "server")
        environment = dict(self.mod_env)
        environment.setdefault("JARVIS_PACKAGE_NAME", "builtin.paraview")
        environment.setdefault("JARVIS_PACKAGE_ID", str(self.pkg_id or "paraview"))
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
            pvbatch_bin = os.path.expandvars(
                cast(str, self.config.get("pvbatch_bin") or "pvbatch")
            )
            command = [shlex.quote(pvbatch_bin)]
            pvbatch_options = cast(
                str,
                self.config.get("pvbatch_options") or "",
            )
            command.extend(shlex.quote(item) for item in shlex.split(pvbatch_options))
            if self.config["force_offscreen_rendering"]:
                command.append("--force-offscreen-rendering")
            command.append(shlex.quote(script))
            script_args = cast(str, self.config.get("script_args") or "")
            if script_args:
                command.extend(shlex.quote(item) for item in shlex.split(script_args))
            result = Exec(" ".join(command), exec_info).run()
            self._raise_for_exec_failure(result, operation="ParaView batch")
            return
        if mode != "server":
            raise ValueError(f"Unsupported ParaView mode: {mode!r}")

        port_id = self.config["port_id"]
        time_out = self.config["time_out"]
        condition = ""
        if self.config["force_offscreen_rendering"]:
            condition += " --force-offscreen-rendering"
        result = Exec(
            f"pvserver --server-port={port_id} --timeout={time_out}{condition}",
            exec_info,
        ).run()
        self._raise_for_exec_failure(result, operation="ParaView server")

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

    def _execution_info(
        self,
        environment: dict[str, str],
        line_callback: Callable[[str, str], None] | None,
    ) -> ExecInfo:
        """Select direct execution for one rank and MPI for multiple ranks."""
        nprocs = int(self.config["nprocs"])
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

    @staticmethod
    def _raise_for_exec_failure(result: Any, *, operation: str) -> None:
        """Raise when an execution has no status or any host exits nonzero."""
        exit_codes = getattr(result, "exit_code", None)
        if not isinstance(exit_codes, dict) or not exit_codes:
            raise RuntimeError(f"{operation} returned no process exit status")
        failures = {
            str(host): code
            for host, code in exit_codes.items()
            if isinstance(code, bool) or not isinstance(code, int) or code != 0
        }
        if failures:
            details = ", ".join(
                f"{host}={code!r}" for host, code in sorted(failures.items())
            )
            raise RuntimeError(f"{operation} failed with exit status: {details}")

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
