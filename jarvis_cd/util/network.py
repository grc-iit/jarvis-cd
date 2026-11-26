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
import shutil
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
#: path of the ``ucx_info`` belonging to the UCX the MPI uses
UCX_INFO_ENVIRONMENT = "JARVIS_UCX_INFO"
_UCX_NON_RDMA_TRANSPORTS = frozenset({
    "tcp", "self", "posix", "sysv", "cma", "knem", "xpmem",
    "cuda_copy", "cuda_ipc", "gdr_copy", "rocm_copy", "rocm_ipc", "ze_copy", "ze_ipc",
})

_SYS_NET = Path("/sys/class/net")
_SYS_IB = Path("/sys/class/infiniband")
_ARPHRD_INFINIBAND = 32
#: interface kinds that carry real node-to-node traffic
_DIRECT_KINDS = ("physical", "bond", "vlan", "ipoib")

#: signatures of the launcher failing to start or wire up its daemons: they
#: count only while some rank never started, since a job tearing down after
#: the application failed prints them too
MPI_LAUNCH_STAGE_SIGNATURES = (
    "unable to reliably start",
    "lost communication with a remote daemon",
    "has lost communication",
    "no route to host",
    "oob_tcp",
    "hydra_sock_connect",
    "control_cb (pm/pmiserv",
)
#: signatures of ranks failing to connect to their peers over the MPI network
MPI_RANK_STAGE_SIGNATURES = (
    "received unexpected process identifier",
    "btl_tcp_endpoint",
    "btl_tcp: connect",
    "the tcp btl failed",
    "unable to connect to the peer",
)
#: launch-failure signatures that point at the MPI network choice
MPI_NETWORK_FAILURE_SIGNATURES = MPI_LAUNCH_STAGE_SIGNATURES + MPI_RANK_STAGE_SIGNATURES

