"""
Job scheduler integration for Jarvis-CD.

Provides a base ``Scheduler`` class plus concrete backends (SLURM, PBS, ...)
that turn a pipeline's ``scheduler:`` YAML block into a submittable job
script. The generated script is responsible for:

  1. Constructing a hostfile from the scheduler's allocation
     (e.g. ``$SLURM_JOB_NODELIST``) and writing it to the execution-scoped
     location the pipeline expects.
  2. Running the pipeline's isolated execution snapshot once the hostfile is
     in place.

The hostfile path is exported back onto the pipeline so every package
that resolves ``self.hostfile`` sees the same file the job script will
populate.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional


_DIRECTIVE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_HOST_SUFFIX = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SLURM_NATIVE_ID = re.compile(r"^[0-9]{1,64}$")
_UNRESOLVED_SLURM_ARTIFACT_PATH = (
    "JARVIS could not prove one concrete path for this SLURM filename pattern"
)


@dataclass(frozen=True, slots=True)
class SchedulerArtifactPathResolution:
    """One provider-owned attempt to resolve a scheduler filename pattern."""

    path: Optional[str]
    diagnostic_code: Optional[str] = None
    diagnostic: Optional[str] = None


SchedulerExecutionState = Literal[
    "submitted",
    "running",
    "completed",
    "failed",
    "canceled",
    "missing",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class SchedulerExecutionStatus:
    """One bounded observation of provider-owned execution state."""

    state: SchedulerExecutionState
    terminal: bool
    return_code: Optional[int]
    provider_state: Optional[str]
    diagnostic: Optional[str] = None
    #: which query produced the state: ``queue``, ``accounting`` or ``absent``
    basis: Optional[str] = None
    #: ``available``, ``unavailable`` (no slurmdbd and similar) or ``None``
    accounting: Optional[str] = None

    def __post_init__(self) -> None:
        """Reject internally inconsistent provider observations."""
        terminal_states = {"completed", "failed", "canceled"}
        if self.terminal != (self.state in terminal_states):
            raise ValueError("scheduler observation terminal state is inconsistent")
        if self.state == "completed" and self.return_code != 0:
            raise ValueError("completed scheduler observations require return_code=0")
        if self.state == "failed" and self.return_code in {None, 0}:
            raise ValueError("failed scheduler observations require a nonzero code")
        if self.return_code is not None and (
            isinstance(self.return_code, bool) or not isinstance(self.return_code, int)
        ):
            raise ValueError("scheduler observation return_code is invalid")


SchedulerControlAction = Literal["cancel", "stop"]


class SchedulerControlUnsupported(ValueError):
    """The scheduler provider has no JARVIS cancel or stop command."""


@dataclass(frozen=True, slots=True)
class SchedulerControlResult:
    """The outcome of one provider control command (cancel or stop).

    ``accepted`` means the provider took the request. ``job_ended`` means the
    provider no longer has the job (it finished or was already removed), which
    makes the request a no-op rather than an error. ``job_pending`` means a
    signal could not reach a job that has not started yet.
    """

    action: SchedulerControlAction
    argv: tuple[str, ...]
    return_code: Optional[int]
    diagnostic: Optional[str]
    accepted: bool
    job_ended: bool = False
    job_pending: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the bounded result for execution record metadata."""
        return {
            "action": self.action,
            "command": list(self.argv),
            "return_code": self.return_code,
            "diagnostic": self.diagnostic,
            "accepted": self.accepted,
            "job_ended": self.job_ended,
            "job_pending": self.job_pending,
        }


_DEFAULT_STOP_TIMEOUT_SECONDS = 60
_DEFAULT_STOP_GRACE_SECONDS = 20


