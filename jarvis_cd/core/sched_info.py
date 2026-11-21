"""``jarvis sched info``: what the site scheduler, MPI and network offer.

One ``jarvis.sched-info.v1`` document built from light, short reads only
(``sinfo``, ``scontrol show partition``, ``spack find``, the resource graph),
so it is safe on a login node:

* ``provider``: the scheduler (``--provider``, else the client on PATH);
* ``partitions``: state, default, node counts (allocated/idle/other/total),
  CPUs and memory per node, gres, and limits (max/default time, min/max
  nodes, QoS, allowed accounts) from ``scontrol show partition -o``;
* ``account``: what jarvis would submit with (``SBATCH_ACCOUNT``...);
* ``mpi``: the Spack MPI candidates the installer would offer, the MPI
  launchers jarvis supports, and ``mpiexec`` on PATH;
* ``net``: the resource graph's effective data/control network;
* ``site_skills``: the skills stored next to the graph.

Missing pieces become ``issues`` (``jarvis.issue.v1``), never an exception.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHED_INFO_SCHEMA = "jarvis.sched-info.v1"

#: scheduler client on PATH -> jarvis provider name
_CLIENTS = (("sbatch", "slurm"), ("qsub", "pbs"), ("bsub", "lsf"), ("flux", "flux"))
_PARTITION_LIMITS = {
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


def detect_provider() -> Optional[str]:
    for client, provider in _CLIENTS:
        if shutil.which(client):
            return provider
    return None


def _int_or_text(value: str) -> Any:
    if value.isdigit():
        return int(value)
    if value in ("UNLIMITED", "NONE", "N/A", "(null)", ""):
        return None if value != "UNLIMITED" else "UNLIMITED"
    return value


def slurm_partition_limits() -> Dict[str, Dict[str, Any]]:
    """``scontrol show partition -o``: one ``Key=Value`` line per partition."""
    from jarvis_cd.core.scheduler import _parse_slurm_duration, _run_scheduler_query

    completed, _diagnostic = _run_scheduler_query(["scontrol", "show", "partition", "-o"])
    if completed is None or completed.returncode != 0:
        return {}
    limits: Dict[str, Dict[str, Any]] = {}
    for line in completed.stdout.splitlines():
        fields = dict(
            token.split("=", 1) for token in line.split() if "=" in token
        )
        name = fields.get("PartitionName")
        if not name:
            continue
        entry: Dict[str, Any] = {}
        for key, target in _PARTITION_LIMITS.items():
            if key in fields:
                entry[target] = _int_or_text(fields[key])
        for key in ("max_time", "default_time"):
            if isinstance(entry.get(key), str) and entry[key] != "UNLIMITED":
                entry[f"{key}_s"] = _parse_slurm_duration(entry[key])
        for key in ("allow_accounts", "allow_qos", "allow_groups"):
            if isinstance(entry.get(key), str):
                entry[key] = entry[key].split(",")
        limits[name] = entry
    return limits


def partitions(provider: str) -> List[Dict[str, Any]]:
    from jarvis_cd.core.scheduler import scheduler_partition_facts

    facts = scheduler_partition_facts(provider)
    if provider == "slurm":
        limits = slurm_partition_limits()
        for entry in facts:
            entry["limits"] = limits.get(entry["partition"], {})
    return facts


def account_facts() -> Dict[str, Any]:
    names = ("SBATCH_ACCOUNT", "SLURM_ACCOUNT", "SBATCH_QOS", "SBATCH_PARTITION",
             "PBS_ACCOUNT", "LSB_DEFAULTPROJECT")
    return {name: os.environ[name] for name in names if os.environ.get(name)}


def mpi_facts(issues: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Spack MPI candidates (light ``spack find``), launchers, PATH mpiexec."""
    from jarvis_cd.core.spack_install import (
        InstallContext,
        SpackPlanner,
        spack_missing_issue,
        spack_unavailable_reason,
        discover_site_spacks,
    )

    from jarvis_cd.shell.exec_info import ExecType

    supported = [kind.value for kind in ExecType
                 if kind.value in ("openmpi", "mpich", "intel_mpi", "cray_mpich")]
    document: Dict[str, Any] = {
        "launchers": supported,
        "path_mpiexec": shutil.which("mpiexec") or shutil.which("mpirun"),
        "spack_candidates": [],
        "spack_available": False,
        "choice_needed": False,
    }
    reason = spack_unavailable_reason()
    if reason is not None:
        issue = spack_missing_issue(reason, discover_site_spacks())
        issues.append(issue)
        return document
    document["spack_available"] = True
    with tempfile.TemporaryDirectory(prefix="jarvis-sched-info-") as scratch:
        context = InstallContext(install_dir=Path(scratch), mode="check",
                                 decisions_path=Path(scratch) / "decisions.yaml")
        try:
            candidates = SpackPlanner(context).mpi_candidates()
        except RuntimeError as error:
            document["spack_error"] = str(error)[:1024]
            return document
    document["spack_candidates"] = [
        {key: candidate.get(key) for key in
         ("id", "name", "version", "constraint", "prefix", "hashes",
          "external_hashes", "modules", "source")}
        for candidate in candidates
    ]
    document["choice_needed"] = len(candidates) > 1
    if len(candidates) > 1:
        document["note"] = ("several MPIs: an install asks ambiguous_provider unless "
                            "a package or answer pins one")
    return document


