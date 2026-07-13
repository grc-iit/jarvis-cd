"""
Core execution classes for Jarvis shell execution.
"""

import subprocess
import threading
import time
import os
import signal
from abc import ABC, abstractmethod
from typing import Callable, Dict, cast

from .exec_info import ExecInfo, ExecType
from .windows_job import WindowsJob, spawn_windows_job_process


def _callback_failure_detail(error: Exception, limit: int = 4096) -> str:
    """Return a bounded diagnostic including nested exception-group causes."""
    details = [str(error) or type(error).__name__]
    pending: list[BaseException] = list(
        error.exceptions if isinstance(error, BaseExceptionGroup) else []
    )
    causes: list[str] = []
    while pending and len(causes) < 16:
        current = pending.pop(0)
        if isinstance(current, BaseExceptionGroup):
            pending[0:0] = list(current.exceptions)
            continue
        causes.append(str(current) or type(current).__name__)
    if causes:
        details.append("causes: " + "; ".join(causes))
    detail = ": ".join(details)
    if len(detail) > limit:
        return detail[: limit - 3] + "..."
    return detail


class CoreExec(ABC):
    """
    An abstract class representing a class which is intended to run
    shell commands. This includes SSH, MPI, etc.
    """

    def __init__(self):
        self.exit_code = {}  # hostname -> exit_code
        self.stdout = {}  # hostname -> stdout
        self.stderr = {}  # hostname -> stderr
        self.processes = {}  # hostname -> process
        self.process_groups = {}  # hostname -> process-group leader pid
        self.windows_jobs: Dict[str, WindowsJob] = {}
        self.output_threads = {}  # hostname -> (stdout_thread, stderr_thread)
        self.output_callback_errors = []
        self._output_callback_failure = threading.Event()
        self._output_callback_lock = threading.Lock()
        self._output_callback_finalized = False

    @abstractmethod
    def run(self):
        """Execute the command"""
        pass

    @abstractmethod
    def get_cmd(self) -> str:
        """Get the command string"""
        pass

    def wait(self, hostname: str = "localhost") -> int:
        """
        Wait for execution to complete.

        :param hostname: Hostname to wait for
        :return: Exit code
        """
        if hostname in self.processes:
            process = self.processes[hostname]
            timeout = getattr(getattr(self, "exec_info", None), "timeout", None)
            timed_out = False
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self.kill(hostname)
                exit_code = 124
            self.exit_code[hostname] = exit_code

            self._join_output_threads(hostname)
            semantic_return_code = exit_code
            if semantic_return_code == 0 and self._output_callback_failure.is_set():
                semantic_return_code = 1
            self._finalize_output_callback(semantic_return_code)
            if semantic_return_code == 0 and self._output_callback_failure.is_set():
                semantic_return_code = 1
            if semantic_return_code != 0:
                self._reconcile_output_callback(semantic_return_code)
            exit_code = semantic_return_code
            self.exit_code[hostname] = exit_code

            if self.output_callback_errors:
                existing = self.stderr.get(hostname, "")
                separator = "" if not existing or existing.endswith("\n") else "\n"
                self.stderr[hostname] = (
                    f"{existing}{separator}{''.join(self.output_callback_errors)}"
                )
                self.output_callback_errors.clear()

            windows_job = self.windows_jobs.get(hostname)
            if windows_job is not None:
                try:
                    windows_job.ensure_empty(process)
                finally:
                    windows_job.close(process)
                    self.windows_jobs.pop(hostname, None)

            if timed_out:
                diagnostic = f"Command timed out after {timeout} seconds"
                existing = self.stderr.get(hostname, "")
                separator = "" if not existing or existing.endswith("\n") else "\n"
                self.stderr[hostname] = f"{existing}{separator}{diagnostic}\n"

            return exit_code
        return 0

    def _join_output_threads(self, hostname: str) -> None:
        """Bound output-drain joins and terminate descendants retaining pipes."""
        if hostname not in self.output_threads:
            return
        threads = [
            thread for thread in self.output_threads[hostname] if thread is not None
        ]
        deadline = time.monotonic() + 5
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if not any(thread.is_alive() for thread in threads):
            return
        self.kill(hostname)
        process = self.processes.get(hostname)
        if process is not None:
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except (OSError, ValueError):
                        pass
        for thread in threads:
            thread.join(timeout=1)
        if any(thread.is_alive() for thread in threads):
            diagnostic = "Output capture did not close after process-tree termination\n"
            self.stderr[hostname] = self.stderr.get(hostname, "") + diagnostic

    def _record_output_callback_failure(
        self,
        output_type: str,
        exc: Exception,
        *,
        terminate: bool,
    ) -> None:
        """Latch one bounded callback failure and terminate the owned process."""
        should_terminate = False
        with self._output_callback_lock:
            if self._output_callback_failure.is_set():
                if not terminate:
                    detail = _callback_failure_detail(exc)
                    self.output_callback_errors.append(
                        f"Output line callback failed for {output_type}: {detail}\n"
                    )
                return
            self._output_callback_failure.set()
            detail = _callback_failure_detail(exc)
            self.output_callback_errors.append(
                f"Output line callback failed for {output_type}: {detail}\n"
            )
            should_terminate = terminate
        if should_terminate:
            hostname = getattr(self, "hostname", "localhost")
            try:
                self.kill(hostname)
            except Exception as kill_error:
                detail = str(kill_error)
                if len(detail) > 1024:
                    detail = detail[:1021] + "..."
                with self._output_callback_lock:
                    self.output_callback_errors.append(
                        f"Could not terminate process after callback failure: {detail}\n"
                    )

    def _finalize_output_callback(self, return_code: int) -> None:
        """Finalize a stateful callback with the owned process return code."""
        callback = getattr(getattr(self, "exec_info", None), "line_callback", None)
        process_finalizer = getattr(callback, "finalize_process", None)
        finalizer = getattr(callback, "finalize", None)
        if not callable(process_finalizer) and not callable(finalizer):
            return
        with self._output_callback_lock:
            if self._output_callback_finalized:
                return
            self._output_callback_finalized = True
        try:
            if callable(process_finalizer):
                cast(Callable[[int], None], process_finalizer)(return_code)
            elif callable(finalizer):
                cast(Callable[[], None], finalizer)()
        except Exception as exc:
            self._record_output_callback_failure("finalization", exc, terminate=False)

    def _reconcile_output_callback(self, return_code: int) -> None:
        """Correct semantic success using the effective nonzero return code."""
        callback = getattr(getattr(self, "exec_info", None), "line_callback", None)
        reconciler = getattr(callback, "reconcile_process_exit", None)
        if not callable(reconciler):
            return
        try:
            cast(Callable[[int], None], reconciler)(return_code)
        except Exception as exc:
            self._record_output_callback_failure(
                "reconciliation",
                exc,
                terminate=False,
            )

    def wait_all(self) -> Dict[str, int]:
        """
        Wait for all processes to complete.

        :return: Dictionary of hostname -> exit_code
        """
        for hostname in list(self.processes.keys()):
            self.wait(hostname)
        return self.exit_code.copy()

    def kill(self, hostname: str = "localhost"):
        """
        Kill the process.

        :param hostname: Hostname to kill process on
        """
        if hostname in self.processes:
            process = self.processes[hostname]
            try:
                if os.name == "nt":
                    windows_job = self.windows_jobs.get(hostname)
                    if windows_job is None:
                        raise RuntimeError(
                            "Windows subprocess has no identity-pinned Job Object"
                        )
                    windows_job.terminate(process)
                else:
                    process_group = self.process_groups.get(hostname, process.pid)
                    try:
                        os.killpg(process_group, signal.SIGTERM)
                    except ProcessLookupError:
                        process_group = None
                    if process_group is not None:
                        deadline = time.monotonic() + 1
                        while time.monotonic() < deadline:
                            try:
                                os.killpg(process_group, 0)
                            except ProcessLookupError:
                                break
                            time.sleep(0.02)
                        try:
                            os.killpg(process_group, 0)
                        except ProcessLookupError:
                            pass
                        else:
                            os.killpg(process_group, signal.SIGKILL)
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            except ProcessLookupError:
                # Process already terminated
                pass

    def kill_all(self):
        """Kill all processes"""
        for hostname in list(self.processes.keys()):
            self.kill(hostname)


