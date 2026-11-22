"""Static validation of a pipeline's coupling and configuration rules.

``jarvis ppl check`` (and every ``ppl submit``/``ppl run``) runs
:func:`check_pipeline`. It validates, without starting anything:

* each step's ``host_slice`` against the allocation size (``scheduler.nodes``)
  or the pipeline hostfile, and its ``start_mode``;
* the coupling: which steps are services, where they run, their ports, and
  whether they start beside the steps that need them;
* each package's declared ``configuration_rules``
  (:class:`jarvis_cd.deployment.ConfigurationRule`).

Problems are ``jarvis.issue.v1`` documents; ``errors`` refuse the pipeline
(CLI exit 5), ``warnings`` are informational.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from jarvis_cd.core.issues import JarvisIssueError, make_jarvis_issue

CHECK_SCHEMA = "jarvis.pipeline-check.v1"

_SLICE = re.compile(r"(-?\d*):(-?\d*)")
_NOT_PORTS = {"dbg_port"}
_PORT_KEY = re.compile(r"(^|_)port(_id)?$")


def _allocation_size(ppl: Any) -> Tuple[Optional[int], str]:
    scheduler = getattr(ppl, "scheduler", None) or {}
    nodes = scheduler.get("nodes")
    try:
        if nodes is not None and int(nodes) > 0:
            return int(nodes), "scheduler.nodes"
    except (TypeError, ValueError):
        pass
    try:
        hostfile = ppl.get_hostfile()
        count = len(list(hostfile.hosts))
        if count:
            return count, "hostfile"
    except Exception:
        pass
    return None, "unknown"


def _slice_range(
    text: str, size: Optional[int]
) -> Tuple[Optional[Tuple[int, int]], Optional[str]]:
    match = _SLICE.fullmatch(text)
    if match is None:
        return None, "host_slice must look like 'start:end' (Python slice)"
    if size is None:
        return None, None
    start = int(match.group(1)) if match.group(1) else None
    end = int(match.group(2)) if match.group(2) else None
    if (end is not None and end > size) or (start is not None and start >= size):
        return None, (
            f"host_slice {text!r} reaches past the allocation's {size} nodes "
            "(raise scheduler.nodes or shrink the slice)"
        )
    indices = range(size)[start:end]
    if len(indices) == 0:
        return None, f"host_slice {text!r} selects no node of {size}"
    return (indices[0], indices[-1] + 1), None


def _ports(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    ports = []
    for key, value in sorted(config.items()):
        if key in _NOT_PORTS or _PORT_KEY.search(key) is None:
            continue
        if isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 0 < number < 65536:
            ports.append({"parameter": key, "port": number})
    return ports


def _profile(contract: Any, config: Dict[str, Any]) -> Optional[Any]:
    for profile in getattr(contract, "execution_profiles", ()) or ():
        if all(condition.holds(config) for condition in profile.when):
            return profile
    return None


#: kinds a declared ``couplings:`` edge may have
COUPLING_KINDS = ("stream", "file", "service", "interceptor", "control", "other")


def _label(instance: Any, config: Dict[str, Any], pkg_id: str) -> str:
    """A person-facing name: the ``label`` option, the package's, or the id."""
    label = config.get("label")
    if isinstance(label, str) and label.strip():
        return label.strip()[:120]
    try:
        hook = getattr(instance, "component_label", None)
        text = hook() if callable(hook) else None
        if isinstance(text, str) and text.strip():
            return text.strip()[:120]
    except Exception:
        pass
    return pkg_id


def _ranks(config: Dict[str, Any]) -> Optional[int]:
    for key in ("nprocs", "ntasks", "ranks"):
        value = config.get(key)
        if isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None


