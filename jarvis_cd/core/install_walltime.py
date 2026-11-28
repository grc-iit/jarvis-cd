"""Install job wall time from JARVIS's own build estimate.

``jarvis ppl install +submit`` sets the job's time limit itself unless
``scheduler.install.time`` is given (``scheduler.time`` is the run job's
limit; an install job never inherits it):

    time = (startup + estimate) x safety factor, rounded up to 5 min,
           at most the partition's MaxTime

``estimate`` is the check plus the build of what is still missing, from the
same per-package estimates install placement uses (:func:`install_placement.
estimate`): only seconds this site measured, the missing Spack nodes of the
last full check's ``install-check.json`` each timed by an earlier build here
(Spack's ``install_times.json``, recorded after every build job whatever its
outcome). A check record whose only changed inputs are the recorded decisions
(answers given after the check, often to its own questions) or ``spack.lock``
still counts. Nodes a previous install job of this pipeline already built (a
timed-out or failed attempt; Spack keeps them) are not counted again and are
reported as ``already_built``.

When anything is unmeasured (a node never built here, a check never timed, a
non-Spack install), the time is the install partition's MaxTime. MaxTime comes
from the scheduler (Slurm ``scontrol show partition -o``; the resource graph
does not record partition limits). A measured estimate longer than MaxTime is
capped at it. Either way the document says the build may take more than one
job (``may_take_more_than_one_job``): a job that runs out of time is submitted
again, and Spack keeps every package already built.

Everything here is light (file reads, one ``scontrol``, ``sacct`` for a
previous job still recorded as live) and writes nothing, so ``+plan`` uses it.
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

WALLTIME_SCHEMA = "jarvis.install.walltime.v1"

#: queue-to-build overhead: job start, environment load, Spack bootstrap
STARTUP_SECONDS = 600
SAFETY_FACTOR = 1.5
ROUND_SECONDS = 300
#: previous install executions scanned for already-built nodes
MAX_ATTEMPTS = 10

_BUILT = re.compile(r"^==> ([A-Za-z0-9_.+-]+): Successfully installed (\S+)\s*$")


def _env_number(name: str, default: float, minimum: float) -> float:
    try:
        value = float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return value if value >= minimum else default


def startup_seconds() -> float:
    return _env_number("JARVIS_INSTALL_STARTUP_SECONDS", STARTUP_SECONDS, 0)


def safety_factor() -> float:
    return _env_number("JARVIS_INSTALL_TIME_FACTOR", SAFETY_FACTOR, 1.0)


def format_time(seconds: float, provider: Optional[str]) -> str:
    """A time directive: ``D-HH:MM:SS`` for Slurm past a day, else ``HH:MM:SS``."""
    minutes = int(math.ceil(seconds / 60.0))
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    if provider == "slurm" and days:
        return f"{days}-{hours:02d}:{mins:02d}:00"
    return f"{days * 24 + hours:02d}:{mins:02d}:00"


def _human(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    minutes = int(math.ceil(seconds / 60.0))
    hours, mins = divmod(minutes, 60)
    return f"{hours} h {mins:02d} min" if hours else f"{mins} min"


# ---------------------------------------------------------------------------
# Partition limits
# ---------------------------------------------------------------------------


def slurm_partitions() -> Optional[List[Dict[str, Any]]]:
    """Every partition's MaxTime, DefaultTime, state and default flag."""
    from jarvis_cd.core.queues import parse_slurm_duration as _parse_slurm_duration
    from jarvis_cd.core.queues import run_query

    completed, _diagnostic = run_query(["scontrol", "show", "partition", "-o"])
    if completed is None or completed.returncode != 0:
        return None
    partitions = []
    for line in completed.stdout.splitlines():
        fields = dict(token.split("=", 1) for token in line.split() if "=" in token)
        name = fields.get("PartitionName")
        if not name:
            continue
        max_time = fields.get("MaxTime")
        default_time = fields.get("DefaultTime")
        partitions.append({
            "partition": name,
            "default": fields.get("Default") == "YES",
            "state": fields.get("State"),
            "max_time": max_time,
            "max_time_s": _parse_slurm_duration(max_time) if max_time else None,
            "default_time": default_time,
            "default_time_s": (_parse_slurm_duration(default_time)
                               if default_time else None),
        })
    return partitions


