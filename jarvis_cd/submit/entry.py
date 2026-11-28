"""The one program of a JARVIS batch job: ``python -m jarvis_cd.submit.entry``.

PSI/J starts it as a single process on the first node of the allocation.
It writes the JARVIS hostfile from ``PSIJ_NODEFILE`` (every allocated node,
in allocation order, once), or, for the local executor, from the hosts the
submission sealed (``--nodefile``), binds the scheduler identity to the execution
record, runs the pipeline (or its install) as a child, and stops it on
scheduler TERM or on the stop-request file ``jarvis execution stop`` writes.
It finalizes the execution record and writes the return code to
``<work-dir>/exit_code``, which status reads first.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

from jarvis_cd.core.scheduler import EXIT_CODE_NAME, STOP_REQUEST_NAME, _write_atomic
from jarvis_cd.core.queues import allocation_identity


def allocation_hosts(nodefile: Optional[str], suffix: str = "") -> List[str]:
    """Allocated hosts, once each, in allocation order (``host_slice`` indexes them).

    ``PSIJ_NODEFILE`` names a file with one line per process slot; LSF sets
    it to the host list itself. No allocation means this host.
    """
    text = ""
    if nodefile:
        path = Path(nodefile)
        text = path.read_text(encoding="utf-8") if path.is_file() else nodefile
    hosts: List[str] = []
    for host in text.split():
        if host and host not in hosts:
            hosts.append(host)
    return [host + suffix for host in hosts] or ["localhost"]


class _Termination:
    def __init__(self) -> None:
        self.signal_name: Optional[str] = None

    def __call__(self, signum: int, _frame: object) -> None:
        self.signal_name = signal.Signals(signum).name.removeprefix("SIG")


def _pipeline_commands(arguments: argparse.Namespace, action: str) -> List[List[str]]:
    cli = [sys.executable, "-m", "jarvis_cd.core.cli"]
    extra = list(arguments.action_arg or []) if action != "stop" else []
    if arguments.snapshot_dir:
        yaml = str(Path(arguments.snapshot_dir) / "pipeline.yaml")
        return [cli + ["ppl", action, "yaml", yaml] + extra]
    return [cli + ["ppl", action, "yaml", arguments.pipeline_yaml] + extra]


def _environment(arguments: argparse.Namespace, action: str) -> dict:
    env = dict(os.environ)
    if arguments.snapshot_dir:
        env["JARVIS_PIPELINE_SNAPSHOT_DIR"] = str(arguments.snapshot_dir)
        if arguments.jarvis_root:
            env["JARVIS_ROOT"] = str(arguments.jarvis_root)
        if arguments.install_on_load and action == "run":
            env["JARVIS_INSTALL_ON_LOAD"] = arguments.install_on_load
    return env


def _run_steps(commands: List[List[str]], env: dict,
               timeout: Optional[float] = None) -> int:
    deadline = None if timeout is None else time.monotonic() + timeout
    for command in commands:
        remaining = None if deadline is None else max(deadline - time.monotonic(), 1)
        try:
            code = subprocess.run(command, env=env, timeout=remaining).returncode
        except subprocess.TimeoutExpired:
            return 124
        if code:
            return code
    return 0


def _signal_tree(child: subprocess.Popen, signum: int) -> None:
    try:
        import psutil

        for process in psutil.Process(child.pid).children(recursive=True):
            try:
                process.send_signal(signum)
            except psutil.Error:
                pass
    except Exception:
        pass
    killpg = getattr(os, "killpg", None)
    try:
        if killpg is None:
            raise OSError("no process groups on this host")
        killpg(child.pid, signum)
    except OSError:
        try:
            child.send_signal(signum)
        except OSError:
            pass


class Entry:
    def __init__(self, arguments: argparse.Namespace):
        self.arguments = arguments
        self.work_dir = Path(arguments.work_dir)
        self.stop_file = self.work_dir / STOP_REQUEST_NAME
        snapshot = Path(arguments.snapshot_dir) if arguments.snapshot_dir else None
        self.root = snapshot.parent if snapshot is not None else None

    def _record_termination(self, phase: str, signal_name: Optional[str] = None,
                            stop_return_code: Optional[int] = None,
                            child_reaped: Optional[bool] = None) -> Optional[str]:
        if self.root is None:
            return None
        from jarvis_cd.core.execution import record_scheduler_termination

        try:
            return record_scheduler_termination(
                self.root, self.root.name, phase=phase, signal_name=signal_name,
                stop_return_code=stop_return_code, child_reaped=child_reaped)
        except Exception as exc:
            print(f"JARVIS: could not record termination: {exc}", file=sys.stderr)
            return None

    def activate(self) -> None:
        if self.root is None:
            return
        from jarvis_cd.core.execution import activate_scheduler_execution

        identity = allocation_identity()
        provider = self.arguments.provider
        if identity is not None and identity[0] == provider:
            native_id, cluster = identity[1], identity[2]
        else:
            # the local executor's job id is the launcher's pid
            native_id, cluster = str(os.getppid()), None
        activate_scheduler_execution(self.root, self.root.name, provider, native_id, cluster)

    def write_hostfile(self) -> None:
        hosts = allocation_hosts(self.arguments.nodefile or os.environ.get("PSIJ_NODEFILE"),
                                 self.arguments.suffix or "")
        if len(hosts) < self.arguments.nodes:
            raise RuntimeError(
                f"the allocation has {len(hosts)} nodes but the pipeline needs "
                f"{self.arguments.nodes}")
        hostfile = Path(self.arguments.hostfile)
        _write_atomic(hostfile, "".join(f"{host}\n" for host in hosts), 0o644)
        print(f"Wrote hostfile: {hostfile}")
        print("".join(f"{host}\n" for host in hosts), end="", flush=True)

    def terminate(self, child: subprocess.Popen, signal_name: Optional[str],
                  by_file: bool) -> int:
        reason = self._record_termination(phase="received",
                                          signal_name=signal_name or "TERM")
        if reason is None:
            reason = "stop_request" if by_file else "scheduler"
        print(f"JARVIS: termination reason={reason}", flush=True)
        stop_rc: Optional[int] = None
        action = self.arguments.action
        if reason == "stop_request" and action == "run":
            print("JARVIS: ppl stop BEGIN", flush=True)
            stop_rc = _run_steps(_pipeline_commands(self.arguments, "stop"),
                                 _environment(self.arguments, "stop"),
                                 timeout=self.arguments.stop_timeout)
            print(f"JARVIS: ppl stop END rc={stop_rc}", flush=True)
        _signal_tree(child, signal.SIGTERM)
        try:
            child.wait(timeout=self.arguments.stop_grace)
        except subprocess.TimeoutExpired:
            print("JARVIS: pipeline child ignored TERM; sending KILL", flush=True)
            _signal_tree(child, getattr(signal, "SIGKILL", signal.SIGTERM))
            child.wait()
        print("JARVIS: pipeline child reaped", flush=True)
        self._record_termination(phase="stopped", stop_return_code=stop_rc,
                                 child_reaped=True)
        if reason == "stop_request":
            return 0 if not stop_rc else 1
        return 143

    def run_pipeline(self) -> int:
        action = self.arguments.action
        env = _environment(self.arguments, action)
        commands = _pipeline_commands(self.arguments, action)
        code = _run_steps(commands[:-1], env)
        if code:
            return code
        termination = _Termination()
        signal.signal(signal.SIGTERM, termination)
        child = subprocess.Popen(commands[-1], env=env, start_new_session=True)
        while True:
            try:
                code: Optional[int] = child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                code = None
            by_file = code is None and self.stop_file.exists()
            # A scheduler TERM reaches the child too, and it can exit first;
            # the termination is still recorded, not the child's own code.
            if termination.signal_name or by_file:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                return self.terminate(child, termination.signal_name, by_file)
            if code is not None:
                return code

    def main(self) -> int:
        code = 1
        try:
            self.activate()
            self.write_hostfile()
            code = self.run_pipeline()
        except Exception as exc:
            print(f"JARVIS: {exc}", file=sys.stderr, flush=True)
            code = 1
        finally:
            if self.root is not None:
                from jarvis_cd.core.execution import finalize_execution

                print(f"JARVIS: finalizing execution record (exit {code})", flush=True)
                try:
                    finalize_execution(self.root, self.root.name, code)
                except Exception as exc:
                    print(f"Could not persist JARVIS execution state: {exc}",
                          file=sys.stderr, flush=True)
                    code = code or 1
            _write_atomic(self.work_dir / EXIT_CODE_NAME, f"{code}\n", 0o644)
        return code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m jarvis_cd.submit.entry")
    parser.add_argument("--provider", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--hostfile", required=True)
    # local executor: the hosts sealed at submit, since PSIJ_NODEFILE is only localhost
    parser.add_argument("--nodefile")
    parser.add_argument("--suffix")
    parser.add_argument("--nodes", type=int, default=1)
    parser.add_argument("--stop-timeout", type=int, default=60)
    parser.add_argument("--stop-grace", type=int, default=20)
    parser.add_argument("--action", choices=("run", "install"), default="run")
    parser.add_argument("--action-arg", action="append")
    # The job never resolves the mutable current pipeline: concurrent jobs on
    # one JARVIS root would race on it.
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--snapshot-dir")
    source.add_argument("--pipeline-yaml")
    parser.add_argument("--jarvis-root")
    parser.add_argument("--install-on-load", choices=("install", "check", "skip"))
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    return Entry(_parser().parse_args(argv)).main()


if __name__ == "__main__":
    raise SystemExit(main())
