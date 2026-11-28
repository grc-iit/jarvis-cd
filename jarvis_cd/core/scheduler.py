"""
Batch submission for Jarvis-CD through PSI/J.

A pipeline's ``scheduler:`` block becomes one PSI/J ``JobSpec``: the
resources the whole pipeline needs, the scheduler attributes, and one
program, ``python -m jarvis_cd.submit.entry``, that runs as a single
process on the first allocated node (PSI/J launcher ``single``). The entry
writes the JARVIS hostfile from the allocation, runs the pipeline's
execution snapshot, handles ``jarvis execution stop`` and scheduler TERM,
and finalizes the execution record.

Every scheduler takes the same path:
``JobExecutor.get_instance(name, config=...)`` with ``name`` one of
``slurm``, ``pbs``, ``lsf``, ``flux``, ``cobalt`` or ``local`` (no
scheduler: PSI/J's local executor runs the same spec on this host).
Scheduler-specific queries (partitions, pending-job facts) and the job move
live in :mod:`jarvis_cd.core.queues`.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from jarvis_cd.core import queues


_DIRECTIVE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_HOST_SUFFIX = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_MEMORY = re.compile(r"^([0-9]+)([KMGT]?)B?$", re.IGNORECASE)

#: JARVIS scheduler names; each is also the PSI/J executor name
SUPPORTED_SCHEDULERS = ("slurm", "pbs", "lsf", "flux", "cobalt", "local")
_SCHEDULER_ALIASES = {"none": "local"}

JOBSPEC_SCHEMA = "jarvis.submit.jobspec.v1"
STOP_REQUEST_NAME = "stop-request"
EXIT_CODE_NAME = "exit_code"


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
    #: what produced the state: ``exit_code_file``, ``queue``,
    #: ``psij_exit_code_file`` or ``absent``
    basis: Optional[str] = None

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


class SchedulerSubmitRefused(RuntimeError):
    """The scheduler (through PSI/J) refused the submission."""


@dataclass(frozen=True, slots=True)
class SchedulerControlResult:
    """The outcome of one provider control request (cancel or stop).

    ``accepted`` means the provider took the request. ``job_ended`` means the
    provider no longer has the job (it finished or was already removed), which
    makes the request a no-op rather than an error.
    """

    action: SchedulerControlAction
    argv: tuple[str, ...]
    return_code: Optional[int]
    diagnostic: Optional[str]
    accepted: bool
    job_ended: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the bounded result for execution record metadata."""
        return {
            "action": self.action,
            "command": list(self.argv),
            "return_code": self.return_code,
            "diagnostic": self.diagnostic,
            "accepted": self.accepted,
            "job_ended": self.job_ended,
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
    ``flux``), so a caller can write one block for every site. ``local``
    (alias ``none``) runs the job on this host through PSI/J's local
    executor. Raises :class:`JarvisIssueError` (``scheduler_missing`` or
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
            raise JarvisIssueError(
                [
                    make_jarvis_issue(
                        kind="scheduler_missing",
                        subject="scheduler",
                        message=(
                            f"scheduler.name is {repr(requested) if requested else 'unset'} (auto) but no "
                            "scheduler client ("
                            + ", ".join(client for client, _provider in _CLIENTS)
                            + ") is on PATH"
                        ),
                        field="scheduler.name",
                        details={
                            "requested": requested,
                            "clients": [client for client, _provider in _CLIENTS],
                        },
                        remedy="submit from a host with the site's scheduler client, or "
                        "name the scheduler in scheduler.name (local runs on this host)",
                    )
                ]
            )
    name = _SCHEDULER_ALIASES.get(name, name)
    if name not in SUPPORTED_SCHEDULERS:
        raise JarvisIssueError(
            [
                make_jarvis_issue(
                    kind="scheduler_unsupported",
                    subject="scheduler",
                    message=f"JARVIS has no {name} scheduler provider",
                    identity={"provider": name},
                    field="scheduler.name",
                    details={"requested": requested,
                             "supported": sorted(SUPPORTED_SCHEDULERS)},
                    remedy="use a supported scheduler",
                )
            ]
        )
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
    local_hosts: Optional[List[str]] = None,
) -> "Scheduler":
    """Factory: build the PSI/J submission for a parsed ``scheduler:`` block.

    :param spec: ``scheduler:`` block from the pipeline YAML
    :param pipeline_shared_dir: execution directory: the job's working
        directory, the default hostfile and log location, and the home of
        the job description and PSI/J's own files
    :param pipeline_yaml: path to the pipeline YAML the job should run
        (None when invoked from a pre-loaded current pipeline)
    :param pipeline_name: pipeline name (used for ``jarvis ppl run`` when
        no YAML path is supplied)
    :param pipeline_snapshot_dir: immutable execution-scoped pipeline input
        directory. When provided, the job runs that snapshot rather than
        reloading mutable named-pipeline state.
    :param jarvis_root: configuration root whose repository selection must be
        retained by a scheduled snapshot.
    :param pipeline_action: the ``jarvis ppl`` command the allocation runs on
        the snapshot: ``run`` (default) or ``install``.
    :param pipeline_action_args: extra trusted arguments for that command.
    :param install_on_load: ``JARVIS_INSTALL_ON_LOAD`` sealed into the run
        command, so the job's load applies the submitter's policy even when
        the scheduler does not export the environment.
    :param local_hosts: for ``local`` only, the hosts the job runs on (the
        JARVIS hostfile at submit); a scheduler's allocation sets them otherwise.
    """
    name = resolve_scheduler_name(spec)
    if str((spec or {}).get("name") or "").strip().lower() != name:
        spec = {**spec, "name": name}
    return Scheduler(
        spec,
        pipeline_shared_dir,
        pipeline_yaml=pipeline_yaml,
        pipeline_name=pipeline_name,
        pipeline_snapshot_dir=pipeline_snapshot_dir,
        jarvis_root=jarvis_root,
        pipeline_action=pipeline_action,
        pipeline_action_args=pipeline_action_args,
        install_on_load=install_on_load,
        local_hosts=local_hosts,
    )


