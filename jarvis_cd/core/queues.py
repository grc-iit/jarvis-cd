"""Scheduler queue reads and the job move, per scheduler.

Submission, status and cancel go through PSI/J
(:mod:`jarvis_cd.core.scheduler`). This module is the one place where
scheduler-specific commands remain, all light and read-only except the move:

* partitions: node counts and shape from ``sinfo``, and per-queue limits
  with the same ``limits`` keys (:data:`LIMIT_KEYS`) for every scheduler
  (Slurm ``scontrol show partition -o``, PBS ``qstat -Qf``/``-Bf``, LSF
  ``bqueues -l``/``bparams``, Flux ``flux queue list``). A key the scheduler
  reports holds its value (``None`` for Slurm ``NONE``, ``"UNLIMITED"`` for
  unlimited); a key it does not expose is ``None`` and listed in
  ``limits_unknown``; ``limits_source`` names the provider, the command, the
  native field per key, and the raw ``error`` of a failed read. Queue states
  use Slurm's words: ``UP``, ``DOWN``, ``DRAIN``, ``INACTIVE``;
* pending-job facts (``squeue``) and moving a pending job to another
  partition (``scontrol update``), Slurm only;
* the allocation identity a job sees in its environment, and when it started;
* the per-core default JARVIS adds to a Slurm request.

No query uses ``sacct``: many sites run without accounting.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from jarvis_cd.util import stall as _stall

_SLURM_NATIVE_ID = re.compile(r"^[0-9]{1,64}$")
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def run_query(
    argv: List[str],
) -> Tuple[Optional[subprocess.CompletedProcess[str]], Optional[str]]:
    """Run one scheduler query without a shell.

    No time limit (a busy controller can take a while to answer); a query
    that stalls (:mod:`jarvis_cd.util.stall`) is stopped, not retried: the
    caller's next poll is the retry.
    """
    try:
        completed = _stall.run(argv, what=argv[0], attempts=1)
    except OSError as exc:
        return None, f"{argv[0]} query failed: {str(exc)[:1024]}"
    stalled = _stall.stall_error(completed)
    if stalled is not None:
        return None, f"{argv[0]} query {stalled}"
    if completed.returncode == 0:
        return completed, None
    diagnostic = (completed.stderr or completed.stdout or "").strip()
    if len(diagnostic) > 1024:
        diagnostic = "[truncated] " + diagnostic[-1024:]
    return completed, (
        f"{argv[0]} query exited {completed.returncode}"
        + (f": {diagnostic}" if diagnostic else "")
    )


def parse_slurm_duration(value: str) -> Optional[int]:
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


#: keys (and sbatch flags) that already say how tasks map to cores or
#: hardware threads; any of them leaves the per-core request to the person
_CORE_KEYS = ("ntasks_per_core", "threads_per_core", "cores_per_socket",
              "sockets_per_node", "hint", "extra_node_info")
_CORE_FLAGS = ("--ntasks-per-core", "--threads-per-core", "--cores-per-socket",
               "--sockets-per-node", "--hint", "--extra-node-info", "-B")


def core_request(provider: str, spec: Mapping[str, Any]) -> Dict[str, Any]:
    """How ranks claim cores: Slurm ``--ntasks-per-core=1`` unless the person said.

    With ``CR_CORE`` on nodes with several hardware threads per core, a task
    otherwise counts one hardware thread, so two ranks share a core. One task
    per core makes each rank claim a whole core; nodes without SMT are
    unchanged. A rank request is ``ntasks`` or ``ntasks_per_node``. Other
    schedulers get no default.
    """
    if provider != "slurm":
        return {"ntasks_per_core": None, "source": "unset", "key": None}
    for key in _CORE_KEYS:
        if key in spec:
            return {"ntasks_per_core": spec.get(key) if key == "ntasks_per_core" else None,
                    "source": "pipeline.scheduler", "key": key}
    for raw in spec.get("sbatch_args") or []:
        flag = str(raw).split("=", 1)[0]
        if flag in _CORE_FLAGS:
            return {"ntasks_per_core": None, "source": "pipeline.scheduler.sbatch_args",
                    "key": flag}
    ranks = any(spec.get(key) not in (None, False, 0)
                for key in ("ntasks", "ntasks_per_node"))
    if not ranks:
        return {"ntasks_per_core": None, "source": "unset", "key": None}
    return {"ntasks_per_core": 1, "source": "jarvis", "key": "ntasks_per_core"}


def _matching_line(stdout: str, native_id: str, field_count: int) -> Optional[List[str]]:
    for raw_line in stdout.splitlines():
        fields = [field.strip() for field in raw_line.strip().strip("|").split("|")]
        if len(fields) == field_count and fields[0] == native_id:
            return fields
    return None


def partition_facts(provider: str) -> List[Dict[str, Any]]:
    """Every partition with availability, node counts, time limit and topology."""
    if provider != "slurm":
        return []
    listing, _diagnostic = run_query(
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
        entry = merged.setdefault(name, {
            "partition": name,
            "default": default,
            "available": fields[1],
            "allocated_nodes": 0,
            "idle_nodes": 0,
            "other_nodes": 0,
            "total_nodes": 0,
            "max_time": fields[3],
            "max_time_s": parse_slurm_duration(fields[3]),
            "cpus_per_node": None,
            "sockets_per_node": None,
            "cores_per_socket": None,
            "threads_per_core": None,
            "physical_cores_per_node": None,
            "memory_mb_per_node": None,
            "gres": None,
            "heterogeneous": False,
        })
        seen = "_seen" in entry
        entry["_seen"] = True
        for key, value in zip(
            ("allocated_nodes", "idle_nodes", "other_nodes", "total_nodes"), counts
        ):
            entry[key] += int(value)
        # One sinfo line per node configuration. A partition with several
        # reports the smallest node, so sizing from it fits every node.
        if len(fields) >= 7:
            line = {
                "cpus_per_node": count(fields[4]),
                "memory_mb_per_node": count(fields[5]),
            }
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


_JOB_HOLD_REASONS = ("dependency", "begintime", "jobheld", "held", "jobarraytasklimit")
_LIMIT_REASONS = ("assoc", "qos", "partitiontimelimit", "partitionnodelimit")


def queue_facts(provider: str, native_id: str) -> Optional[Dict[str, Any]]:
    """Queue facts (``jarvis.scheduler.queue.v1``) for one pending job.

    The job's partition, pending reason, the scheduler's estimated start, and
    every other available partition with its idle nodes and whether the job
    ``fits`` there. ``None`` when the job is not pending or the scheduler has
    no such query.
    """
    if provider != "slurm" or _SLURM_NATIVE_ID.fullmatch(native_id) is None:
        return None
    job, _diagnostic = run_query([
        "squeue", "--noheader", "--states=PENDING", "--jobs", native_id,
        "--format=%i|%P|%r|%S|%D|%l",
    ])
    if job is None or job.returncode != 0:
        return None
    row = _matching_line(job.stdout, native_id, 6)
    if row is None:
        return None
    _job_id, partition, reason, start_text, nodes_text, limit_text = row
    nodes = int(nodes_text) if nodes_text.isdigit() else None
    limit_s = parse_slurm_duration(limit_text)
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
    if any(lowered.startswith(prefix) for prefix in _JOB_HOLD_REASONS):
        reason_kind = "job_hold"
    elif any(lowered.startswith(prefix) for prefix in _LIMIT_REASONS):
        reason_kind = "limit"
    elif lowered.startswith(("resources", "priority", "reqnodenotavail", "reservation")):
        reason_kind = "resources"
    else:
        reason_kind = "other"
    current = {item for item in partition.split(",") if item}
    partitions = [entry for entry in partition_facts(provider)
                  if entry["partition"] not in current and entry["available"] == "up"]
    for entry in partitions:
        time_ok = entry["max_time_s"] is None or (
            limit_s is not None and entry["max_time_s"] >= limit_s
        )
        entry["fits"] = bool(nodes is not None and entry["idle_nodes"] >= nodes and time_ok)
    partitions.sort(key=lambda entry: (not entry["fits"], -entry["idle_nodes"]))
    return {
        "schema_version": "jarvis.scheduler.queue.v1",
        "provider": provider,
        "native_id": native_id,
        "observed_at": _iso_utc(datetime.now(timezone.utc)),
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


def move_argv(provider: str, native_id: str, queue: str, *,
              cluster: Optional[str] = None) -> List[str]:
    """``scontrol update`` argv that moves a pending Slurm job to ``queue``."""
    from jarvis_cd.core.scheduler import SchedulerControlUnsupported

    if provider != "slurm":
        raise SchedulerControlUnsupported(
            f"JARVIS has no move command for scheduler provider {provider!r}")
    if _SLURM_NATIVE_ID.fullmatch(native_id) is None:
        raise ValueError("SLURM move requires a numeric job ID")
    if _NAME.fullmatch(queue) is None:
        raise ValueError("SLURM partition name is invalid")
    argv = ["scontrol"]
    if cluster is not None:
        if _NAME.fullmatch(cluster) is None:
            raise ValueError("SLURM cluster name is invalid")
        argv.append(f"--clusters={cluster}")
    argv.extend(["update", f"JobId={native_id}", f"Partition={queue}"])
    return argv


def move_job(provider: str, native_id: str, queue: str, *,
             cluster: Optional[str] = None) -> Dict[str, Any]:
    """Move one pending job; ``{command, return_code, accepted, refusal, diagnostic}``.

    Raises ``SchedulerControlUnsupported`` for a scheduler with no move.
    """
    argv = move_argv(provider, native_id, queue, cluster=cluster)
    completed: Optional[subprocess.CompletedProcess[str]]
    try:
        completed = _stall.run(argv, what=argv[0], attempts=1)
        diagnostic = None
        stalled = _stall.stall_error(completed)
        if stalled is not None:
            completed, diagnostic = None, f"{argv[0]} {stalled}"
    except OSError as exc:
        completed, diagnostic = None, f"{argv[0]} failed: {str(exc)[:1024]}"
    accepted, refusal, detail = False, "scheduler_unavailable", diagnostic
    if completed is not None:
        text = " ".join(part.strip() for part in (completed.stderr, completed.stdout) if part)
        accepted = completed.returncode == 0
        refusal = None if accepted else "scheduler_rejected"
        detail = text[:2048] or (None if accepted else diagnostic)
        lowered = text.lower()
        for marker, reason in () if accepted else _MOVE_REFUSALS:
            if marker in lowered:
                refusal = reason
                break
    return {
        "command": argv,
        "return_code": None if completed is None else completed.returncode,
        "accepted": accepted,
        "refusal": refusal,
        "diagnostic": detail,
    }


#: environment variable naming the job id -> scheduler, as a job sees it
_JOB_ID_VARIABLES = (
    ("SLURM_JOB_ID", "slurm"),
    ("PBS_JOBID", "pbs"),
    ("LSB_JOBID", "lsf"),
    ("FLUX_JOB_ID", "flux"),
    ("COBALT_JOBID", "cobalt"),
)


def allocation_identity(
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[Tuple[str, str, Optional[str]]]:
    """``(provider, job id, cluster)`` of the allocation this process runs in."""
    env = os.environ if environ is None else environ
    for variable, provider in _JOB_ID_VARIABLES:
        value = str(env.get(variable) or "").strip()
        if value:
            cluster = env.get("SLURM_CLUSTER_NAME") if provider == "slurm" else None
            return provider, value, (cluster or None)
    return None


def _iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_iso(text: str, fmt: str) -> Optional[str]:
    """A scheduler's local wall-clock time (no zone) as ISO-8601 UTC."""
    try:
        return _iso_utc(datetime.strptime(text.strip(), fmt).astimezone())
    except ValueError:
        return None


