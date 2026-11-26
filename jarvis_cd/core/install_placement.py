"""Where a scheduled pipeline's Spack check and build run: install placement.

On the submitting host of a scheduled pipeline (usually a login node) JARVIS
answers a check from the recorded ``install-check.json``. When no record
matches, the full check (``spack spec -I -l``) or a build has to run
somewhere. That is a per-site, and possibly per-package, choice the person
makes, never a hard-coded cluster rule:

* ``login_limited``: run it here now, niced (19, idle I/O class), with a few
  build jobs and a memory bound sized from this host's cores, load, free
  memory and the person's own processes. It is deferred to a compute job when
  the host is busy;
* ``compute_job``: run it in a short 1-node compute job;
* ``ask_each_time``: record no standing choice; ask on each occasion.

The choice is keyed by phase (``check`` or ``build``) and by the estimated
cost class (``short`` or ``long``) of the package: a 20-minute build and a
4-hour build are separate decisions at a site. A package override (keyed by
the Spack package name) beats the site's answer. Answers are recorded in the
decisions store (``JARVIS_SPACK_DECISIONS``), and estimates come from
``install-costs.json`` next to it, which every full check and build updates.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from jarvis_cd.util import stall as _stall

PLACEMENT_KIND = "install_placement"
LOGIN_LIMITED = "login_limited"
COMPUTE_JOB = "compute_job"
ASK_EACH_TIME = "ask_each_time"
CHOICES = (LOGIN_LIMITED, COMPUTE_JOB, ASK_EACH_TIME)
#: the choices that state where work may run here (site facts)
STANDING = (LOGIN_LIMITED, COMPUTE_JOB)
PHASES = ("check", "build")
SHORT, LONG = "short", "long"

COSTS_NAME = "install-costs.json"
COSTS_SCHEMA = "jarvis.install-costs.v1"
PLACEMENT_SCHEMA = "jarvis.install-placement.v1"
#: the resource graph section holding person-confirmed site answers, per phase
GRAPH_SECTION = "install_placement"
GRAPH_SCHEMA = "jarvis.resource-graph.install-placement.v1"
GRAPH_ENTRY_KEYS = ("constraint", "basis", "answered_by", "answered_at", "issue_id",
                    "recorded_in")

#: JSON merged over the measured login-host sample (proofs and tests only)
SAMPLE_ENV = "JARVIS_LOGIN_HOST_SAMPLE"
#: caller default for a phase when neither a package nor a site answer exists
DEFAULT_ENV = {"check": "JARVIS_INSTALL_PLACEMENT_CHECK",
               "build": "JARVIS_INSTALL_PLACEMENT_BUILD"}

#: packages known to have large dependency graphs or long builds anywhere
KNOWN_HEAVY = frozenset({
    "paraview", "vtk", "vtk-m", "qt", "qt-base", "qt-declarative", "llvm", "gcc",
    "trilinos", "dealii", "openfoam", "openfoam-org", "py-torch", "py-tensorflow",
    "py-jax", "jaxlib", "root", "rust", "mesa", "boost", "petsc", "visit",
    "ecp-data-vis-sdk", "kokkos-kernels", "hipblas", "rocblas", "cuda", "nvhpc",
})

#: heuristic seconds per missing node (ordinary, known-heavy), used only for
#: nodes this site has not built before. Calibrated on a measured ParaView
#: 5.13.1 build (62 nodes on one compute node, Spack ``install_times.json``):
#: 59 ordinary nodes averaged 34 s (max 249 s); llvm 3276 s, paraview
#: 2698 s, mesa 116 s.
NODE_BUILD_SECONDS = 60
HEAVY_NODE_BUILD_SECONDS = 1800
#: known-heavy nodes whose build is far from the known-heavy default
HEAVY_BUILD_SECONDS = {"llvm": 3600, "gcc": 3600, "paraview": 3000, "mesa": 600}
#: a known-heavy package with neither a check nor a prior build here
HEAVY_PACKAGE_BUILD_SECONDS = 4 * 3600
DEFAULT_CHECK_SECONDS = 60
HEAVY_CHECK_SECONDS = 600


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def short_threshold(phase: str) -> float:
    """Estimated seconds up to which a check or build counts as ``short``."""
    if phase == "check":
        return _env_float("JARVIS_PLACEMENT_SHORT_CHECK_SECONDS", 300)
    return _env_float("JARVIS_PLACEMENT_SHORT_BUILD_SECONDS", 1800)


def spec_name(pkg_def: Mapping[str, Any]) -> str:
    """The Spack package a pipeline package installs (its query's root name)."""
    config = pkg_def.get("config", {}) or {}
    query = str(config.get("install_query") or config.get("install") or "").strip()
    match = re.match(r"\s*([A-Za-z0-9_][A-Za-z0-9_-]*)", query)
    return match.group(1).lower() if match else str(pkg_def.get("pkg_id") or "pkg")


def _human(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 90:
        return f"~{int(round(seconds))} s"
    if seconds < 5400:
        return f"~{int(round(seconds / 60))} min"
    return f"~{seconds / 3600:.1f} h"


# ---------------------------------------------------------------------------
# Login host measurement
# ---------------------------------------------------------------------------


def _meminfo() -> Dict[str, Optional[int]]:
    found: Dict[str, Optional[int]] = {"total_mb": None, "available_mb": None}
    try:
        with open("/proc/meminfo", "r", encoding="ascii", errors="replace") as stream:
            for line in stream:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    value = int(rest.split()[0]) // 1024
                    found["total_mb" if key == "MemTotal" else "available_mb"] = value
    except (OSError, ValueError, IndexError):
        pass
    return found


def _own_processes(limit: int = 32768) -> Optional[Dict[str, Any]]:
    """This user's processes here: count, running (state R) and summed RSS."""
    if not os.path.isdir("/proc") or not hasattr(os, "getuid"):
        return None
    uid = str(os.getuid())
    count = running = rss_kb = 0
    scanned = 0
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        scanned += 1
        if scanned > limit:
            break
        try:
            with open(f"/proc/{entry}/status", "r", encoding="ascii",
                      errors="replace") as stream:
                fields = dict(line.split(":", 1) for line in stream if ":" in line)
        except OSError:
            continue
        if (fields.get("Uid") or "").split()[:1] != [uid]:
            continue
        count += 1
        if (fields.get("State") or "").strip().startswith("R"):
            running += 1
        try:
            rss_kb += int((fields.get("VmRSS") or "0 kB").split()[0])
        except (ValueError, IndexError):
            pass
    return {"count": count, "running": running, "rss_mb": rss_kb // 1024}


def _cgroup_limits() -> Optional[Dict[str, Any]]:
    """The tightest cgroup v2 CPU and memory limits over this process's cgroup."""
    try:
        with open("/proc/self/cgroup", "r", encoding="ascii", errors="replace") as stream:
            lines = stream.read().splitlines()
    except OSError:
        return None
    relative = next((line.split("::", 1)[1] for line in lines if line.startswith("0::")),
                    None)
    if relative is None:
        return None
    root = Path("/sys/fs/cgroup")
    current = root / relative.lstrip("/")
    cpu_cores: Optional[float] = None
    memory_max: Optional[int] = None
    memory_used: Optional[int] = None
    where: Dict[str, str] = {}
    while True:
        try:
            quota, period = (current / "cpu.max").read_text().split()[:2]
            if quota != "max":
                cores = int(quota) / int(period)
                if cpu_cores is None or cores < cpu_cores:
                    cpu_cores, where["cpu"] = cores, str(current)
        except (OSError, ValueError):
            pass
        try:
            value = (current / "memory.max").read_text().strip()
            if value != "max":
                limit = int(value)
                if memory_max is None or limit < memory_max:
                    memory_max, where["memory"] = limit, str(current)
                    used = (current / "memory.current").read_text().strip()
                    memory_used = int(used)
        except (OSError, ValueError):
            pass
        if current == root or root not in current.parents:
            break
        current = current.parent
    if cpu_cores is None and memory_max is None:
        return {"cpu_cores": None, "memory_max_mb": None, "path": relative}
    return {
        "cpu_cores": round(cpu_cores, 2) if cpu_cores is not None else None,
        "memory_max_mb": memory_max // 2**20 if memory_max is not None else None,
        "memory_used_mb": memory_used // 2**20 if memory_used is not None else None,
        "path": relative,
        "limited_at": where,
    }


def measure_login_host() -> Dict[str, Any]:
    """Cores, load, free memory, the person's processes and cgroup limits here."""
    try:
        cores = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        cores = os.cpu_count() or 1
    try:
        load: Optional[List[float]] = [round(value, 2) for value in os.getloadavg()]
    except (AttributeError, OSError):
        load = None
    sample: Dict[str, Any] = {
        "host": socket.gethostname(),
        "measured_at": _utc_now(),
        "cores": cores,
        "load": load,
        **_meminfo(),
        "own_processes": _own_processes(),
        "cgroup": _cgroup_limits(),
    }
    override = os.environ.get(SAMPLE_ENV, "").strip()
    if override:
        try:
            values = json.loads(override)
        except ValueError:
            values = {}
        if isinstance(values, dict):
            sample.update(values)
            sample["overridden"] = sorted(values)
    return sample


def limits_for(sample: Mapping[str, Any]) -> Dict[str, Any]:
    """The concurrency, priority and memory bound for work on this host now.

    ``jobs`` is a share of the cores free right now (cores, or the cgroup's
    CPU limit, minus the 1-minute load), at least 1 and at most a modest
    ceiling; the memory bound is a share of the free memory. ``defer`` is set
    (with reasons) when the host is too busy: then the work goes to a job.
    """
    share = _env_float("JARVIS_LOGIN_CHECK_SHARE", 0.25)
    ceiling = max(1, int(_env_float("JARVIS_LOGIN_CHECK_MAX_JOBS", 4)))
    min_free = _env_float("JARVIS_LOGIN_CHECK_MIN_FREE_CORES", 1.0)
    min_mem = _env_float("JARVIS_LOGIN_CHECK_MIN_FREE_MB", 4096)
    mem_share = _env_float("JARVIS_LOGIN_CHECK_MEM_SHARE", 0.25)
    mem_ceiling = _env_float("JARVIS_LOGIN_CHECK_MAX_MEM_MB", 8192)
    max_running = int(_env_float("JARVIS_LOGIN_CHECK_MAX_OWN_RUNNING", 8))
    cores = float(sample.get("cores") or 1)
    cgroup = sample.get("cgroup") or {}
    if cgroup.get("cpu_cores"):
        cores = min(cores, float(cgroup["cpu_cores"]))
    load = sample.get("load") or []
    load1 = float(load[0]) if load else 0.0
    free = max(0.0, cores - load1)
    available = sample.get("available_mb")
    if cgroup.get("memory_max_mb") is not None:
        room = int(cgroup["memory_max_mb"]) - int(cgroup.get("memory_used_mb") or 0)
        available = room if available is None else min(int(available), room)
    own = sample.get("own_processes") or {}
    reasons: List[str] = []
    if free < min_free:
        reasons.append(f"{free:.1f} free cores (load {load1:g} on {cores:g}) is below "
                       f"{min_free:g}")
    if available is not None and available < min_mem:
        reasons.append(f"{available} MB free memory is below {min_mem:g} MB")
    if own.get("running") is not None and int(own["running"]) >= max_running:
        reasons.append(f"{own['running']} of your processes are running here "
                       f"(limit {max_running})")
    jobs = max(1, min(ceiling, int(free * share)))
    if available is None:
        memory_mb = int(mem_ceiling)
    else:
        memory_mb = int(max(min(mem_ceiling, available * mem_share),
                            min(min_mem, available)))
    return {
        "defer": bool(reasons),
        "defer_reasons": reasons,
        "jobs": jobs,
        "nice": 19,
        "ionice": "idle (class 3)",
        "memory_mb": memory_mb,
        "effective_cores": round(cores, 2),
        "free_cores": round(free, 2),
        "available_mb": available,
        "rule": (f"jobs = clamp(floor(free_cores x {share:g}), 1, {ceiling}); "
                 f"memory = clamp(free_mb x {mem_share:g}, {min_mem:g}, "
                 f"{mem_ceiling:g}) MB (address-space limit on each Spack process); "
                 f"defer when free cores < {min_free:g}, free memory < {min_mem:g} MB "
                 f"or {max_running}+ of your processes are running"),
        "time_limit": None,
        "stop": "no hard time limit; stop it with Ctrl-C or by killing the jarvis "
                "process (the record is only written when the check finishes)",
    }


# ---------------------------------------------------------------------------
# Applying the limits to Spack subprocesses
# ---------------------------------------------------------------------------

_ACTIVE: List[Dict[str, Any]] = []


def active_limits() -> Optional[Dict[str, Any]]:
    """The limits the current login-host work runs under, if any."""
    return _ACTIVE[-1] if _ACTIVE else None


def spack_arguments(arguments: Sequence[str]) -> List[str]:
    """Prefix ``-c config:build_jobs:N`` while limits are active."""
    limits = active_limits()
    if limits is None:
        return list(arguments)
    return ["-c", f"config:build_jobs:{int(limits['jobs'])}", *arguments]


def wrap_script(script: str) -> str:
    """Put the shell (and so Spack) in the idle I/O class while limits are active."""
    if active_limits() is None or os.name != "posix":
        return script
    return "{ command -v ionice >/dev/null 2>&1 && ionice -c 3 -p $$; } " \
           ">/dev/null 2>&1; " + script


def subprocess_environment(environment: Dict[str, str]) -> Dict[str, str]:
    limits = active_limits()
    if limits is None:
        return environment
    jobs = str(int(limits["jobs"]))
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        environment[name] = jobs
    return environment


def preexec():
    """``preexec_fn`` that lowers priority and bounds memory, or ``None``."""
    limits = active_limits()
    if limits is None or os.name != "posix":
        return None
    nice = int(limits.get("nice") or 19)
    memory = int(limits.get("memory_mb") or 0) * 2**20

    def apply() -> None:
        try:
            os.nice(nice)
        except OSError:
            pass
        if memory > 0:
            try:
                import resource

                soft, hard = resource.getrlimit(resource.RLIMIT_AS)
                bound = memory if hard == resource.RLIM_INFINITY else min(memory, hard)
                resource.setrlimit(resource.RLIMIT_AS, (bound, hard))
            except (ImportError, OSError, ValueError):
                pass

    return apply


def _children_usage() -> Tuple[float, int]:
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        return usage.ru_utime + usage.ru_stime, int(usage.ru_maxrss)
    except (ImportError, OSError):
        return 0.0, 0


@contextlib.contextmanager
def applied_limits(limits: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """Run Spack under ``limits``; fills ``usage`` (CPU, peak RSS, wall)."""
    usage: Dict[str, Any] = {}
    cpu_before, _rss = _children_usage()
    started = time.monotonic()
    _ACTIVE.append(dict(limits))
    try:
        yield usage
    finally:
        _ACTIVE.pop()
        cpu_after, rss_kb = _children_usage()
        wall = time.monotonic() - started
        usage.update(
            wall_seconds=round(wall, 3),
            cpu_seconds=round(cpu_after - cpu_before, 3),
            cpu_share_of_one_core=round((cpu_after - cpu_before) / wall, 3) if wall else None,
            peak_child_rss_mb=rss_kb // 1024 if rss_kb else None,
        )


# ---------------------------------------------------------------------------
# Cost estimates
# ---------------------------------------------------------------------------


def costs_path(decisions_path: Path) -> Path:
    """``install-costs.json`` next to the (per-site) decisions store."""
    return Path(decisions_path).with_name(COSTS_NAME)


def read_costs(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    packages = document.get("packages") if isinstance(document, dict) else None
    return {str(k): dict(v) for k, v in (packages or {}).items() if isinstance(v, dict)}


def record_costs(path: Path, updates: Mapping[str, Mapping[str, Any]]) -> None:
    """Merge measured seconds per Spack package into ``install-costs.json``."""
    if not updates:
        return
    from jarvis_cd.core.spack_install import _atomic_write_text, _file_lock

    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(path.with_name(f".{path.name}.lock"), exclusive=True):
            packages = read_costs(path)
            for name, fields in updates.items():
                entry = packages.setdefault(str(name), {})
                entry.update({k: v for k, v in fields.items() if v is not None})
            _atomic_write_text(path, json.dumps(
                {"schema_version": COSTS_SCHEMA, "packages": packages},
                indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"install placement: costs not recorded ({exc})")


def node_build_seconds(node: str) -> int:
    """Heuristic build seconds of one Spack node never built at this site."""
    if node in HEAVY_BUILD_SECONDS:
        return HEAVY_BUILD_SECONDS[node]
    return HEAVY_NODE_BUILD_SECONDS if node in KNOWN_HEAVY else NODE_BUILD_SECONDS


def measured_nodes(costs: Mapping[str, Mapping[str, Any]],
                   nodes: Sequence[str]) -> Dict[str, float]:
    """Spack nodes with a measured ``node_build_seconds`` at this site."""
    measured: Dict[str, float] = {}
    for node in nodes:
        seconds = (costs.get(node) or {}).get("node_build_seconds")
        if seconds is not None:
            measured[node] = float(seconds)
    return measured


def _heuristic_basis(nodes: Sequence[str]) -> str:
    heavy = sorted({node for node in nodes if node in KNOWN_HEAVY})
    light = sum(1 for node in nodes if node not in KNOWN_HEAVY)
    return (f"{len(nodes)} missing Spack node(s): {light} x {NODE_BUILD_SECONDS} s"
            + (", known-heavy " + ", ".join(f"{node} {node_build_seconds(node)} s"
                                            for node in heavy) if heavy else ""))


def estimate(phase: str, name: str, costs: Mapping[str, Mapping[str, Any]],
             missing: Optional[Sequence[str]] = None,
             use_prior: bool = True) -> Dict[str, Any]:
    """Estimated seconds and cost class of one package's check or build.

    A build with known ``missing`` nodes sums each node's measured seconds
    at this site where Spack recorded one, else the per-node heuristic. With
    no node measured, a prior whole build of ``name`` is used (unless
    ``use_prior`` is false), then the heuristic.
    """
    prior = costs.get(name) or {}
    seconds: Optional[float]
    measured = measured_nodes(costs, missing) if missing is not None else {}
    if phase == "check":
        if prior.get("check_seconds") is not None:
            seconds = float(prior["check_seconds"])
            basis = (f"prior check of {name} at this site ({prior.get('check_host')}, "
                     f"{prior.get('checked_at')})")
        elif name in KNOWN_HEAVY:
            seconds, basis = HEAVY_CHECK_SECONDS, \
                f"{name} is a known-heavy package (large dependency graph)"
        else:
            seconds, basis = DEFAULT_CHECK_SECONDS, \
                "default for a package never checked at this site"
    else:
        if measured and missing is not None:
            rest = [node for node in missing if node not in measured]
            measured_total = sum(measured[node] for node in missing if node in measured)
            seconds = measured_total + sum(node_build_seconds(node) for node in rest)
            basis = (f"{len(missing) - len(rest)} of {len(missing)} missing Spack "
                     f"node(s) built before at this site ({_human(measured_total)}, "
                     "Spack install_times)"
                     + (f"; the other {_heuristic_basis(rest)}" if rest else ""))
        elif use_prior and prior.get("build_seconds") is not None:
            seconds = float(prior["build_seconds"])
            basis = (f"prior build of {name} at this site ({prior.get('build_host')}, "
                     f"{prior.get('built_at')}, {prior.get('built_nodes')} nodes)")
        elif missing is not None:
            seconds = float(sum(node_build_seconds(node) for node in missing))
            basis = _heuristic_basis(missing)
        elif name in KNOWN_HEAVY:
            seconds, basis = HEAVY_PACKAGE_BUILD_SECONDS, \
                f"{name} is a known-heavy package"
        else:
            seconds, basis = None, "unknown: no prior build and no check of it here"
    threshold = short_threshold(phase)
    return {
        "seconds": None if seconds is None else round(seconds, 1),
        "human": _human(seconds),
        "class": SHORT if seconds is not None and seconds <= threshold else LONG,
        "short_up_to_seconds": threshold,
        "basis": basis,
    }


# ---------------------------------------------------------------------------
# Queue evidence
# ---------------------------------------------------------------------------


def queue_evidence(scheduler: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """When a short 1-node job would start (``sbatch --test-only``; light)."""
    spec = dict(scheduler or {})
    provider = str(spec.get("name") or "").strip().lower() or None
    if provider != "slurm":
        return {"provider": provider, "measured": False,
                "why": "start estimate is only measured for Slurm (sbatch --test-only)"}
    if shutil.which("sbatch") is None:
        return {"provider": provider, "measured": False, "why": "sbatch not on PATH"}
    argv = ["sbatch", "--test-only", "--nodes=1", "--time=30", "--job-name=jarvis-probe"]
    for key in ("partition", "account", "qos", "reservation", "constraint"):
        if spec.get(key):
            argv.append(f"--{key}={spec[key]}")
    argv += ["--wrap", "true"]
    try:
        completed = _stall.run(argv, what="sbatch --test-only", attempts=1)
    except OSError as exc:
        return {"provider": provider, "measured": False, "command": argv,
                "why": str(exc)[:256]}
    text = (completed.stderr or "") + (completed.stdout or "")
    match = re.search(r"to start at (\S+)", text)
    evidence: Dict[str, Any] = {"provider": provider, "command": argv,
                                "measured": match is not None,
                                "output": text.strip()[:512]}
    if match:
        evidence["start_at"] = match.group(1)
        try:
            start = datetime.fromisoformat(match.group(1))
            evidence["wait_seconds"] = max(0, int((start - datetime.now()).total_seconds()))
            evidence["wait"] = _human(evidence["wait_seconds"])
        except ValueError:
            pass
    return evidence


# ---------------------------------------------------------------------------
# Questions and resolution
# ---------------------------------------------------------------------------


def _does(phase: str, choice: str) -> str:
    if choice == LOGIN_LIMITED:
        return (f"run the {phase} on this submitting host now, niced and bounded "
                "(see limits), so questions come back without a queue wait")
    if choice == COMPUTE_JOB:
        return ("check in a short 1-node job: the run job checks at load, or "
                "`jarvis ppl install yaml FILE +check +submit`" if phase == "check"
                else "build in a 1-node job: `jarvis ppl install yaml FILE +submit`")
    return f"keep no standing choice: ask before each {phase} of this kind"


def _candidates(phase: str, recommended: Optional[str], *, once: bool = False
                ) -> List[Dict[str, Any]]:
    choices = (LOGIN_LIMITED, COMPUTE_JOB) if once else CHOICES
    return [{"id": choice, "constraint": choice, "does": _does(phase, choice),
             "recommended": choice == recommended} for choice in choices]


def site_subject(phase: str, *, once: bool = False) -> str:
    """The site subject for ``phase``: one decision covers every package."""
    return f"install_placement.{phase}" + (".occasion" if once else "")


def legacy_site_subject(phase: str, cost_class: str, *, once: bool = False) -> str:
    """The per-cost-class site subject jarvis-cd 58cd5b1a and earlier recorded."""
    return f"install_placement.{phase}.{cost_class}" + (".occasion" if once else "")


def is_site_subject(subject: str, phase: str) -> bool:
    """Whether ``subject`` is a site placement subject for ``phase``, legacy included."""
    base = site_subject(phase)
    return subject == base or subject.startswith(base + ".")


def _home_record(phase: str, cost_class: str,
                 site_entries: Mapping[str, Mapping[str, Any]]
                 ) -> Optional[Tuple[str, Mapping[str, Any]]]:
    """This home's standing site answer for ``phase`` (legacy per-class included)."""
    keys = [site_subject(phase), legacy_site_subject(phase, cost_class)]
    keys += [subject for subject in sorted(site_entries)
             if is_site_subject(subject, phase) and subject not in keys
             and not subject.endswith(".occasion")]
    for subject in keys:
        entry = site_entries.get(subject)
        if isinstance(entry, Mapping) and entry.get("constraint") in STANDING:
            return subject, entry
    return None


def _recorded(phase: str, cost_class: str,
              site_entries: Mapping[str, Mapping[str, Any]],
              graph_answers: Mapping[str, Mapping[str, Any]]
              ) -> Optional[Dict[str, Any]]:
    """The site's recorded constraint for ``phase``: this home's, then the graph's."""
    home = _home_record(phase, cost_class, site_entries)
    if home is not None:
        subject, entry = home
        return {"source": "site_decision", "subject": subject, "phase": phase,
                **_provenance(entry)}
    entry = graph_answers.get(phase)
    if isinstance(entry, Mapping) and entry.get("constraint") in STANDING:
        return {"source": "resource_graph", "subject": site_subject(phase),
                "phase": phase, "graph": entry.get("graph"), **_provenance(entry)}
    return None


def _provenance(entry: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: entry.get(key) for key in GRAPH_ENTRY_KEYS if entry.get(key) is not None}


def _describe(record: Mapping[str, Any]) -> str:
    who = ", ".join(str(part) for part in (
        f"answered by {record['answered_by']}" if record.get("answered_by") else None,
        record.get("answered_at"),
    ) if part)
    where = (f"the site resource graph {record.get('graph')}"
             if record.get("source") == "resource_graph" else "this home's decisions")
    text = f"{record['constraint']} for Spack {record['phase']}s"
    if who:
        text += f" ({who})"
    if record.get("basis"):
        text += f"; basis: {record['basis']}"
    return f"{text}; recorded in {where}"


def recommend(phase: str, cost_class: str, limits: Mapping[str, Any],
              site_entries: Mapping[str, Mapping[str, Any]],
              est: Mapping[str, Any],
              graph_answers: Optional[Mapping[str, Mapping[str, Any]]] = None,
              ) -> Tuple[str, str, Dict[str, Any]]:
    """The recommended choice, why, and its basis.

    A constraint the site recorded for ``phase`` (this home's decisions,
    then the active resource graph's ``install_placement`` section) is
    recommended with its provenance. Without one, a recorded answer for the
    other phase still informs it where the order of cost does: a check
    recorded ``compute_job`` means the heavier build goes to a job too, and
    a build recorded ``login_limited`` means the lighter check may run here.
    Otherwise cost and load decide. This is only a recommendation: the
    question still offers every choice.
    """
    graph_answers = graph_answers or {}
    busy = ("the login host is busy now: " + "; ".join(limits.get("defer_reasons") or [])
            if limits.get("defer") else None)
    record = _recorded(phase, cost_class, site_entries, graph_answers)
    if record is not None:
        reason = f"this site recorded {_describe(record)}"
        if busy and record["constraint"] == LOGIN_LIMITED:
            reason += f"; {busy}, so a login_limited run defers to a compute job this time"
        return str(record["constraint"]), reason, record
    other = "build" if phase == "check" else "check"
    implied = COMPUTE_JOB if phase == "build" else LOGIN_LIMITED
    record = _recorded(other, cost_class, site_entries, graph_answers)
    if record is not None and record["constraint"] == implied:
        why = ("a build is at least as heavy as a check" if phase == "build"
               else "a check is lighter than a build")
        reason = (f"this site recorded {_describe(record)}, and {why}; no answer is "
                  f"recorded for {phase}s yet")
        if busy and implied == LOGIN_LIMITED:
            reason += f"; {busy}, so a login_limited run defers to a compute job this time"
        return implied, reason, {**record, "source": f"{record['source']} ({other})",
                                 "inferred_for": phase}
    heuristic = {"source": "heuristic", "phase": phase, "cost_class": cost_class}
    if busy:
        return COMPUTE_JOB, busy, heuristic
    if phase == "check":
        if cost_class == SHORT:
            return LOGIN_LIMITED, (
                f"estimated {est.get('human')} concretization and {limits.get('free_cores')} "
                "free cores here: checking now saves a queue wait"), heuristic
        return COMPUTE_JOB, (
            f"estimated {est.get('human')} concretization: a short compute job keeps "
            "the login host light"), heuristic
    if cost_class == SHORT:
        return COMPUTE_JOB, (
            f"estimated {est.get('human')} build: builds go to a compute job until the "
            "person confirms (from the site's guidance or its admins) that login "
            "builds are fine here; login_limited stays an option"), heuristic
    return COMPUTE_JOB, (
        f"estimated {est.get('human')} build would hold a shared login host for that "
        "long: a compute job"), heuristic


# -- confirmed answers in the resource graph ----------------------------------


def _graph_file(path: Optional[Path] = None) -> Path:
    if path is not None:
        return Path(path)
    from jarvis_cd.util.network import active_graph_file

    return active_graph_file()


def _file_answers(graph_path: Path) -> Dict[str, Dict[str, Any]]:
    import yaml

    try:
        graph = yaml.safe_load(graph_path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    section = graph.get(GRAPH_SECTION) if isinstance(graph, dict) else None
    if not isinstance(section, Mapping):
        return {}
    found: Dict[str, Dict[str, Any]] = {}
    for phase in PHASES:
        entry = section.get(phase)
        if isinstance(entry, Mapping) and entry.get("constraint") in STANDING:
            found[phase] = {**_provenance(entry), "graph": str(graph_path)}
    return found


def graph_answers(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """The person-confirmed placement answers in the resource graph, per phase.

    Without ``path``: JARVIS's own graph and, when ``JARVIS_RESOURCE_GRAPH``
    names one, the graph the site publishes; per phase the newer answer
    wins. Each answer carries ``graph`` (the file it came from) and, from
    the site's graph, ``site_graph: true``. Unreadable graphs give none.
    """
    if path is not None:
        return _file_answers(Path(path))
    from jarvis_cd.util.site_state import site_graph_file

    found = _file_answers(_graph_file())
    site_file = site_graph_file()
    if site_file is None:
        return found
    for phase, entry in _file_answers(site_file).items():
        current = found.get(phase)
        if (current is None or str(entry.get("answered_at") or "")
                > str(current.get("answered_at") or "")):
            found[phase] = {**entry, "site_graph": True}
    return found


@contextlib.contextmanager
def _graph_locked(graph_path: Path) -> Iterator[Dict[str, Any]]:
    """Hold the graph lock and yield the graph mapping; written back on exit."""
    import tempfile

    import yaml

    with open(graph_path.parent / ".resource_graph.lock", "a+") as lock:
        try:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]
        except ImportError:  # pragma: no cover - non-POSIX
            pass
        try:
            graph = yaml.safe_load(graph_path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            graph = {}
        if not isinstance(graph, dict):
            raise ValueError(f"{graph_path} is not a mapping")
        before = json.dumps(graph, sort_keys=True, default=str)
        yield graph
        if json.dumps(graph, sort_keys=True, default=str) == before:
            return
        descriptor, temporary = tempfile.mkstemp(
            dir=graph_path.parent, prefix=".resource_graph.", suffix=".tmp", text=True)
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                yaml.dump(graph, stream, default_flow_style=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, graph_path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def persist_graph_answer(phase: str, entry: Mapping[str, Any], *,
                         recorded_in: Optional[Path] = None,
                         path: Optional[Path] = None) -> Dict[str, Any]:
    """Write a person-confirmed site answer into JARVIS's own graph.

    Only standing constraints (``login_limited``, ``compute_job``) are site
    facts; ``ask_each_time`` is not written. An answer already in the graph
    (JARVIS's own or the site's) that is newer is kept. The site graph is
    never written. Without a shared site home (a site graph or
    ``JARVIS_SITE_HOME``) the root's directory must exist (it is never
    created here); a shared site home is created. Never raises: the
    decision is recorded either way.
    """
    from jarvis_cd.util.site_state import shared_site_home

    result: Dict[str, Any] = {"phase": phase, "persisted": False}
    try:
        graph_path = _graph_file(path)
    except ValueError as error:
        result["error"] = str(error)[:500]
        return result
    result["graph"] = str(graph_path)
    if phase not in PHASES or entry.get("constraint") not in STANDING:
        result["skipped"] = "not a standing constraint"
        return result
    if path is None and shared_site_home():
        try:
            graph_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            result["error"] = str(error)[:500]
            return result
    if not graph_path.parent.is_dir():
        result["skipped"] = "the graph's directory does not exist"
        return result
    record = _provenance(entry)
    if recorded_in is not None:
        record["recorded_in"] = str(recorded_in)
    published = (graph_answers() if path is None else {}).get(phase)
    if (published and published.get("site_graph")
            and str(published.get("answered_at") or "") > str(record.get("answered_at") or "")):
        result["skipped"] = "the site graph holds a newer answer"
        return result
    try:
        with _graph_locked(graph_path) as graph:
            section = graph.get(GRAPH_SECTION)
            section = dict(section) if isinstance(section, Mapping) else {}
            current = section.get(phase)
            if (isinstance(current, Mapping)
                    and str(current.get("answered_at") or "")
                    > str(record.get("answered_at") or "")):
                result["skipped"] = "the graph holds a newer answer"
                return result
            section["schema_version"] = GRAPH_SCHEMA
            section[phase] = record
            graph[GRAPH_SECTION] = section
        result["persisted"] = True
    except Exception as error:
        result["error"] = str(error)[:500]
    return result


def forget_graph_answers(forgotten: Sequence[Mapping[str, Any]], *,
                         path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Drop graph answers that came from forgotten site decisions.

    A graph answer is dropped only when it is the same record (``issue_id``
    and ``answered_at``) as a forgotten per-phase site decision, so another
    person's answer in a published graph stays. Only JARVIS's own graph is
    changed; see :func:`site_graph_answers_kept`. Never raises.
    """
    try:
        graph_path = _graph_file(path)
    except ValueError:
        return []
    targets = _forget_targets(forgotten)
    dropped: List[Dict[str, Any]] = []
    if not targets or not graph_path.is_file():
        return dropped
    try:
        with _graph_locked(graph_path) as graph:
            section = graph.get(GRAPH_SECTION)
            if not isinstance(section, Mapping):
                return dropped
            section = dict(section)
            for phase, identity in targets.items():
                current = section.get(phase)
                if (isinstance(current, Mapping)
                        and (current.get("issue_id"), current.get("answered_at")) == identity):
                    section.pop(phase)
                    dropped.append({"graph": str(graph_path), "phase": phase,
                                    **_provenance(current)})
            if any(phase in section for phase in PHASES):
                graph[GRAPH_SECTION] = section
            else:
                graph.pop(GRAPH_SECTION, None)
    except Exception:
        return []
    return dropped


def _forget_targets(forgotten: Sequence[Mapping[str, Any]]) -> Dict[str, Tuple[Any, Any]]:
    targets: Dict[str, Tuple[Any, Any]] = {}
    for entry in forgotten:
        for phase in PHASES:
            if entry.get("scope") == "site" and entry.get("subject") == site_subject(phase):
                targets[phase] = (entry.get("issue_id"), entry.get("answered_at"))
    return targets


def site_graph_answers_kept(forgotten: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Answers for the forgotten phases that the site's graph still holds.

    The site graph is read-only to JARVIS, so these stay in effect; the
    report names them so the person knows the site's record still applies.
    """
    targets = _forget_targets(forgotten)
    if not targets:
        return []
    return [{"phase": phase, **entry,
             "note": "the site graph is read-only to JARVIS; its answer still applies"}
            for phase, entry in graph_answers().items()
            if phase in targets and entry.get("site_graph")]


class PlacementResolver:
    """Resolve each package's placement for one phase on the submitting host."""

    def __init__(self, ppl, context):
        from jarvis_cd.core.spack_install import Decisions

        self.ppl = ppl
        self.context = context
        self.decisions = Decisions(context.decisions_path)
        self.costs_file = costs_path(context.decisions_path)
        self.costs = read_costs(self.costs_file)
        self._sample: Optional[Dict[str, Any]] = None
        self._limits: Optional[Dict[str, Any]] = None
        self._queue: Optional[Dict[str, Any]] = None

    @property
    def sample(self) -> Dict[str, Any]:
        if self._sample is None:
            self._sample = measure_login_host()
        return self._sample

    @property
    def limits(self) -> Dict[str, Any]:
        if self._limits is None:
            self._limits = limits_for(self.sample)
        return self._limits

    @property
    def queue(self) -> Dict[str, Any]:
        if self._queue is None:
            self._queue = queue_evidence(getattr(self.ppl, "scheduler", None))
        return self._queue

    def site_entries(self) -> Dict[str, Dict[str, Any]]:
        return {
            str(subject): dict(entry)
            for subject, entry in (self.decisions.data.get("site") or {}).items()
            if str(subject).startswith("install_placement.") and isinstance(entry, Mapping)
        }

    def graph_answers(self) -> Dict[str, Dict[str, Any]]:
        """The confirmed answers in the active resource graph (read each time)."""
        return graph_answers()

    def sync_graph(self, site: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, Any]]:
        """Copy this home's per-phase standing answers into the graph.

        Answers recorded before the graph held them (or while it was
        unwritable) reach it here; a newer graph answer is kept.
        """
        written: List[Dict[str, Any]] = []
        graph = self.graph_answers()
        for phase in PHASES:
            entry = site.get(site_subject(phase))
            if not isinstance(entry, Mapping) or entry.get("constraint") not in STANDING:
                continue
            current = graph.get(phase) or {}
            if all(current.get(key) == entry.get(key)
                   for key in ("constraint", "issue_id", "answered_at")):
                continue
            result = persist_graph_answer(phase, entry, recorded_in=self.decisions.path)
            if result.get("persisted"):
                written.append(result)
        return written

    def recommendation(self, phase: str, est: Mapping[str, Any]
                       ) -> Tuple[str, str, Dict[str, Any]]:
        return recommend(phase, est["class"], self.limits, self.site_entries(), est,
                         self.graph_answers())

    # -- issues ------------------------------------------------------------

    def override_issue(self, phase: str, name: str,
                       est: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """A package override question; with ``est`` it carries a recommendation.

        The id depends only on the package, phase and choices, so it is the
        same with or without the recommendation.
        """
        from jarvis_cd.core.spack_install import make_issue

        choice = reason = basis = None
        if est is not None:
            choice, reason, basis = self.recommendation(phase, est)
        issue = make_issue(
            kind=PLACEMENT_KIND, package=name, subject=f"install_placement.{phase}",
            scope="package",
            question=(f"Where should JARVIS run the Spack {phase} of {name} at this site?"
                      + (f" Recommended: {choice} ({reason})." if choice else "")),
            candidates=_candidates(phase, choice), answerable=True,
            hint="a package override: beats the site's answer for this package",
        )
        if choice is not None:
            issue.update(recommended=choice, recommendation_reason=reason,
                         recommendation_basis=basis)
        return issue

    def legacy_site_issue(self, phase: str, cost_class: str, *, once: bool = False
                          ) -> Dict[str, Any]:
        """The per-cost-class question 58cd5b1a asked: its id still takes answers."""
        from jarvis_cd.core.spack_install import make_issue

        return make_issue(
            kind=PLACEMENT_KIND, package=str(getattr(self.ppl, "name", "") or ""),
            subject=legacy_site_subject(phase, cost_class, once=once), scope="site",
            question=f"(legacy) Spack {phase} placement of {cost_class} packages",
            candidates=_candidates(phase, None, once=once), answerable=True,
        )

    def site_issue(self, phase: str, packages: List[Dict[str, Any]],
                   *, once: bool = False) -> Dict[str, Any]:
        from jarvis_cd.core.spack_install import make_issue

        limits = self.limits
        worst = max(packages, key=lambda item: item["estimate"].get("seconds") or 1e12)
        cost_class = worst["estimate"]["class"]
        graph = self.graph_answers()
        choice, reason, basis = recommend(phase, cost_class, limits, self.site_entries(),
                                          worst["estimate"], graph)
        if len(packages) > 1 and basis.get("source") == "heuristic":
            reason = f"costliest here is {worst['spec_name']}: {reason}"
        names = ", ".join(f"{item['spec_name']} ({item['estimate']['human']})"
                          for item in packages)
        subject = site_subject(phase, once=once)
        if once:
            question = (f"Where should JARVIS run the Spack {phase} this time: {names}? "
                        f"Recommended: {choice} ({reason}).")
        else:
            question = (f"Where should JARVIS run Spack {phase}s at this site? The answer "
                        f"covers every package, these and later ones: {names}; a package "
                        f"override places one package otherwise. Recommended: {choice} "
                        f"({reason}).")
        issue = make_issue(
            kind=PLACEMENT_KIND, package=str(getattr(self.ppl, "name", "") or ""),
            subject=subject, scope="site",
            question=question,
            candidates=_candidates(phase, choice, once=once),
            evidence=self._evidence_lines(phase, packages),
            answerable=True,
            hint=("answers.json: {issue_id: '<candidate id>'} or {issue_id: "
                  "{constraint, answered_by, basis}}; `basis` records why (e.g. the "
                  "site's documentation an agent found); answer a package_overrides "
                  "issue_id to choose for one package"),
        )
        sample = self.sample
        issue.update(
            schema_placement=PLACEMENT_SCHEMA,
            phase=phase, cost_class=cost_class, once=once,
            cost_classes=sorted({item["estimate"]["class"] for item in packages}),
            covers="every package at this site" if not once else "this run",
            recommended=choice, recommendation_reason=reason,
            recommendation_basis=basis,
            packages=packages,
            login_host=sample,
            limits=limits,
            queue=self.queue,
            site_policy={
                "recorded": self.site_entries(),
                "graph": graph,
                "discovered": {"cgroup": sample.get("cgroup")},
                "how_to_supply": ("an agent may read the site's documentation, propose a "
                                  "choice and give the source as the answer's `basis`; "
                                  "the person confirms; JARVIS records it per site"),
            },
            decisions_path=str(self.context.decisions_path),
            costs_path=str(self.costs_file),
        )
        if not once:
            overrides = []
            for item in packages:
                override = self.override_issue(phase, item["spec_name"], item["estimate"])
                overrides.append({
                    "package": item["spec_name"], "phase": phase,
                    "issue_id": item["override_issue_id"],
                    "subject": f"install_placement.{phase}",
                    "question": override["question"],
                    "candidates": override["candidates"],
                    "recommended": override["recommended"],
                    "recommendation_reason": override["recommendation_reason"],
                    "recommendation_basis": override["recommendation_basis"],
                })
            issue["package_overrides"] = overrides
        return issue

    def _evidence_lines(self, phase: str, packages: List[Dict[str, Any]]) -> List[str]:
        sample, limits, queue = self.sample, self.limits, self.queue
        own = sample.get("own_processes") or {}
        lines = [
            f"login host {sample.get('host')}: {sample.get('cores')} cores, load "
            f"{sample.get('load')}, {sample.get('available_mb')} MB free of "
            f"{sample.get('total_mb')} MB, your processes {own.get('count')} "
            f"({own.get('running')} running, {own.get('rss_mb')} MB RSS)",
            f"limits here now: {limits['jobs']} job(s), nice {limits['nice']}, I/O "
            f"{limits['ionice']}, memory {limits['memory_mb']} MB"
            + (f"; deferred: {'; '.join(limits['defer_reasons'])}" if limits["defer"] else ""),
        ]
        cgroup = sample.get("cgroup") or {}
        if cgroup.get("cpu_cores") or cgroup.get("memory_max_mb"):
            lines.append(f"site cgroup limit: {cgroup.get('cpu_cores')} cores, "
                         f"{cgroup.get('memory_max_mb')} MB")
        if queue.get("measured"):
            lines.append(f"a 1-node job would start in {queue.get('wait')} "
                         f"({queue.get('start_at')})")
        else:
            lines.append(f"queue wait not measured: {queue.get('why') or queue.get('output')}")
        for item in packages:
            lines.append(f"{item['spec_name']} {phase}: {item['estimate']['human']} "
                         f"({item['estimate']['basis']})")
        return lines

    # -- resolution --------------------------------------------------------

    def _answer(self, issue: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        answer = self.context.answer_for(issue)
        if answer is not None and answer["constraint"] not in CHOICES:
            raise ValueError(f"answer for {issue['issue_id']} must be one of {CHOICES}")
        return answer

    def _record(self, issue: Mapping[str, Any], answer: Mapping[str, Any],
                extra: Mapping[str, Any]) -> None:
        self.decisions.record(issue, answer, extra={**extra, "basis": answer.get("basis")})
        phase = extra.get("phase")
        if issue.get("scope") == "site" and issue.get("subject") == site_subject(str(phase)):
            entry = self.decisions.site(str(issue["subject"]))
            if entry is not None:
                result = persist_graph_answer(str(phase), entry,
                                              recorded_in=self.decisions.path)
                log = getattr(self.context, "log", None)
                if callable(log) and (result.get("persisted") or result.get("error")):
                    log(f"install placement: graph answer {json.dumps(result)}\n")

    def resolve(self, phase: str, pkg_defs: Sequence[Mapping[str, Any]],
                missing: Optional[Mapping[str, Sequence[str]]] = None,
                *, raise_questions: bool = True) -> Dict[str, Any]:
        """``{packages: {pkg_id: {...choice}}, choice, questions}`` for ``phase``.

        ``choice`` is ``login_limited`` only when every package resolved to
        it (one concretization covers them all), ``compute_job`` when any
        resolved to that, else ``None`` (questions are open).
        """
        with self.decisions.locked():
            pass  # re-read the store fresh
        site = self.site_entries()
        synced = self.sync_graph(site)
        log = getattr(self.context, "log", None)
        if synced and callable(log):
            log(f"install placement: recorded answers copied to the graph "
                f"{json.dumps(synced)}\n")
        resolved: Dict[str, Dict[str, Any]] = {}
        pending: Dict[bool, List[Dict[str, Any]]] = {}
        for pkg_def in pkg_defs:
            pkg_id = str(pkg_def.get("pkg_id"))
            name = spec_name(pkg_def)
            est = estimate(phase, name, self.costs,
                           None if missing is None else missing.get(pkg_id))
            override = self.override_issue(phase, name)
            item = {"pkg_id": pkg_id, "spec_name": name, "estimate": est,
                    "override_issue_id": override["issue_id"]}
            entry = next((e for e in self.decisions.package_entries(name)
                          if e.get("subject") == f"install_placement.{phase}"), None)
            choice = source = None
            if entry is not None:
                choice, source = entry["constraint"], "package_decision"
                self.context.note_decision(override, entry, source="decisions_file",
                                           path=self.decisions.path, package=name)
            else:
                answer = self._answer(override)
                if answer is not None:
                    self._record(override, answer, {"phase": phase})
                    choice, source = answer["constraint"], "package_answer"
            subject = site_subject(phase)
            if choice is None:
                choice, source = self._site_choice(phase, est["class"], site)
            if choice is None:
                probe = self.site_issue(phase, [item])
                answer = self._answer(probe)
                if answer is not None:
                    self._record(probe, answer, {"phase": phase})
                    site = self.site_entries_after_record(site, subject, answer)
                    choice, source = answer["constraint"], "site_answer"
                else:
                    site = self._legacy_answers(phase, site)
                    choice, source = self._site_choice(phase, est["class"], site)
            if choice is None:
                default = os.environ.get(DEFAULT_ENV[phase], "").strip().lower()
                if default in (LOGIN_LIMITED, COMPUTE_JOB):
                    choice, source = default, f"environment ({DEFAULT_ENV[phase]})"
            if choice == ASK_EACH_TIME:
                once = self.site_issue(phase, [item], once=True)
                answer = (self._answer(once)
                          or self._answer(self.legacy_site_issue(phase, est["class"],
                                                                 once=True)))
                if answer is not None:
                    choice, source = answer["constraint"], f"{source} -> this time"
                else:
                    pending.setdefault(True, []).append(item)
                    choice = None
            elif choice is None:
                pending.setdefault(False, []).append(item)
            if (phase == "build" and choice == LOGIN_LIMITED and est["class"] == LONG
                    and str(source).startswith(("site", "environment"))):
                item["note"] = (
                    f"the site's login_limited build answer covers {name} "
                    f"({est['human']}, long); answer its package override "
                    f"{override['issue_id']} with compute_job to build it in a job")
                log = getattr(self.context, "log", None)
                if callable(log):
                    log(f"install placement: {item['note']}\n")
            item.update(choice=choice, source=source)
            resolved[pkg_id] = item
        questions: List[Dict[str, Any]] = []
        for once, items in sorted(pending.items()):
            question = self.site_issue(phase, items, once=once)
            questions.append(question)
            if raise_questions:
                self.context.raise_issue(question)
        choices = {item.get("choice") for item in resolved.values()}
        if questions:
            overall = None
        elif COMPUTE_JOB in choices:
            overall = COMPUTE_JOB
        else:
            overall = LOGIN_LIMITED if resolved else None
        return {
            "schema_version": PLACEMENT_SCHEMA,
            "phase": phase,
            "choice": overall,
            "packages": resolved,
            "questions": [q["issue_id"] for q in questions],
        }

    def _site_choice(self, phase: str, cost_class: str,
                     site: Mapping[str, Mapping[str, Any]]
                     ) -> Tuple[Optional[str], Optional[str]]:
        """The site's recorded choice for ``phase``, and where it came from.

        The per-phase subject covers every package. Entries 58cd5b1a recorded
        per cost class still count: when they agree they cover every package
        too; when they differ, each still covers its own class.
        """
        subject = site_subject(phase)
        found: Optional[Tuple[str, Mapping[str, Any]]] = None
        source = "site_decision"
        if subject in site:
            found = (subject, site[subject])
        else:
            legacy = {cls: site[legacy_site_subject(phase, cls)] for cls in (SHORT, LONG)
                      if legacy_site_subject(phase, cls) in site}
            constraints = {str(entry.get("constraint")) for entry in legacy.values()}
            if len(constraints) == 1:
                cls = SHORT if SHORT in legacy else LONG
                found = (legacy_site_subject(phase, cls), legacy[cls])
                source = (f"site_decision (legacy "
                          f"{', '.join(legacy_site_subject(phase, c) for c in sorted(legacy))}"
                          ", covers every package)")
            elif cost_class in legacy:
                found = (legacy_site_subject(phase, cost_class), legacy[cost_class])
                source = f"site_decision (legacy {found[0]})"
        if found is None:
            return None, None
        key, entry = found
        if entry.get("issue_id"):
            self.context.note_decision(
                {"issue_id": entry.get("issue_id"), "kind": PLACEMENT_KIND,
                 "subject": key, "scope": "site"}, entry,
                source="decisions_file", path=self.decisions.path)
        return str(entry["constraint"]), source

    def _legacy_answers(self, phase: str, site: Dict[str, Dict[str, Any]]
                        ) -> Dict[str, Dict[str, Any]]:
        """Apply answers keyed by 58cd5b1a's per-class question ids.

        Agreeing answers are recorded under the per-phase subject (with
        ``rekeyed_from``); differing ones keep their per-class subjects.
        """
        answered = []
        for cls in (SHORT, LONG):
            issue = self.legacy_site_issue(phase, cls)
            answer = self._answer(issue)
            if answer is not None:
                answered.append((issue, answer))
        if not answered:
            return site
        if len({answer["constraint"] for _, answer in answered}) == 1:
            issue, answer = answered[0]
            current = self.site_issue_identity(phase)
            self._record(current, answer, {"phase": phase,
                                           "rekeyed_from": issue["issue_id"]})
            return self.site_entries_after_record(site, site_subject(phase), answer)
        for issue, answer in answered:
            self._record(issue, answer, {"phase": phase})
            site = self.site_entries_after_record(site, issue["subject"], answer)
        return site

    def site_issue_identity(self, phase: str) -> Dict[str, Any]:
        """The per-phase site question's identity (its id), without evidence."""
        from jarvis_cd.core.spack_install import make_issue

        return make_issue(
            kind=PLACEMENT_KIND, package=str(getattr(self.ppl, "name", "") or ""),
            subject=site_subject(phase), scope="site",
            question=f"Spack {phase} placement at this site",
            candidates=_candidates(phase, None), answerable=True,
        )

    @staticmethod
    def site_entries_after_record(site: Dict[str, Dict[str, Any]], subject: str,
                                  answer: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
        updated = dict(site)
        updated[subject] = {"constraint": answer["constraint"], "subject": subject}
        return updated

    def evidence(self) -> Dict[str, Any]:
        """What the placement saw: the host sample, limits and queue estimate."""
        return {"login_host": self.sample, "limits": self.limits, "queue": self.queue}


def build_command(choice: Optional[str]) -> Optional[str]:
    """The command that builds a package under its build placement."""
    if choice == LOGIN_LIMITED:
        return "jarvis ppl install yaml FILE"
    if choice == COMPUTE_JOB:
        return "jarvis ppl install yaml FILE +submit"
    return None


def decisions_digest_without_placement(path: Path) -> Optional[str]:
    """The decisions digest that install records key on, placement excluded.

    Placement answers decide where a check runs, not what it finds, so
    recording one must not make ``install-check.json`` stale: the digest is
    over the store's other entries (canonical JSON), and a store with none
    digests like an absent one (None).
    """
    import hashlib

    import yaml

    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return "sha256:" + hashlib.sha256(raw).hexdigest()
    if not isinstance(data, dict):
        return "sha256:" + hashlib.sha256(raw).hexdigest()
    site = {k: v for k, v in (data.get("site") or {}).items()
            if not str(k).startswith("install_placement.")}
    packages = {}
    for name, entries in (data.get("packages") or {}).items():
        kept = [e for e in entries or []
                if not (isinstance(e, dict) and e.get("kind") == PLACEMENT_KIND)]
        if kept:
            packages[name] = kept
    if not site and not packages:
        return None
    canonical = json.dumps({"site": site, "packages": packages}, sort_keys=True,
                           default=str)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