def partition_limit(provider: Optional[str], partition: Optional[str]) -> Dict[str, Any]:
    """The MaxTime the install job's partition allows, and every partition's."""
    if provider != "slurm":
        return {"provider": provider, "partition": partition, "max_time_s": None,
                "source": None,
                "why": f"partition limits are only read for Slurm (provider {provider})"}
    listing = slurm_partitions()
    if listing is None:
        return {"provider": provider, "partition": partition, "max_time_s": None,
                "source": "scontrol show partition -o",
                "why": "scontrol show partition failed; no MaxTime known"}
    chosen = next((entry for entry in listing
                   if (entry["partition"] == partition if partition else entry["default"])),
                  None)
    limit: Dict[str, Any] = {
        "provider": provider,
        "partition": chosen["partition"] if chosen else partition,
        "partition_source": "scheduler.partition" if partition else "slurm default",
        "source": "scontrol show partition -o",
        "max_time": chosen.get("max_time") if chosen else None,
        "max_time_s": chosen.get("max_time_s") if chosen else None,
        "default_time": chosen.get("default_time") if chosen else None,
        "partitions": listing,
    }
    if chosen is None:
        limit["why"] = (f"partition {partition} not found" if partition
                        else "no default partition")
    return limit


# ---------------------------------------------------------------------------
# Previous attempts
# ---------------------------------------------------------------------------


