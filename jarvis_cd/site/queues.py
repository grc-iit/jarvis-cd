"""Read-only queue queries and the job move, per scheduler.

Submission, status and cancel go through PSI/J
(:mod:`jarvis_cd.core.scheduler`). This module is the one place where
scheduler-specific commands remain: partition and pending-job facts
(``sinfo``, ``squeue``, ``scontrol``), moving a pending job to another
partition (``scontrol update``), the allocation identity a job sees in its
environment, and the per-core default JARVIS adds to a Slurm request. Only
Slurm is implemented; other schedulers report no facts and refuse a move.
No query uses ``sacct``: many sites run without accounting.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

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
