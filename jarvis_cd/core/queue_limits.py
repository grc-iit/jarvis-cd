"""Per-queue job limits from every scheduler JARVIS can detect.

Each reader runs light, read-only queries and gives every partition (queue)
the same ``limits`` keys (:data:`LIMIT_KEYS`) as Slurm's
``scontrol show partition -o``:

* a key the scheduler reports holds its value. ``None`` means the scheduler
  reports no value (Slurm ``NONE``), and ``"UNLIMITED"`` means unlimited;
* a key the scheduler does not expose (or that could not be read) is ``None``
  and is listed in ``limits_unknown``: it is unknown, not unlimited;
* ``limits_source`` names the provider, the command and, per known key, the
  native field it came from, plus the raw ``error`` when the read failed.

Queue states map to Slurm's words: ``UP`` (accepts and runs jobs), ``DOWN``
(accepts, does not run), ``DRAIN`` (runs, does not accept), ``INACTIVE``.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

#: the ``limits`` keys every partition carries, for every scheduler
LIMIT_KEYS: Tuple[str, ...] = (
    "max_time", "max_time_s", "default_time", "default_time_s",
    "min_nodes", "max_nodes", "max_cpus_per_node",
    "default_mem_per_cpu_mb", "max_mem_per_node_mb",
    "qos", "allow_accounts", "allow_qos", "allow_groups", "state",
    "total_nodes", "total_cpus", "priority_tier", "oversubscribe", "exclusive_user",
)

#: the entry keys Slurm's sinfo gives; other schedulers fill what they know
ENTRY_KEYS: Tuple[str, ...] = (
    "partition", "default", "available", "allocated_nodes", "idle_nodes",
    "other_nodes", "total_nodes", "max_time", "max_time_s", "cpus_per_node",
    "sockets_per_node", "cores_per_socket", "threads_per_core",
    "physical_cores_per_node", "memory_mb_per_node", "gres", "heterogeneous",
)

_AVAILABLE = {"UP": "up", "DOWN": "down", "DRAIN": "drain", "INACTIVE": "inact"}

#: one known value: (value, native field it came from)
Known = Dict[str, Tuple[Any, str]]


def finish_limits(provider: str, command: str, known: Optional[Known],
                  error: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """``limits``, ``limits_unknown`` and ``limits_source`` for one partition."""
    known = known or {}
    source: Dict[str, Any] = {
        "provider": provider, "command": command,
        "fields": {key: known[key][1] for key in LIMIT_KEYS if key in known},
    }
    if error:
        source["error"] = error
    return {
        "limits": {key: known[key][0] if key in known else None for key in LIMIT_KEYS},
        "limits_unknown": [key for key in LIMIT_KEYS if key not in known],
        "limits_source": source,
    }


def _number(text: Any) -> Any:
    text = str(text).strip()
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def _state(enabled: Optional[bool], started: Optional[bool]) -> Optional[str]:
    if enabled is None or started is None:
        return None
    return {(True, True): "UP", (True, False): "DOWN",
            (False, True): "DRAIN", (False, False): "INACTIVE"}[(enabled, started)]


def _query(argv: List[str]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """stdout of one bounded query, or the raw error (command, exit code, output)."""
    from jarvis_cd.core.scheduler import _run_scheduler_query

    completed, diagnostic = _run_scheduler_query(argv)
    if completed is not None and completed.returncode == 0:
        return completed.stdout, None
    return None, {"command": " ".join(argv),
                  "exit_code": completed.returncode if completed is not None else None,
                  "output": diagnostic}


def _entry(provider: str, command: str, name: Optional[str], known: Known,
           default: Optional[bool], extra: Optional[Dict[str, Any]] = None,
           error: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    entry: Dict[str, Any] = {key: None for key in ENTRY_KEYS}
    entry.update(finish_limits(provider, command, known, error))
    limits = entry["limits"]
    entry.update({
        "partition": name,
        "default": default,
        "available": _AVAILABLE.get(str(limits["state"])) if limits["state"] else None,
        "total_nodes": limits["total_nodes"],
        "max_time": limits["max_time"],
        "max_time_s": limits["max_time_s"],
    })
    entry.update(extra or {})
    return entry


# --------------------------------------------------------------------- Slurm

#: ``scontrol show partition -o`` field -> limits key
SLURM_FIELDS = {
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
SLURM_COMMAND = "scontrol show partition -o"


def _slurm_value(text: str) -> Any:
    if text in ("NONE", "N/A", "(null)", ""):
        return None
    return text if text == "UNLIMITED" else _number(text)


def parse_slurm_partitions(text: str) -> Dict[str, Known]:
    """Known limits per partition from ``scontrol show partition -o``."""
    from jarvis_cd.core.scheduler import _parse_slurm_duration

    result: Dict[str, Known] = {}
    for line in text.splitlines():
        fields = dict(token.split("=", 1) for token in line.split() if "=" in token)
        name = fields.get("PartitionName")
        if not name:
            continue
        known: Known = {}
        for native, key in SLURM_FIELDS.items():
            if native not in fields:
                continue
            raw = fields[native]
            if key in ("max_time", "default_time"):
                value = None if raw in ("NONE", "N/A", "") else raw
                known[key] = (value, native)
                known[f"{key}_s"] = (
                    _parse_slurm_duration(raw) if value not in (None, "UNLIMITED") else None,
                    native)
            elif key in ("allow_accounts", "allow_qos", "allow_groups"):
                known[key] = (raw.split(",") if raw not in ("", "(null)") else None, native)
            else:
                known[key] = (_slurm_value(raw), native)
        result[name] = known
    return result


def slurm_limits() -> Tuple[Dict[str, Known], Optional[Dict[str, Any]]]:
    stdout, error = _query(["scontrol", "show", "partition", "-o"])
    return (parse_slurm_partitions(stdout) if stdout is not None else {}), error


def attach_slurm_limits(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Give sinfo's partition entries their ``scontrol`` limits; the error if it failed."""
    per_partition, error = slurm_limits()
    for entry in entries:
        known = per_partition.get(entry["partition"])
        missing = error or (None if known is not None else {
            "command": SLURM_COMMAND, "exit_code": 0,
            "output": f"partition {entry['partition']} is not in the output"})
        entry.update(finish_limits("slurm", SLURM_COMMAND, known, missing))
    return error