class LocalExec(CoreExec):
    """
    Execute commands locally using subprocess.
    """

    def __init__(self, cmd: str, exec_info: ExecInfo):
        """
        Initialize local execution.

        :param cmd: Command to execute
        :param exec_info: Execution information
        """
        super().__init__()
        self.cmd = cmd
        self.exec_info = exec_info
        self.hostname = "localhost"

        # Initialize output storage
        self.stdout[self.hostname] = ""
        self.stderr[self.hostname] = ""
        self.exit_code[self.hostname] = 0

        # Skip execution in dry_run mode (used to build command strings)
        if exec_info.dry_run:
            return

        # Run the command
        self.run()

        # If not async, wait for completion
        if not exec_info.exec_async:
            self.wait(self.hostname)

    def get_cmd(self) -> str:
        """Get the command string"""
        return self.cmd

    def run(self):
        """Execute the command locally"""
        # Set up environment
        if self.exec_info.env:
            env = os.environ.copy()
            # Convert all env values to strings
            for key, value in self.exec_info.env.items():
                env[key] = str(value)
        else:
            env = os.environ

        # Prepare stdin
        stdin_pipe = subprocess.PIPE if self.exec_info.stdin else None

        # Start process
        try:
            popen_options = {}
            if os.name == "nt":
                popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_options["start_new_session"] = True
            if os.name == "nt":
                process, windows_job = spawn_windows_job_process(
                    self.cmd,
                    shell=True,
                    stdin_payload=self.exec_info.stdin,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    cwd=self.exec_info.cwd,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                    **popen_options,
                )
                self.windows_jobs[self.hostname] = windows_job
            else:
                process = subprocess.Popen(
                    self.cmd,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=stdin_pipe,
                    env=env,
                    cwd=self.exec_info.cwd,
                    text=True,
                    bufsize=1,  # Line buffered
                    universal_newlines=True,
                    **popen_options,
                )

            self.processes[self.hostname] = process
            self.process_groups[self.hostname] = process.pid

            # Start output monitoring threads
            if (
                self.exec_info.collect_output
                or not self.exec_info.hide_output
                or self.exec_info.line_callback is not None
            ):
                stdout_thread = threading.Thread(
                    target=self._monitor_output, args=(process.stdout, "stdout")
                )
                stderr_thread = threading.Thread(
                    target=self._monitor_output, args=(process.stderr, "stderr")
                )

                stdout_thread.daemon = True
                stderr_thread.daemon = True

                stdout_thread.start()
                stderr_thread.start()

                self.output_threads[self.hostname] = (stdout_thread, stderr_thread)

            # Send stdin if provided
            if self.exec_info.stdin and os.name != "nt":
                try:
                    process.stdin.write(self.exec_info.stdin)
                    process.stdin.close()
                except BrokenPipeError:
                    # Process may have terminated before we could write
                    pass

        except Exception as e:
            print(f"Error starting process: {e}")
            self.exit_code[self.hostname] = 1

    def _monitor_output(self, pipe, output_type: str):
        """
        Monitor stdout or stderr in a separate thread.

        :param pipe: The pipe to monitor
        :param output_type: 'stdout' or 'stderr'
        """
        output_buffer = []

        try:
            for line in iter(pipe.readline, ""):
                if not line:
                    break

                callback = self.exec_info.line_callback
                if callback is not None and not self._output_callback_failure.is_set():
                    try:
                        callback(output_type, line)
                    except Exception as exc:
                        self._record_output_callback_failure(
                            output_type,
                            exc,
                            terminate=True,
                        )

                # Store in buffer if collecting output
                if self.exec_info.collect_output:
                    output_buffer.append(line)

                # Print to console if not hidden
                if not self.exec_info.hide_output:
                    if output_type == "stdout":
                        print(line, end="")
                    else:
                        print(line, end="", file=subprocess.sys.stderr)

                # Write to file if specified
                pipe_file = (
                    self.exec_info.pipe_stdout
                    if output_type == "stdout"
                    else self.exec_info.pipe_stderr
                )
                if pipe_file:
                    try:
                        with open(pipe_file, "a") as f:
                            f.write(line)
                    except Exception as e:
                        print(f"Error writing to {pipe_file}: {e}")

        except Exception as e:
            print(f"Error monitoring {output_type}: {e}")
        finally:
            pipe.close()

        # Store collected output
        if self.exec_info.collect_output:
            if output_type == "stdout":
                self.stdout[self.hostname] = "".join(output_buffer)
            else:
                self.stderr[self.hostname] = "".join(output_buffer)

    def wait(self, hostname: str = "localhost") -> int:
        """Wait for completion and handle sleep"""
        exit_code = super().wait(hostname)

        # Sleep if specified
        if self.exec_info.sleep_ms > 0:
            time.sleep(self.exec_info.sleep_ms / 1000.0)

        return exit_code