#: keys JARVIS consumes itself; they never become scheduler attributes
_STRUCTURAL_KEYS = {
    "name", "hostfile", "suffix", "sbatch_args", "pre_cmds", "post_cmds",
    "stop_timeout", "stop_grace", "output", "error",
}
#: scheduler key -> ResourceSpecV1 field (positive integers)
_RESOURCE_KEYS = {
    "nodes": "node_count",
    "ntasks": "process_count",
    "ntasks_per_node": "processes_per_node",
    "cpus_per_task": "cpu_cores_per_process",
    "gpus_per_task": "gpu_cores_per_process",
}
#: scheduler key -> JobAttributes field
_ATTRIBUTE_KEYS = {
    "partition": "queue_name",
    "account": "account",
    "reservation": "reservation_id",
}
_REFUSED_KEYS = {
    "array": "job arrays are not supported; submit one execution per element",
    "clusters": "multi-cluster submission is not supported; submit from the "
                "cluster's own login node",
    "cluster": "multi-cluster submission is not supported; submit from the "
               "cluster's own login node",
    "hetjob": "heterogeneous jobs are not supported; one execution requests "
              "one homogeneous node set",
    "packjob": "heterogeneous jobs are not supported; one execution requests "
               "one homogeneous node set",
}
_REFUSED_FLAGS = {"array": "array", "clusters": "clusters", "hetjob": "hetjob",
                  "packjob": "packjob", "output": None, "error": None}
_UNLIMITED = {"UNLIMITED", "INFINITE"}


def _positive_int(value: Any, *, field: str) -> Optional[int]:
    if isinstance(value, bool):
        raise ValueError(f"scheduler.{field} must be a whole number")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int):
        raise ValueError(f"scheduler.{field} must be a whole number")
    return value or None


def _memory_bytes(value: Any) -> int:
    match = _MEMORY.fullmatch(str(value).strip())
    if match is None:
        raise ValueError("scheduler.mem must be a size such as 4000 (MB), 4000M or 64G")
    unit = (match.group(2) or "M").upper()
    return int(match.group(1)) * 1024 ** (1 + "KMGT".index(unit))