# ----------------------------------------------------------------------- PBS

PBS_COMMAND = "qstat -Qf"


def parse_pbs_status(text: str, header: str = "Queue") -> Dict[str, Dict[str, str]]:
    """``qstat -Qf`` / ``qstat -Bf`` text: attributes per object.

    Attributes are ``    name = value``. Long values wrap onto lines that
    start with a tab (or the 8 spaces a copy turns it into), sometimes inside
    a token, so continuations are joined as they are.
    """
    objects: Dict[str, Dict[str, str]] = {}
    current: Optional[Dict[str, str]] = None
    last: Optional[str] = None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if raw.startswith(f"{header}: ") or raw.startswith(f"{header}:\t"):
            current = objects.setdefault(raw.split(":", 1)[1].strip(), {})
            last = None
        elif current is None:
            continue
        elif (raw.startswith("\t") or raw.startswith(" " * 8)) and last is not None:
            current[last] += raw.strip()
        elif " = " in raw:
            key, value = raw.strip().split(" = ", 1)
            current[key.strip()] = value.strip()
            last = key.strip()
    return objects


def _pbs_seconds(text: str) -> Optional[int]:
    """Seconds for a PBS walltime, ``[[hours:]minutes:]seconds[.ms]``."""
    parts = text.strip().split(":")
    if not parts or len(parts) > 3:
        return None
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return None
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return int(seconds)