def built_nodes(log_path: Path) -> List[Dict[str, str]]:
    """Spack nodes an install log says were built (``Successfully installed``)."""
    built: Dict[str, Dict[str, str]] = {}
    try:
        with open(log_path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = _BUILT.match(line.rstrip("\n"))
                if match:
                    spec = match.group(2)
                    built[spec] = {"name": match.group(1), "spec": spec,
                                   "hash": spec.rsplit("-", 1)[-1]}
    except OSError:
        return []
    return list(built.values())


def previous_attempts(ppl, *, exclude: Optional[str] = None) -> Dict[str, Any]:
    """Unfinished install jobs since the last one that finished, newest first.

    A Slurm TIMEOUT kills the job before it updates its progress, so an
    attempt is any install execution whose ``metadata.install.phase`` is not
    ``done``. A record still shown as live is observed once (read-only).
    """
    try:
        records = ppl._execution_store().list()
    except Exception as exc:  # no store yet, or unreadable: nothing to report
        return {"attempts": [], "already_built": [], "why": str(exc)[:256]}
    installs = [record for record in records
                if (record.metadata or {}).get("action") == "install"
                and ((record.metadata or {}).get("install") or {}).get("mode") == "install"
                and record.execution_id != exclude]
    installs.sort(key=lambda record: record.created_at, reverse=True)
    attempts: List[Dict[str, Any]] = []
    built: Dict[str, Dict[str, str]] = {}
    for record in installs[:MAX_ATTEMPTS]:
        progress = (record.metadata or {}).get("install") or {}
        if progress.get("phase") == "done":
            break
        observation = (record.metadata or {}).get("scheduler_observation") or {}
        state, provider_state = record.state, observation.get("provider_state")
        if not record.terminal and record.scheduler_provider and record.scheduler_native_id:
            try:
                from jarvis_cd.core.scheduler import query_scheduler_execution

                observed = query_scheduler_execution(
                    record.scheduler_provider, record.scheduler_native_id,
                    execution_root=Path(ppl._execution_store().executions_dir)
                    / record.execution_id)
                if observed.state not in ("unknown", "missing"):
                    state, provider_state = observed.state, observed.provider_state
            except Exception:
                pass
        log = progress.get("log") or str(
            Path(ppl._execution_store().executions_dir) / record.execution_id
            / "install" / "install.log")
        nodes = built_nodes(Path(log))
        for node in nodes:
            built.setdefault(node["spec"], node)
        attempts.append({
            "execution_id": record.execution_id,
            "native_id": record.scheduler_native_id,
            "state": state,
            "provider_state": provider_state,
            "phase": progress.get("phase"),
            "log": log,
            "built": len(nodes),
        })
    return {"attempts": attempts, "already_built": sorted(
        built.values(), key=lambda node: node["spec"])}


# ---------------------------------------------------------------------------
# Estimate
# ---------------------------------------------------------------------------


def _decisions_path(ppl) -> Path:
    from jarvis_cd.core.installer import shared_install_dir

    configured = os.environ.get("JARVIS_SPACK_DECISIONS", "").strip()
    return Path(configured) if configured else shared_install_dir(ppl) / "decisions.yaml"


def _missing_names(entries: Sequence[Mapping[str, Any]]) -> List[str]:
    return [str(entry.get("name")) for entry in entries]


def install_estimate(ppl, *, install_check: bool,
                     built_hashes: Sequence[str] = ()) -> Dict[str, Any]:
    """Estimated seconds of the job's check and build, package by package."""
    from jarvis_cd.core import install_placement as placement
    from jarvis_cd.core.installer import Installer, read_install_check

    costs = placement.read_costs(placement.costs_path(_decisions_path(ppl)))
    groups = Installer.get_installers(ppl)
    spack_pkgs = list(groups.get("spack") or [])
    packages: List[Dict[str, Any]] = []
    record: Dict[str, Any] = {"used": False}
    missing_by_pkg: Optional[Dict[str, List[Dict[str, Any]]]] = None
    if spack_pkgs and not install_check:
        document, why = read_install_check(ppl, spack_pkgs, for_estimate=True)
        if document is not None:
            record = {"used": True, "checked_at": document.get("checked_at"),
                      "where": (document.get("where") or {}).get("host")}
            if why:
                # answers recorded after the check: its node count still holds
                record["stale"] = why
            missing_by_pkg = {}
            for name, entry in (document.get("packages") or {}).items():
                if isinstance(entry, Mapping) and entry.get("status") == "missing":
                    missing_by_pkg[str(name)] = [
                        dict(item) for item in entry.get("missing") or []
                        if isinstance(item, Mapping)]
            for issue in document.get("issues") or []:
                if isinstance(issue, Mapping) and issue.get("kind") == "not_installed" \
                        and issue.get("package"):
                    missing_by_pkg.setdefault(str(issue["package"]), [])
        else:
            record["why"] = why
    built = {str(value) for value in built_hashes}
    for pkg_def in spack_pkgs:
        pkg_id = str(pkg_def.get("pkg_id"))
        name = placement.spec_name(pkg_def)
        check = placement.estimate("check", name, costs)
        packages.append({"pkg_id": pkg_id, "method": "spack", "phase": "check",
                         "seconds": check["seconds"], "basis": check["basis"]})
        if install_check:
            continue
        item: Dict[str, Any] = {"pkg_id": pkg_id, "method": "spack", "phase": "build"}
        if missing_by_pkg is not None and pkg_id not in missing_by_pkg:
            item.update(seconds=0.0, basis="installed per the last full check")
        elif missing_by_pkg is not None and missing_by_pkg[pkg_id]:
            entries = missing_by_pkg[pkg_id]
            remaining = [entry for entry in entries
                         if not any(str(entry.get("hash") or "").startswith(h)
                                    or h.startswith(str(entry.get("hash") or "-"))
                                    for h in built)]
            skipped = len(entries) - len(remaining)
            full = placement.estimate("build", name, costs, _missing_names(remaining))
            basis = full["basis"]
            if skipped:
                basis += f"; {skipped} of {len(entries)} missing node(s) already built"
            item.update(seconds=full["seconds"], basis=basis, missing=len(entries),
                        already_built=skipped)
            if full.get("unmeasured"):
                item["unmeasured"] = full["unmeasured"]
        else:
            full = placement.estimate("build", name, costs, None)
            item.update(seconds=full["seconds"], basis=full["basis"]
                        + (f"; check record not used ({record['why']})"
                           if full["seconds"] is None and record.get("why") else ""))
        packages.append(item)
    for method, pkg_defs in sorted(groups.items()):
        if method == "spack":
            continue
        for pkg_def in pkg_defs:
            packages.append({"pkg_id": str(pkg_def.get("pkg_id")), "method": method,
                             "phase": "check" if install_check else "install",
                             "seconds": None,
                             "basis": f"unmeasured: JARVIS does not time {method} installs"})
    unknown = [item["pkg_id"] for item in packages if item["seconds"] is None]
    total = None if unknown else round(sum(float(item["seconds"]) for item in packages), 1)
    return {"seconds": total, "human": _human(total), "known": not unknown,
            "unknown": sorted(set(unknown)), "packages": packages,
            "check_record": record}


# ---------------------------------------------------------------------------
# The time directive
# ---------------------------------------------------------------------------


def resolve(ppl, *, provider: Optional[str], partition: Optional[str],
            install_check: bool, explicit_time: Any = None,
            exclude: Optional[str] = None) -> Dict[str, Any]:
    """``jarvis.install.walltime.v1``: the install job's time and its basis."""
    if explicit_time is not None:
        return {"schema_version": WALLTIME_SCHEMA, "source": "explicit",
                "time": explicit_time}
    previous = previous_attempts(ppl, exclude=exclude) if not install_check else \
        {"attempts": [], "already_built": []}
    estimate = install_estimate(
        ppl, install_check=install_check,
        built_hashes=[node["hash"] for node in previous["already_built"]])
    startup = startup_seconds()
    factor = safety_factor()
    limit = partition_limit(provider, partition)
    document: Dict[str, Any] = {
        "schema_version": WALLTIME_SCHEMA,
        "startup_seconds": startup,
        "safety_factor": factor,
        "estimate": estimate,
        "partition_limit": {key: value for key, value in limit.items()
                            if key != "partitions"},
        "previous": previous,
    }
    max_s = limit.get("max_time_s")
    partition_name = limit.get("partition") or "the default partition"
    if not estimate["known"]:
        document["why"] = ("unmeasured at this site: " + ", ".join(estimate["unknown"])
                           + "; nothing measured to estimate from")
        if max_s is None:
            # no MaxTime to give (not Slurm, UNLIMITED, or scontrol failed):
            # the scheduler's own limit applies
            document.update(source="scheduler_limit", time=None,
                            formula=f"unmeasured; {limit.get('why') or 'no MaxTime'}: "
                                    "no time directive, the scheduler's limit applies")
        else:
            document.update(source="partition_max_time", time_seconds=int(max_s),
                            time=format_time(max_s, provider),
                            formula=f"unmeasured: partition {partition_name} "
                                    f"MaxTime {limit.get('max_time')}")
        document["may_take_more_than_one_job"] = True
    else:
        needed = startup + float(estimate["seconds"])
        wall = math.ceil(needed * factor / ROUND_SECONDS) * ROUND_SECONDS
        capped = max_s is not None and wall > max_s
        if max_s is not None and capped:
            wall = int(max_s)
        document.update(source="jarvis_estimate", needed_seconds=round(needed, 1),
                        time_seconds=int(wall), time=format_time(wall, provider),
                        capped_at_max_time=capped,
                        may_take_more_than_one_job=max_s is not None and needed > max_s,
                        formula=f"({_human(startup)} + {estimate['human']}) x {factor:g}"
                                + (f", capped at MaxTime {limit.get('max_time')}"
                                   if capped else ""))
    if document["may_take_more_than_one_job"]:
        document["note"] = (
            "the build may take more than one job: if this one reaches its time "
            "limit, submit the install again; Spack keeps every package already "
            "built and the next job builds the rest")
    return document


def summary(document: Mapping[str, Any]) -> Dict[str, Any]:
    """The part of the document an execution record keeps."""
    if document.get("source") == "explicit":
        return {"source": "explicit"}
    keep = {key: document.get(key) for key in
            ("source", "time", "time_seconds", "needed_seconds", "startup_seconds",
             "safety_factor", "capped_at_max_time", "may_take_more_than_one_job",
             "formula", "why", "note", "partition_limit")
            if document.get(key) is not None}
    estimate = document.get("estimate") or {}
    keep["estimate"] = {key: estimate.get(key) for key in
                        ("seconds", "human", "known", "unknown", "packages",
                         "check_record")}
    previous = document.get("previous") or {}
    keep["previous_attempts"] = previous.get("attempts") or []
    keep["already_built"] = [node["spec"] for node in previous.get("already_built") or []]
    return keep
