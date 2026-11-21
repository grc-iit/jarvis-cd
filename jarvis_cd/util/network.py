"""Network facts for the resource graph and the MPI launch network.

The resource graph's ``net`` section (``jarvis.resource-graph.net.v1``) is
collected on the compute nodes themselves (``jarvis rg build`` inside an
allocation, or the ``builtin.resource_graph`` package submitted with
``jarvis ppl submit``). It records every IPv4 interface per node, groups
them into networks by CIDR, and selects:

* ``data``: the fastest usable network that reaches every node;
* ``control``: the network that holds each node's hostname address.

A network is unusable when it is loopback, link-local, down on some node,
or when its addresses repeat across nodes (``docker0`` is 172.17.0.1 on
every node, so an MPI rank that dials it reaches its own node). A person's
override (``jarvis rg net set``) wins over the selection and keeps its
provenance. The MPI execs turn the choice into each flavour's own settings.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

NET_SCHEMA = "jarvis.resource-graph.net.v1"
LAUNCH_NETWORK_SCHEMA = "jarvis.mpi-network.v1"
#: ``off``/``none`` disables the graph-driven launch network for one run
MPI_NETWORK_ENVIRONMENT = "JARVIS_MPI_NETWORK"
OVERRIDE_FIELD = "resource_graph.net.override"

_SYS_NET = Path("/sys/class/net")
_SYS_IB = Path("/sys/class/infiniband")
_ARPHRD_INFINIBAND = 32
#: interface kinds that carry real node-to-node traffic
_DIRECT_KINDS = ("physical", "bond", "vlan", "ipoib")

#: launch-failure signatures that point at the MPI network choice
MPI_NETWORK_FAILURE_SIGNATURES = (
    "received unexpected process identifier",
    "btl_tcp_endpoint",
    "btl_tcp: connect",
    "unable to reliably start",
    "lost communication with a remote daemon",
    "has lost communication",
    "no route to host",
    "the tcp btl failed",
    "unable to connect to the peer",
    "oob_tcp",
    "hydra_sock_connect",
    "control_cb (pm/pmiserv",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _interface_kind(name: str, addr: ipaddress.IPv4Interface) -> str:
    if name == "lo" or addr.ip.is_loopback:
        return "loopback"
    base = _SYS_NET / name
    if (base / "bridge").exists():
        return "bridge"
    if (base / "bonding").exists():
        return "bond"
    if Path("/proc/net/vlan", name).exists():
        return "vlan"
    if _read(base / "type") == str(_ARPHRD_INFINIBAND):
        return "ipoib"
    try:
        if (base.resolve().as_posix()).startswith("/sys/devices/virtual/"):
            return "virtual"
    except OSError:
        pass
    return "physical"


def _rdma_device(name: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (rdma device, UCX device ``dev:port``, port state) for ``name``."""
    ib_dir = _SYS_NET / name / "device" / "infiniband"
    try:
        devices = sorted(entry.name for entry in ib_dir.iterdir())
    except OSError:
        return None, None, None
    if not devices:
        return None, None, None
    device = devices[0]
    dev_port = _read(_SYS_NET / name / "dev_port") or "0"
    try:
        port = int(dev_port, 0) + 1
    except ValueError:
        port = 1
    state = _read(_SYS_IB / device / "ports" / str(port) / "state")
    return device, f"{device}:{port}", state


def _speed_mbps(name: str) -> Optional[int]:
    raw = _read(_SYS_NET / name / "speed")
    try:
        value = int(raw) if raw is not None else -1
    except ValueError:
        return None
    return value if value > 0 else None


def _parse_ip_addr(text: str) -> List[tuple[str, str]]:
    """Return ``(interface, address/prefix)`` pairs from ``ip -o -4 addr``."""
    pairs = []
    for line in text.splitlines():
        match = re.match(r"\s*\d+:\s+(\S+?)(?:@\S+)?\s+inet\s+(\S+)", line)
        if match:
            pairs.append((match.group(1), match.group(2)))
    return pairs


