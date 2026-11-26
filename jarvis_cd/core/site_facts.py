"""Uncertain site facts: typed evidence for an agent, answers per site.

JARVIS decides deterministically: it collects facts, excludes what cannot
work, ranks the rest and applies the pick, and says nothing when the pick is
confident. When a pick rests on a cluster fact JARVIS cannot settle by
itself, it raises one ``jarvis.issue.v1`` of kind ``site_fact_uncertain``
instead of guessing silently. The issue carries:

* ``details.reasons[]``: reason codes, each with a severity and the
  consequence if JARVIS's pick is wrong;
* ``details.evidence``: the facts the pick rests on, and their digest;
* ``candidates[]``: every option, with why it ranks where it does;
* ``details.pick``: what JARVIS will use if nobody answers;
* ``details.probes[]``: read-only commands an agent may run to investigate
  (JARVIS never runs them, and never runs agent-supplied code);
* ``details.answer``: the field to answer, the accepted values, and the
  command or ``answers.json`` entry that records the answer.

``block`` reasons refuse before submit or a direct run (exit 3): a wrong
pick could make the run fail or silently be wrong. ``advisory`` reasons let
the run proceed and attach the issue to its record.

An answer is validated deterministically against the facts, recorded per
site next to the resource graph (``<graph dir>/site-facts/answers.yaml``)
with ``source: person`` and the reason, and suppresses the question while
the evidence digest stays the same. Changed evidence asks again, naming the
previous answer.

The first fact is ``net.launch``: the MPI launch network chosen from the
resource graph's ``net`` section.
"""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from jarvis_cd.core.issues import JarvisIssueError, make_jarvis_issue

UCX_INFO_ENVIRONMENT = "JARVIS_UCX_INFO"

SITE_FACT_KIND = "site_fact_uncertain"
SITE_FACTS_SCHEMA = "jarvis.site-facts.v1"
ANSWER_SCHEMA = "jarvis.site-fact-answer.v1"
#: CLI exit code when a ``block`` site fact needs an answer (like install)
EXIT_SITE_FACT = 3
BLOCK = "block"
ADVISORY = "advisory"
NET_LAUNCH = "net.launch"
ANSWER_FIELD_PREFIX = "resource_graph.site_facts"