class MpiVersion(LocalExec):
    """
    Introspect the current MPI implementation from the machine using
    mpiexec --version
    """

    def __init__(self, exec_info: ExecInfo):
        self.cmd = "mpiexec --version"

        # When running in a container, detect MPI inside the container
        c = exec_info.container
        if c and c not in ("none",):
            img = exec_info.container_image or ""
            if c == "apptainer":
                # Enter the long-running pipeline instance, not a fresh
                # SIF exec — keeps detection in the same namespace as
                # the rest of the pipeline.
                self.cmd = f"apptainer exec instance://{img} mpiexec --version"
            else:
                container_name = f"{img}_container" if img else ""
                self.cmd = f"{c} exec {container_name} mpiexec --version"

        # Create modified exec_info for introspection
        # CRITICAL: Must set exec_async=False to ensure we wait for output
        # CRITICAL: Must set dry_run=False to actually run mpiexec --version
        introspect_info = exec_info.mod(
            env=exec_info.basic_env,
            collect_output=True,
            hide_output=True,
            exec_async=False,
            dry_run=False,
            container="none",
            # MPI detection is an internal probe, not the application process.
            # Reusing an application callback here would finalize its durable
            # progress/artifact providers before the real MPI stream starts.
            line_callback=None,
        )

        # If the hostfile points to remote nodes, run via SSH on the
        # first host (the container lives there, not on the login node).
        hostfile = introspect_info.hostfile
        if hostfile and not hostfile.is_local():
            from .ssh_exec import SshExec

            probe = SshExec(
                self.cmd,
                introspect_info.mod(
                    exec_type=ExecType.SSH,
                    hostfile=hostfile.subset(1),
                    port=22,
                ),
            )
        else:
            probe = LocalExec(self.cmd, introspect_info)
        self.exit_code = probe.exit_code
        self.stdout = probe.stdout
        self.stderr = probe.stderr
        self.processes = probe.processes
        self.output_threads = probe.output_threads

        # Determine MPI version from output (key may be hostname, not 'localhost')
        vinfo = ""
        for v in self.stdout.values():
            if v:
                vinfo = v
                break

        if "mpich" in vinfo.lower():
            self.version = ExecType.MPICH
        elif "Open MPI" in vinfo or "OpenRTE" in vinfo:
            self.version = ExecType.OPENMPI
        elif "Intel(R) MPI Library" in vinfo:
            self.version = ExecType.INTEL_MPI
        elif "mpiexec version" in vinfo:
            self.version = ExecType.CRAY_MPICH
        else:
            # Default to MPICH if we can't determine
            print(f"Warning: Could not identify MPI implementation from: {vinfo}")
            self.version = ExecType.MPICH