def sched_info(jarvis: Any, provider: Optional[str] = None, *, include_mpi: bool = True,
               extra_issues: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    from jarvis_cd.core.issues import make_jarvis_issue
    from jarvis_cd.core.scheduler import _SCHEDULERS

    issues: List[Dict[str, Any]] = list(extra_issues or [])
    chosen = (provider or "").strip().lower() or detect_provider()
    document: Dict[str, Any] = {
        "schema_version": SCHED_INFO_SCHEMA,
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "host": os.uname().nodename if hasattr(os, "uname") else None,
        "provider": chosen,
        "providers_supported": sorted(_SCHEDULERS),
        "in_allocation": None,
        "partitions": [],
        "account": account_facts(),
    }
    from jarvis_cd.core.spack_install import current_allocation

    document["in_allocation"] = current_allocation()
    if chosen is None:
        issues.append(make_jarvis_issue(
            kind="scheduler_missing", subject="scheduler",
            message="no scheduler client (sbatch, qsub, bsub, flux) on PATH",
            field="--provider"))
    elif chosen not in _SCHEDULERS:
        issues.append(make_jarvis_issue(
            kind="scheduler_unsupported", subject="scheduler",
            message=f"jarvis has no {chosen} scheduler provider yet",
            identity={"provider": chosen}, field="--provider",
            details={"supported": sorted(_SCHEDULERS)}))
    else:
        document["partitions"] = partitions(chosen)
        if not document["partitions"]:
            issues.append(make_jarvis_issue(
                kind="scheduler_query_failed", subject="scheduler",
                message=f"{chosen} reported no partitions", identity={"provider": chosen}))
    if include_mpi:
        document["mpi"] = mpi_facts(issues)
    try:
        from jarvis_cd.core.resource_graph import ResourceGraphManager

        report = ResourceGraphManager().net_report()
        document["net"] = {key: report.get(key) for key in
                           ("graph", "site_graph", "present", "effective", "override",
                            "collected_at", "collected_in")}
        issues.extend(report.get("issues") or [])
    except Exception as error:
        document["net"] = {"error": str(error)[:1024]}
    try:
        from jarvis_cd.core.site_skills import SiteSkills

        document["site_skills"] = SiteSkills(
            jarvis.resource_graph_file,
            site=bool(getattr(jarvis, "resource_graph_is_site", False)),
        ).summary()
    except Exception as error:
        document["site_skills"] = []
        document["site_skills_error"] = str(error)[:1024]
    document["issues"] = issues
    return document