def _pbs_bool(text: Optional[str]) -> Optional[bool]:
    if text is None:
        return None
    return text.strip().lower() in ("true", "t", "1", "yes", "y")


def pbs_known(queue: Dict[str, str], server: Dict[str, str]) -> Known:
    """Known limits for one PBS queue; unset queue limits inherit the server's."""
    known: Known = {}

    def pick(attribute: str) -> Optional[Tuple[str, str]]:
        if attribute in queue:
            return queue[attribute], attribute
        if attribute in server:
            return server[attribute], f"server {attribute}"
        return None

    for key, attribute in (("max_time", "resources_max.walltime"),
                           ("default_time", "resources_default.walltime")):
        found = pick(attribute)
        if found:
            known[key] = (found[0], found[1])
            known[f"{key}_s"] = (_pbs_seconds(found[0]), found[1])
    for key, attribute in (("min_nodes", "resources_min.nodect"),
                           ("max_nodes", "resources_max.nodect")):
        found = pick(attribute)
        if found:
            known[key] = (_number(found[0]), found[1])
    if "max_nodes" not in known:
        found = pick("resources_max.nodes")
        match = re.fullmatch(r"(\d+)(:.*)?", found[0]) if found else None
        if found and match:
            known["max_nodes"] = (int(match.group(1)), found[1])
    enabled, started = _pbs_bool(queue.get("enabled")), _pbs_bool(queue.get("started"))
    if enabled is not None and started is not None:
        known["state"] = (_state(enabled, started), "enabled,started")
    acl = _pbs_bool(queue.get("acl_group_enable"))
    if acl is True and queue.get("acl_groups"):
        known["allow_groups"] = (queue["acl_groups"].split(","), "acl_groups")
    elif acl is False:
        known["allow_groups"] = (["ALL"], "acl_group_enable")
    for key, attribute in (("total_nodes", "resources_available.nodect"),
                           ("total_cpus", "resources_available.ncpus"),
                           ("priority_tier", "Priority")):
        if attribute in queue:
            known[key] = (_number(queue[attribute]), attribute)
    return known


def pbs_entries(queues_text: str, server_text: Optional[str]) -> List[Dict[str, Any]]:
    servers = parse_pbs_status(server_text or "", "Server")
    server = next(iter(servers.values()), {})
    default_queue = server.get("default_queue")
    entries = []
    for name, queue in parse_pbs_status(queues_text).items():
        routes = queue.get("route_destinations")
        entries.append(_entry(
            "pbs", PBS_COMMAND, name, pbs_known(queue, server),
            (name == default_queue) if default_queue else None,
            {"queue_type": queue.get("queue_type"),
             "route_destinations": routes.split(",") if routes else None,
             "from_route_only": _pbs_bool(queue.get("from_route_only"))}))
    return entries


def pbs_partitions() -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    queues, error = _query(["qstat", "-Qf"])
    if queues is None:
        return [], error
    server, _server_error = _query(["qstat", "-Bf"])
    return pbs_entries(queues, server), None


# ----------------------------------------------------------------------- LSF

LSF_COMMAND = "bqueues -l"
_LSF_STATES = {("Open", True): "UP", ("Open", False): "DOWN",
               ("Closed", True): "DRAIN", ("Closed", False): "INACTIVE"}


def _lsf_block(header: str, values: str) -> Dict[str, str]:
    """One limits block: values sit under their header names, right-aligned."""
    spans = list(re.finditer(r"\S+", header))
    result: Dict[str, str] = {}
    for index, span in enumerate(spans):
        start = spans[index - 1].end() if index else 0
        end = span.end() if index + 1 < len(spans) else len(values)
        result[span.group()] = values[start:end].strip()
    return result