def _duration_seconds(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        return value * 60
    text = str(value).strip()
    if text.upper() in _UNLIMITED:
        return None
    seconds = queues.parse_slurm_duration(text)
    if seconds is None:
        raise ValueError(
            "scheduler.time must be minutes or [days-]hours:minutes:seconds")
    return seconds


def _check_resource_shape(resources: Dict[str, Any]) -> None:
    """PSI/J's constraint: ``process_count = node_count * processes_per_node``."""
    nodes = resources["node_count"]
    tasks = resources["process_count"]
    per_node = resources["processes_per_node"]
    if nodes and tasks and per_node and tasks != nodes * per_node:
        raise ValueError(
            f"scheduler.ntasks ({tasks}) must equal nodes ({nodes}) x "
            f"ntasks_per_node ({per_node})")
    if tasks and per_node and not nodes and tasks % per_node:
        raise ValueError("scheduler.ntasks must be a multiple of ntasks_per_node")
    if tasks and nodes and not per_node and tasks % nodes:
        raise ValueError(
            "scheduler.ntasks must be a multiple of nodes (PSI/J places the same "
            "number of tasks on every node)")


def _computed_nodes(resources: Dict[str, Any]) -> int:
    if resources["node_count"]:
        return int(resources["node_count"])
    if resources["process_count"] and resources["processes_per_node"]:
        return int(resources["process_count"]) // int(resources["processes_per_node"])
    return 1


def _write_atomic(path: Path, text: str, mode: int = 0o600) -> Path:
    """Durably replace ``path`` with ``text``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.chmod(mode)
        os.replace(temporary_path, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary_path.unlink(missing_ok=True)
    return path


@dataclass(frozen=True)
class SchedulerSubmission:
    """One job PSI/J accepted."""

    job: Any
    executor: Any
    native_id: str
    command: List[str]


class Scheduler:
    """One pipeline submission: a ``scheduler:`` block mapped onto PSI/J."""

    #: the JARVIS scheduler name, also the PSI/J executor name
    NAME: str = ""

    #: the job description JARVIS writes (and binds ``+plan`` to)
    SCRIPT_NAME: str = "psij-job.json"
    PRE_LAUNCH_NAME = "pre-launch.sh"
    POST_LAUNCH_NAME = "post-launch.sh"
    LOCAL_NODEFILE_NAME = "local-nodefile.txt"
    LAUNCHER = "single"

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
        local_hosts: Optional[List[str]] = None,
    ):
        self.spec = dict(spec or {})
        name = str(self.spec.get("name") or "").strip().lower()
        self.NAME = _SCHEDULER_ALIASES.get(name, name)
        if self.NAME not in SUPPORTED_SCHEDULERS:
            raise ValueError(f"unknown scheduler {name!r}")
        # With no scheduler there is no allocation to read, so the job runs
        # on the hosts sealed here (ssh reaches all but the first).
        self.local_hosts: Optional[List[str]] = None
        if self.NAME == "local" and local_hosts:
            self.local_hosts = [
                _validated_single_line(host, field="hostfile", limit=255)
                for host in local_hosts
            ]
        if install_on_load is not None and install_on_load not in (
            "install",
            "check",
            "skip",
        ):
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
        self._validate_spec()
        self._mapping = self._map_spec()

    def _validate_spec(self) -> None:
        """Reject values that are not one bounded token, and unsupported shapes."""
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
            if key in {"output", "error"} and "%" in rendered:
                raise ValueError(
                    f"scheduler.{key} cannot hold %-patterns: PSI/J writes the "
                    "job's output to this exact path"
                )
        suffix = self.spec.get("suffix")
        if suffix is not None and _HOST_SUFFIX.fullmatch(str(suffix)) is None:
            raise ValueError("scheduler.suffix is not a valid hostname suffix")

    def _map_spec(self) -> Dict[str, Any]:
        """The PSI/J job this block asks for, as plain data."""
        resources: Dict[str, Any] = {field: None for field in _RESOURCE_KEYS.values()}
        resources["exclusive_node_use"] = False
        resources["memory"] = None
        attributes: Dict[str, Any] = {field: None for field in _ATTRIBUTE_KEYS.values()}
        attributes["duration_seconds"] = None
        custom: Dict[str, str] = {}
        job_name: Optional[str] = None
        for key, value in self.spec.items():
            if key in _STRUCTURAL_KEYS or value is None or value is False:
                continue
            if key in _REFUSED_KEYS:
                raise ValueError(f"scheduler.{key}: {_REFUSED_KEYS[key]}")
            if key in _RESOURCE_KEYS:
                resources[_RESOURCE_KEYS[key]] = _positive_int(value, field=key)
            elif key == "exclusive" and value is True:
                resources["exclusive_node_use"] = True
            elif key == "mem" and str(value).strip() not in {"0", "0M", "0G"}:
                resources["memory"] = _memory_bytes(value)
            elif key == "time":
                attributes["duration_seconds"] = _duration_seconds(value)
            elif key in _ATTRIBUTE_KEYS:
                attributes[_ATTRIBUTE_KEYS[key]] = str(value)
            elif key == "job_name":
                job_name = str(value)
            elif value is True:
                raise ValueError(
                    f"scheduler.{key}: true cannot be passed to the scheduler "
                    "(PSI/J renders every extra as --key=value); give it a value")
            else:
                custom[f"{self.NAME}.{key.replace('_', '-')}"] = str(value)
        for raw in self.spec.get("sbatch_args") or []:
            flag, separator, value = str(raw)[2:].partition("=")
            if flag in _REFUSED_FLAGS:
                reason = _REFUSED_FLAGS[flag]
                raise ValueError(
                    f"scheduler.sbatch_args --{flag}: "
                    + (_REFUSED_KEYS[reason] if reason else
                       "set scheduler.output or scheduler.error instead"))
            if not separator or not value:
                raise ValueError(
                    f"scheduler.sbatch_args {raw}: give the flag a value (--flag=value)")
            custom[f"slurm.{flag}"] = value
        cores = queues.core_request(self.NAME, self.spec)
        if cores["source"] == "jarvis":
            custom.setdefault(
                f"{self.NAME}.ntasks-per-core", str(cores["ntasks_per_core"]))
        _check_resource_shape(resources)
        stdout = str(self.spec.get("output") or self.shared_dir / "stdout.log")
        stderr = str(self.spec.get("error") or self.shared_dir / "stderr.log")
        if any(character.isspace() for character in stdout + stderr):
            raise ValueError(
                "the job's log paths cannot contain whitespace (PSI/J's launcher "
                "does not quote them); set scheduler.output and scheduler.error")
        return {
            "name": job_name or self.pipeline_name,
            "resources": resources,
            "attributes": attributes,
            "custom_attributes": dict(sorted(custom.items())),
            "stdout_path": stdout,
            "stderr_path": stderr,
            "node_count": _computed_nodes(resources),
        }

    @property
    def script_path(self) -> Path:
        return self.shared_dir / self.SCRIPT_NAME

    @property
    def stop_request_path(self) -> Path:
        return self.shared_dir / STOP_REQUEST_NAME

    @property
    def array_requested(self) -> bool:
        """Arrays are refused, so a submission is always one job."""
        return False

    def entry_arguments(self) -> List[str]:
        """Arguments of ``python`` for the job's one program."""
        arguments = [
            "-m", "jarvis_cd.submit.entry",
            "--provider", self.NAME,
            "--work-dir", str(self.shared_dir),
            "--hostfile", self.hostfile,
            "--nodes", str(self._mapping["node_count"]),
            "--stop-timeout", str(self.stop_timeout),
            "--stop-grace", str(self.stop_grace),
            "--action", self.pipeline_action,
        ]
        if self.local_hosts:
            arguments += ["--nodefile", str(self.shared_dir / self.LOCAL_NODEFILE_NAME)]
        suffix = self.spec.get("suffix")
        if suffix:
            arguments += ["--suffix", str(suffix)]
        arguments += [f"--action-arg={argument}" for argument in self.pipeline_action_args]
        if self.pipeline_snapshot_dir is not None:
            arguments += ["--snapshot-dir", str(self.pipeline_snapshot_dir)]
        elif self.pipeline_yaml:
            arguments += ["--pipeline-yaml", self.pipeline_yaml]
        else:
            raise ValueError("a submitted job needs a pipeline snapshot or YAML")
        if self.jarvis_root is not None:
            arguments += ["--jarvis-root", str(self.jarvis_root)]
        if self.install_on_load is not None:
            arguments += ["--install-on-load", self.install_on_load]
        return arguments

    def _hook_text(self, key: str) -> Optional[str]:
        """A launcher hook file: the commands, with output in their own log.

        The launcher truncates the job's stdout when the program starts, and a
        local job's own stdout is a pipe nobody reads after submit returns.
        The launcher sources the pre-launch hook, so it always starts with
        :data:`TERM_TRAP`: a scheduler TERM then waits for the entry instead
        of ending the launcher shell.
        """
        trap = TERM_TRAP if key == "pre_cmds" else ""
        commands = [str(command) for command in self.spec.get(key) or []]
        if not commands:
            return trap or None
        log = shlex.quote(str(self.shared_dir / f"{key.removesuffix('_cmds')}-launch.log"))
        return (
            trap
            + f"exec 8>&1 9>&2 >>{log} 2>&1\n"
            + "\n".join(commands)
            + "\nexec 1>&8 2>&9 8>&- 9>&-\n"
        )

    def describe(self) -> Dict[str, Any]:
        """The job description, as written to :attr:`script_path`."""
        mapping = self._mapping
        description = {
            "schema_version": JOBSPEC_SCHEMA,
            "executor": self.NAME,
            "launcher": self.LAUNCHER,
            "name": mapping["name"],
            "directory": str(self.shared_dir),
            "executable": sys.executable,
            "arguments": self.entry_arguments(),
            "stdout_path": mapping["stdout_path"],
            "stderr_path": mapping["stderr_path"],
            "resources": mapping["resources"],
            "attributes": mapping["attributes"],
            "custom_attributes": mapping["custom_attributes"],
            "pre_launch": self._hook_text("pre_cmds"),
            "post_launch": self._hook_text("post_cmds"),
        }
        if self.local_hosts:
            description["local_hosts"] = list(self.local_hosts)
        return description

    def render(self) -> str:
        """The canonical job description text (what ``+plan`` hashes)."""
        return json.dumps(self.describe(), indent=2, sort_keys=True) + "\n"

    def write_script(self) -> Path:
        """Durably write the job description and its launcher hook files."""
        for key, name in (("pre_cmds", self.PRE_LAUNCH_NAME),
                          ("post_cmds", self.POST_LAUNCH_NAME)):
            text = self._hook_text(key)
            if text is not None:
                _write_atomic(self.shared_dir / name, text, 0o700)
        if self.local_hosts:
            _write_atomic(self.shared_dir / self.LOCAL_NODEFILE_NAME,
                          "".join(f"{host}\n" for host in self.local_hosts), 0o644)
        return _write_atomic(self.script_path, self.render())

    def build_job(self) -> Any:
        """The PSI/J ``Job`` for this submission."""
        from jarvis_cd.submit._psij import load_psij

        load_psij()
        from psij import Job, JobAttributes, JobSpec, ResourceSpecV1

        mapping = self._mapping
        resources = dict(mapping["resources"])
        attributes = mapping["attributes"]
        job_attributes = JobAttributes(
            queue_name=attributes["queue_name"],
            account=attributes["account"],
            reservation_id=attributes["reservation_id"],
            custom_attributes=dict(mapping["custom_attributes"]) or None,
        )
        seconds = attributes["duration_seconds"]
        # no time asks for the queue's default, not PSI/J's 10 minutes
        setattr(job_attributes, "duration",
                timedelta(seconds=seconds) if seconds is not None else None)
        hooks = {}
        for key, name, field in (("pre_cmds", self.PRE_LAUNCH_NAME, "pre_launch"),
                                 ("post_cmds", self.POST_LAUNCH_NAME, "post_launch")):
            if self._hook_text(key) is not None:
                hooks[field] = self.shared_dir / name
        spec = JobSpec(
            executable=sys.executable,
            arguments=self.entry_arguments(),
            directory=self.shared_dir,
            name=mapping["name"],
            inherit_environment=True,
            stdout_path=Path(mapping["stdout_path"]),
            stderr_path=Path(mapping["stderr_path"]),
            resources=ResourceSpecV1(**resources),
            attributes=job_attributes,
            launcher=self.LAUNCHER,
            **hooks,
        )
        return Job(spec)

    def submit(self) -> SchedulerSubmission:
        """Submit through PSI/J; raises :class:`SchedulerSubmitRefused` on refusal."""
        if not self.script_path.is_file():
            self.write_script()
        for stale in (EXIT_CODE_NAME, STOP_REQUEST_NAME):
            (self.shared_dir / stale).unlink(missing_ok=True)
        job = self.build_job()
        from psij import InvalidJobException, SubmitException

        executor = _executor(self.NAME, self.shared_dir)
        try:
            executor.submit(job)
        except (SubmitException, InvalidJobException) as exc:
            raise SchedulerSubmitRefused(str(exc)[-4096:] or type(exc).__name__) from exc
        native_id = str(job.native_id or "").strip()
        if not native_id:
            raise RuntimeError("PSI/J accepted the job but returned no native id")
        return SchedulerSubmission(
            job=job, executor=executor, native_id=native_id,
            command=_submit_command(executor, job))

    def wait(self, submission: SchedulerSubmission) -> int:
        """Block until the job ends; its return code."""
        status = submission.job.wait()
        code = read_exit_code(self.shared_dir)
        if code is not None:
            return code
        exit_code = getattr(status, "exit_code", None)
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            return exit_code
        state = getattr(getattr(status, "state", None), "name", None)
        return 0 if state == "COMPLETED" else 1


def node_shape(
    packages: List[Dict[str, Any]], declared_nodes: Any,
) -> Tuple[int, Optional[int], List[Dict[str, Any]]]:
    """``(nodes, tasks per node, problems)`` one allocation needs for these packages.

    ``nodes`` is the largest of the declared node count, the smallest node
    count that makes every ``host_slice`` select at least one node, and 1.
    Tasks per node is the largest per-node demand of a package: its
    ``ppn``, else its ranks spread over its slice. ``problems`` lists each
    slice that is still invalid for ``nodes`` (a negative start, say).
    Packages with their own ``hostfile`` are skipped.
    """
    from jarvis_cd.core.pipeline_check import _ranks, _slice_range

    try:
        declared = int(declared_nodes or 0)
    except (TypeError, ValueError):
        declared = 0
    slices: List[Tuple[str, str, Dict[str, Any]]] = []
    for package in packages:
        config = dict(package.get("config") or {})
        if config.get("hostfile"):
            continue
        text = str(config.get("host_slice") or "").strip()
        slices.append((str(package.get("pkg_id")), text, config))
    nodes = max(declared, 1)
    for _pkg_id, text, _config in slices:
        if not text:
            continue
        for size in range(1, 4097):
            if _slice_range(text, size)[1] is None:
                nodes = max(nodes, size)
                break
    problems: List[Dict[str, Any]] = []
    per_node: Optional[int] = None
    for pkg_id, text, config in slices:
        span = nodes
        if text:
            selected, problem = _slice_range(text, nodes)
            if problem or selected is None:
                problems.append({"pkg_id": pkg_id, "host_slice": text,
                                 "nodes": nodes, "problem": problem})
                continue
            span = selected[1] - selected[0]
        ppn = str(config.get("ppn") or "").strip()
        demand = int(ppn) if ppn.isdigit() and int(ppn) > 0 else None
        ranks = _ranks(config)
        if demand is None and ranks:
            demand = -(-ranks // span)
        if demand:
            per_node = max(per_node or 0, demand)
    return nodes, per_node, problems


def read_exit_code(execution_root: Path) -> Optional[int]:
    """The return code the job's entry wrote, if it finished."""
    return _read_int(Path(execution_root) / EXIT_CODE_NAME)


def _read_int(path: Path) -> Optional[int]:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def write_stop_request(execution_root: Path) -> Path:
    """Ask the job to stop its pipeline; the entry polls this file."""
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return _write_atomic(Path(execution_root) / STOP_REQUEST_NAME,
                         json.dumps({"requested_at": stamp}) + "\n")


_EXECUTORS: Dict[str, Any] = {}

# A scheduler TERM (cancel, time limit, preemption) reaches every process in
# the job. PSI/J's batch script and launcher run the entry in the foreground
# without a trap, so both shells would die at once, and the scheduler then
# kills the entry before its handler can tear down and finalize. A trap makes
# each shell wait for its child instead.
TERM_TRAP = "trap ':' TERM\n"
_BATCH_TRAP_MARKER = "exec &>>"


class _TermTrapGenerator:
    """Wraps a PSI/J batch script generator to trap TERM in the batch shell.

    The trap goes before the output redirect, which follows the scheduler
    directives in every PSI/J batch template; a template without it is
    refused rather than submitted untrapped.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.config = getattr(inner, "config", None)

    def generate_submit_script(self, job: Any, context: Dict[str, object], out: Any) -> None:
        import io

        buffer = io.StringIO()
        self.inner.generate_submit_script(job, context, buffer)
        lines = buffer.getvalue().splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.lstrip().startswith(_BATCH_TRAP_MARKER):
                lines.insert(index, TERM_TRAP)
                out.write("".join(lines))
                return
        raise RuntimeError(
            "the PSI/J batch template has no output redirect line; JARVIS "
            "cannot trap TERM in its batch shell")


def _executor(name: str, execution_root: Optional[Path] = None) -> Any:
    """A PSI/J executor; one per scheduler unless a submission needs its own."""
    from jarvis_cd.submit._psij import load_psij

    load_psij()
    from psij import JobExecutor
    from psij.executors.batch.batch_scheduler_executor import (
        BatchSchedulerExecutorConfig,
    )

    if execution_root is None and name in _EXECUTORS:
        return _EXECUTORS[name]
    config = BatchSchedulerExecutorConfig(
        work_directory=(Path(execution_root) / "psij") if execution_root else None,
        keep_files=True,
    )
    executor: Any = JobExecutor.get_instance(name, config=config)
    if _is_batch(executor) and not isinstance(executor.generator, _TermTrapGenerator):
        executor.generator = _TermTrapGenerator(executor.generator)
    if execution_root is None:
        _EXECUTORS[name] = executor
    return executor


def _is_batch(executor: Any) -> bool:
    from psij.executors.batch.batch_scheduler_executor import BatchSchedulerExecutor

    return isinstance(executor, BatchSchedulerExecutor)


def _submit_command(executor: Any, job: Any) -> List[str]:
    if _is_batch(executor):
        return [str(part) for part in executor.get_submit_command(
            job, executor.work_directory / f"{job.id}.job")]
    return ["psij", executor.name, "submit", job.id]


def _supported(provider: str, action: str) -> str:
    name = _SCHEDULER_ALIASES.get(provider.strip().lower(), provider.strip().lower())
    if name not in SUPPORTED_SCHEDULERS:
        raise SchedulerControlUnsupported(
            f"JARVIS has no {action} command for scheduler provider {provider!r}")
    return name


def _observation(
    state: SchedulerExecutionState,
    *,
    return_code: Optional[int] = None,
    provider_state: Optional[str] = None,
    diagnostic: Optional[str] = None,
    basis: Optional[str] = None,
) -> SchedulerExecutionStatus:
    return SchedulerExecutionStatus(
        state=state,
        terminal=state in {"completed", "failed", "canceled"},
        return_code=return_code,
        provider_state=provider_state,
        diagnostic=diagnostic[:2048] if diagnostic else None,
        basis=basis,
    )


def _from_psij_state(
    state: str, message: Optional[str], psij_code: Optional[int],
) -> SchedulerExecutionStatus:
    if state in {"NEW", "QUEUED"}:
        return _observation("submitted", provider_state=state, basis="queue")
    if state == "ACTIVE":
        return _observation("running", provider_state=state, basis="queue")
    if state == "CANCELED":
        return _observation("canceled", provider_state=state, diagnostic=message,
                            basis="queue")
    if state == "FAILED":
        return _observation("failed", return_code=psij_code or 1, provider_state=state,
                            diagnostic=message, basis="queue")
    return _observation(
        "missing", provider_state=state, basis="absent",
        diagnostic="the job ended without writing its JARVIS exit code")


def query_scheduler_execution(
    provider: str,
    native_id: str,
    *,
    execution_root: Optional[Path] = None,
) -> SchedulerExecutionStatus:
    """Execution state from the entry's exit code, else from PSI/J.

    Batch schedulers get one status query (PSI/J's own command, no polling
    thread); a job the queue no longer lists is ``missing`` unless PSI/J's
    exit code file says it failed. Local and Flux jobs are attached. Never
    queries scheduler accounting.
    """
    try:
        name = _supported(provider, "query")
    except SchedulerControlUnsupported as exc:
        return _observation("unknown", diagnostic=str(exc))
    if execution_root is not None:
        code = read_exit_code(Path(execution_root))
        if code is not None:
            return _observation("completed" if code == 0 else "failed",
                                return_code=code, provider_state="EXITED",
                                basis="exit_code_file")
    psij_code = None
    if execution_root is not None:
        psij_code = _read_int(Path(execution_root) / "psij" / name / f"{native_id}.ec")
    try:
        executor = _executor(name)
        if _is_batch(executor):
            completed, diagnostic = queues.run_query(
                list(executor.get_status_command([native_id])))
            if completed is None:
                return _observation("unknown", diagnostic=diagnostic)
            statuses = executor.parse_status_output(
                completed.returncode, completed.stdout or "")
            status = statuses.get(native_id)
            if status is None:
                if psij_code:
                    return _observation("failed", return_code=psij_code,
                                        provider_state="ABSENT",
                                        basis="psij_exit_code_file")
                return _observation("missing", provider_state=None, basis="absent")
        else:
            from psij import Job, JobState

            job = Job()
            try:
                executor.attach(job, native_id)
            except Exception as exc:
                if type(exc).__name__ == "NoSuchProcess":
                    return _observation("missing", basis="absent")
                raise
            status = job.wait(timeout=timedelta(seconds=10),
                              target_states=[JobState.QUEUED]) or job.status
    except Exception as exc:
        return _observation(
            "unknown", diagnostic=f"PSI/J {name} status failed: "
            f"{str(exc)[:1024] or type(exc).__name__}")
    return _from_psij_state(status.state.name, status.message, psij_code)


def scheduler_control_argv(
    provider: str,
    action: SchedulerControlAction,
    native_id: str,
    *,
    cluster: Optional[str] = None,
    execution_root: Optional[Path] = None,
) -> List[str]:
    """The command a cancel or stop records, without running it.

    Cancel is the PSI/J executor's own cancel command (``scancel -Q ID``
    for Slurm); stop writes the execution's stop-request file. Raises
    ``SchedulerControlUnsupported`` for an unknown provider, so callers can
    refuse before recording any request. ``cluster`` is unused: multi-cluster
    submission is refused.
    """
    del cluster
    name = _supported(provider, action)
    _validated_single_line(native_id, field="native_id", limit=256)
    if action == "stop":
        target = Path(execution_root) / STOP_REQUEST_NAME if execution_root else None
        return ["jarvis-stop-request", str(target or STOP_REQUEST_NAME)]
    if action != "cancel":
        raise ValueError(f"unknown scheduler control action {action!r}")
    executor = _executor(name)
    if _is_batch(executor):
        return [str(part) for part in executor.get_cancel_command(native_id)]
    return ["psij", name, "cancel", native_id]


def control_scheduler_execution(
    provider: str,
    action: SchedulerControlAction,
    native_id: str,
    *,
    cluster: Optional[str] = None,
    execution_root: Optional[Path] = None,
) -> SchedulerControlResult:
    """Cancel through PSI/J, or ask the job to stop through its stop-request file."""
    argv = scheduler_control_argv(provider, action, native_id, cluster=cluster,
                                  execution_root=execution_root)
    name = _supported(provider, action)
    if action == "stop":
        observed = query_scheduler_execution(name, native_id,
                                             execution_root=execution_root)
        if observed.terminal or observed.state == "missing":
            return SchedulerControlResult(
                action=action, argv=tuple(argv), return_code=None,
                diagnostic=observed.diagnostic or f"job is {observed.state}",
                accepted=False, job_ended=True)
        if execution_root is None:
            return SchedulerControlResult(
                action=action, argv=tuple(argv), return_code=None,
                diagnostic="no execution directory to write the stop request to",
                accepted=False)
        try:
            write_stop_request(Path(execution_root))
        except OSError as exc:
            return SchedulerControlResult(
                action=action, argv=tuple(argv), return_code=None,
                diagnostic=f"could not write the stop request: {str(exc)[:1024]}",
                accepted=False)
        return SchedulerControlResult(action=action, argv=tuple(argv), return_code=0,
                                      diagnostic=None, accepted=True)
    from psij import Job

    executor = _executor(name)
    try:
        job = Job()
        executor.attach(job, native_id)
        executor.cancel(job)
    except Exception as exc:
        observed = query_scheduler_execution(name, native_id,
                                             execution_root=execution_root)
        ended = observed.terminal or observed.state == "missing"
        return SchedulerControlResult(
            action=action, argv=tuple(argv), return_code=None,
            diagnostic=(str(exc) or type(exc).__name__)[:2048],
            accepted=False, job_ended=ended)
    if not _is_batch(executor):
        # the local executor kills the process tree from its reaper thread
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and query_scheduler_execution(
                name, native_id).state == "running":
            time.sleep(0.5)
    return SchedulerControlResult(action=action, argv=tuple(argv), return_code=0,
                                  diagnostic=None, accepted=True)


def resolve_scheduler_artifact_path(
    provider: str,
    path_pattern: str,
    *,
    native_id: str,
    array_requested: bool = False,
) -> SchedulerArtifactPathResolution:
    """The job's log path: PSI/J writes to the exact configured path.

    ``%`` patterns are refused when the job is described, so there is
    nothing to substitute.
    """
    del native_id, array_requested
    if _SCHEDULER_ALIASES.get(provider.strip().lower(),
                              provider.strip().lower()) not in SUPPORTED_SCHEDULERS:
        return SchedulerArtifactPathResolution(
            path=None,
            diagnostic_code="scheduler_artifact_provider_unsupported",
            diagnostic="JARVIS has no artifact path resolver for this scheduler",
        )
    pattern = _validated_single_line(path_pattern, field="artifact_path")
    if "%" in pattern:
        return SchedulerArtifactPathResolution(
            path=None,
            diagnostic_code="scheduler_artifact_path_unresolved",
            diagnostic="JARVIS does not substitute %-patterns in log paths",
        )
    return SchedulerArtifactPathResolution(path=pattern)
