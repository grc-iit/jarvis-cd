"""Ranks per node for an MPI launch inside a scheduler allocation.

Rank placement is the launcher's job, not a package's. Inside an
allocation the scheduler's task geometry bounds how many ranks each node
may run:

* Slurm: ``SLURM_NTASKS_PER_NODE``, else ``SLURM_TASKS_PER_NODE`` paired
  with ``SLURM_JOB_NODELIST`` (only when the job asked for tasks, i.e.
  ``SLURM_NTASKS``/``SLURM_NPROCS`` or ``SLURM_NTASKS_PER_NODE`` is set);
* PBS: one ``PBS_NODEFILE`` line per MPI slot (else ``PBS_NUM_PPN``);
* LSF: ``LSB_MCPU_HOSTS`` (``host slots host slots ...``).

When the job asked for no task geometry, the physical cores this process
may run on (its CPU affinity, hardware threads of one core counted once)
are the default bound. Outside an allocation nothing changes.

The package's ``ppn`` is kept when it fits the bound and places every rank
(``ppn * hosts >= nprocs``), so a step can still under-fill nodes on
purpose. Otherwise the bound wins: a package default can neither halve a
grant (8 ranks on one 8-task node launched as ``ppr:4:node``) nor exceed
it. ``JARVIS_MPI_PLACEMENT=off`` keeps the package's value unchanged.
"""

from __future__ import annotations

import math
import os
import re
from typing import Any, Dict, List, Mapping, Optional

PLACEMENT_SCHEMA = "jarvis.launch-placement.v1"

_ALLOCATION_IDS = (
    ("slurm", "SLURM_JOB_ID"),
    ("pbs", "PBS_JOBID"),
    ("lsf", "LSB_JOBID"),
    ("flux", "FLUX_JOB_ID"),
)


def _short(host: str) -> str:
    return str(host).split(".", 1)[0]


def _split_top(text: str) -> List[str]:
    parts, depth, current = [], 0, ""
    for char in text:
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    if current:
        parts.append(current)
    return [part.strip() for part in parts if part.strip()]


def expand_nodelist(nodelist: str) -> List[str]:
    """Expand a Slurm hostlist (``node[01-03,07],login``) like scontrol."""
    hosts: List[str] = []
    for item in _split_top(nodelist or ""):
        match = re.match(r"^([^\[]*)\[([^\]]+)\](.*)$", item)
        if not match:
            hosts.append(item)
            continue
        prefix, ranges, rest = match.groups()
        for chunk in ranges.split(","):
            if "-" in chunk:
                low, high = chunk.split("-", 1)
                width = len(low)
                for number in range(int(low), int(high) + 1):
                    hosts.extend(expand_nodelist(f"{prefix}{number:0{width}d}{rest}"))
            else:
                hosts.extend(expand_nodelist(f"{prefix}{chunk}{rest}"))
    return hosts


def _expand_tasks_per_node(value: str) -> List[int]:
    """``2(x3),1`` -> ``[2, 2, 2, 1]``."""
    counts: List[int] = []
    for item in (value or "").split(","):
        match = re.fullmatch(r"\s*(\d+)(?:\(x(\d+)\))?\s*", item)
        if not match:
            return []
        counts.extend([int(match.group(1))] * int(match.group(2) or 1))
    return counts


def allocation(environ: Mapping[str, str]) -> Optional[Dict[str, str]]:
    """The scheduler allocation this process runs in, if any."""
    for scheduler, variable in _ALLOCATION_IDS:
        if environ.get(variable):
            return {"scheduler": scheduler, "job_id": environ[variable]}
    return None


def task_geometry(environ: Mapping[str, str]) -> Optional[Dict[str, Any]]:
    """Tasks per node the scheduler granted, or None when the job set none."""
    if environ.get("SLURM_JOB_ID"):
        asked = any(
            environ.get(name)
            for name in ("SLURM_NTASKS_PER_NODE", "SLURM_NTASKS", "SLURM_NPROCS")
        )
        if not asked:
            return None
        uniform = environ.get("SLURM_NTASKS_PER_NODE", "")
        if uniform.isdigit() and int(uniform) > 0:
            return {"scheduler": "slurm", "variable": "SLURM_NTASKS_PER_NODE",
                    "value": uniform, "uniform": int(uniform), "per_host": None}
        raw = environ.get("SLURM_TASKS_PER_NODE", "")
        counts = _expand_tasks_per_node(raw)
        if not counts:
            return None
        hosts = expand_nodelist(environ.get("SLURM_JOB_NODELIST", ""))
        per_host = (
            {_short(h): c for h, c in zip(hosts, counts)}
            if len(hosts) == len(counts) else None
        )
        return {"scheduler": "slurm", "variable": "SLURM_TASKS_PER_NODE",
                "value": raw,
                "uniform": counts[0] if len(set(counts)) == 1 else None,
                "per_host": per_host, "min": min(counts)}
    if environ.get("PBS_JOBID"):
        nodefile = environ.get("PBS_NODEFILE")
        if nodefile:
            try:
                with open(nodefile, encoding="utf-8") as stream:
                    lines = [_short(line.strip()) for line in stream if line.strip()]
            except OSError:
                lines = []
            if lines:
                per_host: Dict[str, int] = {}
                for host in lines:
                    per_host[host] = per_host.get(host, 0) + 1
                counts = list(per_host.values())
                return {"scheduler": "pbs", "variable": "PBS_NODEFILE",
                        "value": nodefile,
                        "uniform": counts[0] if len(set(counts)) == 1 else None,
                        "per_host": per_host, "min": min(counts)}
        ppn = environ.get("PBS_NUM_PPN", "")
        if ppn.isdigit() and int(ppn) > 0:
            return {"scheduler": "pbs", "variable": "PBS_NUM_PPN", "value": ppn,
                    "uniform": int(ppn), "per_host": None}
        return None
    if environ.get("LSB_JOBID"):
        tokens = (environ.get("LSB_MCPU_HOSTS") or "").split()
        if tokens and len(tokens) % 2 == 0 and all(t.isdigit() for t in tokens[1::2]):
            per_host = {}
            for host, slots in zip(tokens[::2], tokens[1::2]):
                per_host[_short(host)] = per_host.get(_short(host), 0) + int(slots)
            counts = list(per_host.values())
            return {"scheduler": "lsf", "variable": "LSB_MCPU_HOSTS",
                    "value": environ["LSB_MCPU_HOSTS"],
                    "uniform": counts[0] if len(set(counts)) == 1 else None,
                    "per_host": per_host, "min": min(counts)}
    return None