def _declared_couplings(
    ppl: Any, steps: Sequence[Dict[str, Any]], errors: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """The pipeline's ``couplings:`` (validated) plus derived interceptor edges.

    A declared edge names two package or interceptor ids, a ``kind`` from
    :data:`COUPLING_KINDS` and an optional ``label`` (for example
    ``{from: xcompact3d, to: pvserver, kind: stream, label: ADIOS2 BP}``).
    Bad ids or kinds are ``coupling_invalid`` errors (exit 5).
    """
    known = {str(step["pkg_id"]) for step in steps}
    interceptors = {str(key) for key in (getattr(ppl, "interceptors", None) or {})}
    edges: List[Dict[str, Any]] = []
    for index, item in enumerate(getattr(ppl, "couplings", None) or []):
        field = f"couplings[{index}]"
        problems = []
        for end in ("from", "to"):
            value = item.get(end)
            if not value:
                problems.append(f"{end} is required")
            elif value not in known | interceptors:
                problems.append(f"{end} {value!r} is not a package or interceptor id")
        kind = item.get("kind") or "other"
        if kind not in COUPLING_KINDS:
            problems.append(f"kind {kind!r} must be one of {', '.join(COUPLING_KINDS)}")
        if item.get("from") and item.get("from") == item.get("to"):
            problems.append("from and to must differ")
        if problems:
            errors.append(make_jarvis_issue(
                kind="coupling_invalid",
                subject=f"{item.get('from')}->{item.get('to')}",
                message=f"{field}: " + "; ".join(problems),
                identity={"index": index, "coupling": dict(item)},
                field=field,
                candidates=[{"value": value} for value in sorted(known)],
            ))
            continue
        edge = {"from": item["from"], "to": item["to"], "kind": kind,
                "label": item.get("label") or kind, "source": "pipeline"}
        if edge not in edges:
            edges.append(edge)
    declared = {(edge["from"], edge["to"]) for edge in edges}
    for step in steps:
        for interceptor in step.get("interceptors") or []:
            if (interceptor, step["pkg_id"]) in declared:
                continue
            definition = (getattr(ppl, "interceptors", None) or {}).get(interceptor) or {}
            edges.append({"from": interceptor, "to": step["pkg_id"], "kind": "interceptor",
                          "label": str(definition.get("pkg_type") or interceptor),
                          "source": "derived"})
    return edges


def check_pipeline(ppl: Any) -> Dict[str, Any]:
    """Return a ``jarvis.pipeline-check.v1`` document for ``ppl``."""
    from jarvis_cd.core.pkg import Service

    size, size_source = _allocation_size(ppl)
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    steps: List[Dict[str, Any]] = []
    subject = str(getattr(ppl, "name", None) or "pipeline")

    for index, pkg_def in enumerate(getattr(ppl, "packages", []) or []):
        pkg_id = str(pkg_def.get("pkg_id"))
        config = dict(pkg_def.get("config") or {})
        field = f"pkgs[{index}]"
        step: Dict[str, Any] = {
            "index": index,
            "pkg_id": pkg_id,
            "pkg_type": pkg_def.get("pkg_type"),
        }
        try:
            instance = ppl._load_package_instance(pkg_def, dict(ppl.env or {}))
        except Exception as exc:
            errors.append(
                make_jarvis_issue(
                    kind="package_unloadable",
                    subject=pkg_id,
                    message=f"{pkg_id}: cannot load package: {str(exc)[:500]}",
                    identity={"pkg_type": pkg_def.get("pkg_type")},
                    field=f"{field}.pkg_type",
                )
            )
            steps.append(step)
            continue

        try:
            for item in instance.configure_menu() or []:
                name = item.get("name")
                if name and name not in config and item.get("default") is not None:
                    config[name] = item.get("default")
        except Exception:
            pass

        contract = None
        try:
            contract = instance.deployment_contract()
        except Exception as exc:
            warnings.append(
                make_jarvis_issue(
                    kind="deployment_contract_invalid",
                    subject=pkg_id,
                    message=f"{pkg_id}: deployment contract failed: {str(exc)[:500]}",
                    field=field,
                )
            )
        profile = _profile(contract, config) if contract is not None else None
        if profile is not None:
            role = profile.execution_kind
            step["execution_profile"] = profile.name
            step["readiness"] = profile.readiness.to_dict()
        else:
            role = "service" if isinstance(instance, Service) else "batch"
        step["role"] = role

        start_mode = str(config.get("start_mode") or "auto").strip().lower()
        try:
            background = bool(instance.starts_in_background())
        except ValueError as exc:
            background = False
            errors.append(
                make_jarvis_issue(
                    kind="start_mode_invalid",
                    subject=pkg_id,
                    message=str(exc),
                    identity={"start_mode": start_mode},
                    field=f"{field}.start_mode",
                    candidates=[
                        {"value": value} for value in ("auto", "foreground", "background")
                    ],
                )
            )
        step["start_mode"] = start_mode
        step["starts"] = "background" if background else "foreground"

        host_slice = str(config.get("host_slice") or "").strip()
        step["host_slice"] = host_slice or None
        if host_slice:
            nodes, problem = _slice_range(host_slice, size)
            if problem:
                errors.append(
                    make_jarvis_issue(
                        kind="host_slice_invalid",
                        subject=pkg_id,
                        message=f"{pkg_id}: {problem}",
                        identity={"host_slice": host_slice, "nodes": size},
                        field=f"{field}.host_slice",
                        details={"allocation_nodes": size, "source": size_source},
                    )
                )
            step["nodes"] = None if nodes is None else {"first": nodes[0], "end": nodes[1]}
        else:
            step["nodes"] = None if size is None else {"first": 0, "end": size}
        if step["nodes"] is not None:
            step["node_count"] = step["nodes"]["end"] - step["nodes"]["first"]
        step["ports"] = _ports(config)
        step["label"] = _label(instance, config, pkg_id)
        step["ranks"] = _ranks(config)
        step["interceptors"] = [str(item) for item in config.get("interceptors") or []
                                if isinstance(item, (str, int))]

        for rule in getattr(contract, "configuration_rules", ()) or ():
            for unmet in rule.violations(config):
                errors.append(
                    make_jarvis_issue(
                        kind="configuration_rule_violated",
                        subject=pkg_id,
                        message=f"{pkg_id}: {rule.description} "
                        f"({unmet.parameter} must satisfy {unmet.operator}"
                        + (
                            f" {unmet.value!r})"
                            if unmet.operator not in ("is_empty", "is_not_empty")
                            else ")"
                        ),
                        identity={"rule": rule.to_dict(), "unmet": unmet.to_dict()},
                        field=f"{field}.{unmet.parameter}",
                        details={
                            "rule": rule.to_dict(),
                            "unmet": unmet.to_dict(),
                            "value": config.get(unmet.parameter),
                        },
                    )
                )
        steps.append(step)

    _coupling(steps, errors, warnings)
    couplings = _declared_couplings(ppl, steps, errors)
    site_facts = None
    questions: List[Dict[str, Any]] = []
    try:
        from jarvis_cd.core.site_facts import pipeline_site_facts

        site_facts = pipeline_site_facts(ppl, launch_nodes=size)
    except Exception as exc:  # a site-fact evaluation must never break a check
        site_facts = {"error": str(exc)[:500]}
    for issue in (site_facts or {}).get("issues") or []:
        (questions if issue.get("blocking") else warnings).append(issue)
    services = [
        {
            key: step.get(key)
            for key in ("index", "pkg_id", "pkg_type", "starts", "host_slice",
                        "nodes", "node_count", "ports", "readiness")
        }
        for step in steps
        if step.get("role") == "service"
    ]
    return {
        "schema_version": CHECK_SCHEMA,
        "pipeline_id": subject,
        "ok": not errors and not questions,
        "allocation": {"nodes": size, "source": size_source},
        "steps": steps,
        "services": services,
        "couplings": couplings,
        "errors": errors,
        "questions": questions,
        "warnings": warnings,
        "site_facts": site_facts,
    }


def _overlaps(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    if a.get("nodes") is None or b.get("nodes") is None:
        return True
    return a["nodes"]["first"] < b["nodes"]["end"] and b["nodes"]["first"] < a["nodes"]["end"]


def _coupling(
    steps: Sequence[Dict[str, Any]],
    errors: List[Dict[str, Any]],
    warnings: List[Dict[str, Any]],
) -> None:
    services = [step for step in steps if step.get("role") == "service"]
    for service in services:
        later_batch = [
            step for step in steps
            if step["index"] > service["index"] and step.get("role") == "batch"
        ]
        earlier_blocking = [
            step for step in steps
            if step["index"] < service["index"] and step.get("role") == "batch"
            and step.get("starts") == "foreground"
        ]
        field = f"pkgs[{service['index']}]"
        if earlier_blocking:
            warnings.append(
                make_jarvis_issue(
                    kind="service_after_batch",
                    subject=service["pkg_id"],
                    message=(
                        f"{service['pkg_id']} starts only after "
                        f"{', '.join(s['pkg_id'] for s in earlier_blocking)} finished; "
                        "put the service first to run beside it"
                    ),
                    identity={"before": [s["pkg_id"] for s in earlier_blocking]},
                    field=f"{field}",
                )
            )
        if later_batch and service.get("starts") == "foreground":
            warnings.append(
                make_jarvis_issue(
                    kind="service_blocks_pipeline",
                    subject=service["pkg_id"],
                    message=(
                        f"{service['pkg_id']} is a service started in the "
                        "foreground; later steps wait for its start() to return"
                    ),
                    identity={"start_mode": service.get("start_mode")},
                    field=f"{field}.start_mode",
                    remedy="set start_mode: background (or auto)",
                )
            )
        for other in steps:
            if other is service or not _overlaps(service, other):
                continue
            if other.get("role") == "batch" and service.get("host_slice") is not None:
                warnings.append(
                    make_jarvis_issue(
                        kind="host_slice_overlap",
                        subject=service["pkg_id"],
                        message=(
                            f"{service['pkg_id']} and {other['pkg_id']} share nodes"
                        ),
                        identity={"with": other["pkg_id"]},
                        field=f"{field}.host_slice",
                    )
                )
            if other.get("role") == "service" and other["index"] > service["index"]:
                shared = {p["port"] for p in service.get("ports", [])} & {
                    p["port"] for p in other.get("ports", [])
                }
                for port in sorted(shared):
                    parameter = next(
                        (p["parameter"] for p in other.get("ports", [])
                         if p["port"] == port),
                        "port",
                    )
                    errors.append(
                        make_jarvis_issue(
                            kind="service_port_conflict",
                            subject=other["pkg_id"],
                            message=(
                                f"{service['pkg_id']} and {other['pkg_id']} both "
                                f"listen on port {port} on the same nodes"
                            ),
                            identity={"with": service["pkg_id"], "port": port},
                            field=f"pkgs[{other['index']}].{parameter}",
                        )
                    )


def enforce_pipeline_check(ppl: Any) -> Dict[str, Any]:
    """Run :func:`check_pipeline`; raise :class:`JarvisIssueError` on errors.

    Errors refuse with exit 5. Blocking site-fact questions
    (``site_fact_uncertain``) refuse with exit 3, like an install decision.
    """
    from jarvis_cd.core.site_facts import EXIT_SITE_FACT

    document = check_pipeline(ppl)
    if document["errors"]:
        raise JarvisIssueError(
            document["errors"],
            document={"check": {k: v for k, v in document.items() if k != "errors"}},
        )
    if document["questions"]:
        raise JarvisIssueError(
            document["questions"],
            exit_code=EXIT_SITE_FACT,
            document={"check": {k: v for k, v in document.items() if k != "questions"}},
        )
    return document