def _query_start(provider: str, job_id: str) -> Tuple[Optional[str], Optional[str]]:
    if provider == "slurm":
        shown, _ = run_query(["scontrol", "show", "job", "--oneliner", job_id])
        if shown is not None and shown.returncode == 0:
            match = re.search(r"(?:^|\s)StartTime=(\S+)", shown.stdout or "")
            started = _local_iso(match.group(1), "%Y-%m-%dT%H:%M:%S") if match else None
            if started:
                return started, "scontrol"
    elif provider == "pbs":
        shown, _ = run_query(["qstat", "-f", job_id])
        text = (shown.stdout or "") if shown is not None and shown.returncode == 0 else ""
        match = re.search(r"^\s*stime\s*=\s*(.+?)\s*$", text, re.MULTILINE)
        started = _local_iso(match.group(1), "%a %b %d %H:%M:%S %Y") if match else None
        if started:
            return started, "qstat"
    return None, None


def scheduler_job_start(
    environ: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """When the allocation this process runs in really started.

    ``None`` outside an allocation. Otherwise ``{provider, job_id, started_at,
    source}``: ``started_at`` is ISO-8601 UTC, from ``SLURM_JOB_START_TIME``,
    else ``scontrol`` (Slurm) or ``qstat`` (PBS), else this moment with
    ``source: run_start``.
    """
    env = os.environ if environ is None else environ
    identity = allocation_identity(env)
    if identity is None:
        return None
    provider, job_id, _cluster = identity
    started: Optional[str] = None
    source: Optional[str] = None
    epoch = str(env.get("SLURM_JOB_START_TIME") or "").strip()
    if provider == "slurm" and epoch.isdigit() and int(epoch) > 0:
        started = _iso_utc(datetime.fromtimestamp(int(epoch), tz=timezone.utc))
        source = "SLURM_JOB_START_TIME"
    else:
        started, source = _query_start(provider, job_id)
    if not started:
        started, source = _iso_utc(datetime.now(timezone.utc)), "run_start"
    return {"provider": provider, "job_id": job_id, "started_at": started, "source": source}


#: the ``limits`` keys every partition carries, for every scheduler
LIMIT_KEYS: Tuple[str, ...] = (
    "max_time", "max_time_s", "default_time", "default_time_s",
    "min_nodes", "max_nodes", "max_cpus_per_node",
    "default_mem_per_cpu_mb", "max_mem_per_node_mb",
    "qos", "allow_accounts", "allow_qos", "allow_groups", "state",
    "total_nodes", "total_cpus", "priority_tier", "oversubscribe", "exclusive_user",
)

#: the entry keys Slurm's sinfo gives; other schedulers fill what they know
ENTRY_KEYS: Tuple[str, ...] = (
    "partition", "default", "available", "allocated_nodes", "idle_nodes",
    "other_nodes", "total_nodes", "max_time", "max_time_s", "cpus_per_node",
    "sockets_per_node", "cores_per_socket", "threads_per_core",
    "physical_cores_per_node", "memory_mb_per_node", "gres", "heterogeneous",
)

_AVAILABLE = {"UP": "up", "DOWN": "down", "DRAIN": "drain", "INACTIVE": "inact"}

#: one known value: (value, native field it came from)
Known = Dict[str, Tuple[Any, str]]


def finish_limits(provider: str, command: str, known: Optional[Known],
                  error: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """``limits``, ``limits_unknown`` and ``limits_source`` for one partition."""
    known = known or {}
    source: Dict[str, Any] = {
        "provider": provider, "command": command,
        "fields": {key: known[key][1] for key in LIMIT_KEYS if key in known},
    }
    if error:
        source["error"] = error
    return {
        "limits": {key: known[key][0] if key in known else None for key in LIMIT_KEYS},
        "limits_unknown": [key for key in LIMIT_KEYS if key not in known],
        "limits_source": source,
    }


def _number(text: Any) -> Any:
    text = str(text).strip()
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def _state(enabled: Optional[bool], started: Optional[bool]) -> Optional[str]:
    if enabled is None or started is None:
        return None
    return {(True, True): "UP", (True, False): "DOWN",
            (False, True): "DRAIN", (False, False): "INACTIVE"}[(enabled, started)]


def _query(argv: List[str]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """stdout of one bounded query, or the raw error (command, exit code, output)."""
    completed, diagnostic = run_query(argv)
    if completed is not None and completed.returncode == 0:
        return completed.stdout, None
    return None, {"command": " ".join(argv),
                  "exit_code": completed.returncode if completed is not None else None,
                  "output": diagnostic}


def _entry(provider: str, command: str, name: Optional[str], known: Known,
           default: Optional[bool], extra: Optional[Dict[str, Any]] = None,
           error: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    entry: Dict[str, Any] = {key: None for key in ENTRY_KEYS}
    entry.update(finish_limits(provider, command, known, error))
    limits = entry["limits"]
    entry.update({
        "partition": name,
        "default": default,
        "available": _AVAILABLE.get(str(limits["state"])) if limits["state"] else None,
        "total_nodes": limits["total_nodes"],
        "max_time": limits["max_time"],
        "max_time_s": limits["max_time_s"],
    })
    entry.update(extra or {})
    return entry


# --------------------------------------------------------------------- Slurm

#: ``scontrol show partition -o`` field -> limits key
SLURM_FIELDS = {
    "MaxTime": "max_time",
    "DefaultTime": "default_time",
    "MinNodes": "min_nodes",
    "MaxNodes": "max_nodes",
    "MaxCPUsPerNode": "max_cpus_per_node",
    "DefMemPerCPU": "default_mem_per_cpu_mb",
    "MaxMemPerNode": "max_mem_per_node_mb",
    "QoS": "qos",
    "AllowAccounts": "allow_accounts",
    "AllowQos": "allow_qos",
    "AllowGroups": "allow_groups",
    "State": "state",
    "TotalNodes": "total_nodes",
    "TotalCPUs": "total_cpus",
    "PriorityTier": "priority_tier",
    "OverSubscribe": "oversubscribe",
    "ExclusiveUser": "exclusive_user",
}
SLURM_COMMAND = "scontrol show partition -o"


def _slurm_value(text: str) -> Any:
    if text in ("NONE", "N/A", "(null)", ""):
        return None
    return text if text == "UNLIMITED" else _number(text)


def parse_slurm_partitions(text: str) -> Dict[str, Known]:
    """Known limits per partition from ``scontrol show partition -o``."""
    result: Dict[str, Known] = {}
    for line in text.splitlines():
        fields = dict(token.split("=", 1) for token in line.split() if "=" in token)
        name = fields.get("PartitionName")
        if not name:
            continue
        known: Known = {}
        for native, key in SLURM_FIELDS.items():
            if native not in fields:
                continue
            raw = fields[native]
            if key in ("max_time", "default_time"):
                value = None if raw in ("NONE", "N/A", "") else raw
                known[key] = (value, native)
                known[f"{key}_s"] = (
                    parse_slurm_duration(raw) if value not in (None, "UNLIMITED") else None,
                    native)
            elif key in ("allow_accounts", "allow_qos", "allow_groups"):
                known[key] = (raw.split(",") if raw not in ("", "(null)") else None, native)
            else:
                known[key] = (_slurm_value(raw), native)
        result[name] = known
    return result


def slurm_limits() -> Tuple[Dict[str, Known], Optional[Dict[str, Any]]]:
    stdout, error = _query(["scontrol", "show", "partition", "-o"])
    return (parse_slurm_partitions(stdout) if stdout is not None else {}), error


def attach_slurm_limits(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Give sinfo's partition entries their ``scontrol`` limits; the error if it failed."""
    per_partition, error = slurm_limits()
    for entry in entries:
        known = per_partition.get(entry["partition"])
        missing = error or (None if known is not None else {
            "command": SLURM_COMMAND, "exit_code": 0,
            "output": f"partition {entry['partition']} is not in the output"})
        entry.update(finish_limits("slurm", SLURM_COMMAND, known, missing))
    return error


# ----------------------------------------------------------------------- PBS

PBS_COMMAND = "qstat -Qf"


def parse_pbs_status(text: str, header: str = "Queue") -> Dict[str, Dict[str, str]]:
    """``qstat -Qf`` / ``qstat -Bf`` text: attributes per object.

    Attributes are ``    name = value``. Long values wrap onto lines that
    start with a tab (or the 8 spaces a copy turns it into), sometimes inside
    a token, so continuations are joined as they are.
    """
    objects: Dict[str, Dict[str, str]] = {}
    current: Optional[Dict[str, str]] = None
    last: Optional[str] = None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if raw.startswith(f"{header}: ") or raw.startswith(f"{header}:\t"):
            current = objects.setdefault(raw.split(":", 1)[1].strip(), {})
            last = None
        elif current is None:
            continue
        elif (raw.startswith("\t") or raw.startswith(" " * 8)) and last is not None:
            current[last] += raw.strip()
        elif " = " in raw:
            key, value = raw.strip().split(" = ", 1)
            current[key.strip()] = value.strip()
            last = key.strip()
    return objects


def _pbs_seconds(text: str) -> Optional[int]:
    """Seconds for a PBS walltime, ``[[hours:]minutes:]seconds[.ms]``."""
    parts = text.strip().split(":")
    if not parts or len(parts) > 3:
        return None
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return None
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return int(seconds)


def _pbs_bool(text: Optional[str]) -> Optional[bool]:
    if text is None:
        return None
    return text.strip().lower() in ("true", "t", "1", "yes", "y")


def pbs_known(queue: Dict[str, str], server: Dict[str, str]) -> Known:
    """Known limits for one PBS queue; unset queue limits inherit the server's."""
    known: Known = {}

    def pick(attribute: str) -> Optional[Tuple[str, str]]:
        if attribute in queue:
            return queue[attribute], attribute
        if attribute in server:
            return server[attribute], f"server {attribute}"
        return None

    for key, attribute in (("max_time", "resources_max.walltime"),
                           ("default_time", "resources_default.walltime")):
        found = pick(attribute)
        if found:
            known[key] = (found[0], found[1])
            known[f"{key}_s"] = (_pbs_seconds(found[0]), found[1])
    for key, attribute in (("min_nodes", "resources_min.nodect"),
                           ("max_nodes", "resources_max.nodect")):
        found = pick(attribute)
        if found:
            known[key] = (_number(found[0]), found[1])
    if "max_nodes" not in known:
        found = pick("resources_max.nodes")
        match = re.fullmatch(r"(\d+)(:.*)?", found[0]) if found else None
        if found and match:
            known["max_nodes"] = (int(match.group(1)), found[1])
    enabled, started = _pbs_bool(queue.get("enabled")), _pbs_bool(queue.get("started"))
    if enabled is not None and started is not None:
        known["state"] = (_state(enabled, started), "enabled,started")
    acl = _pbs_bool(queue.get("acl_group_enable"))
    if acl is True and queue.get("acl_groups"):
        known["allow_groups"] = (queue["acl_groups"].split(","), "acl_groups")
    elif acl is False:
        known["allow_groups"] = (["ALL"], "acl_group_enable")
    for key, attribute in (("total_nodes", "resources_available.nodect"),
                           ("total_cpus", "resources_available.ncpus"),
                           ("priority_tier", "Priority")):
        if attribute in queue:
            known[key] = (_number(queue[attribute]), attribute)
    return known


def pbs_entries(queues_text: str, server_text: Optional[str]) -> List[Dict[str, Any]]:
    servers = parse_pbs_status(server_text or "", "Server")
    server = next(iter(servers.values()), {})
    default_queue = server.get("default_queue")
    entries = []
    for name, queue in parse_pbs_status(queues_text).items():
        routes = queue.get("route_destinations")
        entries.append(_entry(
            "pbs", PBS_COMMAND, name, pbs_known(queue, server),
            (name == default_queue) if default_queue else None,
            {"queue_type": queue.get("queue_type"),
             "route_destinations": routes.split(",") if routes else None,
             "from_route_only": _pbs_bool(queue.get("from_route_only"))}))
    return entries


def pbs_partitions() -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    queues, error = _query(["qstat", "-Qf"])
    if queues is None:
        return [], error
    server, _server_error = _query(["qstat", "-Bf"])
    return pbs_entries(queues, server), None


# ----------------------------------------------------------------------- LSF

LSF_COMMAND = "bqueues -l"
_LSF_STATES = {("Open", True): "UP", ("Open", False): "DOWN",
               ("Closed", True): "DRAIN", ("Closed", False): "INACTIVE"}


def _lsf_block(header: str, values: str) -> Dict[str, str]:
    """One limits block: values sit under their header names, right-aligned."""
    spans = list(re.finditer(r"\S+", header))
    result: Dict[str, str] = {}
    for index, span in enumerate(spans):
        start = spans[index - 1].end() if index else 0
        end = span.end() if index + 1 < len(spans) else len(values)
        result[span.group()] = values[start:end].strip()
    return result


def parse_bqueues_long(text: str) -> Dict[str, Dict[str, Any]]:
    """``bqueues -l``: per queue its statistics row, limits, users and hosts."""
    queues: Dict[str, Dict[str, Any]] = {}
    for block in re.split(r"(?m)^QUEUE:\s*", text)[1:]:
        lines = block.splitlines()
        queue: Dict[str, Any] = {"params": {}, "default": {}, "maximum": {},
                                 "users": None, "hosts": None}
        queues[lines[0].strip()] = queue
        section: Optional[str] = None
        index = 1
        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            if stripped == "PARAMETERS/STATISTICS" and index + 2 < len(lines):
                queue["params"] = dict(zip(lines[index + 1].split(), lines[index + 2].split()))
                index += 2
            elif stripped in ("DEFAULT LIMITS:", "MAXIMUM LIMITS:"):
                section = "default" if stripped.startswith("DEFAULT") else "maximum"
            elif stripped.startswith("USERS:"):
                queue["users"] = stripped.split(":", 1)[1].split()
                section = None
            elif stripped.startswith("HOSTS:"):
                queue["hosts"] = stripped.split(":", 1)[1].split()
                section = None
            elif stripped and not line[:1].isspace():
                section = None
            elif (section and re.fullmatch(r"[A-Z_]+( +[A-Z_]+)*", stripped)
                  and index + 1 < len(lines) and lines[index + 1].strip()):
                queue[section].update(_lsf_block(line, lines[index + 1]))
                index += 1
            index += 1
    return queues


def _lsf_minutes(text: str) -> Optional[int]:
    match = re.match(r"\s*(\d+(?:\.\d+)?)\s*min\b", text)
    return int(float(match.group(1)) * 60) if match else None


def lsf_known(queue: Dict[str, Any]) -> Known:
    known: Known = {}
    for key, section in (("max_time", "maximum"), ("default_time", "default")):
        value = queue[section].get("RUNLIMIT")
        if value:
            known[key] = (value, f"{section.upper()} LIMITS RUNLIMIT")
            known[f"{key}_s"] = (_lsf_minutes(value), f"{section.upper()} LIMITS RUNLIMIT")
    hosts = queue["maximum"].get("HOSTLIMIT_PER_JOB")
    if hosts:
        known["max_nodes"] = (_number(hosts), "MAXIMUM LIMITS HOSTLIMIT_PER_JOB")
    status = queue["params"].get("STATUS")
    if status and ":" in status:
        opened, active = status.split(":", 1)
        state = _LSF_STATES.get((opened, active == "Active"))
        if state:
            known["state"] = (state, "STATUS")
    if queue["params"].get("PRIO"):
        known["priority_tier"] = (_number(queue["params"]["PRIO"]), "PRIO")
    users = queue["users"]
    if users == ["all"]:
        known["allow_groups"] = (["ALL"], "USERS")
    elif users and all(user.endswith("/") for user in users):
        known["allow_groups"] = ([user.rstrip("/") for user in users], "USERS")
    return known


def parse_bparams_default_queues(text: str) -> Optional[List[str]]:
    for line in (text or "").splitlines():
        if line.strip().startswith("Default Queues:"):
            return line.split(":", 1)[1].split()
    return None


def lsf_entries(queues_text: str, params_text: Optional[str]) -> List[Dict[str, Any]]:
    defaults = parse_bparams_default_queues(params_text or "")
    return [
        _entry("lsf", LSF_COMMAND, name, lsf_known(queue),
               (name in defaults) if defaults is not None else None)
        for name, queue in parse_bqueues_long(queues_text).items()
    ]


def lsf_partitions() -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    queues, error = _query(["bqueues", "-l"])
    if queues is None:
        return [], error
    params, _params_error = _query(["bparams"])
    return lsf_entries(queues, params), None


# ---------------------------------------------------------------------- Flux

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_FLUX_YES = {"✔": True, "y": True, "✗": False, "n": False, "⏸": False, "p": False}
_FSD = {"": 1, "ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}


def _flux_seconds(text: str) -> Optional[int]:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)?", text)
    return int(float(match.group(1)) * _FSD[match.group(2) or ""]) if match else None


def _flux_time(text: str, column: str) -> Known:
    if text == "inf":
        return {"value": ("UNLIMITED", column), "seconds": (None, column)}
    return {"value": (text, column), "seconds": (_flux_seconds(text), column)}


def parse_flux_queue_list(text: str) -> List[Dict[str, Optional[str]]]:
    """``flux queue list`` rows keyed by heading (colour codes removed)."""
    lines = [_ANSI.sub("", line).rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    header = lines[0].split()
    rows = []
    for line in lines[1:]:
        values: List[Optional[str]] = list(line.split())
        if "PARENT" in header and len(values) == len(header) - 1:
            values.insert(header.index("PARENT"), None)
        if len(values) == len(header):
            rows.append(dict(zip(header, values)))
    return rows


def flux_known(row: Dict[str, Optional[str]], nodes_field: str = "limits.range.nnodes") -> Known:
    known: Known = {}
    for key, column in (("max_time", "TLIMIT"), ("default_time", "TDEFAULT")):
        if row.get(column):
            parsed = _flux_time(str(row[column]), column)
            known[key], known[f"{key}_s"] = parsed["value"], parsed["seconds"]
    nodes = row.get("NNODES")
    if nodes:
        low, _, high = nodes.partition("-")
        known["min_nodes"] = (_number(low), nodes_field)
        high = high or low
        known["max_nodes"] = ("UNLIMITED" if high == "inf" else _number(high), nodes_field)
    enabled = _FLUX_YES.get(row.get("EN") or "")
    started = _FLUX_YES.get(row.get("ST") or "")
    if enabled is not None and started is not None:
        known["state"] = (_state(enabled, started), "EN,ST")
    return known


def flux_entries(text: str, command: str,
                 nodes_field: str = "limits.range.nnodes") -> List[Dict[str, Any]]:
    entries = []
    for row in parse_flux_queue_list(text):
        name = row.get("QUEUE")
        # without named queues Flux has one anonymous queue and no QUEUE column
        default = True if name is None else name.endswith("*")
        entries.append(_entry("flux", command, name.rstrip("*") if name else None,
                              flux_known(row, nodes_field), default,
                              {"parent": row.get("PARENT")} if "PARENT" in row else None))
    return entries


#: the configured (policy) limits, without the built-in formats' 8-character
#: queue-name cut; ``?:`` drops the column when there are no named queues
FLUX_FORMAT = ("?:{queuem} {enabled.ascii} {started.ascii} {defaults.timelimit!F} "
               "{limits.timelimit!F} {limits.range.nnodes}")

#: (argv, native field of NNODES); older flux-core releases fall through to
#: the named ``policy`` format, then to the default (effective-range) table
FLUX_QUERIES: Tuple[Tuple[List[str], str], ...] = (
    (["flux", "queue", "list", "-o", FLUX_FORMAT], "limits.range.nnodes"),
    (["flux", "queue", "list", "-o", "policy"], "limits.range.nnodes"),
    (["flux", "queue", "list"], "limits.range.effective.nnodes"),
)


def flux_partitions() -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    error: Optional[Dict[str, Any]] = None
    for argv, nodes_field in FLUX_QUERIES:
        stdout, error = _query(argv)
        if stdout is not None and "NNODES" in stdout.split("\n", 1)[0]:
            return flux_entries(stdout, " ".join(argv), nodes_field), None
        if stdout is not None:
            error = {"command": " ".join(argv), "exit_code": 0, "output": stdout[:1024]}
    return [], error


#: provider -> reader for schedulers JARVIS reads queues from but cannot submit to
QUEUE_READERS: Dict[str, Callable[[], Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]]] = {
    "pbs": pbs_partitions,
    "lsf": lsf_partitions,
    "flux": flux_partitions,
}