def _hostname_addresses(hostname: str) -> List[str]:
    addresses = set()
    try:
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass
    return sorted(addresses)


def collect_local_interfaces() -> Dict[str, Any]:
    """Collect this node's IPv4 interfaces for the resource graph."""
    hostname = socket.gethostname()
    result: Dict[str, Any] = {
        "hostname": hostname,
        "control_addrs": [
            address
            for address in _hostname_addresses(hostname)
            if not address.startswith("127.")
        ],
        "interfaces": [],
    }
    try:
        output = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        result["error"] = f"ip -o -4 addr show failed: {error}"
        return result
    for name, cidr_text in _parse_ip_addr(output):
        try:
            addr = ipaddress.IPv4Interface(cidr_text)
        except ValueError:
            continue
        rdma, ucx, port_state = _rdma_device(name)
        result["interfaces"].append(
            {
                "name": name,
                "addr": str(addr.ip),
                "prefixlen": addr.network.prefixlen,
                "cidr": str(addr.network),
                "state": (_read(_SYS_NET / name / "operstate") or "unknown").lower(),
                "speed_mbps": _speed_mbps(name),
                "kind": _interface_kind(name, addr),
                "mtu": _int_or_none(_read(_SYS_NET / name / "mtu")),
                "rdma_device": rdma,
                "ucx_device": ucx,
                "rdma_port_state": port_state,
            }
        )
    return result