def parse_bqueues_long(text: str) -> Dict[str, Dict[str, Any]]:
    """``bqueues -l``: per queue its statistics row, limits, users and hosts."""
    queues: Dict[str, Dict[str, Any]] = {}
    for block in re.split(r"(?m)^QUEUE:\s*", text)[1:]:
        lines = block.splitlines()
        queue: Dict[str, Any] = {"params": {}, "default": {}, "maximum": {},
                                 "users": None, "hosts": None}
        queues[lines[0].strip()] = queue
        section: Optional[str] = None
        index = 1
        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            if stripped == "PARAMETERS/STATISTICS" and index + 2 < len(lines):
                queue["params"] = dict(zip(lines[index + 1].split(), lines[index + 2].split()))
                index += 2
            elif stripped in ("DEFAULT LIMITS:", "MAXIMUM LIMITS:"):
                section = "default" if stripped.startswith("DEFAULT") else "maximum"
            elif stripped.startswith("USERS:"):
                queue["users"] = stripped.split(":", 1)[1].split()
                section = None
            elif stripped.startswith("HOSTS:"):
                queue["hosts"] = stripped.split(":", 1)[1].split()
                section = None
            elif stripped and not line[:1].isspace():
                section = None
            elif (section and re.fullmatch(r"[A-Z_]+( +[A-Z_]+)*", stripped)
                  and index + 1 < len(lines) and lines[index + 1].strip()):
                queue[section].update(_lsf_block(line, lines[index + 1]))
                index += 1
            index += 1
    return queues


def _lsf_minutes(text: str) -> Optional[int]:
    match = re.match(r"\s*(\d+(?:\.\d+)?)\s*min\b", text)
    return int(float(match.group(1)) * 60) if match else None


def lsf_known(queue: Dict[str, Any]) -> Known:
    known: Known = {}
    for key, section in (("max_time", "maximum"), ("default_time", "default")):
        value = queue[section].get("RUNLIMIT")
        if value:
            known[key] = (value, f"{section.upper()} LIMITS RUNLIMIT")
            known[f"{key}_s"] = (_lsf_minutes(value), f"{section.upper()} LIMITS RUNLIMIT")
    hosts = queue["maximum"].get("HOSTLIMIT_PER_JOB")
    if hosts:
        known["max_nodes"] = (_number(hosts), "MAXIMUM LIMITS HOSTLIMIT_PER_JOB")
    status = queue["params"].get("STATUS")
    if status and ":" in status:
        opened, active = status.split(":", 1)
        state = _LSF_STATES.get((opened, active == "Active"))
        if state:
            known["state"] = (state, "STATUS")
    if queue["params"].get("PRIO"):
        known["priority_tier"] = (_number(queue["params"]["PRIO"]), "PRIO")
    users = queue["users"]
    if users == ["all"]:
        known["allow_groups"] = (["ALL"], "USERS")
    elif users and all(user.endswith("/") for user in users):
        known["allow_groups"] = ([user.rstrip("/") for user in users], "USERS")
    return known


def parse_bparams_default_queues(text: str) -> Optional[List[str]]:
    for line in (text or "").splitlines():
        if line.strip().startswith("Default Queues:"):
            return line.split(":", 1)[1].split()
    return None


def lsf_entries(queues_text: str, params_text: Optional[str]) -> List[Dict[str, Any]]:
    defaults = parse_bparams_default_queues(params_text or "")
    return [
        _entry("lsf", LSF_COMMAND, name, lsf_known(queue),
               (name in defaults) if defaults is not None else None)
        for name, queue in parse_bqueues_long(queues_text).items()
    ]


def lsf_partitions() -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    queues, error = _query(["bqueues", "-l"])
    if queues is None:
        return [], error
    params, _params_error = _query(["bparams"])
    return lsf_entries(queues, params), None


# ---------------------------------------------------------------------- Flux

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_FLUX_YES = {"✔": True, "y": True, "✗": False, "n": False, "⏸": False, "p": False}
_FSD = {"": 1, "ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}


def _flux_seconds(text: str) -> Optional[int]:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)?", text)
    return int(float(match.group(1)) * _FSD[match.group(2) or ""]) if match else None