#: interface kinds that carry real node-to-node traffic (util.network)
_DIRECT_KINDS = ("physical", "bond", "vlan", "ipoib")
#: two data networks within this speed ratio are "near equal"
NEAR_EQUAL_RATIO = 0.9
_IFACE_NAME = re.compile(r"[A-Za-z0-9_.:@-]{1,32}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def evidence_digest(evidence: Any) -> str:
    payload = json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def mpi_flavour(name: Optional[str]) -> Optional[str]:
    """Map an MPI package or launcher name to a network flavour."""
    text = str(name or "").lower()
    if not text:
        return None
    if "cray" in text:
        return "cray"
    if "intel" in text:
        return "intel"
    if "openmpi" in text or "open-mpi" in text or text == "ompi":
        return "openmpi"
    if "mpich" in text or "mvapich" in text:
        return "mpich"
    return None


# --------------------------------------------------------------------------
# The answer store, in JARVIS's site home (next to JARVIS's own graph)
# --------------------------------------------------------------------------


def site_facts_dir(resource_graph_file: Path) -> Path:
    return Path(resource_graph_file).expanduser().resolve().parent / "site-facts"


class SiteFactStore:
    """``answers.yaml`` (confirmed answers) and ``asked/`` (raised issues).

    ``resource_graph_file`` is JARVIS's own graph; the store sits beside it.
    Answers the site publishes beside its graph (``JARVIS_RESOURCE_GRAPH``)
    are read too, JARVIS's own winning; they are never written.
    """

    def __init__(self, resource_graph_file: Path):
        from jarvis_cd.util.site_state import published_dir

        self.graph = Path(resource_graph_file)
        self.directory = site_facts_dir(self.graph)
        self.answers_path = self.directory / "answers.yaml"
        self.asked_dir = self.directory / "asked"
        published = published_dir("site-facts")
        self.published_answers_path = (published / "answers.yaml"
                                       if published is not None else None)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with open(self.directory / ".lock", "a+") as lock:
            try:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - non-POSIX
                pass
            yield

    def answers(self) -> Dict[str, Dict[str, Any]]:
        """JARVIS's own answers over the ones the site publishes."""
        published = self._read_answers(self.published_answers_path)
        for entry in published.values():
            if isinstance(entry, dict):
                entry.setdefault("published_in", str(self.published_answers_path))
        return {**published, **self.own_answers()}

    def own_answers(self) -> Dict[str, Dict[str, Any]]:
        return self._read_answers(self.answers_path)

    def published_answers(self) -> Dict[str, Dict[str, Any]]:
        return self._read_answers(self.published_answers_path)

    @staticmethod
    def _read_answers(path: Optional[Path]) -> Dict[str, Dict[str, Any]]:
        import yaml

        if path is None:
            return {}
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            return {}
        facts = (document or {}).get("facts") if isinstance(document, dict) else None
        return dict(facts) if isinstance(facts, dict) else {}

    def _write(self, path: Path, text: str) -> None:
        temporary = path.with_name(path.name + f".tmp{os.getpid()}")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)

    def record_answer(self, fact: str, entry: Dict[str, Any]) -> Dict[str, Any]:
        import yaml

        with self._locked():
            facts = self.own_answers()
            previous = facts.get(fact) or self.published_answers().get(fact)
            if previous:
                entry = dict(entry)
                entry["previous"] = {
                    key: previous.get(key)
                    for key in ("issue_id", "evidence_digest", "value", "reason",
                                "set_by", "set_at")
                }
            facts[fact] = entry
            self._write(
                self.answers_path,
                yaml.safe_dump({"schema_version": SITE_FACTS_SCHEMA, "facts": facts},
                               sort_keys=True),
            )
        return entry

    def forget(self, fact: str) -> Optional[Dict[str, Any]]:
        import yaml

        with self._locked():
            facts = self.own_answers()
            removed = facts.pop(fact, None)
            if removed is not None:
                self._write(
                    self.answers_path,
                    yaml.safe_dump({"schema_version": SITE_FACTS_SCHEMA, "facts": facts},
                                   sort_keys=True),
                )
        return removed

    def record_asked(self, issue: Dict[str, Any], inputs: Dict[str, Any]) -> None:
        """Keep a raised issue and its inputs so an answer can be checked."""
        try:
            with self._locked():
                self.asked_dir.mkdir(parents=True, exist_ok=True)
                path = self.asked_dir / (issue["issue_id"].split(":", 1)[-1] + ".json")
                if not path.exists():
                    self._write(path, json.dumps({"issue": issue, "inputs": inputs},
                                                 sort_keys=True, default=str))
        except OSError:
            pass  # asking must never fail because the store is read-only

    def asked(self, issue_id: str) -> Optional[Dict[str, Any]]:
        hexdigest = str(issue_id).split(":", 1)[-1]
        if not re.fullmatch(r"[0-9a-f]{8,64}", hexdigest):
            return None
        matches = sorted(self.asked_dir.glob(hexdigest + "*.json")) \
            if self.asked_dir.is_dir() else []
        if len(matches) != 1:
            return None
        try:
            return json.loads(matches[0].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


# --------------------------------------------------------------------------
# The net.launch fact
# --------------------------------------------------------------------------


def _rank_key(network: Dict[str, Any]) -> tuple:
    direct = 0 if network.get("kind") in _DIRECT_KINDS else 1
    return (direct, -(network.get("speed_mbps") or 0), network.get("cidr"))


def _ranked(net: Dict[str, Any]) -> List[Dict[str, Any]]:
    return sorted(
        (n for n in net.get("networks") or [] if n.get("usable") and n.get("reaches_all")),
        key=_rank_key,
    )


def _why(network: Dict[str, Any], rank: Optional[int], total_nodes: int,
         picks: Sequence[str]) -> str:
    if not network.get("usable"):
        return {
            "loopback": "excluded: loopback",
            "link_local": "excluded: link-local addresses",
            "down": "excluded: the link is down on some node",
            "duplicate_address": "excluded: the same address on several nodes "
                                 "(a per-node bridge such as docker0)",
        }.get(network.get("excluded_reason"), f"excluded: {network.get('excluded_reason')}")
    if not network.get("reaches_all"):
        return f"not ranked: on {network.get('nodes')} of {total_nodes} nodes"
    speed = network.get("speed_mbps")
    parts = [
        f"rank {rank} for data",
        ("direct" if network.get("kind") in _DIRECT_KINDS else "indirect")
        + f" ({network.get('kind')})",
        f"{speed} Mb/s" if speed else "speed unknown",
        f"on all {total_nodes} nodes",
    ]
    if network.get("interface") is None:
        parts.append("interface names differ between nodes")
    if network.get("rdma_device"):
        statuses = set((network.get("ucx_status") or {}).values())
        parts.append(f"RDMA {network.get('rdma_device')}" + (
            "" if network.get("ucx_device") else
            " (not ACTIVE on every node)" if "port_not_active" in statuses or not statuses else
            " (UCX cannot use it on every node)"))
    if picks:
        parts.append("picked as " + " and ".join(picks))
    return ", ".join(parts)


def _node_interface(net: Dict[str, Any], host: str, cidr: str) -> Optional[Dict[str, Any]]:
    for interface in ((net.get("nodes") or {}).get(host) or {}).get("interfaces") or []:
        if interface.get("cidr") == cidr:
            return interface
    return None


def net_launch_reasons(
    net: Dict[str, Any],
    *,
    flavour: Optional[str],
    launch_nodes: Optional[int] = None,
    hosts_check: Optional[Dict[str, Any]] = None,
    launch_hosts: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """The deterministic uncertainty rules for the MPI launch network.

    ``launch_hosts``: at a launch, the per-host checks (RDMA port state, UCX
    usability, control fallback) look only at the launch hosts the graph
    holds, so another job's host in a merged graph raises nothing here.
    """
    from jarvis_cd.util.network import predates_ucx_verification

    selected = net.get("selected") or {}
    data, control = selected.get("data"), selected.get("control")
    by_cidr = {n.get("cidr"): n for n in net.get("networks") or []}
    graph_hosts = list(net.get("collected_on") or [])
    scope_hosts = graph_hosts
    if launch_hosts:
        known = set(graph_hosts)
        scope_hosts = [host for host in dict.fromkeys(launch_hosts) if host in known]
    needs_names = flavour in ("mpich", "intel")
    override_roles = net.get("override") or {}
    reasons: List[Dict[str, Any]] = []

    def add(code, severity, roles, message, consequence, **extra):
        reason = {"code": code, "severity": severity, "roles": list(roles),
                  "message": message, "consequence": consequence}
        reason.update(extra)
        reasons.append(reason)

    ranked = _ranked(net)
    if not ranked:
        add("no_reachable_network", BLOCK, ("data", "control"),
            "no usable network reaches every node of the graph",
            "MPI falls back to its own interface choice, which can pick a "
            "network the other nodes cannot reach, and the launch hangs or fails")
        return reasons

    top = ranked[0]
    near = []
    for other in ranked[1:]:
        if _rank_key(other)[0] != _rank_key(top)[0]:
            continue
        top_speed, speed = top.get("speed_mbps"), other.get("speed_mbps")
        if (top_speed is None and speed is None) or (
            top_speed and speed and speed >= NEAR_EQUAL_RATIO * top_speed
        ):
            near.append(other["cidr"])
    if near:
        add("near_equal_data", ADVISORY, ("data",),
            f"data {top['cidr']} and {', '.join(near)} rank nearly equal "
            "(same class, speeds within 10% or both unknown)",
            "the run works on either, but may use the slower or the shared "
            "management network", near=near)

    for role, choice in (("data", data), ("control", control)):
        network = by_cidr.get((choice or {}).get("cidr"))
        if network is None or network.get("interface") is not None:
            continue
        names = sorted(set((network.get("interfaces") or {}).values()))
        if needs_names:
            add("interface_names_differ", BLOCK, (role,),
                f"{role} {network['cidr']} has different interface names on the "
                f"nodes ({', '.join(names)}) and {flavour} takes a name",
                f"{flavour} gets no {role} interface (or a wrong one) and the "
                "launch can fail or use the wrong network", names=names)
        elif flavour is None:
            add("interface_names_differ", ADVISORY, (role,),
                f"{role} {network['cidr']} has different interface names on the "
                f"nodes ({', '.join(names)}); the MPI flavour is not known yet",
                "MPICH and Intel MPI take a name and would get none; Open MPI "
                "takes the CIDR and is unaffected", names=names)

    network = by_cidr.get((data or {}).get("cidr"))
    if network is not None:
        states, ucx_hosts = {}, {}
        for host in scope_hosts:
            interface = _node_interface(net, host, network["cidr"]) or {}
            if not interface.get("rdma_device"):
                continue
            states[host] = interface.get("rdma_port_state") or "unknown"
            status = interface.get("ucx_status")
            if status and status not in ("usable", "port_not_active"):
                ucx = ((net.get("nodes") or {}).get(host) or {}).get("ucx") or {}
                ucx_hosts[host] = {
                    "status": status,
                    "port": interface.get("rdma_port"),
                    "link_layer": interface.get("rdma_link_layer"),
                    "ucx_info": ucx.get("ucx_info"),
                    "ucx_version": ucx.get("version"),
                    "ucx_rdma_devices": sorted((ucx.get("rdma_devices") or {})),
                    **({"error": ucx["error"]} if ucx.get("error") else {}),
                }
        inactive = {h: s for h, s in states.items() if "ACTIVE" not in str(s)}
        ompi_tcp = (" Open MPI is kept off UCX (OMPI_MCA_pml and OMPI_MCA_osc "
                    "set to ^ucx) so every rank runs ob1/TCP on the data network."
                    if flavour == "openmpi" else "")
        if states and (inactive or len(states) < len(scope_hosts)):
            add("rdma_not_active", ADVISORY, ("data",),
                f"data {network['cidr']} has an RDMA device that is not ACTIVE "
                "on every node, so UCX_NET_DEVICES is not set",
                "the run uses TCP on that network instead of RDMA: correct, but "
                "slower." + ompi_tcp,
                port_states=states)
        if ucx_hosts:
            add("ucx_device_unusable", ADVISORY, ("data",),
                f"data {network['cidr']} has an RDMA device whose port is ACTIVE, "
                "but UCX on " + ", ".join(sorted(ucx_hosts)) + " cannot use it "
                "(" + ", ".join(sorted({v["status"] for v in ucx_hosts.values()}))
                + "), so UCX_NET_DEVICES is not set",
                "the run uses TCP instead of RDMA: correct, but slower." + ompi_tcp
                + " If the MPI uses a different UCX than the ucx_info checked, set "
                f"{UCX_INFO_ENVIRONMENT} to its ucx_info and re-collect",
                ucx=ucx_hosts)

    if "data" not in override_roles and (hosts_check is None and flavour != "cray"
                                         and predates_ucx_verification(net)):
        device = (data or {}).get("ucx_device") or (data or {}).get("rdma_device")
        add("device_unverified", ADVISORY, ("data",),
            f"the graph's RDMA device {device} predates UCX verification "
            "(no ucx_verified)",
            "each multi-node MPI launch collects the net facts on its hosts first, "
            "inside the allocation, and sets UCX_NET_DEVICES only if UCX verifies "
            "the device there; if that collection fails the device is withheld",
            withholds=["UCX_NET_DEVICES"], value=device, plan="collect_at_launch")

    control_net = by_cidr.get((control or {}).get("cidr"))
    if control_net is not None and scope_hosts:
        subnet = ipaddress.IPv4Network(control_net["cidr"])
        outside = []
        for host in scope_hosts:
            addrs = ((net.get("nodes") or {}).get(host) or {}).get("control_addrs") or []
            if not any(ipaddress.IPv4Address(a) in subnet for a in addrs):
                outside.append(host)
        if outside:
            add("control_fallback", ADVISORY, ("control",),
                f"no network holds every node's hostname address; control fell "
                f"back to {control_net['cidr']}"
                + (" (the data network)" if control_net is network else ""),
                "the MPI daemons wire up over that network; if hostnames resolve "
                "elsewhere the launch can stall at start-up", hosts=outside)

    if not net.get("collected_in"):
        add("collected_outside_allocation", ADVISORY, ("data", "control"),
            "the net section was collected outside a scheduler allocation",
            "it may describe a login node rather than the compute nodes, so "
            "the pick may not exist where the ranks run")

    if hosts_check is not None:
        if hosts_check.get("severity") == "warning":
            add("hosts_not_in_graph", ADVISORY, ("data", "control"),
                hosts_check.get("reason") or "launch hosts are not in the graph",
                "the settings may not fit those hosts; a wrong one fails the "
                "launch with an mpi_network issue",
                hosts=[entry.get("host") for entry in hosts_check.get("hosts") or []])
        for item in hosts_check.get("withheld") or []:
            add("device_unverified", ADVISORY, ("data",),
                f"{item.get('setting')}={item.get('value')} was withheld: "
                + (item.get("reason") or "the graph never saw "
                   + ", ".join(item.get("hosts") or [])),
                "UCX picks its own devices on every node: correct, but it may "
                "use TCP where the RDMA device would be faster",
                setting=item.get("setting"), value=item.get("value"),
                hosts=list(item.get("hosts") or []),
                cause=item.get("cause") or "hosts_not_in_graph")
    elif launch_nodes is not None and launch_nodes > len(graph_hosts):
        # Advisory at check time: the hosts are not known yet, and the launch
        # collects their facts inside the allocation before it applies any.
        add("hosts_not_in_graph", ADVISORY, ("data", "control"),
            f"the job asks {launch_nodes} nodes but the graph was collected on "
            f"{len(graph_hosts)}",
            "the launch collects the net facts on its hosts first, inside the "
            "allocation, and applies the settings those facts give; a block-class "
            "finding there"
            + (f" (e.g. {flavour} interface names that differ)" if needs_names else "")
            + " refuses the launch with exit 3 instead of guessing",
            plan="collect_at_launch")
    return reasons


def _evidence(net: Dict[str, Any], flavour: Optional[str],
              reasons: Sequence[Dict[str, Any]], covers_launch: bool) -> Dict[str, Any]:
    # Raw facts only, so a check before submit (flavour often unknown) and
    # the launch agree. The launch's host names are not a site fact; only
    # whether the graph covers them is. ``reasons`` stays out of the digest
    # because it derives from these facts.
    keep = ("cidr", "interface", "interfaces", "kind", "speed_mbps", "rdma_device",
            "ucx_device", "nodes", "reaches_all", "usable", "excluded_reason")
    iface_keep = ("name", "cidr", "addr", "kind", "speed_mbps", "state", "rdma_device",
                  "rdma_port_state", "ucx_device")
    # Collected since UCX verification; absent keys stay out so older
    # graphs keep their digests.
    iface_optional = ("rdma_port", "rdma_link_layer", "ucx_status", "ucx_transports")
    nodes = {}
    for host in sorted(net.get("collected_on") or []):
        node = (net.get("nodes") or {}).get(host) or {}
        nodes[host] = {
            "control_addrs": sorted(node.get("control_addrs") or []),
            "interfaces": sorted(
                ({**{key: interface.get(key) for key in iface_keep},
                  **{key: interface[key] for key in iface_optional if key in interface}}
                 for interface in node.get("interfaces") or []),
                key=lambda item: (str(item.get("cidr")), str(item.get("name")))),
        }
        if node.get("ucx"):
            ucx = node["ucx"]
            nodes[host]["ucx"] = {"status": ucx.get("status"), "ucx_info": ucx.get("ucx_info"),
                                  "rdma_devices": ucx.get("rdma_devices") or {}}
    return {
        "fact": NET_LAUNCH,
        "flavour_needs_names": flavour in ("mpich", "intel"),
        "graph_covers_launch": covers_launch,
        "collected_on": sorted(net.get("collected_on") or []),
        "collected_in_allocation": bool(net.get("collected_in")),
        "networks": sorted(
            ({key: network.get(key) for key in keep} for network in net.get("networks") or []),
            key=lambda item: str(item.get("cidr")),
        ),
        "nodes": nodes,
    }


def _net_probes(net: Dict[str, Any], reasons: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    codes = {reason["code"] for reason in reasons}
    interfaces = sorted({
        name for network in _ranked(net)[:3]
        for name in (network.get("interfaces") or {}).values()
    })
    probes = [
        {"command": "ip -o -4 addr show", "on": "each launch node",
         "why": "which interfaces and addresses each node has"},
        {"command": "getent hosts $(hostname)", "on": "each launch node",
         "why": "which address the hostname resolves to (the control network)"},
    ]
    for name in interfaces:
        probes.append({"command": f"cat /sys/class/net/{name}/speed /sys/class/net/{name}/operstate",
                       "on": "each launch node", "why": f"link speed and state of {name}"})
    if (codes & {"rdma_not_active", "ucx_device_unusable", "device_unverified"}
            or any(n.get("rdma_device") for n in _ranked(net))):
        probes.append({"command": "ibv_devinfo", "on": "each launch node",
                       "why": "RDMA port state (PORT_ACTIVE) and link layer"})
        probes.append({"command": "ucx_info -d", "on": "one launch node",
                       "why": "which devices UCX can use"})
    probes.append({
        "command": "jarvis ppl submit <pipeline with builtin.resource_graph net_only=true>",
        "on": "a job spanning the launch nodes",
        "why": "re-collect the net section where the ranks run",
        "kind": "jarvis",
    })
    probes.append({
        "command": "jarvis ppl submit <2-node smoke pipeline>",
        "on": "two nodes",
        "why": "launch a tiny MPI program with the pick and see whether it wires up",
        "kind": "jarvis",
    })
    return probes


def _candidates(net: Dict[str, Any]) -> List[Dict[str, Any]]:
    selected = net.get("selected") or {}
    ranked = _ranked(net)
    rank_of = {network["cidr"]: index + 1 for index, network in enumerate(ranked)}
    total = len(net.get("collected_on") or [])
    entries = []
    for network in sorted(net.get("networks") or [],
                          key=lambda n: (rank_of.get(n.get("cidr"), 1 << 20), str(n.get("cidr")))):
        picks = [role for role in ("data", "control")
                 if (selected.get(role) or {}).get("cidr") == network.get("cidr")]
        entries.append({
            "value": network.get("cidr"),
            "cidr": network.get("cidr"),
            "interface": network.get("interface"),
            "interfaces": network.get("interfaces"),
            "kind": network.get("kind"),
            "speed_mbps": network.get("speed_mbps"),
            "rdma_device": network.get("rdma_device"),
            "ucx_device": network.get("ucx_device"),
            "usable": bool(network.get("usable")),
            "reaches_all": bool(network.get("reaches_all")),
            "rank": rank_of.get(network.get("cidr")),
            "picked_as": picks,
            "why": _why(network, rank_of.get(network.get("cidr")), total, picks),
        })
    return entries


def _override_roles(net: Dict[str, Any]) -> List[str]:
    override = net.get("override") or {}
    return [role for role in ("data", "control") if role in override]


def net_launch_issue(
    net: Optional[Dict[str, Any]],
    *,
    flavour: Optional[str] = None,
    launch_nodes: Optional[int] = None,
    hosts_check: Optional[Dict[str, Any]] = None,
    stage: str = "check",
    launch_hosts: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Evaluate ``net.launch``: ``{issue, reasons, resolved_by_override, evidence_digest}``."""
    result: Dict[str, Any] = {"fact": NET_LAUNCH, "issue": None, "reasons": [],
                              "resolved_by_override": [], "evidence_digest": None}
    if not net:
        return result
    all_reasons = net_launch_reasons(net, flavour=flavour, launch_nodes=launch_nodes,
                                     hosts_check=hosts_check, launch_hosts=launch_hosts)
    if hosts_check is not None:
        covers = not hosts_check.get("hosts")
    else:
        covers = launch_nodes is None or launch_nodes <= len(net.get("collected_on") or [])
    evidence = _evidence(net, flavour, all_reasons, covers)
    digest = evidence_digest(evidence)
    result["evidence_digest"] = digest
    overridden = set(_override_roles(net))
    reasons = []
    for reason in all_reasons:
        if overridden and set(reason["roles"]) <= overridden:
            result["resolved_by_override"].append(reason["code"])
        else:
            reasons.append(reason)
    result["reasons"] = reasons
    if not reasons:
        return result
    severity = BLOCK if any(r["severity"] == BLOCK for r in reasons) else ADVISORY
    if stage == "launch":
        # Inside an allocation the run goes ahead; the issue is advisory.
        # ``launch_collected`` (facts this launch collected, which no check
        # saw) keeps the check's block rules.
        severity = ADVISORY
    from jarvis_cd.util.network import resolve_launch_network

    effective = resolve_launch_network(net, net.get("collected_on") or []) or {
        "data": (net.get("selected") or {}).get("data"),
        "control": (net.get("selected") or {}).get("control"),
    }
    field = f"{ANSWER_FIELD_PREFIX}.{NET_LAUNCH}"
    issue = make_jarvis_issue(
        kind=SITE_FACT_KIND,
        subject="net",
        message=(
            "The MPI launch network rests on uncertain cluster facts: "
            + "; ".join(r["message"] for r in reasons)
            + ". Investigate with details.probes, propose a value, and have "
            "the person confirm it."
        ),
        identity={"fact": NET_LAUNCH, "evidence_digest": digest},
        field=field,
        details={
            "schema_version": SITE_FACTS_SCHEMA,
            "fact": NET_LAUNCH,
            "severity": severity,
            "blocking": severity == BLOCK,
            "stage": stage,
            "reasons": reasons,
            "evidence": evidence,
            "evidence_digest": digest,
            "pick": {
                "data": effective.get("data"),
                "control": effective.get("control"),
                "flavour": flavour,
                "consequence_if_wrong": [r["consequence"] for r in reasons],
            },
            "probes": _net_probes(net, reasons),
            "answer": {
                "field": field,
                "values": [
                    "accept: keep JARVIS's pick",
                    "{\"data\": <cidr|iface|addr|none>, \"control\": <cidr|iface|addr|none>}",
                ],
                "command": "jarvis rg facts answer <issue_id> --value accept|'{\"data\": ..., "
                           "\"control\": ...}' --reason <why> [--proposed_by <agent>]",
                "answers_file": {"<issue_id>": {"value": "accept", "reason": "<why>",
                                                "set_by": "<person>",
                                                "proposed_by": "<agent>"}},
                "answers_flag": "jarvis rg facts answer --answers <answers.json>",
                "suppressed_while": "evidence_digest is unchanged",
            },
        },
        candidates=_candidates(net),
        remedy=(
            "jarvis rg facts answer <issue_id> --value accept --reason <why>, or "
            "jarvis rg net set --data <cidr|iface> --control <cidr|iface> --reason <why>"
        ),
    )
    issue["severity"] = severity
    issue["blocking"] = severity == BLOCK
    # Top-level codes so a caller can match the cause (e.g.
    # ucx_device_unusable) without walking details.reasons.
    issue["reason_codes"] = [r["code"] for r in reasons]
    result["issue"] = issue
    return result


# --------------------------------------------------------------------------
# Reports and answers
# --------------------------------------------------------------------------


def site_fact_report(
    resource_graph_file: Path,
    net: Optional[Dict[str, Any]],
    *,
    flavour: Optional[str] = None,
    launch_nodes: Optional[int] = None,
    hosts_check: Optional[Dict[str, Any]] = None,
    stage: str = "check",
    record: bool = True,
    launch_hosts: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Evaluate every site fact; drop questions a recorded answer covers."""
    store = SiteFactStore(resource_graph_file)
    answers = store.answers()
    document: Dict[str, Any] = {
        "schema_version": SITE_FACTS_SCHEMA,
        "stage": stage,
        "answers_path": str(store.answers_path),
        "issues": [],
        "confirmed": [],
        "resolved_by_override": [],
        "blocking": False,
    }
    evaluated = net_launch_issue(net, flavour=flavour, launch_nodes=launch_nodes,
                                 hosts_check=hosts_check, stage=stage,
                                 launch_hosts=launch_hosts)
    if evaluated["resolved_by_override"]:
        document["resolved_by_override"].append(
            {"fact": NET_LAUNCH, "reasons": evaluated["resolved_by_override"],
             "override": (net or {}).get("override")})
    issue = evaluated["issue"]
    if issue is not None:
        answer = answers.get(NET_LAUNCH)
        if answer and answer.get("evidence_digest") == evaluated["evidence_digest"]:
            document["confirmed"].append({
                "fact": NET_LAUNCH,
                "issue_id": answer.get("issue_id"),
                "evidence_digest": answer.get("evidence_digest"),
                "value": answer.get("value"),
                "reason": answer.get("reason"),
                "set_by": answer.get("set_by"),
                "set_at": answer.get("set_at"),
                "source": answer.get("source"),
                "reasons": [r["code"] for r in evaluated["reasons"]],
            })
        else:
            if answer:
                issue["details"]["previous_answer"] = {
                    key: answer.get(key)
                    for key in ("issue_id", "evidence_digest", "value", "reason",
                                "set_by", "set_at")
                }
                issue["supersedes"] = answer.get("issue_id")
            document["issues"].append(issue)
            if record:
                store.record_asked(issue, {
                    "fact": NET_LAUNCH, "flavour": flavour, "launch_nodes": launch_nodes,
                    "hosts_check": hosts_check, "stage": stage,
                })
    document["blocking"] = any(item.get("blocking") for item in document["issues"])
    return document


def _refuse(kind: str, message: str, **details: Any) -> JarvisIssueError:
    return JarvisIssueError([make_jarvis_issue(
        kind=kind, subject="site_facts", message=message,
        identity={k: details.get(k) for k in ("issue_id", "fact") if details.get(k)},
        field="--value" if kind == "site_fact_answer_invalid" else "issue_id",
        details=details,
    )])


def _validate_role_value(net: Dict[str, Any], role: str, value: Any) -> Dict[str, Any]:
    from jarvis_cd.util.network import _network_by_name

    if not isinstance(value, str) or not value.strip():
        raise _refuse("site_fact_answer_invalid", f"{role} must be a CIDR, an interface "
                      "name, an address or 'none'", role=role, value=value)
    value = value.strip()
    if value.lower() == "none":
        return {"role": role, "value": "none", "known": True, "check": "disabled"}
    network = _network_by_name(net, value)
    if network is not None:
        if not network.get("usable") or not network.get("reaches_all"):
            raise _refuse(
                "site_fact_answer_invalid",
                f"{role} {network['cidr']} cannot carry the launch: "
                + (network.get("excluded_reason") or
                   f"on {network.get('nodes')} of {len(net.get('collected_on') or [])} nodes"),
                role=role, value=value, network=network.get("cidr"))
        return {"role": role, "value": value, "known": True, "cidr": network["cidr"],
                "check": "usable and on every node of the graph"}
    try:
        cidr = str(ipaddress.IPv4Network(value, strict=False))
        return {"role": role, "value": value, "known": False, "cidr": cidr,
                "check": "valid CIDR; not seen on the graph's nodes"}
    except ValueError:
        pass
    if _IFACE_NAME.fullmatch(value):
        return {"role": role, "value": value, "known": False, "interface": value,
                "check": "valid interface name; not seen on the graph's nodes"}
    raise _refuse("site_fact_answer_invalid", f"{role} {value!r} is not a CIDR, an "
                  "address or an interface name", role=role, value=value)


def answer_site_fact(
    manager: Any,
    issue_id: str,
    value: Any,
    *,
    reason: Optional[str],
    set_by: Optional[str] = None,
    proposed_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate and record a person's answer to a ``site_fact_uncertain`` issue.

    ``manager`` is the :class:`ResourceGraphManager` of the active graph.
    ``value`` is ``"accept"`` or ``{"data": ..., "control": ...}``.
    """
    graph_file = Path(manager.jarvis.resource_graph_file)
    store = SiteFactStore(graph_file)
    if not reason or not str(reason).strip():
        raise _refuse("site_fact_answer_invalid", "an answer needs --reason (kept as "
                      "provenance)", issue_id=issue_id)
    asked = store.asked(issue_id)
    if asked is None:
        raise _refuse("site_fact_unknown", f"no site-fact question {issue_id} was "
                      "asked on this site", issue_id=issue_id,
                      asked_dir=str(store.asked_dir))
    issue, inputs = asked["issue"], asked.get("inputs") or {}
    fact = inputs.get("fact") or (issue.get("details") or {}).get("fact")
    if fact != NET_LAUNCH:
        raise _refuse("site_fact_unknown", f"unknown site fact {fact!r}", fact=fact)
    net = manager.resource_graph.net
    current = net_launch_issue(net, flavour=inputs.get("flavour"),
                               launch_nodes=inputs.get("launch_nodes"),
                               hosts_check=inputs.get("hosts_check"),
                               stage=inputs.get("stage") or "check")
    if current["evidence_digest"] != issue["details"]["evidence_digest"]:
        raise _refuse(
            "site_fact_answer_stale",
            "the evidence changed since this question was asked; answer the "
            "current question instead",
            issue_id=issue_id, fact=fact,
            current_issue_id=(current["issue"] or {}).get("issue_id"),
            current_evidence_digest=current["evidence_digest"])
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            value = json.loads(value)
        except ValueError as error:
            raise _refuse("site_fact_answer_invalid", f"--value is not JSON: {error}",
                          issue_id=issue_id)
    if isinstance(value, str) and value.strip().lower() == "accept":
        validation: List[Dict[str, Any]] = [{"value": "accept", "check": "keeps JARVIS's pick"}]
        recorded_value: Any = "accept"
    elif isinstance(value, dict) and set(value) and set(value) <= {"data", "control"}:
        validation = [_validate_role_value(net, role, value[role]) for role in sorted(value)]
        recorded_value = {role: value[role] for role in sorted(value)}
        manager.net_set(data=value.get("data"), control=value.get("control"),
                        reason=reason, set_by=set_by)
    else:
        raise _refuse("site_fact_answer_invalid", "--value must be 'accept' or a JSON "
                      "object with data and/or control", issue_id=issue_id, value=value)
    entry = store.record_answer(fact, {
        "fact": fact,
        "issue_id": issue["issue_id"],
        "evidence_digest": issue["details"]["evidence_digest"],
        "value": recorded_value,
        "reason": str(reason).strip(),
        "set_by": set_by or os.environ.get("USER") or os.environ.get("USERNAME"),
        "proposed_by": proposed_by,
        "set_at": _now(),
        "source": "person",
        "validation": validation,
        "reasons": [r["code"] for r in (issue.get("details") or {}).get("reasons") or []],
    })
    return {
        "schema_version": ANSWER_SCHEMA,
        "recorded": entry,
        "answers_path": str(store.answers_path),
        "net": manager.net_report(),
    }


def answer_site_facts_file(manager: Any, path: Path, *, set_by: Optional[str] = None,
                           proposed_by: Optional[str] = None) -> Dict[str, Any]:
    try:
        answers = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise _refuse("site_fact_answer_invalid", f"cannot read {path}: {error}")
    if not isinstance(answers, dict) or not answers:
        raise _refuse("site_fact_answer_invalid", f"{path} must map issue_id to an answer")
    recorded = []
    for issue_id, answer in answers.items():
        if not isinstance(answer, dict):
            answer = {"value": answer}
        recorded.append(answer_site_fact(
            manager, issue_id, answer.get("value"), reason=answer.get("reason"),
            set_by=answer.get("set_by") or set_by,
            proposed_by=answer.get("proposed_by") or proposed_by,
        )["recorded"])
    return {"schema_version": ANSWER_SCHEMA, "recorded": recorded,
            "answers_path": str(SiteFactStore(manager.jarvis.resource_graph_file).answers_path),
            "net": manager.net_report()}


def show_site_facts(manager: Any, *, nodes: Optional[int] = None,
                    flavour: Optional[str] = None) -> Dict[str, Any]:
    """``rg facts show``: evaluate the graph's site facts for a launch shape.

    ``nodes`` defaults to the number of hosts the net was collected on;
    ``flavour`` (openmpi, mpich, intel, cray) defaults to unknown, which
    reports name-sensitive cases as advisory.
    """
    net = manager.resource_graph.net
    graph_file = Path(manager.jarvis.resource_graph_file)
    if not net:
        return {"schema_version": SITE_FACTS_SCHEMA, "stage": "check",
                "answers_path": str(SiteFactStore(graph_file).answers_path),
                "issues": [], "confirmed": [], "resolved_by_override": [],
                "blocking": False, "net_present": False,
                "net_issue": manager._net_missing_issue()}
    report = site_fact_report(
        graph_file, net, flavour=mpi_flavour(flavour) if flavour else None,
        launch_nodes=nodes if nodes else len(net.get("collected_on") or []),
        stage="check")
    report["answers"] = SiteFactStore(graph_file).answers()
    return report


def forget_site_fact(manager: Any, fact: str) -> Dict[str, Any]:
    """``rg facts forget``: drop a recorded answer so JARVIS asks again."""
    store = SiteFactStore(Path(manager.jarvis.resource_graph_file))
    removed = store.forget(fact)
    if removed is None and fact in store.published_answers():
        raise _refuse("site_fact_published", f"the answer for {fact!r} is the one the "
                      "site publishes beside its graph, which JARVIS only reads; answer "
                      "the question again to record your own over it",
                      fact=fact, published_answers_path=str(store.published_answers_path))
    if removed is None:
        raise _refuse("site_fact_unknown", f"no answer is recorded for {fact!r}",
                      fact=fact, answers_path=str(store.answers_path),
                      recorded=sorted(store.answers()))
    return {"schema_version": ANSWER_SCHEMA, "forgotten": removed,
            "answers_path": str(store.answers_path),
            **({"published_still_applies": store.published_answers()[fact]}
               if fact in store.published_answers() else {}),
            "note": "an override the answer set stays in the graph; "
                    "`jarvis rg net clear` drops it"}


# --------------------------------------------------------------------------
# Pipeline entry points
# --------------------------------------------------------------------------


def _pipeline_flavour(ppl: Any) -> Optional[str]:
    explicit = os.environ.get("JARVIS_MPI_FLAVOUR")
    if explicit:
        return mpi_flavour(explicit)
    try:
        from jarvis_cd.core.installer import read_last_install

        last = read_last_install(ppl) or {}
    except Exception:
        return None
    flavours = {
        mpi_flavour((entry.get("mpi") or {}).get("name"))
        for entry in (last.get("packages") or {}).values()
        if isinstance(entry, dict) and entry.get("mpi")
    }
    flavours.discard(None)
    return flavours.pop() if len(flavours) == 1 else None


def _net_plan(net: Optional[Dict[str, Any]], launch_nodes: int,
              form: Optional[str] = None) -> Dict[str, Any]:
    """What the MPI launch will do about the network, for check and submit."""
    from jarvis_cd.util.network import net_collect_reasons

    reasons = net_collect_reasons(net, launch_nodes=launch_nodes, form=form)
    if not reasons:
        return {"action": "use_graph", "reasons": [],
                "note": "the launch applies the graph's net section as is"}
    return {
        "action": "collect_at_launch",
        "reasons": reasons,
        "note": ("before each multi-node MPI launch JARVIS collects the net facts "
                 "on that launch's hosts (read-only, inside the allocation), saves "
                 "them as the graph's net section and applies the normal selection; "
                 "a host that returns no facts, or a block-class site fact in the "
                 "fresh facts, refuses the launch with a typed issue instead of "
                 "launching with no network"),
    }


def _net_not_collected_issue(launch_nodes: int, flavour: Optional[str],
                             plan: Dict[str, Any],
                             net_status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    field = f"{ANSWER_FIELD_PREFIX}.{NET_LAUNCH}"
    form = (net_status or {}).get("form")
    graph_has = {
        "legacy_list": "a net section in the legacy list form (no per-node facts "
                       "or selection)",
        "unreadable": "a net section JARVIS can't read "
                      f"({(net_status or {}).get('error')})",
    }.get(form or "", "no net facts")
    issue = make_jarvis_issue(
        kind=SITE_FACT_KIND,
        subject="net",
        message=(
            f"The resource graph has {graph_has}, and this launch crosses "
            f"{launch_nodes} nodes. JARVIS will collect them on the launch hosts, "
            "inside the allocation, before the MPI launch, then apply the normal "
            "network selection. Nothing to answer now."
        ),
        identity={"fact": NET_LAUNCH, "reason": "net_not_collected"},
        field=field,
        details={
            "schema_version": SITE_FACTS_SCHEMA,
            "fact": NET_LAUNCH,
            "severity": ADVISORY,
            "blocking": False,
            "stage": "check",
            "plan": plan["action"],
            "reasons": [{
                "code": "net_not_collected", "severity": ADVISORY,
                "roles": ["data", "control"],
                "message": f"the resource graph has {graph_has}",
                "consequence": ("the launch collects the net facts first; a host "
                                "with no facts, or a block-class finding in them, "
                                "refuses the launch (exit 3 for a site fact) "
                                "rather than launching blind"),
                "plan": plan["action"],
            }],
            "pick": {"data": None, "control": None, "flavour": flavour,
                     "consequence_if_wrong": []},
            "probes": [{"purpose": "collect the net facts ahead of time (optional)",
                        "command": "jarvis ppl submit <site_net.yaml> (builtin.resource_graph "
                                   "net_only=true over the nodes)"}],
            "answer": {"field": field, "values": [],
                       "note": "not needed: the launch collects the facts itself"},
        },
        remedy=("none needed; to collect ahead of time, submit builtin.resource_graph "
                "with net_only=true over the compute nodes"),
    )
    issue["severity"] = ADVISORY
    issue["blocking"] = False
    issue["reason_codes"] = ["net_not_collected"]
    return issue


def pipeline_site_facts(ppl: Any, *, launch_nodes: Optional[int]) -> Optional[Dict[str, Any]]:
    """Site facts for a pipeline about to be submitted or run, or None.

    Only a launch over more than one node crosses a network. The report's
    ``net_plan`` says whether the launch uses the graph or collects first.
    """
    if launch_nodes is None or launch_nodes <= 1:
        return None
    from jarvis_cd.util.network import active_graph_file, net_section_status, network_disabled

    if network_disabled(getattr(ppl, "env", None), os.environ):
        return None
    flavour = _pipeline_flavour(ppl)
    if flavour == "cray":
        return None  # Cray MPICH picks the network itself; JARVIS sets nothing
    net_status = net_section_status()
    net = net_status["net"]
    plan = _net_plan(net, launch_nodes, form=net_status["form"])
    if not net:
        graph_file = active_graph_file()
        return {"schema_version": SITE_FACTS_SCHEMA, "stage": "check",
                "answers_path": str(SiteFactStore(graph_file).answers_path),
                "issues": [_net_not_collected_issue(launch_nodes, flavour, plan, net_status)],
                "confirmed": [], "resolved_by_override": [], "blocking": False,
                "net_present": False, "net_form": net_status["form"], "net_plan": plan}
    report = site_fact_report(active_graph_file(), net, flavour=flavour,
                              launch_nodes=launch_nodes, stage="check")
    report["net_present"] = True
    report["net_plan"] = plan
    return report


def launch_site_facts(flavour: str, net: Optional[Dict[str, Any]],
                      record: Dict[str, Any], *,
                      collected: bool = False) -> Optional[Dict[str, Any]]:
    """Site facts for one MPI launch, from its applied network record.

    ``collected``: this launch collected the facts itself, so no check saw
    them and the check's block rules apply (stage ``launch_collected``).
    """
    if not net or not record:
        return None
    from jarvis_cd.util.network import active_graph_file

    hosts_check = dict(record.get("hosts_not_in_graph_check") or {"severity": "info", "hosts": []})
    hosts_check["withheld"] = list(record.get("withheld") or [])
    return site_fact_report(
        active_graph_file(), net, flavour=mpi_flavour(flavour),
        launch_nodes=len(record.get("hosts") or []),
        hosts_check=hosts_check,
        stage="launch_collected" if collected else "launch",
        launch_hosts=list(record.get("hosts") or []),
    )


def refuse_blocking(report: Optional[Dict[str, Any]]) -> None:
    if report and report.get("blocking"):
        raise JarvisIssueError(
            [issue for issue in report["issues"] if issue.get("blocking")],
            exit_code=EXIT_SITE_FACT,
            document={"site_facts": report},
        )


def execution_site_facts(execution_root: Path,
                         prior: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Merge the pre-launch report with each launch's site facts for a record.

    Returns ``{schema_version, issues, confirmed, stages}`` or None when no
    stage evaluated anything. Issues are de-duplicated by ``issue_id``.
    """
    stages: List[Dict[str, Any]] = []
    summaries: List[Dict[str, Any]] = []
    if prior:
        if "stages" in prior:  # already merged by an earlier call
            stages.append(prior)
            summaries.extend(prior.get("stages") or [])
        else:
            stages.append(prior)
    seen_launches = {item.get("launch") for item in summaries}
    for launch in sorted((Path(execution_root) / "provenance").glob("launch-*")):
        if launch.name in seen_launches:
            continue
        try:
            document = json.loads((launch / "launch.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        report = (document.get("network") or {}).get("site_facts")
        if isinstance(report, dict):
            stages.append({**report, "launch": launch.name})
    if not stages:
        return None
    issues: Dict[Any, Dict[str, Any]] = {}
    confirmed: Dict[str, Dict[str, Any]] = {}
    superseded: List[str] = []
    for stage in stages:
        superseded.extend(stage.get("superseded_by_launch") or [])
    launch_evaluated = any(stage.get("launch") for stage in stages)
    for stage in stages:
        for issue in stage.get("issues") or []:
            details = issue.get("details") or {}
            # Once a launch evaluated the network on its hosts, the check's
            # graph-wide prediction (made before the hosts were known) is
            # superseded; launches over different hosts keep each version.
            if (launch_evaluated and not stage.get("launch")
                    and details.get("fact") == NET_LAUNCH
                    and details.get("stage") in (None, "check")):
                superseded.append(issue.get("issue_id"))
                continue
            key = (issue.get("issue_id"), tuple(issue.get("reason_codes") or ()))
            if stage.get("launch") and key not in issues:
                issue = dict(issue, launch=stage["launch"])
            issues.setdefault(key, issue)
        for item in stage.get("confirmed") or []:
            confirmed.setdefault(str(item.get("issue_id")), item)
        if "stages" not in stage:
            summaries.append({
                "stage": stage.get("stage"), "launch": stage.get("launch"),
                "blocking": stage.get("blocking"),
                "issue_ids": [i["issue_id"] for i in stage.get("issues") or []],
                "confirmed": [c.get("issue_id") for c in stage.get("confirmed") or []],
            })
    return {
        "schema_version": SITE_FACTS_SCHEMA,
        "issues": list(issues.values()),
        "advisories": launch_advisories({"issues": list(issues.values())}),
        "confirmed": list(confirmed.values()),
        "superseded_by_launch": sorted({i for i in superseded if i}),
        "stages": summaries,
    }


def launch_advisories(report: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The non-blocking issues of a site-facts report (always a list)."""
    if not isinstance(report, dict):
        return []
    return [issue for issue in report.get("issues") or []
            if isinstance(issue, dict) and not issue.get("blocking")]


# The execution record caps all metadata at 48 KB; per-node evidence grows
# with the host count, so the record holds a bounded copy and the full report
# lives next to it.
SITE_FACTS_FILE = "site-facts.json"
RECORD_SITE_FACTS_BUDGET = 24_000
_SLIM_DETAILS = ("schema_version", "fact", "severity", "blocking", "stage",
                 "evidence_digest", "pick", "answer", "previous_answer")
_SLIM_REASON = ("code", "severity", "roles", "message", "consequence", "plan",
                "hosts", "setting", "value", "cause")
_SLIM_CANDIDATE = ("value", "cidr", "kind", "speed_mbps", "rank", "picked_as", "usable")
_MINIMAL_ISSUE = ("schema_version", "issue_id", "kind", "subject", "field", "severity",
                  "blocking", "reason_codes", "supersedes", "remedy")


def _json_size(value: Any) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str))


def _slim_issue(issue: Dict[str, Any], minimal: bool) -> Dict[str, Any]:
    if not isinstance(issue, dict):
        return issue
    if minimal:
        slim = {key: issue[key] for key in _MINIMAL_ISSUE if key in issue}
        slim["message"] = str(issue.get("message") or "")[:400]
    else:
        slim = {key: value for key, value in issue.items()
                if key not in ("details", "candidates")}
        details = issue.get("details") or {}
        slim["details"] = {key: details[key] for key in _SLIM_DETAILS if key in details}
        slim["details"]["reasons"] = [
            {key: reason[key] for key in _SLIM_REASON if key in reason}
            for reason in details.get("reasons") or [] if isinstance(reason, dict)
        ]
        slim["candidates"] = [
            {key: candidate[key] for key in _SLIM_CANDIDATE if key in candidate}
            for candidate in issue.get("candidates") or [] if isinstance(candidate, dict)
        ]
    slim["trimmed"] = True
    return slim


def record_site_facts(report: Optional[Dict[str, Any]],
                      full_in: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """A copy of a site-facts report that fits the execution record.

    Unchanged when small. Otherwise the issues and advisories drop their
    per-node evidence and probes (then everything but their identity and
    codes), and ``trimmed.full_in`` names the file with the full report.
    Issue ids, kinds, severities and ``reason_codes`` always stay.
    """
    if not isinstance(report, dict) or _json_size(report) <= RECORD_SITE_FACTS_BUDGET:
        return report
    for minimal in (False, True):
        bounded = {key: value for key, value in report.items()
                   if key not in ("net_plan",)}
        for key in ("issues", "advisories"):
            if isinstance(report.get(key), list):
                bounded[key] = [_slim_issue(issue, minimal) for issue in report[key]]
        if minimal and isinstance(report.get("confirmed"), list):
            bounded["confirmed"] = [
                {key: item.get(key) for key in ("fact", "issue_id", "evidence_digest",
                                                "value", "reasons")}
                for item in report["confirmed"] if isinstance(item, dict)
            ]
        bounded["trimmed"] = {
            "reason": "the full report exceeds the execution record's metadata budget",
            "level": "minimal" if minimal else "no_evidence",
            "full_in": full_in,
        }
        if _json_size(bounded) <= RECORD_SITE_FACTS_BUDGET:
            return bounded
    return {
        "schema_version": report.get("schema_version"),
        "issue_ids": [i.get("issue_id") for i in report.get("issues") or []
                      if isinstance(i, dict)],
        "reason_codes": sorted({code for i in report.get("issues") or []
                                if isinstance(i, dict) for code in i.get("reason_codes") or []}),
        "trimmed": {"reason": "even the minimal report exceeds the budget",
                    "level": "ids_only", "full_in": full_in},
    }


def write_site_facts_file(execution_root: Path,
                          report: Optional[Dict[str, Any]]) -> Optional[str]:
    """Write the full report to ``<execution>/site-facts.json``; never raises."""
    if not isinstance(report, dict):
        return None
    path = Path(execution_root) / SITE_FACTS_FILE
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)
        return str(path)
    except OSError as error:
        print(f"jarvis: site facts file not written: {error}", file=sys.stderr)
        return None


def _full_prior(root: Path, recorded: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not (isinstance(recorded, dict) and recorded.get("trimmed")):
        return recorded
    try:
        return json.loads((root / SITE_FACTS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return recorded


def record_execution_site_facts(execution_root: Path,
                                execution_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Merge the launches' site facts into the execution record now.

    Called right after each launch writes its ``launch.json`` and again when
    the run ends or the scheduler script finalizes, so a run killed at its
    wall time still carries its launch advisories. The full merged report is
    written to ``site-facts.json``; the record holds :func:`record_site_facts`
    of it. Idempotent; never raises.
    """
    try:
        from jarvis_cd.core.execution import ExecutionStore, read_execution_record

        root = Path(execution_root)
        if not (root / ".jarvis-execution.json").is_file():
            return None
        current = read_execution_record(root, expected_execution_id=execution_id or root.name)
        recorded = current.metadata.get("site_facts")
        merged = execution_site_facts(root, _full_prior(root, recorded))
        if merged is None:
            return None
        full_in = write_site_facts_file(root, merged)
        bounded = record_site_facts(merged, full_in=full_in)
        if bounded == recorded:
            return merged
        ExecutionStore(root.parent, current.pipeline_id).update(
            current.execution_id, metadata={"site_facts": bounded})
        return merged
    except Exception as error:  # advisory evidence must never fail a run
        print(f"jarvis: site facts not recorded: {error}", file=sys.stderr)
        return None


def summarize(issues: Iterable[Dict[str, Any]]) -> List[str]:
    return [f"{issue['issue_id'][:19]} {issue.get('severity')} "
            + ",".join(r["code"] for r in issue["details"]["reasons"]) for issue in issues]