def _int_or_none(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _short(host: str) -> str:
    return str(host).split(".", 1)[0]


def _scheduler_job() -> Optional[Dict[str, str]]:
    for scheduler, variable in (
        ("slurm", "SLURM_JOB_ID"),
        ("pbs", "PBS_JOBID"),
        ("lsf", "LSB_JOBID"),
        ("flux", "FLUX_JOB_ID"),
    ):
        value = os.environ.get(variable)
        if value:
            return {"scheduler": scheduler, "job_id": value}
    return None


def analyze_net(
    node_facts: Dict[str, Dict[str, Any]],
    *,
    override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the ``net`` section from per-node interface facts."""
    hosts = sorted(node_facts)
    by_cidr: Dict[str, List[tuple[str, Dict[str, Any]]]] = {}
    for host in hosts:
        for interface in node_facts[host].get("interfaces", []) or []:
            by_cidr.setdefault(interface["cidr"], []).append((host, interface))
    networks = []
    for cidr, members in sorted(by_cidr.items()):
        network = ipaddress.IPv4Network(cidr)
        member_hosts = sorted({host for host, _ in members})
        addresses = [interface["addr"] for _, interface in members]
        names = sorted({interface["name"] for _, interface in members})
        kinds = sorted({interface["kind"] for _, interface in members})
        speeds = [interface.get("speed_mbps") for _, interface in members]
        rdma = sorted({interface.get("rdma_device") or "" for _, interface in members})
        ucx = sorted({interface.get("ucx_device") or "" for _, interface in members})
        states = {interface.get("state", "unknown") for _, interface in members}
        port_states = {
            str(interface.get("rdma_port_state") or "") for _, interface in members
        }
        excluded = None
        if network.is_loopback or "loopback" in kinds:
            excluded = "loopback"
        elif network.is_link_local:
            excluded = "link_local"
        elif "down" in states or "lowerlayerdown" in states:
            excluded = "down"
        elif len(set(addresses)) < len(addresses):
            excluded = "duplicate_address"
        networks.append(
            {
                "cidr": cidr,
                "interface": names[0] if len(names) == 1 else None,
                "interfaces": {host: interface["name"] for host, interface in members},
                "kind": kinds[0] if len(kinds) == 1 else "mixed",
                "speed_mbps": (
                    min(speeds) if speeds and all(s is not None for s in speeds) else None
                ),
                "rdma_device": rdma[0] if len(rdma) == 1 and rdma[0] else None,
                "ucx_device": (
                    ucx[0]
                    if len(ucx) == 1 and ucx[0] and all("ACTIVE" in s for s in port_states)
                    else None
                ),
                "nodes": len(member_hosts),
                "reaches_all": member_hosts == hosts,
                "usable": excluded is None,
                "excluded_reason": excluded,
            }
        )

    def rank(network: Dict[str, Any]) -> tuple:
        direct = 0 if network["kind"] in _DIRECT_KINDS else 1
        return (direct, -(network["speed_mbps"] or 0), network["cidr"])

    candidates = sorted(
        (n for n in networks if n["usable"] and n["reaches_all"]), key=rank
    )
    data = candidates[0] if candidates else None
    control = None
    for network in networks:
        if not network["usable"] or not network["reaches_all"]:
            continue
        subnet = ipaddress.IPv4Network(network["cidr"])
        if all(
            any(
                ipaddress.IPv4Address(addr) in subnet
                for addr in node_facts[host].get("control_addrs", []) or []
            )
            for host in hosts
        ):
            control = network
            break
    if control is None:
        control = data
    section = {
        "schema_version": NET_SCHEMA,
        "collected_at": _now(),
        "collected_on": hosts,
        "collected_in": _scheduler_job(),
        "networks": networks,
        "selected": {
            "data": _choice(data, "graph", "fastest usable network reaching every node"),
            "control": _choice(
                control,
                "graph",
                "network holding each node's hostname address",
            ),
        },
        "override": override,
        "nodes": {
            host: {
                "control_addrs": node_facts[host].get("control_addrs", []),
                "interfaces": node_facts[host].get("interfaces", []),
            }
            for host in hosts
        },
    }
    errors = {
        host: facts["error"] for host, facts in node_facts.items() if facts.get("error")
    }
    if errors:
        section["errors"] = errors
    return section


def _choice(
    network: Optional[Dict[str, Any]], source: str, why: str
) -> Optional[Dict[str, Any]]:
    if network is None:
        return None
    return {
        "cidr": network["cidr"],
        "interface": network.get("interface"),
        "kind": network.get("kind"),
        "speed_mbps": network.get("speed_mbps"),
        "rdma_device": network.get("rdma_device"),
        "ucx_device": network.get("ucx_device"),
        "source": source,
        "reason": why,
    }


def _network_by_name(net: Dict[str, Any], value: str) -> Optional[Dict[str, Any]]:
    """Find a graph network by CIDR, interface name, or an address inside it."""
    for network in net.get("networks", []) or []:
        if value in (network.get("cidr"), network.get("interface")):
            return network
    try:
        address = ipaddress.IPv4Address(value.split("/", 1)[0])
    except ValueError:
        return None
    for network in net.get("networks", []) or []:
        if address in ipaddress.IPv4Network(network["cidr"]):
            return network
    return None


def make_override(
    net: Dict[str, Any],
    *,
    data: Optional[str],
    control: Optional[str],
    reason: Optional[str],
    set_by: Optional[str],
) -> Dict[str, Any]:
    """Validate and build a person's override of the network selection.

    ``data``/``control`` take a CIDR, an interface name or an address in the
    graph; ``none`` means "set nothing for this role". A value the graph
    does not know is kept (interface names are uniform on most clusters)
    but flagged ``known: false``.
    """
    override: Dict[str, Any] = {
        "set_at": _now(),
        "set_by": set_by or os.environ.get("USER") or os.environ.get("USERNAME"),
        "reason": reason,
        "source": "person",
    }
    for role, value in (("data", data), ("control", control)):
        if value is None:
            continue
        if value.lower() == "none":
            override[role] = {"disabled": True}
            continue
        network = _network_by_name(net, value)
        if network is None:
            entry: Dict[str, Any] = {"known": False}
            try:
                entry["cidr"] = str(ipaddress.IPv4Network(value, strict=False))
            except ValueError:
                entry["interface"] = value
            override[role] = entry
        else:
            entry = _choice(network, "person", reason or "override") or {}
            entry["known"] = True
            override[role] = entry
    return override


def resolve_launch_network(
    net: Optional[Dict[str, Any]], hosts: Iterable[str]
) -> Optional[Dict[str, Any]]:
    """Return the data/control choice for a launch over ``hosts``, or None."""
    if not net:
        return None
    selected = dict(net.get("selected") or {})
    override = net.get("override") or {}
    sources = {}
    for role in ("data", "control"):
        if role in override:
            entry = override[role]
            selected[role] = None if entry.get("disabled") else dict(entry)
            sources[role] = "override"
        elif selected.get(role):
            sources[role] = "graph"
    if not selected.get("data") and not selected.get("control"):
        return None
    graph_hosts = {_short(host) for host in net.get("collected_on", []) or []}
    launch_hosts = sorted({_short(host) for host in hosts})
    local = {"localhost", "127.0.0.1", _short(socket.gethostname())}
    missing = [
        host for host in launch_hosts if host not in graph_hosts and host not in local
    ]
    if launch_hosts and set(launch_hosts) <= local and len(launch_hosts) == 1:
        # A single-node launch never crosses a network.
        return None
    return {
        "schema_version": LAUNCH_NETWORK_SCHEMA,
        "data": selected.get("data"),
        "control": selected.get("control"),
        "sources": sources,
        "override": override or None,
        "graph_collected_at": net.get("collected_at"),
        "graph_collected_in": net.get("collected_in"),
        "hosts": launch_hosts,
        "hosts_not_in_graph": missing,
    }


def _ident(entry: Optional[Dict[str, Any]], *, prefer: str) -> Optional[str]:
    if not entry:
        return None
    if prefer == "interface":
        return entry.get("interface") or entry.get("cidr")
    return entry.get("cidr") or entry.get("interface")


def flavour_settings(flavour: str, choice: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """Return ``{"launcher_env", "rank_env", "args"}`` for one MPI flavour.

    Open MPI and PRRTE accept CIDRs, which stay correct when interface names
    differ between nodes; MPICH Hydra and Intel MPI need a name.
    """
    data, control = choice.get("data"), choice.get("control")
    launcher: Dict[str, str] = {}
    rank: Dict[str, str] = {}
    args: Dict[str, str] = {}
    if flavour == "openmpi":
        data_id = _ident(data, prefer="cidr")
        control_id = _ident(control, prefer="cidr")
        if data_id:
            launcher["OMPI_MCA_btl_tcp_if_include"] = data_id
        if control_id:
            launcher["OMPI_MCA_oob_tcp_if_include"] = control_id
            launcher["PRTE_MCA_oob_tcp_if_include"] = control_id
        rank.update(launcher)
    elif flavour in ("mpich", "intel"):
        data_if = (data or {}).get("interface")
        control_if = (control or {}).get("interface") or data_if
        if flavour == "intel":
            if control_if:
                launcher["I_MPI_HYDRA_IFACE"] = control_if
            if data_if:
                rank["FI_TCP_IFACE"] = data_if
                rank["FI_SOCKETS_IFACE"] = data_if
        else:
            # Hydra's -iface also picks the address ranks publish for
            # ch3/nemesis TCP, so it carries the data network.
            if data_if or control_if:
                launcher["HYDRA_IFACE"] = data_if or control_if
                args["-iface"] = data_if or control_if
            if data_if:
                rank["MPIR_CVAR_CH3_NETWORK_IFACE"] = data_if
                rank["FI_TCP_IFACE"] = data_if
    # Cray MPICH drives the high-speed network itself: nothing to set.
    if flavour != "cray" and data and data.get("ucx_device"):
        rank["UCX_NET_DEVICES"] = data["ucx_device"]
        launcher.setdefault("UCX_NET_DEVICES", data["ucx_device"])
    return {"launcher_env": launcher, "rank_env": rank, "args": args}


def _conflicts(env: Dict[str, Any], key: str) -> bool:
    """True when the caller already set ``key`` or its exclusive partner."""
    if key in env:
        return True
    if key.endswith("_if_include"):
        return key.replace("_if_include", "_if_exclude") in env
    return False


def _caller_value(env: Dict[str, Any], other: Dict[str, Any], key: str) -> Any:
    for source in (env, other):
        if key in source:
            return source[key]
    partner = key.replace("_if_include", "_if_exclude")
    for source in (env, other):
        if partner in source:
            return {partner: source[partner]}
    return None


def apply_launch_network(
    flavour: str,
    choice: Optional[Dict[str, Any]],
    *,
    launcher_env: Dict[str, Any],
    rank_env: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Apply a choice in place; settings the caller already made win."""
    if not choice:
        return None
    settings = flavour_settings(flavour, choice)
    applied: Dict[str, Any] = {"launcher_env": {}, "rank_env": {}, "args": {}}
    kept: Dict[str, Any] = {}
    for key, value in settings["launcher_env"].items():
        if _conflicts(launcher_env, key) or _conflicts(rank_env, key):
            kept[key] = _caller_value(launcher_env, rank_env, key)
            continue
        launcher_env[key] = value
        applied["launcher_env"][key] = value
    for key, value in settings["rank_env"].items():
        if key in kept or _conflicts(rank_env, key):
            kept.setdefault(key, _caller_value(rank_env, launcher_env, key))
            continue
        rank_env[key] = value
        applied["rank_env"][key] = value
    for key, value in settings["args"].items():
        if "HYDRA_IFACE" in kept:
            continue
        applied["args"][key] = value
    record = dict(choice)
    record.update({"flavour": flavour, "applied": applied, "kept_from_caller": kept})
    return record


def network_disabled(*envs: Optional[Dict[str, Any]]) -> bool:
    for env in envs:
        value = str((env or {}).get(MPI_NETWORK_ENVIRONMENT, "")).strip().lower()
        if value in ("off", "none", "0", "false", "disabled"):
            return True
    return False


def load_net_section() -> Optional[Dict[str, Any]]:
    """Load the active graph's ``net`` section without initializing JARVIS."""
    import yaml

    path: Optional[Path] = None
    site = os.environ.get("JARVIS_RESOURCE_GRAPH", "").strip()
    if site:
        path = Path(site).expanduser()
    else:
        try:
            from jarvis_cd.core.config import Jarvis

            instance = Jarvis._instance
            if instance is not None and getattr(instance, "_initialized", False):
                path = Path(instance.resource_graph_file)
        except Exception:
            path = None
        if path is None:
            root = os.environ.get("JARVIS_ROOT")
            path = (
                Path(root).expanduser() if root else Path.home() / ".ppi-jarvis"
            ) / "resource_graph.yaml"
    try:
        with open(path, "r", encoding="utf-8") as stream:
            graph = yaml.safe_load(stream) or {}
    except (OSError, ValueError):
        return None
    net = graph.get("net") if isinstance(graph, dict) else None
    return net if isinstance(net, dict) else None


def classify_mpi_network_failure(
    text: str, *, launch_network: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Turn a launch failure that matches a network signature into an issue."""
    from jarvis_cd.core.issues import make_jarvis_issue

    lowered = (text or "").lower()
    matched = [s for s in MPI_NETWORK_FAILURE_SIGNATURES if s in lowered]
    if not matched:
        return None
    excerpt = ""
    for line in (text or "").splitlines():
        if any(signature in line.lower() for signature in matched):
            excerpt = line.strip()[:400]
            break
    choice = launch_network or {}
    return make_jarvis_issue(
        kind="mpi_network",
        subject="mpi launch network",
        message=(
            "The MPI launch failed with a network signature. Check the "
            "resource graph's network choice and set an override with "
            "'jarvis rg net set --data <cidr|iface> --control <cidr|iface>' "
            "(or rebuild the graph inside an allocation with 'jarvis rg build')."
        ),
        identity={
            "signatures": matched,
            "data": (choice.get("data") or {}).get("cidr"),
            "control": (choice.get("control") or {}).get("cidr"),
        },
        field=OVERRIDE_FIELD,
        details={
            "signatures": matched,
            "excerpt": excerpt,
            "launch_network": choice or None,
            "selected_field": "resource_graph.net.selected",
        },
        remedy="jarvis rg net set --data <cidr|iface> --control <cidr|iface> --reason <why>",
    )