def _validated_single_line(value: Any, *, field: str, limit: int = 4096) -> str:
    """Return one bounded printable scheduler value."""
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > limit:
        raise ValueError(f"scheduler.{field} must be a non-empty bounded string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"scheduler.{field} cannot contain control characters")
    return value


#: ``scheduler.name`` values that ask JARVIS to use the host's scheduler
AUTO_SCHEDULER_NAMES = ("auto", "detect")


def resolve_scheduler_name(spec: Optional[Dict[str, Any]]) -> str:
    """The scheduler a ``scheduler:`` block means, in lower case.

    ``name: auto`` (or ``detect``), or no ``name`` at all, picks the
    scheduler whose client is on ``PATH`` (``sbatch``, ``qsub``, ``bsub``,
    ``flux``), so a caller can write one block for every site. Raises
    :class:`JarvisIssueError` (``scheduler_missing`` or
    ``scheduler_unsupported``, exit 5).
    """
    from jarvis_cd.core.issues import JarvisIssueError, make_jarvis_issue

    raw = (spec or {}).get("name")
    name = str(raw).strip().lower() if isinstance(raw, str) and raw.strip() else ""
    requested = name or None
    if name in AUTO_SCHEDULER_NAMES or not name:
        from jarvis_cd.core.sched_info import _CLIENTS, detect_provider

        name = detect_provider() or ""
        if not name:
            raise JarvisIssueError([make_jarvis_issue(
                kind="scheduler_missing",
                subject="scheduler",
                message=(
                    f"scheduler.name is {repr(requested) if requested else 'unset'} (auto) but no "
                    "scheduler client ("
                    + ", ".join(client for client, _provider in _CLIENTS)
                    + ") is on PATH"
                ),
                field="scheduler.name",
                details={"requested": requested,
                         "clients": [client for client, _provider in _CLIENTS]},
                remedy="submit from a host with the site's scheduler client, or "
                "name the scheduler in scheduler.name",
            )])
    if name not in _SCHEDULERS:
        raise JarvisIssueError([make_jarvis_issue(
            kind="scheduler_unsupported",
            subject="scheduler",
            message=f"JARVIS has no {name} scheduler provider yet",
            identity={"provider": name},
            field="scheduler.name",
            details={"requested": requested, "supported": sorted(_SCHEDULERS)},
            remedy="use a supported scheduler",
        )])
    return name


def make_scheduler(
    spec: Dict[str, Any],
    pipeline_shared_dir: Path,
    pipeline_yaml: Optional[str] = None,
    pipeline_name: Optional[str] = None,
    pipeline_snapshot_dir: Optional[Path] = None,
    jarvis_root: Optional[Path] = None,
    pipeline_action: str = "run",
    pipeline_action_args: Optional[List[str]] = None,
    install_on_load: Optional[str] = None,
) -> "Scheduler":
    """Factory: build a Scheduler from a parsed YAML dict.

    :param spec: ``scheduler:`` block from the pipeline YAML
    :param pipeline_shared_dir: pipeline-level shared directory
        (used to default the hostfile path and to drop the job script)
    :param pipeline_yaml: path to the pipeline YAML the job should run
        (None when invoked from a pre-loaded current pipeline)
    :param pipeline_name: pipeline name (used for ``jarvis ppl run`` when
        no YAML path is supplied)
    :param pipeline_snapshot_dir: immutable execution-scoped pipeline input
        directory. When provided, the scheduler runs that snapshot rather than
        reloading mutable named-pipeline state.
    :param jarvis_root: configuration root whose repository selection must be
        retained by a scheduled snapshot.
    :param pipeline_action: the ``jarvis ppl`` command the allocation runs on
        the snapshot: ``run`` (default) or ``install``.
    :param pipeline_action_args: extra trusted arguments for that command.
    :param install_on_load: ``JARVIS_INSTALL_ON_LOAD`` sealed into the run
        command, so the job's load applies the submitter's policy even when
        the scheduler does not export the environment.
    """
    name = resolve_scheduler_name(spec)
    cls = _SCHEDULERS[name]
    if str((spec or {}).get("name") or "").strip().lower() != name:
        spec = {**spec, "name": name}
    return cls(
        spec,
        pipeline_shared_dir,
        pipeline_yaml=pipeline_yaml,
        pipeline_name=pipeline_name,
        pipeline_snapshot_dir=pipeline_snapshot_dir,
        jarvis_root=jarvis_root,
        pipeline_action=pipeline_action,
        pipeline_action_args=pipeline_action_args,
        install_on_load=install_on_load,
    )


class Scheduler:
    """Base class for batch-scheduler integrations."""

    #: scheduler name as it appears in YAML (``slurm``, ``pbs``, ...)
    NAME: str = ""

    #: filename of the generated submission script inside the pipeline
    #: shared directory
    SCRIPT_NAME: str = "submit.sh"

    def __init__(
        self,
        spec: Dict[str, Any],
        pipeline_shared_dir: Path,
        pipeline_yaml: Optional[str] = None,
        pipeline_name: Optional[str] = None,
        pipeline_snapshot_dir: Optional[Path] = None,
        jarvis_root: Optional[Path] = None,
        pipeline_action: str = "run",
        pipeline_action_args: Optional[List[str]] = None,
        install_on_load: Optional[str] = None,
    ):
        self.spec = dict(spec or {})
        if install_on_load is not None and install_on_load not in ("install", "check", "skip"):
            raise ValueError("install_on_load must be 'install', 'check' or 'skip'")
        self.install_on_load = install_on_load
        self.shared_dir = Path(pipeline_shared_dir)
        self.pipeline_yaml = pipeline_yaml
        self.pipeline_name = pipeline_name
        self.pipeline_snapshot_dir = (
            Path(pipeline_snapshot_dir) if pipeline_snapshot_dir is not None else None
        )
        self.jarvis_root = Path(jarvis_root) if jarvis_root is not None else None
        if pipeline_action not in {"run", "install"}:
            raise ValueError("scheduler pipeline action must be 'run' or 'install'")
        if pipeline_action != "run" and self.pipeline_snapshot_dir is None:
            raise ValueError("a scheduler install job requires an execution snapshot")
        self.pipeline_action = pipeline_action
        self.pipeline_action_args = [
            _validated_single_line(argument, field="pipeline_action_args")
            for argument in (pipeline_action_args or [])
        ]
        stop_timeout = self.spec.get("stop_timeout", _DEFAULT_STOP_TIMEOUT_SECONDS)
        if (
            isinstance(stop_timeout, bool)
            or not isinstance(stop_timeout, int)
            or not 1 <= stop_timeout <= 86_400
        ):
            raise ValueError("scheduler.stop_timeout must be 1..86400 seconds")
        stop_grace = self.spec.get("stop_grace", _DEFAULT_STOP_GRACE_SECONDS)
        if (
            isinstance(stop_grace, bool)
            or not isinstance(stop_grace, int)
            or not 0 <= stop_grace <= 3_600
        ):
            raise ValueError("scheduler.stop_grace must be 0..3600 seconds")
        self.stop_timeout = stop_timeout
        self.stop_grace = stop_grace

        default_hostfile = str(self.shared_dir / "hostfile.txt")
        hostfile = self.spec.get("hostfile") or default_hostfile
        validated_hostfile = _validated_single_line(
            hostfile,
            field="hostfile",
        )
        self.hostfile = os.path.expandvars(validated_hostfile)
        _validated_single_line(self.hostfile, field="hostfile")

    @property
    def script_path(self) -> Path:
        return self.shared_dir / self.SCRIPT_NAME

    def render(self) -> str:
        """Render the submission script as a string."""
        raise NotImplementedError

    def write_script(self) -> Path:
        """Durably write the execution-scoped submission script."""
        self.shared_dir.mkdir(parents=True, exist_ok=True)
        path = self.script_path
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.shared_dir,
            prefix=f".{path.name}.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        descriptor_owned = True
        try:
            try:
                stream = os.fdopen(
                    descriptor,
                    "w",
                    encoding="utf-8",
                    newline="\n",
                )
            except BaseException:
                os.close(descriptor)
                descriptor_owned = False
                raise
            descriptor_owned = False
            with stream:
                stream.write(self.render())
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.chmod(0o700)
            os.replace(temporary_path, path)
            if os.name != "nt":
                directory = os.open(
                    self.shared_dir,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if descriptor_owned:
                os.close(descriptor)
            temporary_path.unlink(missing_ok=True)
        return path

    def submit_command(self, wait: bool = False) -> List[str]:
        """Argv used to submit the rendered script.

        :param wait: when True, ask the scheduler to block until the job
            finishes (e.g. ``sbatch --wait``). Backends that have no
            equivalent ignore the flag.
        """
        raise NotImplementedError

    def parse_submission_output(self, stdout: str) -> Dict[str, Any]:
        """Parse provider-owned submission output into stable metadata.

        Scheduler implementations must reject output that cannot be tied to a
        submission made by their own command.  Callers must never infer job
        identities from arbitrary application stdout.
        """
        raise NotImplementedError

    @classmethod
    def query_execution(cls, native_id: str) -> SchedulerExecutionStatus:
        """Query one provider-native execution identity."""
        del native_id
        return SchedulerExecutionStatus(
            state="unknown",
            terminal=False,
            return_code=None,
            provider_state=None,
            diagnostic=f"JARVIS cannot query scheduler provider {cls.NAME!r}",
        )

    @classmethod
    def query_queue(cls, native_id: str) -> Optional[Dict[str, Any]]:
        """Queue facts for a pending job; ``None`` when the provider has none."""
        del native_id
        return None

    @classmethod
    def partition_facts(cls) -> List[Dict[str, Any]]:
        """Partitions (queues) with node counts and limits, when known."""
        return []

    @classmethod
    def move_argv(
        cls, native_id: str, queue: str, *, cluster: Optional[str] = None
    ) -> List[str]:
        """Argv that moves a pending job to another queue."""
        del native_id, queue, cluster
        raise SchedulerControlUnsupported(
            f"JARVIS has no move command for scheduler provider {cls.NAME!r}"
        )

    @classmethod
    def interpret_move(
        cls,
        completed: Optional[subprocess.CompletedProcess[str]],
        diagnostic: Optional[str],
    ) -> tuple[bool, Optional[str], Optional[str]]:
        """(accepted, refusal reason, diagnostic) for one move command."""
        if completed is None:
            return False, "scheduler_unavailable", diagnostic
        if completed.returncode == 0:
            return True, None, None
        return False, "scheduler_rejected", (completed.stderr or "")[:2048]

    @classmethod
    def control_argv(
        cls,
        action: SchedulerControlAction,
        native_id: str,
        *,
        cluster: Optional[str] = None,
    ) -> List[str]:
        """Argv that cancels the job or delivers TERM to its batch shell."""
        del native_id, cluster
        raise SchedulerControlUnsupported(
            f"JARVIS has no {action} command for scheduler provider {cls.NAME!r}"
        )

    @classmethod
    def interpret_control(
        cls,
        action: SchedulerControlAction,
        argv: List[str],
        completed: Optional[subprocess.CompletedProcess[str]],
        diagnostic: Optional[str],
    ) -> SchedulerControlResult:
        """Map a provider control command's exit into a typed result."""
        return SchedulerControlResult(
            action=action,
            argv=tuple(argv),
            return_code=None if completed is None else completed.returncode,
            diagnostic=diagnostic,
            accepted=completed is not None and completed.returncode == 0,
        )

    @property
    def array_requested(self) -> bool:
        """Return whether this submission can create an array of jobs."""
        return False

    @classmethod
    def resolve_artifact_path(
        cls,
        path_pattern: str,
        *,
        native_id: str,
        array_requested: bool = False,
    ) -> SchedulerArtifactPathResolution:
        """Resolve one provider filename pattern from trusted submission identity."""
        del native_id, array_requested
        return SchedulerArtifactPathResolution(
            path=_validated_single_line(path_pattern, field="artifact_path")
        )

    def _snapshot_environment(self) -> str:
        """``env`` prefix that pins a jarvis CLI call to the execution snapshot."""
        assert self.pipeline_snapshot_dir is not None
        environment = [
            "env",
            "JARVIS_PIPELINE_SNAPSHOT_DIR="
            + shlex.quote(str(self.pipeline_snapshot_dir)),
        ]
        if self.jarvis_root is not None:
            environment.append("JARVIS_ROOT=" + shlex.quote(str(self.jarvis_root)))
        if self.install_on_load is not None and self.pipeline_action == "run":
            environment.append("JARVIS_INSTALL_ON_LOAD=" + self.install_on_load)
        return " ".join(environment)

    def _pipeline_invocation(self) -> str:
        """Shell snippet that runs the pipeline once the hostfile is in
        place. Always points jarvis at the persisted YAML when one was
        supplied, falling back to ``jarvis ppl run`` against the
        currently-loaded pipeline otherwise.
        """
        jarvis_cli = f"{shlex.quote(sys.executable)} -m jarvis_cd.core.cli"
        if self.pipeline_snapshot_dir is not None:
            snapshot_yaml = self.pipeline_snapshot_dir / "pipeline.yaml"
            arguments = "".join(
                f" {shlex.quote(argument)}" for argument in self.pipeline_action_args
            )
            return (
                self._snapshot_environment()
                + f" {jarvis_cli} ppl {self.pipeline_action} yaml "
                + f"{shlex.quote(str(snapshot_yaml))}{arguments}"
            )
        if self.pipeline_yaml:
            return f"{jarvis_cli} ppl run yaml {shlex.quote(self.pipeline_yaml)}"
        if self.pipeline_name:
            return (
                f"{jarvis_cli} cd {shlex.quote(self.pipeline_name)} && "
                f"{jarvis_cli} ppl run"
            )
        return f"{jarvis_cli} ppl run"

    def _pipeline_stop_invocation(self) -> Optional[str]:
        """Shell snippet that runs ``jarvis ppl stop`` for the same pipeline.

        Install jobs have no package services to stop, so they return None and
        the TERM handler only signals the running child.
        """
        if self.pipeline_action != "run":
            return None
        jarvis_cli = f"{shlex.quote(sys.executable)} -m jarvis_cd.core.cli"
        if self.pipeline_snapshot_dir is not None:
            snapshot_yaml = self.pipeline_snapshot_dir / "pipeline.yaml"
            return (
                self._snapshot_environment()
                + f" {jarvis_cli} ppl stop yaml {shlex.quote(str(snapshot_yaml))}"
            )
        if self.pipeline_yaml:
            return f"{jarvis_cli} ppl stop yaml {shlex.quote(self.pipeline_yaml)}"
        if self.pipeline_name:
            return (
                f"{jarvis_cli} cd {shlex.quote(self.pipeline_name)} && "
                f"{jarvis_cli} ppl stop"
            )
        return f"{jarvis_cli} ppl stop"

    def _termination_helper(self, arguments: str) -> str:
        """One best-effort call that records TERM handling in the record."""
        if self.pipeline_snapshot_dir is None:
            return ":"
        execution_root = self.pipeline_snapshot_dir.parent
        return (
            f"{shlex.quote(sys.executable)} -m jarvis_cd.core.execution termination "
            f"--execution-root {shlex.quote(str(execution_root))} "
            f"--execution-id {shlex.quote(execution_root.name)} {arguments}"
        )

    def _termination_block(self) -> str:
        """Render the TERM trap that stops the pipeline before the job exits.

        The pipeline runs as a background child that the batch shell waits on,
        so a TERM delivered to the batch shell (``scancel --signal=TERM
        --batch``) interrupts ``wait`` and runs this handler at once. The
        handler records the signal, runs ``jarvis ppl stop``, signals the
        child's whole process tree, waits for it, and exits. The EXIT trap then
        finalizes the service runtimes and the execution record.
        """
        stop_invocation = self._pipeline_stop_invocation()
        if stop_invocation is None:
            stop_block = (
                '        echo "JARVIS: no ppl stop for this job action"\n'
            )
        else:
            stop_block = (
                '        echo "JARVIS: ppl stop BEGIN"\n'
                "        jarvis_stop_rc=0\n"
                "        jarvis_stop_timeout=()\n"
                "        if command -v timeout >/dev/null 2>&1; then\n"
                "            jarvis_stop_timeout=(timeout --kill-after=5 "
                f"{self.stop_timeout})\n"
                "        fi\n"
                '        ${jarvis_stop_timeout[@]+"${jarvis_stop_timeout[@]}"} '
                f"{stop_invocation} || jarvis_stop_rc=$?\n"
                '        echo "JARVIS: ppl stop END rc=$jarvis_stop_rc"\n'
            )
        received = self._termination_helper("--phase received --signal TERM")
        stopped = self._termination_helper(
            '--phase stopped ${jarvis_stop_rc:+--stop-return-code "$jarvis_stop_rc"} '
            '--child-reaped "$jarvis_child_reaped"'
        )
        return (
            "jarvis_child_pid=\n"
            "jarvis_descendants() {\n"
            "    local jarvis_parent=$1 jarvis_pid\n"
            '    for jarvis_pid in $(pgrep -P "$jarvis_parent" 2>/dev/null || true); do\n'
            '        jarvis_descendants "$jarvis_pid"\n'
            '        echo "$jarvis_pid"\n'
            "    done\n"
            "}\n"
            "jarvis_signal_child_tree() {\n"
            "    local jarvis_signal=$1 jarvis_pid\n"
            '    [ -n "${jarvis_child_pid:-}" ] || return 0\n'
            '    for jarvis_pid in $(jarvis_descendants "$jarvis_child_pid") '
            '"$jarvis_child_pid"; do\n'
            '        kill -s "$jarvis_signal" "$jarvis_pid" 2>/dev/null || true\n'
            "    done\n"
            "}\n"
            "jarvis_on_term() {\n"
            "    trap '' TERM INT\n"
            "    jarvis_stop_rc=\n"
            "    jarvis_child_reaped=false\n"
            '    echo "JARVIS: TERM received by the batch shell"\n'
            f"    jarvis_term_reason=$({received} || echo scheduler)\n"
            '    echo "JARVIS: termination reason=$jarvis_term_reason"\n'
            '    if [ -n "${jarvis_child_pid:-}" ]; then\n'
            '        if [ "$jarvis_term_reason" = stop_request ]; then\n'
            f"{stop_block}"
            "        fi\n"
            "        jarvis_signal_child_tree TERM\n"
            f"        jarvis_stop_deadline=$((SECONDS + {self.stop_grace}))\n"
            '        while kill -0 "$jarvis_child_pid" 2>/dev/null '
            '&& [ "$SECONDS" -lt "$jarvis_stop_deadline" ]; do\n'
            "            sleep 1\n"
            "        done\n"
            '        if kill -0 "$jarvis_child_pid" 2>/dev/null; then\n'
            '            echo "JARVIS: pipeline child ignored TERM; sending KILL"\n'
            "            jarvis_signal_child_tree KILL\n"
            "        fi\n"
            '        wait "$jarvis_child_pid" 2>/dev/null || true\n'
            "        jarvis_child_reaped=true\n"
            '        echo "JARVIS: pipeline child reaped"\n'
            "    fi\n"
            f"    {stopped} >/dev/null || true\n"
            '    if [ "$jarvis_term_reason" = stop_request ]; then\n'
            '        if [ "${jarvis_stop_rc:-0}" -eq 0 ]; then\n'
            "            exit 0\n"
            "        fi\n"
            "        exit 1\n"
            "    fi\n"
            "    exit 143\n"
            "}\n"
            "trap jarvis_on_term TERM\n"
        )

    def _pipeline_launch_block(self) -> str:
        """Run the pipeline as a waited child so TERM reaches the batch shell.

        A foreground child would defer the shell's TERM trap until the child
        exited, and a SLURM ``--batch`` signal never reaches the child itself.
        """
        return (
            f"{self._pipeline_invocation()} &\n"
            "jarvis_child_pid=$!\n"
            "jarvis_child_rc=0\n"
            'wait "$jarvis_child_pid" || jarvis_child_rc=$?\n'
            "jarvis_child_pid=\n"
            'if [ "$jarvis_child_rc" -ne 0 ]; then\n'
            '    exit "$jarvis_child_rc"\n'
            "fi\n"
        )

    def _execution_lifecycle_block(self) -> str:
        """Render an EXIT trap that durably finalizes scheduler script state."""
        finalizer = ""
        if self.pipeline_snapshot_dir is not None:
            execution_root = self.pipeline_snapshot_dir.parent
            execution_id = execution_root.name
            python_executable = shlex.quote(sys.executable)
            finalizer = (
                '    echo "JARVIS: finalizing execution record (exit $jarvis_exit_code)"\n'
                f"    if ! {python_executable} -m jarvis_cd.core.execution finalize "
                f"--execution-root {shlex.quote(str(execution_root))} "
                f"--execution-id {shlex.quote(execution_id)} "
                '--return-code "$jarvis_exit_code"; then\n'
                '        echo "Could not persist JARVIS execution state" >&2\n'
                '        if [ "$jarvis_exit_code" -eq 0 ]; then\n'
                "            jarvis_exit_code=1\n"
                "        fi\n"
                "    fi\n"
            )
        return (
            "jarvis_hostfile_tmp=\n"
            "jarvis_finalize_execution() {\n"
            "    jarvis_exit_code=$?\n"
            "    trap - EXIT\n"
            '    if [ -n "${jarvis_hostfile_tmp:-}" ]; then\n'
            '        if ! rm -f -- "$jarvis_hostfile_tmp"; then\n'
            '            echo "Could not remove temporary JARVIS hostfile" >&2\n'
            '            if [ "$jarvis_exit_code" -eq 0 ]; then\n'
            "                jarvis_exit_code=1\n"
            "            fi\n"
            "        fi\n"
            "    fi\n"
            f"{finalizer}"
            '    exit "$jarvis_exit_code"\n'
            "}\n"
            "trap jarvis_finalize_execution EXIT\n"
        )

    def _execution_activation_block(self) -> str:
        """Render provider-specific runtime identity activation when available."""
        return ""


class SlurmScheduler(Scheduler):
    """SLURM (sbatch) backend."""

    NAME = "slurm"
    SCRIPT_NAME = "submit.slurm"

    #: keys in the scheduler block we consume directly; everything else is
    #: treated as a long-form ``--<key>=<value>`` sbatch directive so users
    #: can pass arbitrary SBATCH flags from YAML without us hard-coding
    #: every one.
    _STRUCTURAL_KEYS = {
        "name",
        "hostfile",
        "suffix",
        "sbatch_args",
        "pre_cmds",
        "post_cmds",
        "stop_timeout",
        "stop_grace",
    }

    #: explicit short-key -> SBATCH long-flag map. Anything not in here
    #: that isn't structural is passed as ``--<key>=<value>`` with
    #: underscores rewritten to dashes.
    _EXPLICIT_DIRECTIVES = {
        "job_name": "--job-name",
        "nodes": "--nodes",
        "ntasks": "--ntasks",
        "ntasks_per_node": "--ntasks-per-node",
        "cpus_per_task": "--cpus-per-task",
        "mem": "--mem",
        "time": "--time",
        "partition": "--partition",
        "account": "--account",
        "qos": "--qos",
        "output": "--output",
        "error": "--error",
        "gres": "--gres",
        "gpus": "--gpus",
        "gpus_per_node": "--gpus-per-node",
        "constraint": "--constraint",
        "reservation": "--reservation",
        "exclusive": "--exclusive",
        "mail_user": "--mail-user",
        "mail_type": "--mail-type",
    }

    _PARSABLE_SUBMISSION = re.compile(
        r"^(?P<job_id>[0-9]+)(?:;(?P<cluster>[A-Za-z0-9_.-]+))?$"
    )

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._validate_spec()

    def _validate_spec(self) -> None:
        """Reject directive and hook text that can alter script structure."""
        if len(self.spec) > 128:
            raise ValueError("scheduler contains too many fields")
        for key, value in self.spec.items():
            if not isinstance(key, str) or _DIRECTIVE_KEY.fullmatch(key) is None:
                raise ValueError(f"invalid scheduler directive key: {key!r}")
            if key in {"sbatch_args", "pre_cmds", "post_cmds"}:
                if not isinstance(value, list) or len(value) > 64:
                    raise ValueError(f"scheduler.{key} must be a bounded list")
                for entry in value:
                    rendered = _validated_single_line(entry, field=key)
                    if key == "sbatch_args" and (
                        not rendered.startswith("--")
                        or any(character.isspace() for character in rendered)
                        or "#" in rendered
                    ):
                        raise ValueError(
                            "scheduler.sbatch_args entries must be single --flag tokens"
                        )
                    if key == "sbatch_args" and rendered.split("=", 1)[0] in {
                        "--error",
                        "--output",
                    }:
                        raise ValueError(
                            "scheduler.sbatch_args cannot override output or error"
                        )
                continue
            if value is None or isinstance(value, bool):
                continue
            if isinstance(value, int):
                if value < 0:
                    raise ValueError(f"scheduler.{key} cannot be negative")
                continue
            rendered = _validated_single_line(value, field=key)
            if key != "hostfile" and (
                any(character.isspace() for character in rendered) or "#" in rendered
            ):
                raise ValueError(
                    f"scheduler.{key} must be one printable directive token"
                )
        suffix = self.spec.get("suffix")
        if suffix is not None and _HOST_SUFFIX.fullmatch(str(suffix)) is None:
            raise ValueError("scheduler.suffix is not a valid hostname suffix")

    def _directives(self) -> List[str]:
        lines: List[str] = []
        for key, value in self.spec.items():
            if key in self._STRUCTURAL_KEYS:
                continue
            if value is None or value is False:
                continue
            flag = self._EXPLICIT_DIRECTIVES.get(key, f"--{key.replace('_', '-')}")
            if value is True:
                lines.append(f"#SBATCH {flag}")
            else:
                lines.append(f"#SBATCH {flag}={value}")
        for raw in self.spec.get("sbatch_args", []) or []:
            lines.append(f"#SBATCH {raw}")
        return lines

    def render(self) -> str:
        directives = "\n".join(self._directives())
        pre = "\n".join(self.spec.get("pre_cmds", []) or [])
        post = "\n".join(self.spec.get("post_cmds", []) or [])
        hostfile = shlex.quote(self.hostfile)
        python_executable = shlex.quote(sys.executable)
        invocation = self._pipeline_launch_block()
        lifecycle = self._execution_lifecycle_block()
        termination = self._termination_block()
        activation = self._execution_activation_block()

        # Build hostfile from the allocation. scontrol expands the
        # compact nodelist into one host per line; that's exactly what
        # jarvis.util.hostfile.Hostfile consumes. When `suffix:` is set,
        # append it to each hostname — used to redirect traffic onto a
        # secondary NIC (e.g. `ares-comp-1` -> `ares-comp-1-40g` for the
        # 40GbE interface).
        suffix = self.spec.get("suffix")
        if suffix:
            hostnames_cmd = (
                f'scontrol show hostnames "$SLURM_JOB_NODELIST" '
                f"| awk -v s={shlex.quote(str(suffix))} '{{print $0 s}}'"
            )
        else:
            hostnames_cmd = 'scontrol show hostnames "$SLURM_JOB_NODELIST"'
        hostfile_block = (
            f"jarvis_hostfile={hostfile}\n"
            'jarvis_hostfile_dir=$(dirname -- "$jarvis_hostfile")\n'
            "jarvis_hostfile_name=${jarvis_hostfile##*/}\n"
            'mkdir -p -- "$jarvis_hostfile_dir"\n'
            "jarvis_hostfile_tmp=$(mktemp -- "
            '"$jarvis_hostfile_dir/.${jarvis_hostfile_name}.jarvis.$$.XXXXXX")\n'
            f'if ! {{ {hostnames_cmd}; }} > "$jarvis_hostfile_tmp"; then\n'
            '    echo "Could not expand the SLURM allocation hostfile" >&2\n'
            "    exit 1\n"
            "fi\n"
            'if [ ! -s "$jarvis_hostfile_tmp" ]; then\n'
            '    echo "SLURM allocation produced an empty hostfile" >&2\n'
            "    exit 1\n"
            "fi\n"
            f'{python_executable} - "$jarvis_hostfile_tmp" '
            "\"$jarvis_hostfile\" <<'PY'\n"
            "import errno\n"
            "import os\n"
            "import sys\n"
            "\n"
            "temporary_path, final_path = sys.argv[1:]\n"
            "with open(temporary_path, 'rb') as stream:\n"
            "    os.fsync(stream.fileno())\n"
            "os.replace(temporary_path, final_path)\n"
            "directory_path = os.path.dirname(final_path) or '.'\n"
            "unsupported = {errno.EBADF, errno.EINVAL}\n"
            "for name in ('ENOTSUP', 'EOPNOTSUPP'):\n"
            "    value = getattr(errno, name, None)\n"
            "    if value is not None:\n"
            "        unsupported.add(value)\n"
            "try:\n"
            "    directory = os.open(\n"
            "        directory_path,\n"
            "        os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0),\n"
            "    )\n"
            "except OSError as error:\n"
            "    if error.errno not in unsupported:\n"
            "        raise\n"
            "else:\n"
            "    try:\n"
            "        os.fsync(directory)\n"
            "    except OSError as error:\n"
            "        if error.errno not in unsupported:\n"
            "            raise\n"
            "    finally:\n"
            "        os.close(directory)\n"
            "PY\n"
            "jarvis_hostfile_tmp=\n"
            'echo "Wrote hostfile: $jarvis_hostfile"\n'
            'cat -- "$jarvis_hostfile"\n'
        )

        script = (
            "#!/bin/bash\n"
            f"{directives}\n"
            "\n"
            "set -euo pipefail\n"
            "\n"
            f"{lifecycle}"
            f"{termination}"
            f"{activation}"
            "\n"
            "# --- Pre-run hooks ---\n"
            f"{pre}\n"
            "\n"
            "# --- Build hostfile from SLURM allocation ---\n"
            f"{hostfile_block}"
            "\n"
            "# --- Run the Jarvis pipeline as a waited child (TERM-safe) ---\n"
            f"{invocation}"
            "\n"
            "# --- Post-run hooks ---\n"
            f"{post}\n"
        )
        return script

    def _execution_activation_block(self) -> str:
        """Bind the allocation's trusted SLURM identity before user hooks."""
        if self.pipeline_snapshot_dir is None:
            return ""
        execution_root = self.pipeline_snapshot_dir.parent
        execution_id = execution_root.name
        python_executable = shlex.quote(sys.executable)
        return (
            "jarvis_scheduler_cluster_args=()\n"
            'if [ -n "${SLURM_CLUSTER_NAME:-}" ]; then\n'
            '    jarvis_scheduler_cluster_args=(--cluster "$SLURM_CLUSTER_NAME")\n'
            "fi\n"
            f"{python_executable} -m jarvis_cd.core.execution activate "
            f"--execution-root {shlex.quote(str(execution_root))} "
            f"--execution-id {shlex.quote(execution_id)} "
            f"--provider {shlex.quote(self.NAME)} "
            '--native-id "$SLURM_JOB_ID" "${jarvis_scheduler_cluster_args[@]}"\n'
        )

    def submit_command(self, wait: bool = False) -> List[str]:
        # ``--parsable`` is the provider boundary: stdout is a stable
        # ``job_id[;cluster]`` record rather than human-oriented text.
        # Command-line output/error options override both inherited SBATCH_*
        # variables and script directives. Removing SBATCH_ARRAY_INX prevents
        # an ambient shell variable from silently turning a scalar submission
        # into an array whose log paths require task identity.
        cmd = ["env", "-u", "SBATCH_ARRAY_INX", "sbatch", "--parsable"]
        if wait:
            cmd.append("--wait")
        for key in ("output", "error"):
            value = self.spec.get(key)
            if value is not None:
                cmd.append(f"--{key}={value}")
        cmd.append(str(self.script_path))
        return cmd

    def parse_submission_output(self, stdout: str) -> Dict[str, Any]:
        """Parse the exact record produced by ``sbatch --parsable``."""
        value = stdout.strip()
        match = self._PARSABLE_SUBMISSION.fullmatch(value)
        if match is None:
            raise ValueError(
                "SLURM submission did not return a valid "
                "`sbatch --parsable` job identity"
            )
        return {
            "provider": self.NAME,
            "scheduler_job_id": match.group("job_id"),
            "scheduler_cluster": match.group("cluster"),
            "identity_source": "scheduler_submit_api",
        }

    @classmethod
    def query_execution(cls, native_id: str) -> SchedulerExecutionStatus:
        """Query live and accounting SLURM state for one exact scalar job."""
        if _SLURM_NATIVE_ID.fullmatch(native_id) is None:
            raise ValueError("SLURM execution query requires a numeric job ID")

        # --states=all keeps ended jobs visible for MinJobAge, so a cancel or
        # an exit is seen even on sites without accounting (no slurmdbd).
        queue, queue_diagnostic = _run_scheduler_query(
            [
                "squeue",
                "--noheader",
                "--states=all",
                "--jobs",
                native_id,
                "--format=%i|%T",
            ]
        )
        queue_reports_absent = False
        if queue is not None and queue.returncode == 0:
            queue_record = _matching_slurm_status_line(queue.stdout, native_id, 2)
            if queue_record is not None:
                observed = _slurm_execution_status(
                    queue_record[1],
                    return_code=None,
                )
                return_code = None
                if observed.terminal:
                    return_code = _slurm_control_exit_code(native_id)
                    observed = _slurm_execution_status(
                        queue_record[1], return_code=return_code
                    )
                return SchedulerExecutionStatus(
                    state=observed.state,
                    terminal=observed.terminal,
                    return_code=observed.return_code,
                    provider_state=observed.provider_state,
                    diagnostic=observed.diagnostic,
                    basis="queue",
                )
            queue_reports_absent = True

        accounting, accounting_diagnostic = _run_scheduler_query(
            [
                "sacct",
                "--noheader",
                "--parsable2",
                "--allocations",
                "--jobs",
                native_id,
                "--format=JobIDRaw,State,ExitCode",
            ]
        )
        if accounting is not None and accounting.returncode == 0:
            accounting_record = _matching_slurm_status_line(
                accounting.stdout,
                native_id,
                3,
            )
            if accounting_record is None:
                return SchedulerExecutionStatus(
                    state="missing",
                    terminal=False,
                    return_code=None,
                    provider_state=None,
                    diagnostic="SLURM accounting has no record for the accepted job",
                    basis="accounting",
                    accounting="available",
                )
            observed = _slurm_execution_status(
                accounting_record[1],
                return_code=_parse_slurm_exit_code(accounting_record[2]),
            )
            return SchedulerExecutionStatus(
                state=observed.state,
                terminal=observed.terminal,
                return_code=observed.return_code,
                provider_state=observed.provider_state,
                basis="accounting",
                accounting="available",
            )

        accounting_state = (
            "unavailable" if _slurm_accounting_unavailable(accounting) else None
        )
        if queue_reports_absent or _slurm_queue_reports_missing(queue):
            return SchedulerExecutionStatus(
                state="missing",
                terminal=False,
                return_code=None,
                provider_state=None,
                diagnostic=(
                    "SLURM no longer reports the accepted job; accounting did not "
                    "provide a historical allocation record"
                ),
                basis="absent",
                accounting=accounting_state,
            )

        diagnostics = [
            value
            for value in (queue_diagnostic, accounting_diagnostic)
            if value is not None
        ]
        return SchedulerExecutionStatus(
            state="unknown",
            terminal=False,
            return_code=None,
            provider_state=None,
            diagnostic="; ".join(diagnostics) or "SLURM state query was unavailable",
        )

    _JOB_HOLD_REASONS = (
        "dependency",
        "begintime",
        "jobheld",
        "held",
        "jobarraytasklimit",
    )
    _LIMIT_REASONS = ("assoc", "qos", "partitiontimelimit", "partitionnodelimit")

    @classmethod
    def query_queue(cls, native_id: str) -> Optional[Dict[str, Any]]:
        """Queue facts for one pending job, or ``None`` when it is not pending.

        Returns a ``jarvis.scheduler.queue.v1`` document: the job's partition,
        pending reason, the scheduler's estimated start, and every other
        available partition with its idle nodes and whether the job ``fits``
        there (enough idle nodes now and a long enough time limit).
        """
        if _SLURM_NATIVE_ID.fullmatch(native_id) is None:
            raise ValueError("SLURM queue query requires a numeric job ID")
        job, _diagnostic = _run_scheduler_query(
            [
                "squeue",
                "--noheader",
                "--states=PENDING",
                "--jobs",
                native_id,
                "--format=%i|%P|%r|%S|%D|%l",
            ]
        )
        if job is None or job.returncode != 0:
            return None
        row = _matching_slurm_status_line(job.stdout, native_id, 6)
        if row is None:
            return None
        _job_id, partition, reason, start_text, nodes_text, limit_text = row
        nodes = int(nodes_text) if nodes_text.isdigit() else None
        limit_s = _parse_slurm_duration(limit_text)
        estimated_start: Optional[str] = None
        starts_in_s: Optional[int] = None
        try:
            start: Optional[datetime] = datetime.fromisoformat(start_text)
        except ValueError:
            start = None
        if start is not None:
            estimated_start = start.isoformat()
            starts_in_s = max(0, int((start - datetime.now()).total_seconds()))
        lowered = reason.lower()
        if any(lowered.startswith(prefix) for prefix in cls._JOB_HOLD_REASONS):
            reason_kind = "job_hold"
        elif any(lowered.startswith(prefix) for prefix in cls._LIMIT_REASONS):
            reason_kind = "limit"
        elif lowered.startswith(("resources", "priority", "reqnodenotavail", "reservation")):
            reason_kind = "resources"
        else:
            reason_kind = "other"
        current = {item for item in partition.split(",") if item}
        partitions = [
            entry
            for entry in cls.partition_facts()
            if entry["partition"] not in current and entry["available"] == "up"
        ]
        for entry in partitions:
            time_ok = entry["max_time_s"] is None or (
                limit_s is not None and entry["max_time_s"] >= limit_s
            )
            entry["fits"] = bool(
                nodes is not None and entry["idle_nodes"] >= nodes and time_ok
            )
        partitions.sort(key=lambda entry: (not entry["fits"], -entry["idle_nodes"]))
        return {
            "schema_version": "jarvis.scheduler.queue.v1",
            "provider": cls.NAME,
            "native_id": native_id,
            "observed_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "state": "pending",
            "partition": partition,
            "reason": reason,
            "reason_kind": reason_kind,
            "estimated_start": estimated_start,
            "starts_in_s": starts_in_s,
            "nodes": nodes,
            "time_limit": limit_text,
            "time_limit_s": limit_s,
            "partitions": partitions,
            "move_supported": True,
        }

    @classmethod
    def partition_facts(cls) -> List[Dict[str, Any]]:
        """Every partition with availability, node counts and time limit."""
        listing, _diagnostic = _run_scheduler_query(
            ["sinfo", "--noheader", "--format=%P|%a|%F|%l|%c|%m|%G|%X|%Y|%Z"]
        )

        def count(text: str) -> Optional[int]:
            text = text.rstrip("+")
            return int(text) if text.isdigit() else None

        def smallest(current: Optional[int], new: Optional[int]) -> Optional[int]:
            if current is None or new is None:
                return new if current is None else current
            return min(current, new)
        if listing is None or listing.returncode != 0:
            return []
        merged: Dict[str, Dict[str, Any]] = {}
        for raw_line in listing.stdout.splitlines():
            fields = [field.strip() for field in raw_line.split("|")]
            if len(fields) < 4 or not fields[0]:
                continue
            name = fields[0]
            default = name.endswith("*")
            name = name.rstrip("*")
            counts = fields[2].split("/")
            if len(counts) != 4 or not all(item.isdigit() for item in counts):
                continue
            entry = merged.setdefault(
                name,
                {
                    "partition": name,
                    "default": default,
                    "available": fields[1],
                    "allocated_nodes": 0,
                    "idle_nodes": 0,
                    "other_nodes": 0,
                    "total_nodes": 0,
                    "max_time": fields[3],
                    "max_time_s": _parse_slurm_duration(fields[3]),
                    "cpus_per_node": None,
                    "sockets_per_node": None,
                    "cores_per_socket": None,
                    "threads_per_core": None,
                    "physical_cores_per_node": None,
                    "memory_mb_per_node": None,
                    "gres": None,
                    "heterogeneous": False,
                },
            )
            seen = "_seen" in entry
            entry["_seen"] = True
            for key, value in zip(
                ("allocated_nodes", "idle_nodes", "other_nodes", "total_nodes"),
                counts,
            ):
                entry[key] += int(value)
            # One sinfo line per node configuration. A partition with several
            # reports the smallest node, so sizing from it fits every node.
            if len(fields) >= 7:
                line = {"cpus_per_node": count(fields[4]),
                        "memory_mb_per_node": count(fields[5])}
                if len(fields) >= 10:
                    line["sockets_per_node"] = count(fields[7])
                    line["cores_per_socket"] = count(fields[8])
                    line["threads_per_core"] = count(fields[9])
                    if line["sockets_per_node"] and line["cores_per_socket"]:
                        line["physical_cores_per_node"] = (
                            line["sockets_per_node"] * line["cores_per_socket"]
                        )
                for key, value in line.items():
                    if seen and entry[key] is not None and value != entry[key]:
                        entry["heterogeneous"] = True
                    entry[key] = smallest(entry[key], value) if seen else value
                if entry["gres"] is None:
                    entry["gres"] = None if fields[6] in {"", "(null)"} else fields[6]
        for entry in merged.values():
            entry.pop("_seen", None)
        return list(merged.values())

    @classmethod
    def move_argv(
        cls, native_id: str, queue: str, *, cluster: Optional[str] = None
    ) -> List[str]:
        """``scontrol update`` argv that moves a pending job to ``queue``."""
        if _SLURM_NATIVE_ID.fullmatch(native_id) is None:
            raise ValueError("SLURM move requires a numeric job ID")
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", queue) is None:
            raise ValueError("SLURM partition name is invalid")
        argv = ["scontrol"]
        if cluster is not None:
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", cluster) is None:
                raise ValueError("SLURM cluster name is invalid")
            argv.append(f"--clusters={cluster}")
        argv.extend(["update", f"JobId={native_id}", f"Partition={queue}"])
        return argv

    _MOVE_REFUSALS = (
        ("no longer pending", "not_pending"),
        ("already completing or completed", "not_pending"),
        ("invalid job id", "not_pending"),
        ("invalid partition", "invalid_queue"),
        ("node configuration is not available", "does_not_fit"),
        ("partition configuration not available", "does_not_fit"),
        ("time limit", "does_not_fit"),
        ("access denied", "not_permitted"),
        ("not authorized", "not_permitted"),
        ("permission denied", "not_permitted"),
    )

    @classmethod
    def interpret_move(
        cls,
        completed: Optional[subprocess.CompletedProcess[str]],
        diagnostic: Optional[str],
    ) -> tuple[bool, Optional[str], Optional[str]]:
        """(accepted, refusal reason, bounded diagnostic) for one move."""
        if completed is None:
            return False, "scheduler_unavailable", diagnostic
        text = " ".join(
            part.strip() for part in (completed.stderr, completed.stdout) if part
        )
        if completed.returncode == 0:
            return True, None, text[:2048] or None
        lowered = text.lower()
        for marker, reason in cls._MOVE_REFUSALS:
            if marker in lowered:
                return False, reason, text[:2048]
        return False, "scheduler_rejected", text[:2048] or diagnostic

    @classmethod
    def control_argv(
        cls,
        action: SchedulerControlAction,
        native_id: str,
        *,
        cluster: Optional[str] = None,
    ) -> List[str]:
        """``scancel`` argv for cancel, or a TERM to the batch shell for stop."""
        if _SLURM_NATIVE_ID.fullmatch(native_id) is None:
            raise ValueError("SLURM control requires a numeric job ID")
        argv = ["scancel"]
        if action == "stop":
            # --batch signals only the batch shell, whose TERM trap runs
            # `jarvis ppl stop` and then signals the pipeline's process tree.
            argv.extend(["--signal=TERM", "--batch"])
        elif action != "cancel":
            raise ValueError(f"unknown scheduler control action {action!r}")
        if cluster is not None:
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", cluster) is None:
                raise ValueError("SLURM cluster name is invalid")
            argv.append(f"--clusters={cluster}")
        argv.append(native_id)
        return argv

    _ENDED_JOB_DIAGNOSTICS = (
        "invalid job id",
        "already completing or completed",
        "job/step already completing",
        "job has already finished",
    )
    _PENDING_JOB_DIAGNOSTICS = ("pending",)

    @classmethod
    def interpret_control(
        cls,
        action: SchedulerControlAction,
        argv: List[str],
        completed: Optional[subprocess.CompletedProcess[str]],
        diagnostic: Optional[str],
    ) -> SchedulerControlResult:
        """Treat 'job already gone' as an ended job rather than a failure."""
        if completed is None:
            return SchedulerControlResult(
                action=action,
                argv=tuple(argv),
                return_code=None,
                diagnostic=diagnostic,
                accepted=False,
            )
        text = " ".join(
            part.strip() for part in (completed.stderr, completed.stdout) if part
        )
        bounded = text[:2048] or diagnostic
        lowered = text.lower()
        ended = any(marker in lowered for marker in cls._ENDED_JOB_DIAGNOSTICS)
        pending = action == "stop" and any(
            marker in lowered for marker in cls._PENDING_JOB_DIAGNOSTICS
        )
        return SchedulerControlResult(
            action=action,
            argv=tuple(argv),
            return_code=completed.returncode,
            diagnostic=bounded,
            accepted=completed.returncode == 0 and not ended and not pending,
            job_ended=ended,
            job_pending=pending,
        )

    @property
    def array_requested(self) -> bool:
        """Return whether spec or raw arguments request a SLURM job array."""
        configured = self.spec.get("array")
        if configured is not None and configured is not False and configured != "":
            return True
        return any(
            str(argument).split("=", 1)[0] == "--array"
            for argument in self.spec.get("sbatch_args", []) or []
        )

    @classmethod
    def resolve_artifact_path(
        cls,
        path_pattern: str,
        *,
        native_id: str,
        array_requested: bool = False,
    ) -> SchedulerArtifactPathResolution:
        """Resolve only SLURM path tokens proven by scalar submission output.

        ``%j`` and width-qualified forms are bound to the parsed native job ID.
        ``%%`` is a literal percent and is never reinterpreted. Other percent
        replacement symbols depend on allocation or task state, so JARVIS
        leaves their artifact location unset rather than guessing.
        """
        pattern = _validated_single_line(path_pattern, field="artifact_path")
        if _SLURM_NATIVE_ID.fullmatch(native_id) is None:
            raise ValueError("SLURM artifact resolution requires a numeric job ID")
        if "\\" in pattern:
            return SchedulerArtifactPathResolution(
                path=None,
                diagnostic_code="slurm_artifact_path_unresolved",
                diagnostic=_UNRESOLVED_SLURM_ARTIFACT_PATH,
            )
        rendered: list[str] = []
        index = 0
        while index < len(pattern):
            if pattern[index] != "%":
                rendered.append(pattern[index])
                index += 1
                continue
            if index + 1 < len(pattern) and pattern[index + 1] == "%":
                rendered.append("%")
                index += 2
                continue
            token_end = index + 1
            while token_end < len(pattern) and "0" <= pattern[token_end] <= "9":
                token_end += 1
            if token_end >= len(pattern) or not pattern[token_end].isalpha():
                return SchedulerArtifactPathResolution(
                    path=None,
                    diagnostic_code="slurm_artifact_path_unresolved",
                    diagnostic=_UNRESOLVED_SLURM_ARTIFACT_PATH,
                )
            token = pattern[token_end]
            if token != "j" or array_requested:
                return SchedulerArtifactPathResolution(
                    path=None,
                    diagnostic_code="slurm_artifact_path_unresolved",
                    diagnostic=_UNRESOLVED_SLURM_ARTIFACT_PATH,
                )
            width_text = pattern[index + 1 : token_end]
            width = 0
            if width_text:
                width = min(int(width_text), 10)
            rendered.append(native_id.zfill(width))
            index = token_end + 1
        return SchedulerArtifactPathResolution(path="".join(rendered))


_SCHEDULERS: Dict[str, type[Scheduler]] = {
    SlurmScheduler.NAME: SlurmScheduler,
}


def scheduler_control_argv(
    provider: str,
    action: SchedulerControlAction,
    native_id: str,
    *,
    cluster: Optional[str] = None,
) -> List[str]:
    """Validated provider argv for cancel or stop, without running it.

    Raises ``SchedulerControlUnsupported`` when JARVIS has no command for the
    provider, so callers can refuse before recording any request.
    """
    scheduler_type = _SCHEDULERS.get(provider.strip().lower())
    if scheduler_type is None:
        raise SchedulerControlUnsupported(
            f"JARVIS has no {action} command for scheduler provider {provider!r}"
        )
    return scheduler_type.control_argv(action, native_id, cluster=cluster)


def control_scheduler_execution(
    provider: str,
    action: SchedulerControlAction,
    native_id: str,
    *,
    cluster: Optional[str] = None,
) -> SchedulerControlResult:
    """Run one bounded provider cancel or stop command without a shell."""
    argv = scheduler_control_argv(provider, action, native_id, cluster=cluster)
    scheduler_type = _SCHEDULERS[provider.strip().lower()]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return scheduler_type.interpret_control(
            action, argv, None, f"{argv[0]} failed: {str(exc)[:1024]}"
        )
    return scheduler_type.interpret_control(action, argv, completed, None)


def _run_scheduler_query(
    argv: list[str],
) -> tuple[Optional[subprocess.CompletedProcess[str]], Optional[str]]:
    """Run one bounded scheduler query without invoking a shell."""
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{argv[0]} query failed: {str(exc)[:1024]}"
    if completed.returncode == 0:
        return completed, None
    diagnostic = (completed.stderr or completed.stdout or "").strip()
    if len(diagnostic) > 1024:
        diagnostic = "[truncated] " + diagnostic[-1024:]
    return completed, (
        f"{argv[0]} query exited {completed.returncode}"
        + (f": {diagnostic}" if diagnostic else "")
    )


def _matching_slurm_status_line(
    stdout: str,
    native_id: str,
    field_count: int,
) -> Optional[list[str]]:
    """Return the exact allocation row while ignoring step records."""
    for raw_line in stdout.splitlines():
        fields = [field.strip() for field in raw_line.strip().strip("|").split("|")]
        if len(fields) == field_count and fields[0] == native_id:
            return fields
    return None


def _slurm_queue_reports_missing(
    completed: Optional[subprocess.CompletedProcess[str]],
) -> bool:
    """Recognize SLURM's exact absent-job response without broad error guessing."""
    if completed is None or completed.returncode == 0:
        return False
    diagnostic = (completed.stderr or completed.stdout or "").strip().lower()
    return diagnostic == "slurm_load_jobs error: invalid job id specified"


def _parse_slurm_exit_code(value: str) -> Optional[int]:
    """Parse SLURM's ``exit:signal`` field without guessing malformed data."""
    match = re.fullmatch(r"([0-9]{1,10}):([0-9]{1,10})", value.strip())
    if match is None:
        return None
    exit_code = int(match.group(1))
    signal = int(match.group(2))
    if exit_code != 0:
        return exit_code
    if signal != 0:
        return 128 + signal
    return 0


def _slurm_accounting_unavailable(
    completed: Optional[subprocess.CompletedProcess[str]],
) -> bool:
    """Whether sacct failed because the site runs no accounting storage."""
    if completed is None or completed.returncode == 0:
        return False
    text = ((completed.stderr or "") + (completed.stdout or "")).lower()
    return "accounting storage is disabled" in text or "slurmdbd" in text


def _slurm_control_exit_code(native_id: str) -> Optional[int]:
    """ExitCode from ``scontrol show job`` while the ended job is still known."""
    shown, _diagnostic = _run_scheduler_query(
        ["scontrol", "show", "job", "--oneliner", native_id]
    )
    if shown is None or shown.returncode != 0:
        return None
    match = re.search(r"(?:^|\s)ExitCode=([0-9]+:[0-9]+)", shown.stdout or "")
    return _parse_slurm_exit_code(match.group(1)) if match else None


def _parse_slurm_duration(value: str) -> Optional[int]:
    """Seconds for a Slurm time limit; ``None`` for unlimited or unknown."""
    text = value.strip()
    if not text or text.upper() in {"UNLIMITED", "INFINITE", "NOT_SET", "N/A"}:
        return None
    days = 0
    if "-" in text:
        day_text, text = text.split("-", 1)
        if not day_text.isdigit():
            return None
        days = int(day_text)
        parts = text.split(":")
        if not all(part.isdigit() for part in parts) or len(parts) > 3:
            return None
        hours, minutes, seconds = ([int(part) for part in parts] + [0, 0])[:3]
    else:
        parts = text.split(":")
        if not all(part.isdigit() for part in parts) or len(parts) > 3:
            return None
        numbers = [int(part) for part in parts]
        if len(numbers) == 1:
            hours, minutes, seconds = 0, numbers[0], 0
        elif len(numbers) == 2:
            hours, minutes, seconds = 0, numbers[0], numbers[1]
        else:
            hours, minutes, seconds = numbers
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _slurm_execution_status(
    provider_state: str,
    *,
    return_code: Optional[int],
) -> SchedulerExecutionStatus:
    """Map a SLURM lifecycle value into JARVIS execution semantics."""
    normalized = provider_state.strip().upper().split(maxsplit=1)[0].rstrip("+")
    submitted_states = {
        "PENDING",
        "CONFIGURING",
        "REQUEUED",
        "REQUEUE_FED",
        "REQUEUE_HOLD",
        "RESIZING",
    }
    running_states = {"RUNNING", "COMPLETING", "SIGNALING", "STAGE_OUT", "SUSPENDED"}
    failed_states = {
        "BOOT_FAIL",
        "DEADLINE",
        "FAILED",
        "NODE_FAIL",
        "OUT_OF_MEMORY",
        "PREEMPTED",
        "REVOKED",
        "SPECIAL_EXIT",
        "TIMEOUT",
    }
    if normalized in submitted_states:
        state: SchedulerExecutionState = "submitted"
        terminal = False
        canonical_return_code = None
    elif normalized in running_states:
        state = "running"
        terminal = False
        canonical_return_code = None
    elif normalized == "COMPLETED" and return_code in {None, 0}:
        state = "completed"
        terminal = True
        canonical_return_code = 0
    elif normalized.startswith("CANCELLED"):
        state = "canceled"
        terminal = True
        canonical_return_code = return_code
    elif normalized in failed_states or normalized == "COMPLETED":
        state = "failed"
        terminal = True
        canonical_return_code = return_code if return_code not in {None, 0} else 1
    else:
        state = "unknown"
        terminal = False
        canonical_return_code = None
    return SchedulerExecutionStatus(
        state=state,
        terminal=terminal,
        return_code=canonical_return_code,
        provider_state=normalized or None,
        diagnostic=None,
    )


def query_scheduler_execution(
    provider: str,
    native_id: str,
) -> SchedulerExecutionStatus:
    """Query execution state through the named scheduler provider."""
    scheduler_type = _SCHEDULERS.get(provider.strip().lower())
    if scheduler_type is None:
        return SchedulerExecutionStatus(
            state="unknown",
            terminal=False,
            return_code=None,
            provider_state=None,
            diagnostic=f"JARVIS cannot query scheduler provider {provider!r}",
        )
    return scheduler_type.query_execution(native_id)


def query_scheduler_queue(provider: str, native_id: str) -> Optional[Dict[str, Any]]:
    """Queue facts (``jarvis.scheduler.queue.v1``) for one pending job."""
    scheduler_type = _SCHEDULERS.get(provider.strip().lower())
    if scheduler_type is None:
        return None
    return scheduler_type.query_queue(native_id)


def scheduler_partition_facts(provider: str) -> List[Dict[str, Any]]:
    """Partitions with node counts and limits for one provider."""
    scheduler_type = _SCHEDULERS.get(provider.strip().lower())
    if scheduler_type is None:
        return []
    return scheduler_type.partition_facts()


def move_scheduler_execution(
    provider: str,
    native_id: str,
    queue: str,
    *,
    cluster: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one bounded move of a pending job; returns the typed outcome."""
    scheduler_type = _SCHEDULERS.get(provider.strip().lower())
    if scheduler_type is None:
        raise SchedulerControlUnsupported(
            f"JARVIS has no move command for scheduler provider {provider!r}"
        )
    argv = scheduler_type.move_argv(native_id, queue, cluster=cluster)
    try:
        completed: Optional[subprocess.CompletedProcess[str]] = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        diagnostic = None
    except (OSError, subprocess.TimeoutExpired) as exc:
        completed, diagnostic = None, f"{argv[0]} failed: {str(exc)[:1024]}"
    accepted, reason, detail = scheduler_type.interpret_move(completed, diagnostic)
    return {
        "command": argv,
        "return_code": None if completed is None else completed.returncode,
        "accepted": accepted,
        "refusal": reason,
        "diagnostic": detail,
    }


def resolve_scheduler_artifact_path(
    provider: str,
    path_pattern: str,
    *,
    native_id: str,
    array_requested: bool = False,
) -> SchedulerArtifactPathResolution:
    """Resolve a filename pattern through its explicit scheduler provider."""
    scheduler_type = _SCHEDULERS.get(provider.strip().lower())
    if scheduler_type is None:
        return SchedulerArtifactPathResolution(
            path=None,
            diagnostic_code="scheduler_artifact_provider_unsupported",
            diagnostic="JARVIS has no artifact path resolver for this scheduler",
        )
    return scheduler_type.resolve_artifact_path(
        path_pattern,
        native_id=native_id,
        array_requested=array_requested,
    )
