"""Plan-only submissions: ``jarvis ppl submit +plan +json``.

A plan resolves everything a real ``+submit`` would, without submitting and
without Spack: the scheduler and its directives, the total nodes, each
component's nodes and ranks, the couplings between components, the exact
PSI/J job description, and the issues that would refuse the submit (exit 3 or 5).

The plan binds to the later submission by ``plan_sha256``, a digest of the
job description bytes and the sealed snapshot input. ``jarvis ppl submit ...
--execution-id ID --plan_sha256 SHA`` renders again and refuses with a
``submission_plan_changed`` issue (exit 5) before recording or submitting
anything when they differ.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, List, Mapping, Optional

from jarvis_cd.core.pipeline_check import package_name

PLAN_SCHEMA = "jarvis.submit-plan.v1"

#: directives a job card shows, in order
_DIRECTIVE_FIELDS = ("partition", "account", "qos", "time", "job_name")


def submission_plan_sha256(script_sha256: str, input_sha256: str) -> str:
    """The digest a person's yes binds to: the script and the sealed input."""
    payload = json.dumps(
        {"schema": PLAN_SCHEMA, "script_sha256": script_sha256,
         "input_sha256": input_sha256},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _time_limit_seconds(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value * 60  # Slurm: a bare number is minutes
    from jarvis_cd.core.queues import parse_slurm_duration

    return parse_slurm_duration(str(value))


def _source(key: str, spec: Mapping[str, Any], declared: Mapping[str, Any],
            overrides: Mapping[str, Any], forced: tuple = ()) -> str:
    if key in overrides:
        return "pipeline.scheduler.install"
    if key in forced:
        return "jarvis"
    if key in declared:
        return "pipeline.scheduler"
    if spec.get(key) is not None:
        return "jarvis"
    return "unset"


def _components(ppl: Any, check: Optional[Mapping[str, Any]], *, action: str,
                install_check: bool, nodes: Optional[int]) -> List[Dict[str, Any]]:
    if action == "install":
        return [{
            "id": "install",
            "package": "jarvis.install",
            "label": "install check" if install_check else "install",
            "role": "batch",
            "starts": "foreground",
            "nodes": 1,
            "node_range": {"first": 0, "end": 1},
            "host_slice": None,
            "ranks": 1,
            "packages": [str(pkg.get("pkg_id")) for pkg in ppl.packages],
        }]
    components = []
    for step in (check or {}).get("steps") or []:
        index = step.get("index")
        config = {}
        if isinstance(index, int) and index < len(ppl.packages):
            config = ppl.packages[index].get("config") or {}
        components.append({
            "id": step.get("pkg_id"),
            "package": step.get("pkg_type"),
            "label": step.get("label") or package_name(step.get("pkg_type"),
                                                       step.get("pkg_id")),
            "role": step.get("role"),
            "starts": step.get("starts"),
            "after_steps": str(config.get("after_steps") or "auto"),
            "nodes": step.get("node_count", nodes),
            "node_range": step.get("nodes"),
            "host_slice": step.get("host_slice"),
            "ranks": step.get("ranks"),
            "ports": step.get("ports") or [],
            **({"readiness": step["readiness"]} if step.get("readiness") else {}),
        })
    return components


def _install_summary(ppl: Any, outcome: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """What the load's install phase decided on this host (no Spack ran)."""
    report = dict((outcome or {}).get("report")
                  or getattr(ppl, "_install_load_report", None) or {})
    activation = dict(getattr(ppl, "_install_activation", None) or {})
    check = dict(report.get("check") or activation.get("check") or {})
    placement = dict(report.get("placement") or {})
    summary: Dict[str, Any] = {
        "policy": getattr(ppl, "_install_policy", None),
        "status": ((report.get("installers") or {}).get("spack") or {}).get("status"),
        "check": {key: check.get(key) for key in
                  ("source", "outcome", "reason", "checked_at", "checked_where",
                   "record", "missing", "deferred_to") if check.get(key) is not None},
        "runs_in": "allocation",
    }
    plan_only = placement.get("plan_only")
    if plan_only:
        # A consented login_limited check (or build) runs here at submit.
        summary["at_submit"] = plan_only.get("at_submit")
        summary["at_submit_limits"] = plan_only.get("limits")
        summary["runs_in"] = "submitting host (login_limited), then allocation"
    if placement:
        summary["placement"] = {key: placement.get(key) for key in
                                ("check", "build", "fallback") if placement.get(key)}
    if outcome:
        summary["exit_code"] = outcome.get("exit_code")
        if outcome.get("refused"):
            summary["refused"] = outcome["refused"]
    return summary


def _partition_topology(spec: Mapping[str, Any]) -> Dict[str, Any]:
    """The submit partition's node topology: sinfo, else the resource graph."""
    from jarvis_cd.core.queues import partition_facts

    fields = ("sockets_per_node", "cores_per_socket", "threads_per_core",
              "physical_cores_per_node", "cpus_per_node", "heterogeneous")
    wanted = spec.get("partition")
    try:
        facts = partition_facts("slurm")
    except Exception:
        facts = []
    for entry in facts:
        if (entry.get("partition") == wanted if wanted else entry.get("default")):
            if entry.get("physical_cores_per_node"):
                return {"partition": entry.get("partition"), "source": "sinfo",
                        **{key: entry.get(key) for key in fields}}
    try:
        from jarvis_cd.core.resource_graph import ResourceGraphManager

        cpu = ResourceGraphManager().resource_graph.cpu or {}
    except Exception:
        cpu = {}
    smallest = cpu.get("smallest") or {}
    if smallest.get("physical_cores"):
        return {"partition": wanted, "source": "resource_graph.cpu",
                "sockets_per_node": smallest.get("sockets"),
                "cores_per_socket": smallest.get("cores_per_socket"),
                "threads_per_core": smallest.get("threads_per_core"),
                "physical_cores_per_node": smallest.get("physical_cores"),
                "cpus_per_node": smallest.get("cpus"),
                "heterogeneous": cpu.get("heterogeneous")}
    return {"partition": wanted, "source": None, **dict.fromkeys(fields)}


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def cores_plan(provider: str, spec: Mapping[str, Any], nodes: Optional[int]) -> Dict[str, Any]:
    """What a run's request claims per node, in physical cores.

    The partition's topology is discovered (sinfo, else the collector's
    graph); the request is the rendered spec plus JARVIS's per-core default
    (:func:`jarvis_cd.core.queues.core_request`).
    """
    from jarvis_cd.core.queues import core_request

    request = core_request(provider, spec)
    topology = _partition_topology(spec)
    ranks_per_node = _int(spec.get("ntasks_per_node"))
    total = _int(spec.get("ntasks"))
    if ranks_per_node is None and total and nodes:
        ranks_per_node = math.ceil(total / max(1, nodes))
    cpus_per_task = _int(spec.get("cpus_per_task")) or 1
    per_core = _int(topology.get("threads_per_core"))
    cores = _int(topology.get("physical_cores_per_node"))
    cores_per_rank = None
    if request.get("ntasks_per_core") == 1 and per_core:
        cores_per_rank = math.ceil(cpus_per_task / per_core)
    claimed = (ranks_per_node * cores_per_rank
               if ranks_per_node and cores_per_rank else None)
    fits = claimed <= cores if claimed is not None and cores else None
    if claimed is not None and cores:
        meaning = (f"each rank claims {cores_per_rank} whole core(s): {ranks_per_node} "
                   f"ranks per node claim {claimed} of the {cores} physical cores "
                   f"({per_core} hardware thread(s) per core) on {topology['partition']}")
        if fits is False:
            meaning += "; Slurm refuses the submit (too few cores per node)"
        elif not spec.get("exclusive") and claimed < cores:
            meaning += "; other jobs may use the remaining cores (exclusive is not set)"
    elif request["source"] == "jarvis":
        meaning = "each rank claims a whole core (--ntasks-per-core=1); topology unknown"
    elif request["source"] == "unset":
        meaning = "no rank request; Slurm's default task per node"
    else:
        meaning = (f"per-core mapping set by the person ({request['key']}); JARVIS adds "
                   "no --ntasks-per-core")
    return {
        "schema_version": "jarvis.submit-cores.v1",
        **topology,
        "ranks_per_node": ranks_per_node,
        "cpus_per_task": cpus_per_task,
        "ntasks_per_core": request.get("ntasks_per_core"),
        "ntasks_per_core_source": request["source"],
        "cores_per_rank": cores_per_rank,
        "cores_claimed_per_node": claimed,
        "fits": fits,
        "exclusive": bool(spec.get("exclusive")),
        "meaning": meaning,
        "override": ("set scheduler.ntasks_per_core (false for Slurm's default of one "
                     "task per hardware thread), or cpus_per_task for more cores per rank"),
    }


def build_submit_plan(
    ppl: Any,
    execution_id: str,
    *,
    action: str = "run",
    install_check: bool = False,
    answers_bytes: Optional[bytes] = None,
    install_outcome: Optional[Mapping[str, Any]] = None,
    pipeline_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Return one ``jarvis.submit-plan.v1`` document for ``ppl``.

    ``install_outcome`` carries an install decision the load raised
    (``{"exit_code": 3|4, "issues": [...], "report": {...}}``); its issues
    join the plan's and its exit code is the plan's unless a check error
    (exit 5) wins, the same order a real submit refuses in.
    """
    from jarvis_cd.core.pipeline_check import check_pipeline
    from jarvis_cd.core.site_facts import EXIT_SITE_FACT

    issues: List[Dict[str, Any]] = []
    exit_code = 0
    check = None
    if action == "run":
        check = check_pipeline(ppl)
        if check["errors"]:
            issues.extend(check["errors"])
            exit_code = 5
        elif check["questions"]:
            issues.extend(check["questions"])
            exit_code = EXIT_SITE_FACT
    if install_outcome and exit_code == 0:
        issues.extend(dict(issue) for issue in install_outcome.get("issues") or [])
        exit_code = int(install_outcome.get("exit_code") or 3)

    # The snapshot reads the saved pipeline, exactly as submit() does.
    ppl.save()
    rendered = ppl._render_submission(
        execution_id, action=action, install_check=install_check,
        answers_bytes=answers_bytes)
    sched = rendered["scheduler"]
    spec = rendered["scheduler_spec"]
    walltime = getattr(ppl, "_install_walltime_last", None) if action == "install" else None
    if walltime and walltime.get("issues") and exit_code == 0:
        # an estimate past the partition's MaxTime: the person chooses
        issues.extend(dict(issue) for issue in walltime["issues"])
        exit_code = 3
    declared = dict(ppl.scheduler or {})
    overrides = declared.get("install") if action == "install" else None
    overrides = overrides if isinstance(overrides, Mapping) else {}
    nodes = spec.get("nodes")
    try:
        nodes = int(nodes) if nodes is not None else None
    except (TypeError, ValueError):
        nodes = None
    if nodes is None and check is not None:
        nodes = (check.get("allocation") or {}).get("nodes")

    execution_root = rendered["execution_root"]
    if action == "install":
        # ppl install +plan resolves the current pipeline (the plan made it current)
        submit_with = ["jarvis", "ppl", "install"] + (["+check"] if install_check else [])
        submit_with.append("+submit")
    else:
        submit_with = ["jarvis", "ppl", "submit"]
        if pipeline_file:
            submit_with.append(str(pipeline_file))
    submit_with += ["--execution-id", execution_id,
                    "--plan_sha256", rendered["plan_sha256"], "+json"]
    document: Dict[str, Any] = {
        "schema_version": PLAN_SCHEMA,
        "pipeline_id": ppl.name,
        "execution_id": execution_id,
        "action": action if not install_check else "install_check",
        "scheduler": sched.NAME,
        "partition": spec.get("partition"),
        "account": spec.get("account"),
        "qos": spec.get("qos"),
        "time": spec.get("time"),
        "time_limit_s": _time_limit_seconds(spec.get("time")),
        "job_name": spec.get("job_name"),
        "nodes": nodes,
        "sources": {key: _source(key, spec, declared, overrides,
                                 ("nodes",) if action == "install" else ())
                    for key in _DIRECTIVE_FIELDS + ("nodes",)},
        "components": _components(ppl, check, action=action,
                                  install_check=install_check, nodes=nodes),
        "couplings": list((check or {}).get("couplings") or []),
        "script": {
            "path": str(sched.script_path),
            "text": rendered["script_text"],
            "sha256": rendered["script_sha256"],
        },
        "input_sha256": rendered["input_sha256"],
        "plan_sha256": rendered["plan_sha256"],
        "execution_root": str(execution_root),
        "hostfile": str(sched.hostfile),
        "component_states": str(execution_root / "components.jsonl"),
        "submit_with": submit_with,
        "issues": issues,
        "exit_code": exit_code,
        "would_submit": exit_code == 0,
        "install": _install_summary(ppl, install_outcome),
        "warnings": list((check or {}).get("warnings") or []),
        "binding": (
            "submit with the same --execution-id and --plan_sha256; jarvis refuses "
            "(submission_plan_changed, exit 5) when the script or the sealed input "
            "differ. Plan and submit from the same working directory: the sealed "
            "input records it"),
    }
    if rendered.get("install_job_shape"):
        document["install_job"] = rendered["install_job_shape"]
    if action == "run" and sched.NAME == "slurm":
        document["cores"] = cores_plan(sched.NAME, spec, nodes)
        if document["cores"].get("fits") is False:
            document["warnings"].append(document["cores"]["meaning"])
    if walltime and walltime.get("source") == "jarvis_estimate":
        document["sources"]["time"] = "jarvis_estimate"
    return document