def _geometry_bound(geometry: Mapping[str, Any], hosts: List[str]) -> int:
    """Smallest task count among the launch hosts (a host_slice may differ)."""
    if geometry.get("uniform"):
        return int(geometry["uniform"])
    per_host = geometry.get("per_host") or {}
    counts = []
    for host in hosts:
        short = _short(host)
        if short in per_host:
            counts.append(per_host[short])
            continue
        # A hostfile suffix (e.g. node1-40g) still names node1.
        owners = [name for name in per_host if short.startswith(name)]
        if owners:
            counts.append(per_host[max(owners, key=len)])
    if counts:
        return min(counts)
    return int(geometry.get("min") or 1)


def physical_cores(root: str = "/sys/devices/system/cpu") -> Optional[int]:
    """Physical cores this process may run on (hardware threads counted once)."""
    try:
        cpus = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        count = os.cpu_count()
        return count or None
    cores = set()
    for cpu in cpus:
        base = os.path.join(root, f"cpu{cpu}", "topology")
        try:
            with open(os.path.join(base, "core_id"), encoding="utf-8") as stream:
                core = stream.read().strip()
            with open(os.path.join(base, "physical_package_id"), encoding="utf-8") as stream:
                package = stream.read().strip()
        except OSError:
            return len(cpus) or None
        cores.add((package, core))
    return len(cores) or None


def resolve_ppn(
    nprocs: Optional[int],
    ppn: Optional[int],
    hosts: List[str],
    environ: Mapping[str, str],
) -> tuple[Optional[int], Optional[Dict[str, Any]]]:
    """Return ``(ppn, placement record)`` for one launch.

    The record is None outside an allocation or when placement is off; the
    package's ``ppn`` is then used unchanged.
    """
    if str(environ.get("JARVIS_MPI_PLACEMENT", "")).lower() in {"off", "0", "false", "package"}:
        return ppn, None
    job = allocation(environ)
    if job is None:
        return ppn, None
    n_hosts = max(1, len(hosts))
    geometry = task_geometry(environ)
    record: Dict[str, Any] = {
        "schema_version": PLACEMENT_SCHEMA,
        "scheduler": job["scheduler"],
        "job_id": job["job_id"],
        "nprocs": nprocs,
        "hosts": n_hosts,
        "ppn_requested": ppn,
        "geometry": geometry,
    }
    if geometry is not None:
        bound = _geometry_bound(geometry, hosts)
        record["source"] = "scheduler_geometry"
    else:
        bound = physical_cores()
        record["source"] = "physical_cores"
        record["physical_cores"] = bound
        if not bound:
            record["source"] = "package"
            record["ppn"] = ppn
            record["adjusted"] = False
            record["reason"] = "no task geometry and no core count; package ppn kept"
            return ppn, record
    record["bound"] = bound
    label = ("granted" if record["source"] == "scheduler_geometry"
             else "physical cores available")
    total = int(nprocs) if isinstance(nprocs, int) and nprocs > 0 else None
    needed = math.ceil(total / n_hosts) if total else None
    over_grant = needed is not None and needed > bound
    if ppn is not None and ppn <= bound and (needed is None or ppn >= needed):
        resolved = ppn
        reason = f"package ppn {ppn} fits the {bound} per node {label} and places all ranks"
    elif over_grant:
        resolved = needed
        reason = (f"{total} ranks on {n_hosts} host(s) need {needed} per node, "
                  f"more than the {bound} {label}")
    else:
        resolved = bound
        if ppn is None:
            reason = f"no package ppn; {bound} per node {label}"
        elif ppn > bound:
            reason = f"package ppn {ppn} exceeds the {bound} per node {label}"
        else:
            reason = (f"package ppn {ppn} on {n_hosts} host(s) cannot place "
                      f"{total} ranks; {bound} per node {label}")
    record["ppn"] = resolved
    record["adjusted"] = resolved != ppn
    record["reason"] = reason
    if over_grant:
        record["warning"] = "oversubscribed: nprocs exceeds the tasks the allocation granted"
    return resolved, record