def _flux_time(text: str, column: str) -> Known:
    if text == "inf":
        return {"value": ("UNLIMITED", column), "seconds": (None, column)}
    return {"value": (text, column), "seconds": (_flux_seconds(text), column)}


def parse_flux_queue_list(text: str) -> List[Dict[str, Optional[str]]]:
    """``flux queue list`` rows keyed by heading (colour codes removed)."""
    lines = [_ANSI.sub("", line).rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    header = lines[0].split()
    rows = []
    for line in lines[1:]:
        values: List[Optional[str]] = list(line.split())
        if "PARENT" in header and len(values) == len(header) - 1:
            values.insert(header.index("PARENT"), None)
        if len(values) == len(header):
            rows.append(dict(zip(header, values)))
    return rows


def flux_known(row: Dict[str, Optional[str]], nodes_field: str = "limits.range.nnodes") -> Known:
    known: Known = {}
    for key, column in (("max_time", "TLIMIT"), ("default_time", "TDEFAULT")):
        if row.get(column):
            parsed = _flux_time(str(row[column]), column)
            known[key], known[f"{key}_s"] = parsed["value"], parsed["seconds"]
    nodes = row.get("NNODES")
    if nodes:
        low, _, high = nodes.partition("-")
        known["min_nodes"] = (_number(low), nodes_field)
        high = high or low
        known["max_nodes"] = ("UNLIMITED" if high == "inf" else _number(high), nodes_field)
    enabled = _FLUX_YES.get(row.get("EN") or "")
    started = _FLUX_YES.get(row.get("ST") or "")
    if enabled is not None and started is not None:
        known["state"] = (_state(enabled, started), "EN,ST")
    return known


def flux_entries(text: str, command: str,
                 nodes_field: str = "limits.range.nnodes") -> List[Dict[str, Any]]:
    entries = []
    for row in parse_flux_queue_list(text):
        name = row.get("QUEUE")
        # without named queues Flux has one anonymous queue and no QUEUE column
        default = True if name is None else name.endswith("*")
        entries.append(_entry("flux", command, name.rstrip("*") if name else None,
                              flux_known(row, nodes_field), default,
                              {"parent": row.get("PARENT")} if "PARENT" in row else None))
    return entries


#: the configured (policy) limits, without the built-in formats' 8-character
#: queue-name cut; ``?:`` drops the column when there are no named queues
FLUX_FORMAT = ("?:{queuem} {enabled.ascii} {started.ascii} {defaults.timelimit!F} "
               "{limits.timelimit!F} {limits.range.nnodes}")

#: (argv, native field of NNODES); older flux-core releases fall through to
#: the named ``policy`` format, then to the default (effective-range) table
FLUX_QUERIES: Tuple[Tuple[List[str], str], ...] = (
    (["flux", "queue", "list", "-o", FLUX_FORMAT], "limits.range.nnodes"),
    (["flux", "queue", "list", "-o", "policy"], "limits.range.nnodes"),
    (["flux", "queue", "list"], "limits.range.effective.nnodes"),
)


def flux_partitions() -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    error: Optional[Dict[str, Any]] = None
    for argv, nodes_field in FLUX_QUERIES:
        stdout, error = _query(argv)
        if stdout is not None and "NNODES" in stdout.split("\n", 1)[0]:
            return flux_entries(stdout, " ".join(argv), nodes_field), None
        if stdout is not None:
            error = {"command": " ".join(argv), "exit_code": 0, "output": stdout[:1024]}
    return [], error


#: provider -> reader for schedulers JARVIS reads queues from but cannot submit to
QUEUE_READERS: Dict[str, Callable[[], Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]]] = {
    "pbs": pbs_partitions,
    "lsf": lsf_partitions,
    "flux": flux_partitions,
}
