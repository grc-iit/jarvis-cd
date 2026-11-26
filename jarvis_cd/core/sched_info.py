"""``jarvis sched info``: what the site scheduler, MPI and network offer.

One ``jarvis.sched-info.v1`` document built from light, short reads only
(``sinfo``, ``scontrol show partition``, ``spack find``, the resource graph),
so it is safe on a login node:

* ``provider``: the scheduler (``--provider``, else the client on PATH);
* ``partitions``: state, default, node counts (allocated/idle/other/total),
  CPUs and memory per node, gres, and limits (max/default time, min/max
  nodes, QoS, allowed accounts). Slurm reads ``sinfo`` and ``scontrol show
  partition -o``; PBS ``qstat -Qf``/``-Bf``, LSF ``bqueues -l``/``bparams``
  and Flux ``flux queue list`` give their queues the same ``limits`` keys
  (see :mod:`jarvis_cd.core.queue_limits`);
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
from typing import Any, Dict, List, Optional, Tuple

SCHED_INFO_SCHEMA = "jarvis.sched-info.v1"

#: scheduler client on PATH -> jarvis provider name
_CLIENTS = (("sbatch", "slurm"), ("qsub", "pbs"), ("bsub", "lsf"), ("flux", "flux"))


def detect_provider() -> Optional[str]:
    for client, provider in _CLIENTS:
        if shutil.which(client):
            return provider
    return None


def partitions(provider: str) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Partitions (queues) with their limits, and the raw error of a failed read."""
    from jarvis_cd.core.queue_limits import QUEUE_READERS, attach_slurm_limits
    from jarvis_cd.core.scheduler import scheduler_partition_facts

    if provider == "slurm":
        facts = scheduler_partition_facts(provider)
        if not facts:
            return [], None
        return facts, attach_slurm_limits(facts)
    reader = QUEUE_READERS.get(provider)
    return reader() if reader else ([], None)


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
         ("id", "key", "name", "version", "constraint", "aliases", "prefix", "hashes",
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
    from jarvis_cd.core.queue_limits import QUEUE_READERS
    from jarvis_cd.core.scheduler import _SCHEDULERS

    issues: List[Dict[str, Any]] = list(extra_issues or [])
    chosen = (provider or "").strip().lower() or detect_provider()
    document: Dict[str, Any] = {
        "schema_version": SCHED_INFO_SCHEMA,
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "host": os.uname().nodename if hasattr(os, "uname") else None,
        "provider": chosen,
        "providers_supported": sorted(_SCHEDULERS),
        "providers_queues_readable": sorted(set(_SCHEDULERS) | set(QUEUE_READERS)),
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
    elif chosen not in _SCHEDULERS and chosen not in QUEUE_READERS:
        issues.append(make_jarvis_issue(
            kind="scheduler_unsupported", subject="scheduler",
            message=f"jarvis has no {chosen} scheduler provider yet",
            identity={"provider": chosen}, field="--provider",
            details={"supported": sorted(_SCHEDULERS),
                     "queues_readable": sorted(QUEUE_READERS)}))
    else:
        if chosen not in _SCHEDULERS:
            issues.append(make_jarvis_issue(
                kind="scheduler_unsupported", subject="scheduler",
                message=(f"jarvis reads {chosen} queues and their limits, "
                         f"but cannot submit to {chosen} yet"),
                identity={"provider": chosen}, field="--provider",
                details={"supported": sorted(_SCHEDULERS), "queues_read": True}))
        document["partitions"], error = partitions(chosen)
        if error is not None:
            issues.append(make_jarvis_issue(
                kind="scheduler_query_failed", subject="scheduler",
                message=f"{error['command']} failed: {error.get('output') or ''}".strip(),
                identity={"provider": chosen, "command": error["command"]},
                details=error))
        elif not document["partitions"]:
            issues.append(make_jarvis_issue(
                kind="scheduler_query_failed", subject="scheduler",
                message=f"{chosen} reported no partitions", identity={"provider": chosen}))
    if include_mpi:
        document["mpi"] = mpi_facts(issues)
    try:
        from jarvis_cd.core.resource_graph import ResourceGraphManager

        report = ResourceGraphManager().net_report()
        document["net"] = {key: report.get(key) for key in
                           ("graph", "site_graph", "present", "form", "effective", "override",
                            "collected_at", "collected_in", "legacy", "error")}
        issues.extend(report.get("issues") or [])
        # per-node topology a collector read on the nodes (partitions carry sinfo's)
        document["cpu"] = ResourceGraphManager().resource_graph.cpu
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