#: MPI settings named in a launch's own description or environment (such as
#: ``OMPI_MCA_oob_tcp_if_include``), which are never a failure signature
_MPI_SETTING_NAME = re.compile(
    r"\b(?:OMPI|PRTE|PMIX)_MCA_\w+"
    r"|(?<!\S)--?(?:g?mca|prtemca|pmixmca)\s+\S+"
    r"|\b\w+_tcp_if_(?:include|exclude)\b",
    re.IGNORECASE,
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


def _rdma_device(
    name: str,
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Return (rdma device, ``dev:port``, port state, link layer) for ``name``."""
    ib_dir = _SYS_NET / name / "device" / "infiniband"
    try:
        devices = sorted(entry.name for entry in ib_dir.iterdir())
    except OSError:
        return None, None, None, None
    if not devices:
        return None, None, None, None
    device = devices[0]
    dev_port = _read(_SYS_NET / name / "dev_port") or "0"
    try:
        port = int(dev_port, 0) + 1
    except ValueError:
        port = 1
    port_dir = _SYS_IB / device / "ports" / str(port)
    return device, f"{device}:{port}", _read(port_dir / "state"), _read(port_dir / "link_layer")


def parse_ucx_info_devices(text: str) -> Dict[str, List[str]]:
    """Map each device ``ucx_info -d`` lists to its RDMA transports.

    Devices UCX only reaches through TCP or shared memory are left out: a
    device counts for ``UCX_NET_DEVICES`` only when this UCX build has an
    RDMA transport (``rc_mlx5``, ``rc_verbs``, ``ud_*``, ``dc_*``...) for it.
    """
    devices: Dict[str, set] = {}
    transport = None
    for line in text.splitlines():
        match = re.match(r"#\s*Transport:\s*(\S+)", line)
        if match:
            transport = match.group(1)
            continue
        match = re.match(r"#\s*Device:\s*(\S+)", line)
        if match and transport and transport not in _UCX_NON_RDMA_TRANSPORTS:
            devices.setdefault(match.group(1), set()).add(transport)
    return {device: sorted(transports) for device, transports in sorted(devices.items())}


def probe_ucx(timeout: int = 30) -> Dict[str, Any]:
    """Ask this node's UCX which devices it can drive over RDMA.

    ``$JARVIS_UCX_INFO`` names the ``ucx_info`` of the UCX the MPI uses when
    that is not the one on ``PATH``. Only fixed arguments are passed.
    """
    path = os.environ.get(UCX_INFO_ENVIRONMENT) or shutil.which("ucx_info")
    if not path:
        return {"ucx_info": None, "status": "ucx_info_missing"}
    result: Dict[str, Any] = {"ucx_info": path}
    try:
        version = subprocess.run([path, "-v"], capture_output=True, text=True,
                                 timeout=timeout, check=False).stdout
        match = re.search(r"version=(\S+)", version or "")
        result["version"] = match.group(1) if match else None
        listed = subprocess.run([path, "-d"], capture_output=True, text=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return dict(result, status="ucx_info_failed", error=str(error)[-500:])
    if listed.returncode != 0:
        return dict(result, status="ucx_info_failed",
                    error=(listed.stderr or listed.stdout or "")[-500:])
    return dict(result, status="ok", rdma_devices=parse_ucx_info_devices(listed.stdout))


def ucx_device_status(
    port: Optional[str], port_state: Optional[str], ucx: Optional[Dict[str, Any]]
) -> str:
    """``usable`` only when the port is ACTIVE and this UCX lists it for RDMA."""
    if "ACTIVE" not in str(port_state or ""):
        return "port_not_active"
    status = (ucx or {}).get("status") or "ucx_info_missing"
    if status != "ok":
        return status
    if port not in ((ucx or {}).get("rdma_devices") or {}):
        return "not_listed_by_ucx"
    return "usable"


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
    ucx: Optional[Dict[str, Any]] = None
    for name, cidr_text in _parse_ip_addr(output):
        try:
            addr = ipaddress.IPv4Interface(cidr_text)
        except ValueError:
            continue
        rdma, port, port_state, link_layer = _rdma_device(name)
        interface = {
            "name": name,
            "addr": str(addr.ip),
            "prefixlen": addr.network.prefixlen,
            "cidr": str(addr.network),
            "state": (_read(_SYS_NET / name / "operstate") or "unknown").lower(),
            "speed_mbps": _speed_mbps(name),
            "kind": _interface_kind(name, addr),
            "mtu": _int_or_none(_read(_SYS_NET / name / "mtu")),
            "rdma_device": rdma,
            "ucx_device": None,
            "rdma_port_state": port_state,
        }
        if rdma:
            if ucx is None:
                ucx = probe_ucx()
            status = ucx_device_status(port, port_state, ucx)
            interface.update({
                "rdma_port": port,
                "rdma_link_layer": link_layer,
                "ucx_status": status,
                "ucx_transports": ((ucx.get("rdma_devices") or {}).get(port) or []),
                # Pinned only when this node's UCX can drive the port.
                "ucx_device": port if status == "usable" else None,
            })
        result["interfaces"].append(interface)
    if ucx is not None:
        result["ucx"] = ucx
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
        ucx_states = {host: interface.get("ucx_status") for host, interface in members
                      if interface.get("rdma_device")}
        states = {interface.get("state", "unknown") for _, interface in members}
        verified = (
            len(ucx) == 1 and bool(ucx[0]) and len(ucx_states) == len(members)
            and all(status == "usable" for status in ucx_states.values())
        )
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
                # Only a device every member's UCX verified (port ACTIVE and
                # listed by ``ucx_info -d`` with an RDMA transport).
                "ucx_device": ucx[0] if verified else None,
                "ucx_verified": verified,
                **({"ucx_status": ucx_states} if ucx_states else {}),
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
                **({"ucx": node_facts[host]["ucx"]} if node_facts[host].get("ucx") else {}),
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
        "ucx_verified": bool(network.get("ucx_verified")),
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
    choice = {
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
    if sources.get("data") != "override":
        unusable = ucx_unusable_hosts(net, (selected.get("data") or {}).get("cidr"),
                                      [h for h in launch_hosts if h in graph_hosts])
        if unusable:
            choice["ucx_unusable"] = unusable
        elif not missing:
            device = launch_ucx_device(net, selected.get("data"), launch_hosts)
            if device:
                choice["data"] = dict(selected["data"], ucx_device=device,
                                      ucx_verified=True, ucx_scope="launch_hosts")
    return choice


def launch_ucx_device(net: Optional[Dict[str, Any]], data: Optional[Dict[str, Any]],
                      hosts: Iterable[str]) -> Optional[str]:
    """The UCX device every launch host verified, when the graph-wide one is unset.

    A merged graph leaves ``ucx_device`` unset when UCX can't use it on some
    host; a launch whose own hosts all verified the same device still pins
    it. None when the graph already pins one, or any launch host lacks it.
    """
    if not net or not data or data.get("ucx_device") or not data.get("cidr"):
        return None
    devices = set()
    hosts = list(hosts)
    for host in hosts:
        interfaces = ((net.get("nodes") or {}).get(host) or {}).get("interfaces") or []
        interface = next((i for i in interfaces if i.get("cidr") == data["cidr"]), None)
        if (interface is None or interface.get("ucx_status") != "usable"
                or not interface.get("ucx_device")):
            return None
        devices.add(interface["ucx_device"])
    return devices.pop() if hosts and len(devices) == 1 else None


def ucx_unusable_hosts(
    net: Optional[Dict[str, Any]], data_cidr: Optional[str], hosts: Iterable[str]
) -> Dict[str, str]:
    """Launch hosts where UCX cannot use the data network's RDMA device.

    Empty when no launch host has an RDMA device on the data network (UCX
    then has nothing to prefer over TCP) or when UCX can use it on all of
    them. Otherwise ``{host: status}`` for each host that cannot (its
    ``ucx_status``, ``port_not_active``, ``unverified`` or
    ``no_rdma_device``): ranks there would use different transports than
    the rest, or a UCX that errors, so Open MPI is kept off UCX.
    """
    if not net or not data_cidr:
        return {}
    statuses: Dict[str, str] = {}
    any_rdma = False
    for host in hosts:
        interfaces = ((net.get("nodes") or {}).get(host) or {}).get("interfaces") or []
        interface = next((i for i in interfaces if i.get("cidr") == data_cidr), None)
        if interface is None:
            continue
        if not interface.get("rdma_device"):
            statuses[host] = "no_rdma_device"
            continue
        any_rdma = True
        status = interface.get("ucx_status")
        if not status:
            port = str(interface.get("rdma_port_state") or "")
            status = "unverified" if "ACTIVE" in port else "port_not_active"
        statuses[host] = status
    if not any_rdma:
        return {}
    return {host: status for host, status in sorted(statuses.items()) if status != "usable"}


def launch_crosses_network(hosts: Iterable[str]) -> bool:
    """True unless the launch is one node that is this one."""
    launch_hosts = {_short(host) for host in hosts}
    local = {"localhost", "127.0.0.1", _short(socket.gethostname())}
    return bool(launch_hosts) and not (launch_hosts <= local and len(launch_hosts) == 1)


def predates_ucx_verification(net: Optional[Dict[str, Any]]) -> bool:
    """True for a graph whose data pick has an RDMA device but no UCX check."""
    data = ((net or {}).get("selected") or {}).get("data") or {}
    return "ucx_verified" not in data and bool(
        data.get("ucx_device") or data.get("rdma_device")
    )


def net_collect_reasons(
    net: Optional[Dict[str, Any]],
    *,
    hosts: Optional[Iterable[str]] = None,
    launch_nodes: Optional[int] = None,
    form: Optional[str] = None,
) -> List[str]:
    """Why a multi-node launch must collect its hosts' net facts first.

    ``net_missing``: the graph has no ``net`` section (``net_legacy_form``
    when ``form`` says it is the legacy list, ``net_unreadable`` when it
    can't be read: neither has per-node facts). ``hosts_not_in_graph``:
    some launch hosts (or, before the hosts are known, more nodes than the
    graph saw) have no facts. ``graph_predates_ucx_verification``: the data
    pick's RDMA device was never checked with UCX. A person's override of
    both roles needs no facts. Empty when the graph covers the launch.
    """
    if not net or not (net.get("collected_on") or net.get("override")):
        if not net and form == NET_LEGACY_FORM:
            return ["net_legacy_form"]
        if not net and form == "unreadable":
            return ["net_unreadable"]
        return ["net_missing"]
    override = net.get("override") or {}
    if "data" in override and "control" in override:
        return []
    reasons = []
    graph_hosts = {_short(host) for host in net.get("collected_on") or []}
    if hosts is not None:
        local = {"localhost", "127.0.0.1", _short(socket.gethostname())}
        if any(_short(host) not in graph_hosts and _short(host) not in local
               for host in hosts):
            reasons.append("hosts_not_in_graph")
    elif launch_nodes is not None and launch_nodes > len(graph_hosts):
        reasons.append("hosts_not_in_graph")
    if "data" not in override and predates_ucx_verification(net):
        reasons.append("graph_predates_ucx_verification")
    return reasons


def merge_net_facts(
    previous: Optional[Dict[str, Any]],
    facts: Dict[str, Dict[str, Any]],
    *,
    override: Optional[Dict[str, Any]] = None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Merge freshly collected per-host facts into a stored net section.

    Hosts in ``facts`` replace their stored entries (matched by short host
    name); every other stored host is kept, then the selection is
    recomputed over the union. ``node_collected`` records when and in
    which job each host's facts were collected. A stored section without
    per-node facts (older schema) cannot be merged and is replaced.
    Returns ``(section, merge)``.
    """
    fresh_short = {_short(host) for host in facts}
    previous = previous if isinstance(previous, dict) else None
    stored_nodes = (previous or {}).get("nodes")
    kept: Dict[str, Dict[str, Any]] = {}
    merge: Dict[str, Any] = {"mode": "new"}
    if previous and previous.get("collected_on"):
        if isinstance(stored_nodes, dict) and stored_nodes:
            merge["mode"] = "merged"
            errors = previous.get("errors") or {}
            for host, entry in stored_nodes.items():
                if _short(host) in fresh_short or not isinstance(entry, dict):
                    continue
                kept[host] = dict(entry)
                if errors.get(host):
                    kept[host]["error"] = errors[host]
        else:
            merge.update(mode="replaced",
                         why="the stored net section has no per-node facts "
                             "(older schema), so it cannot be merged")
    union = dict(kept)
    union.update(facts)
    section = analyze_net(union, override=override)
    job = section.get("collected_in")
    stored_collected = (previous or {}).get("node_collected") or {}
    node_collected: Dict[str, Any] = {}
    for host in section["collected_on"]:
        if host in kept:
            node_collected[host] = stored_collected.get(host) or {
                "collected_at": previous.get("collected_at"),
                "collected_in": previous.get("collected_in"),
            }
        else:
            node_collected[host] = {"collected_at": section["collected_at"],
                                    "collected_in": job}
    section["node_collected"] = node_collected
    if not all(entry.get("collected_in") for entry in node_collected.values()):
        # Some hosts' facts did not come from inside an allocation.
        section["collected_in"] = None
    previous_short = {_short(host) for host in (previous or {}).get("collected_on") or []}
    merge.update(
        kept_hosts=sorted(kept),
        replaced_hosts=sorted(h for h in facts if _short(h) in previous_short),
        added_hosts=sorted(h for h in facts if _short(h) not in previous_short),
        collected_on=section["collected_on"],
    )
    return section, merge


def persist_net_section(
    net: Optional[Dict[str, Any]],
    *,
    facts: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Save net facts into JARVIS's own graph's net section, under a lock.

    With ``facts`` (per-host collector output) the save merges per host:
    the collected hosts' entries are replaced, all other hosts are kept,
    and the selection is recomputed over the union (``merge_net_facts``);
    the merged section is returned as ``net``. Without ``facts``, ``net``
    replaces the section. A person's override already in the file is
    kept either way. With a site graph, a net section the site publishes is
    the starting point until JARVIS has one of its own; the site graph is
    never written. Never raises: the launch has its facts either way.
    """
    import tempfile

    import yaml

    from jarvis_cd.util.site_state import read_graph_file, site_graph_file

    result: Dict[str, Any] = {"persisted": False}
    try:
        path = active_graph_file()
        result["path"] = str(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.parent / ".resource_graph.lock", "a+") as lock:
            try:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - non-POSIX
                pass
            try:
                graph = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except FileNotFoundError:
                graph = {}
            if not isinstance(graph, dict):
                graph = {}
            replaced_form = classify_net_section(graph.get("net"))["form"]
            previous = graph.get("net") if isinstance(graph.get("net"), dict) else None
            if previous is None:
                published = (read_graph_file(site_graph_file()) or {}).get("net")
                previous = published if isinstance(published, dict) else None
            if facts is not None:
                section, merge = merge_net_facts(
                    previous, facts,
                    override=(previous or {}).get("override")
                    if previous is not None else (net or {}).get("override"),
                )
                result["merge"] = merge
            else:
                section = dict(net or {})
                if previous is not None:
                    section["override"] = previous.get("override")
                result["merge"] = {"mode": "replaced",
                                   "replaced_collected_on": (previous or {}).get("collected_on")}
            if replaced_form in (NET_LEGACY_FORM, "unreadable"):
                # an older or unreadable value in JARVIS's own graph gives way
                result["merge"]["replaced_form"] = replaced_form
            graph["net"] = section
            descriptor, temporary = tempfile.mkstemp(
                dir=path.parent, prefix=".resource_graph.", suffix=".tmp", text=True
            )
            try:
                if os.name != "nt":
                    os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "w") as stream:
                    yaml.dump(graph, stream, default_flow_style=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        result["persisted"] = True
        result["net"] = section
    except Exception as error:
        result["error"] = str(error)[:500]
    return result


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
    withheld = []
    data = choice.get("data") or {}
    from_override = (choice.get("sources") or {}).get("data") == "override"
    if flavour != "cray" and data.get("ucx_device") and not from_override:
        if not data.get("ucx_verified"):
            # A graph collected before JARVIS asked UCX itself: the device
            # may exist in sysfs yet be one this UCX cannot drive.
            withheld.append({
                "setting": "UCX_NET_DEVICES",
                "value": data["ucx_device"],
                "hosts": list(choice.get("hosts") or []),
                "cause": "unverified_graph",
                "reason": "the graph predates UCX verification, so nothing shows "
                          f"this UCX can drive {data['ucx_device']}; re-collect the "
                          "net section to pin it",
            })
        elif choice.get("hosts_not_in_graph"):
            # A UCX device is a per-host name, and CIDRs say nothing about it:
            # the graph only shows it usable on the nodes it saw. Leave
            # UCX_NET_DEVICES unset so UCX picks the devices each node has.
            withheld.append({
                "setting": "UCX_NET_DEVICES",
                "value": data["ucx_device"],
                "hosts": list(choice["hosts_not_in_graph"]),
                "cause": "hosts_not_in_graph",
                "reason": "the graph never saw these hosts, so it cannot say the "
                          f"device {data['ucx_device']} is usable on them; UCX picks "
                          "its own devices instead",
            })
    if withheld:
        choice = dict(choice, data={k: v for k, v in data.items() if k != "ucx_device"})
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
    ucx_excluded = None
    unusable = choice.get("ucx_unusable") or {}
    if flavour == "openmpi" and unusable:
        # UCX cannot use the data network's RDMA device on these hosts: keep
        # Open MPI's UCX components (pml and osc both open UCX) off, so every
        # rank runs ob1/TCP on the data CIDR set above instead of a UCX that
        # errors or picks different transports on different nodes.
        ucx_excluded = {"hosts": dict(unusable), "settings": {}, "kept_from_caller": {}}
        for key in ("OMPI_MCA_pml", "OMPI_MCA_osc"):
            if key in launcher_env or key in rank_env:
                ucx_excluded["kept_from_caller"][key] = _caller_value(
                    rank_env, launcher_env, key)
                kept.setdefault(key, ucx_excluded["kept_from_caller"][key])
                continue
            launcher_env[key] = rank_env[key] = "^ucx"
            applied["launcher_env"][key] = applied["rank_env"][key] = "^ucx"
            ucx_excluded["settings"][key] = "^ucx"
        ucx_excluded["reason"] = (
            "UCX cannot use the data network's RDMA device on "
            + ", ".join(f"{h} ({s})" for h, s in sorted(unusable.items()))
            + "; Open MPI runs ob1/TCP on the data network instead")
    record = dict(choice)
    if withheld:
        record["data"] = dict(data)
        record["withheld"] = withheld
    if ucx_excluded:
        record["ucx_excluded"] = ucx_excluded
    record.update({"flavour": flavour, "applied": applied, "kept_from_caller": kept})
    if record.get("hosts_not_in_graph"):
        record["hosts_not_in_graph_check"] = classify_hosts_not_in_graph(
            flavour, choice, record["hosts_not_in_graph"]
        )
        unseen = [item for item in withheld if item["cause"] == "hosts_not_in_graph"]
        if unseen:
            record["hosts_not_in_graph_check"]["withheld"] = unseen
    return record


def _resolve_host(host: str) -> Optional[str]:
    try:
        return socket.gethostbyname(host)
    except (OSError, UnicodeError):
        return None


def classify_hosts_not_in_graph(
    flavour: str,
    choice: Dict[str, Any],
    hosts: Iterable[str],
    resolve: Any = None,
) -> Dict[str, Any]:
    """Say whether launching on hosts the graph never saw is safe.

    ``info`` when the settings name networks by CIDR (Open MPI and PRRTE)
    and every such host's hostname address lies in a chosen CIDR: each node
    then picks its own interface in that subnet, as the graph's nodes do.
    ``warning`` otherwise, with the remedy of collecting the graph on them.
    """
    resolve = resolve or _resolve_host
    cidrs = []
    for role in ("control", "data"):
        cidr = (choice.get(role) or {}).get("cidr")
        if cidr:
            try:
                cidrs.append((role, ipaddress.IPv4Network(cidr, strict=False)))
            except ValueError:
                pass
    checked = []
    for host in hosts:
        address = resolve(host)
        roles = []
        if address:
            try:
                parsed = ipaddress.IPv4Address(address)
                roles = [role for role, network in cidrs if parsed in network]
            except ValueError:
                pass
        checked.append({"host": host, "address": address, "in_chosen_cidrs": roles})
    outside = [entry["host"] for entry in checked if not entry["in_chosen_cidrs"]]
    remedy = ("collect the resource graph's net section on these hosts: "
              "builtin.resource_graph (or jarvis rg build +net_only) in a job "
              "that spans them")
    if flavour == "cray":
        severity, reason = "info", "Cray MPICH sets no network: the graph is not used"
    elif flavour == "openmpi" and cidrs and not outside:
        severity = "info"
        reason = (
            "networks are chosen by CIDR, and each of these hosts' hostname "
            "address is in a chosen CIDR, so every node binds its own interface "
            "in that subnet just as the graph's nodes do; interface names and "
            "speeds on them were not checked"
        )
    elif flavour == "openmpi":
        severity = "warning"
        reason = ("these hosts' hostname addresses are not in the chosen CIDRs "
                  "(or did not resolve): " + ", ".join(outside))
    else:
        severity = "warning"
        reason = (f"{flavour} takes the networks by interface name, and the graph "
                  "has not seen these hosts' interface names")
    result: Dict[str, Any] = {"severity": severity, "reason": reason, "hosts": checked}
    if severity == "warning":
        result["remedy"] = remedy
    return result


def network_disabled(*envs: Optional[Dict[str, Any]]) -> bool:
    for env in envs:
        value = str((env or {}).get(MPI_NETWORK_ENVIRONMENT, "")).strip().lower()
        if value in ("off", "none", "0", "false", "disabled"):
            return True
    return False


def active_graph_file() -> Path:
    """JARVIS's own (writable) graph file, without initializing JARVIS.

    With ``JARVIS_RESOURCE_GRAPH`` this is the graph in the site home, never
    the site graph, which JARVIS only reads (:mod:`jarvis_cd.util.site_state`).
    """
    from jarvis_cd.util.site_state import own_graph_file

    return own_graph_file()


NET_LEGACY_FORM = "legacy_list"


def classify_net_section(value: Any) -> Dict[str, Any]:
    """What a graph's ``net`` value is, without changing it.

    ``form``: ``absent``; ``v1`` (a mapping, :data:`NET_SCHEMA`);
    ``legacy_list`` (an older JARVIS's list of libfabric entries, each with
    ``fabric``/``provider``/``domain``); or ``unreadable``. ``net`` is the v1
    mapping or None. ``legacy`` reads a legacy list as it is: its entries and
    the fabrics, domains and providers they name, with no per-node facts or
    selection. ``error`` says why an unreadable value can't be read.
    """
    if value is None:
        return {"form": "absent", "net": None, "legacy": None, "error": None}
    if isinstance(value, dict):
        return {"form": "v1", "net": value, "legacy": None, "error": None}
    legacy_keys = ("fabric", "provider", "domain")
    if isinstance(value, list):
        bad = [index for index, item in enumerate(value)
               if not isinstance(item, dict) or not any(key in item for key in legacy_keys)]
        if value and not bad:
            fabrics: Dict[str, Dict[str, Any]] = {}
            for item in value:
                key = str(item.get("fabric") or "")
                entry = fabrics.setdefault(key, {"fabric": item.get("fabric"),
                                                 "domains": [], "providers": [],
                                                 "shared": []})
                for field, plural in (("domain", "domains"), ("provider", "providers"),
                                      ("shared", "shared")):
                    if field in item and item[field] not in entry[plural]:
                        entry[plural].append(item[field])
            return {"form": NET_LEGACY_FORM, "net": None, "error": None,
                    "legacy": {"form": NET_LEGACY_FORM, "entries": value,
                               "fabrics": list(fabrics.values())}}
        error = ("net is an empty list" if not value else
                 f"net is a list, but entries {bad[:10]} are not mappings with "
                 f"{'/'.join(legacy_keys)} (the legacy form)")
    else:
        error = f"net is a {type(value).__name__}"
    return {"form": "unreadable", "net": None, "legacy": None,
            "error": (f"{error}; expected a mapping ({NET_SCHEMA}) or the legacy "
                      f"list form")}


def net_section_status() -> Dict[str, Any]:
    """The effective graph's ``net`` value, classified, with its file.

    :func:`classify_net_section` plus ``graph`` (the file the section comes
    from) and ``graph_errors`` (graph files that exist but can't be read; a
    net in one of them is ``unreadable`` when no other file gives one).
    """
    from jarvis_cd.util.site_state import effective_graph_status

    try:
        status = effective_graph_status()
    except ValueError as error:
        return {"form": "unreadable", "net": None, "legacy": None, "error": str(error)[:1024],
                "graph": None, "graph_errors": []}
    result = classify_net_section(status["graph"].get("net"))
    result["graph"] = status["section_files"].get("net")
    result["graph_errors"] = status["errors"]
    if result["form"] == "absent" and status["errors"]:
        result.update(form="unreadable", error="; ".join(
            f"{error['path']}: {error['error']}" for error in status["errors"])[:1024])
    return result


def load_net_section() -> Optional[Dict[str, Any]]:
    """Load the graph's ``net`` section without initializing JARVIS.

    JARVIS's own section when it has one, else the site graph's. Only the v1
    form: a legacy list has no per-node facts, so a launch collects them.
    """
    return net_section_status()["net"]


def classify_mpi_network_failure(
    text: str,
    *,
    launch_network: Optional[Dict[str, Any]] = None,
    ranks_started: Optional[bool] = None,
) -> Optional[Dict[str, Any]]:
    """Turn a launch failure that matches a network signature into an issue.

    MPI setting names in the text (a launch printing its own environment) are
    not signatures. ``ranks_started`` is whether every launch started all its
    ranks (:func:`launch_ranks_started`): then the launch delivered the
    application, the failure is the application's own, and the launcher-stage
    signatures (:data:`MPI_LAUNCH_STAGE_SIGNATURES`) are its teardown; only a
    rank failing to reach its peers (:data:`MPI_RANK_STAGE_SIGNATURES`) still
    points at the network. ``None`` means it is not known.
    """
    from jarvis_cd.core.issues import make_jarvis_issue

    signatures = (
        MPI_RANK_STAGE_SIGNATURES if ranks_started else MPI_NETWORK_FAILURE_SIGNATURES
    )
    lines = [
        (line, _MPI_SETTING_NAME.sub(" ", line).lower())
        for line in (text or "").splitlines()
    ]
    matched = [s for s in signatures if any(s in masked for _line, masked in lines)]
    if not matched:
        return None
    excerpt = ""
    for line, masked in lines:
        if any(signature in masked for signature in matched):
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
            "ranks_started": ranks_started,
        },
        remedy="jarvis rg net set --data <cidr|iface> --control <cidr|iface> --reason <why>",
    )


def launch_ranks_started(provenance_dir: Path) -> Optional[bool]:
    """Whether every launch under ``provenance_dir`` started all its ranks.

    Each ``launch-*/launch.json`` names its ``nprocs``, and the provenance
    rank wrapper writes ``ranks/<host>-<rank>-<pid>.txt`` just before it
    execs the program, so a launch whose rank files cover ``nprocs`` ranks
    had its application started on every node. ``None`` when no launch
    recorded both, so nothing is known.
    """
    import json

    known = False
    for launch in sorted(provenance_dir.glob("launch-*")):
        try:
            document = json.loads((launch / "launch.json").read_text(encoding="utf-8"))
            nprocs = int(document.get("nprocs"))
        except (OSError, ValueError, TypeError):
            continue
        ranks_dir = launch / "ranks"
        if not ranks_dir.is_dir():
            continue
        ranks = set()
        for entry in ranks_dir.glob("*.txt"):
            try:
                fields = entry.read_text(encoding="utf-8").split()
            except OSError:
                continue
            if len(fields) >= 2:
                ranks.add(fields[1])
        if len(ranks) < nprocs:
            return False
        known = True
    return True if known else None
