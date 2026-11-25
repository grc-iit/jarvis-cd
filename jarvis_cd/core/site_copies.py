"""Find the site's own copies of a package before JARVIS plans a Spack build.

Before a Spack package is planned, JARVIS looks for the copies the site
already has:

* module trees: ``module -t avail <name>`` (Lmod and Tcl modules), plus
  ``module -t spider <name>`` and ``module spider <module>`` with Lmod, which
  also finds modules behind a hierarchy (e.g. under an MPI) and the modules
  that must be loaded first; ``module show`` gives the copy's prefix and what
  it loads (which MPI it uses);
* the site's Spacks (:func:`~jarvis_cd.core.spack_install.discover_site_spacks`,
  not the Spacks JARVIS owns): ``spack find --json <name>``, any version;
* the resource graph's ``software`` section: copies the person confirmed for
  this site (``{name, version, modules, modulepath, prefix, label, implies}``).

Found copies are an answerable ``site_copy`` issue: "use the site's copy or
build with Spack". Every candidate carries a label, version, modules,
implies, whether it fits the package's needs (its version range, MPI and
variants) and why; ``recommended`` is the best copy that fits, else the
Spack build, with the reason. The answer is recorded per site (the decisions
store), so the question is not asked again for the same needs. Choosing a
copy makes the package's ``install_method`` ``site``: :func:`site_run` loads
those modules (or ``spack load``s that site install) into the pipeline's env,
and nothing is built. No copy found is a ``site_copy_none`` fact (not a
question): the Spack path runs as before.

Each site Spack is probed first (:mod:`jarvis_cd.core.spack_health`): one
that is broken apart from any package is the answerable ``spack_broken``
issue, nothing from it is offered, and the package waits for the answer.

Before the install runs, each ``site`` package's copy is loaded once
(:func:`verify_site_choices`). A copy that does not load here is the
answerable ``site_copy_unusable`` issue: its candidates are the site's other
copies, the Spack build, and ``forget`` (drop the recorded choice and ask the
``site_copy`` question again, with that copy marked as not loading).

Nothing here knows any site: all of it is read from the host's module
system, its Spacks and its resource graph.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from jarvis_cd.core.spack_install import (
    MPI_PROVIDERS,
    Decisions,
    _utc_now,
    make_issue,
)

SCHEMA = "jarvis.site-copies.v1"
KIND = "site_copy"
NONE_KIND = "site_copy_none"
SUBJECT = "site_copy"
SPACK_BUILD = "spack"
METHOD = "site"
#: ``off`` skips the search (a package can set ``install_site_search: false``)
SWITCH_ENV = "JARVIS_SITE_COPIES"
#: the resource graph section listing confirmed site copies
GRAPH_SECTION = "software"
_MAX_MODULES = 12
_LOGIN_TIMEOUT = 90
_SPACK_TIMEOUT = 180
_MPI_NAMES = set(MPI_PROVIDERS) | {"impi", "intelmpi", "intel-mpi", "mvapich", "mpt"}
#: a Spack module name's trailing ``-<7 char hash>``
_SPACK_HASH_SUFFIX = re.compile(r"^(?P<version>.+?)-(?P<hash>[a-z0-9]{7})$")
_PREFIX_HASH = re.compile(r"-(?P<hash>[a-z2-7]{32})(?:/|$)")
_INFINITE = ("develop", "main", "master", "head", "trunk", "stable")


# ---------------------------------------------------------------------------
# What the package needs (its Spack query)
# ---------------------------------------------------------------------------


@dataclass
class Requirement:
    """One node of a Spack query: name, versions, variants, compiler."""

    name: Optional[str] = None
    versions: List[str] = field(default_factory=list)
    variants: Dict[str, Any] = field(default_factory=dict)
    compiler: Optional[str] = None
    hash: Optional[str] = None
    unparsed: List[str] = field(default_factory=list)

    def text(self) -> str:
        parts = [self.name or ""]
        if self.versions:
            parts.append("@" + ",".join(self.versions))
        for key, value in self.variants.items():
            parts.append(("+" if value else "~") + key if isinstance(value, bool)
                         else f" {key}={value}")
        if self.hash:
            parts.append("/" + self.hash)
        return "".join(parts)


@dataclass
class Needs:
    """What a site copy must satisfy: the query's root and its ``^`` pins."""

    query: str
    root: Requirement
    deps: List[Requirement]
    #: the site's recorded MPI (``decisions.yaml`` site subject ``mpi``)
    site_mpi: Optional[Requirement] = None
    #: its install prefix, when the record names one (``candidate_key``)
    site_mpi_prefix: Optional[str] = None

    @property
    def name(self) -> str:
        return (self.root.name or "").lower()

    @property
    def mpi(self) -> Optional[Requirement]:
        for dep in self.deps:
            if dep.name and dep.name.lower() in _MPI_NAMES | {"mpi"}:
                return dep
        return None

    def key(self) -> str:
        """What the recorded answer depends on (the query and the site MPI)."""
        key = " ".join(shlex.split(self.query))
        if self.site_mpi is not None:
            key += f" [site mpi {self.site_mpi.text()}]"
        return key

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "name": self.name,
            "versions": list(self.root.versions),
            "variants": dict(self.root.variants),
            "compiler": self.root.compiler,
            "mpi": self.mpi.text() if self.mpi else None,
            "site_mpi": self.site_mpi.text() if self.site_mpi else None,
            "depends_on": [dep.text() for dep in self.deps if dep is not self.mpi],
        }


_TOKEN = re.compile(
    r"\s*(?:"
    r"(?P<at>@=?[^\s+~%^@]+)"
    r"|(?P<plus>\+\+?[A-Za-z0-9_][\w.-]*)"
    r"|(?P<tilde>~~?[A-Za-z0-9_][\w.-]*)"
    r"|(?P<pct>%[^\s^]+)"
    r"|(?P<hash>/[a-z0-9]+)"
    r"|(?P<kv>[A-Za-z0-9_][\w.-]*==?[^\s^]+)"
    r"|(?P<name>[A-Za-z0-9_][\w.-]*)"
    r")"
)


def parse_requirement(text: str) -> Requirement:
    """One Spack spec node (``name@versions+variant k=v %compiler /hash``)."""
    req = Requirement()
    position = 0
    text = text.strip()
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None or match.end() == position:
            leftover = text[position:].strip()
            if leftover:
                req.unparsed.append(leftover)
            break
        position = match.end()
        kind = match.lastgroup
        value = (match.group(kind) or "").strip()
        if kind == "at":
            req.versions.extend(part for part in value[1:].split(",") if part)
        elif kind == "plus":
            req.variants[value.lstrip("+")] = True
        elif kind == "tilde":
            req.variants[value.lstrip("~")] = False
        elif kind == "pct":
            req.compiler = value[1:]
        elif kind == "hash":
            req.hash = value[1:]
        elif kind == "kv":
            key, _, rest = value.partition("=")
            req.variants[key] = rest.lstrip("=")
        elif kind == "name":
            if req.name is None:
                req.name = value
            else:
                req.unparsed.append(value)
    return req


def parse_needs(query: str, site_mpi: Optional[str] = None,
                site_mpi_prefix: Optional[str] = None) -> Needs:
    """The query's root requirement and its ``^`` dependencies."""
    segments = " ".join(shlex.split(query)).split("^")
    root = parse_requirement(segments[0])
    deps = [parse_requirement(segment) for segment in segments[1:] if segment.strip()]
    mpi = None
    if site_mpi:
        parsed = parse_requirement(str(site_mpi).lstrip("^"))
        if parsed.name:
            mpi = parsed
    return Needs(query=query, root=root, deps=deps, site_mpi=mpi,
                 site_mpi_prefix=site_mpi_prefix if mpi is not None else None)


# -- versions (Spack semantics) ------------------------------------------------


def _vkey(version: str) -> List[Tuple[int, Any]]:
    parts = [part for part in re.split(r"[._-]", str(version)) if part]
    key: List[Tuple[int, Any]] = []
    for part in parts:
        if part.isdigit():
            key.append((1, int(part)))
        elif part.lower() in _INFINITE:
            key.append((2, part.lower()))
        else:
            key.append((0, part.lower()))
    return key


def _prefix_of(constraint: str, version: str) -> bool:
    wanted, have = _vkey(constraint), _vkey(version)
    return have[:len(wanted)] == wanted


def version_satisfies(version: Optional[str], constraint: str) -> bool:
    """Whether ``version`` satisfies one Spack version constraint.

    ``X`` matches X and X.* (``3.3`` matches 3.3.0); ``=X`` is exact; ``A:B``
    is inclusive at both ends with the upper end matching B.*; either end of
    a range may be left open.
    """
    if not version:
        return False
    constraint = constraint.strip()
    if constraint.startswith("="):
        return _vkey(version) == _vkey(constraint[1:])
    if ":" not in constraint:
        return _prefix_of(constraint, version)
    low, _, high = constraint.partition(":")
    have = _vkey(version)
    if low and have < _vkey(low):
        return False
    if high and have > _vkey(high) and not _prefix_of(high, version):
        return False
    return True


def versions_satisfied(version: Optional[str], constraints: Sequence[str]) -> bool:
    return not constraints or any(version_satisfies(version, item) for item in constraints)


def module_version(text: str) -> Tuple[str, Optional[str]]:
    """A module version and the Spack hash its name may end with."""
    match = _SPACK_HASH_SUFFIX.match(text)
    if match and re.match(r"\d", match.group("version")):
        return match.group("version"), match.group("hash")
    return text, None


# ---------------------------------------------------------------------------
# Discovery: module trees
# ---------------------------------------------------------------------------


_MODULE_LIST_SCRIPT = r'''
name="$1"
if ! type module >/dev/null 2>&1; then printf '\n@@NOMODULE\n'; exit 0; fi
printf '\n@@LMOD=%s\n' "${LMOD_CMD-}"
printf '@@MODULEPATH=%s\n' "${MODULEPATH-}"
printf '@@AVAIL\n'
module -t avail "$name" 2>&1
if [ -n "${LMOD_CMD-}" ]; then
  printf '\n@@SPIDER\n'
  module -t spider "$name" 2>&1
fi
printf '\n@@END\n'
'''

_MODULE_SPIDER_SCRIPT = r'''
for m in "$@"; do
  printf '\n@@MOD=%s\n' "$m"
  module spider "$m" 2>&1
done
printf '\n@@END\n'
'''

_MODULE_SHOW_SCRIPT = r'''
while [ "$#" -ge 2 ]; do
  m="$1"; pre="$2"; shift 2
  printf '\n@@MOD=%s\n' "$m"
  (
    if [ -n "$pre" ]; then module load $pre >/dev/null 2>&1; fi
    module show "$m" 2>&1
  )
done
printf '\n@@END\n'
'''


def _login(script: str, args: Sequence[str], timeout: float = _LOGIN_TIMEOUT
           ) -> Tuple[int, str]:
    """Run ``script`` in a throwaway login shell (where ``module`` is defined)."""
    try:
        completed = subprocess.run(
            ["bash", "-lc", script, "_", *args], capture_output=True, text=True,
            timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def _sections(output: str) -> Dict[str, List[str]]:
    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for line in output.splitlines():
        if line.startswith("@@"):
            current = line[2:].strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return sections


_SPIDER_ONE = re.compile(r"^\s*[\w.+-]+:\s+(\S+/\S+)\s*$")


def _module_names(lines: Sequence[str]) -> List[str]:
    """``module -t avail``/``spider`` output: the module names it lists.

    Lmod's spider prints its detail form (``name: name/version``) when only
    one module matches; that name is taken too.
    """
    names: List[str] = []
    for raw in lines:
        one = _SPIDER_ONE.match(raw)
        line = one.group(1) if one else raw.strip()
        if not line or line.endswith(":") or line.endswith("/") or " " in line:
            continue
        if line.lower().startswith(("lmod", "error", "warning")):
            continue
        line = re.sub(r"\((?:default|D|L|[a-zA-Z,]*)\)$", "", line).strip()
        if line and line not in names:
            names.append(line)
    return names


def _names_match(part: str, name: str) -> bool:
    return part.lower().replace("_", "-") == name.lower().replace("_", "-")


def module_entry(module: str, name: str) -> Optional[Dict[str, Any]]:
    """A module name that is ``name``'s: ``[hierarchy/]name[/flavor]/version``."""
    parts = [part for part in module.split("/") if part]
    index = max((i for i, part in enumerate(parts) if _names_match(part, name)), default=-1)
    if index < 0:
        return None
    if index == len(parts) - 1:
        version_text, flavor = None, []
    else:
        version_text, flavor = parts[-1], parts[index + 1:-1]
    version, spack_hash = module_version(version_text) if version_text else (None, None)
    return {
        "module": module,
        "short": "/".join(parts[index:]),
        "hierarchy": parts[:index],
        "flavor": flavor,
        "version": version,
        "version_text": version_text,
        "spack_hash7": spack_hash,
    }


def parse_spider_prereqs(lines: Sequence[str]) -> Optional[List[str]]:
    """The modules ``module spider <m>`` says to load first (fewest), or None."""
    options: List[List[str]] = []
    collecting = False
    for raw in lines:
        line = raw.strip()
        if "You will need to load all module(s)" in line:
            collecting = True
            continue
        if not collecting:
            continue
        if not line:
            if options:
                break
            continue
        if line.endswith(":") or line.lower().startswith(("help", "description")):
            break
        options.append(line.split())
    if not options:
        return None
    return min(options, key=lambda option: (len(option), sum(m.count("/") for m in option)))


_LUA_LOAD = re.compile(
    r'\b(?:depends_on|load|prereq|always_load|try_load|prereq_any)\s*\(\s*"([^"]+)"')
_TCL_LOAD = re.compile(r"^\s*(?:module\s+load|module-load|prereq|depends-on)\s+(\S+)")
_LUA_PATH = re.compile(r'prepend_path\s*\(\s*"(PATH|LD_LIBRARY_PATH)"\s*,\s*"([^"]+)"')
_TCL_PATH = re.compile(r"^\s*prepend-path\s+(PATH|LD_LIBRARY_PATH)\s+(\S+)")
_SHOW_FILE = re.compile(r"^\s*(/\S+?):?\s*$")


def parse_module_show(lines: Sequence[str]) -> Dict[str, Any]:
    """``module show``: the module file, what it loads and its install prefix."""
    loads: List[str] = []
    file_path = None
    prefixes: Dict[str, str] = {}
    failed = False
    for raw in lines:
        line = raw.rstrip()
        if "Unable to find" in line or "Unable to locate" in line:
            failed = True
        if file_path is None:
            found = _SHOW_FILE.match(line)
            if found and (found.group(1).endswith(".lua") or "/" in found.group(1)[1:]):
                file_path = found.group(1)
                continue
        for pattern in (_LUA_LOAD, _TCL_LOAD):
            for item in pattern.findall(line):
                if item not in loads:
                    loads.append(item)
        for pattern in (_LUA_PATH, _TCL_PATH):
            found = pattern.search(line)
            if found and found.group(1) not in prefixes:
                prefixes[found.group(1)] = found.group(2)
    prefix = None
    for variable, suffix in (("PATH", "/bin"), ("LD_LIBRARY_PATH", "/lib")):
        value = prefixes.get(variable, "").rstrip("/")
        if value.endswith(suffix):
            prefix = value[: -len(suffix)]
            break
        if variable == "LD_LIBRARY_PATH" and value.endswith("/lib64"):
            prefix = value[:-6]
    return {"file": file_path, "loads": loads, "prefix": prefix, "failed": failed}


def _mpi_of_module(module: str) -> Optional[Dict[str, Any]]:
    parts = [part for part in module.split("/") if part]
    for index, part in enumerate(parts):
        if part.lower() in _MPI_NAMES:
            version_text = parts[index + 1] if index + 1 < len(parts) else None
            version, spack_hash = (module_version(version_text) if version_text
                                   else (None, None))
            return {"name": part.lower(), "version": version, "hash": spack_hash,
                    "module": "/".join(parts[index:index + 2])}
    return None


def _same_install(mpi: Mapping[str, Any], prefix: Optional[str]) -> bool:
    """False only when both prefixes are known and differ.

    Hashes are not compared: the site's Spack and the one JARVIS uses give
    the same MPI different hashes; the prefix is what names it.
    """
    have = mpi.get("prefix")
    if not prefix or not have:
        return True
    return os.path.normpath(str(have)) == os.path.normpath(str(prefix))


def module_copies(name: str, searched: List[str]) -> List[Dict[str, Any]]:
    """The site modules that install ``name``."""
    rc, output = _login(_MODULE_LIST_SCRIPT, [name])
    sections = _sections(output)
    if "NOMODULE" in sections:
        searched.append("modules: no `module` command in a login shell")
        return []
    if "AVAIL" not in sections:
        searched.append(f"modules: the login shell failed (exit {rc}): {output.strip()[-300:]}")
        return []
    lmod = next((key[5:] for key in sections if key.startswith("LMOD=")), "")
    module_path = next((key[11:] for key in sections if key.startswith("MODULEPATH=")), "")
    avail = _module_names(sections.get("AVAIL", []))
    spider = _module_names(sections.get("SPIDER", [])) if lmod else []
    searched.append(
        f"modules ({'Lmod' if lmod else 'environment modules'}): `module -t avail {name}`"
        + (f", `module -t spider {name}`" if lmod else "")
        + f" on MODULEPATH {module_path or '(empty)'}")
    entries: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for module, origin in [*((m, "spider") for m in spider), *((m, "avail") for m in avail)]:
        entry = module_entry(module, name)
        if entry is None or entry["version_text"] is None:
            continue
        key = (entry["version_text"].lower(), tuple(entry["flavor"]))
        current = entries.get(key)
        entry["origin"] = origin
        if current is None or (current["origin"] != "spider" and (
                origin == "spider" or len(module) < len(current["module"]))):
            entries[key] = entry
    chosen = list(entries.values())[:_MAX_MODULES]
    if not chosen:
        return []
    prereqs: Dict[str, List[str]] = {}
    spidered = [entry["module"] for entry in chosen if entry["origin"] == "spider"]
    if lmod and spidered:
        _, output = _login(_MODULE_SPIDER_SCRIPT, spidered)
        for key, lines in _sections(output).items():
            if key.startswith("MOD="):
                found = parse_spider_prereqs(lines)
                if found:
                    prereqs[key[4:]] = found
    args: List[str] = []
    for entry in chosen:
        args += [entry["module"], " ".join(prereqs.get(entry["module"], []))]
    _, output = _login(_MODULE_SHOW_SCRIPT, args)
    shows = {key[4:]: parse_module_show(lines)
             for key, lines in _sections(output).items() if key.startswith("MOD=")}
    copies: List[Dict[str, Any]] = []
    for entry in chosen:
        module = entry["module"]
        show = shows.get(module) or {"loads": [], "prefix": None, "file": None,
                                     "failed": True}
        before = prereqs.get(module, [])
        mpi = None
        for item in [*before, *show["loads"], "/".join(entry["hierarchy"])]:
            mpi = _mpi_of_module(item) if item else None
            if mpi:
                break
        module_dir = None
        if show.get("file") and entry["short"] in show["file"]:
            module_dir = show["file"][: show["file"].rfind(entry["short"])].rstrip("/")
        prefix = show.get("prefix")
        prefix_hash = _PREFIX_HASH.search(prefix or "")
        copies.append({
            "source": "module",
            "name": name,
            "version": entry["version"],
            "modules": [*before, module],
            "module": module,
            # where the module file is; the login MODULEPATH (and the modules
            # loaded first) reach it, so it is not `module use`d at run
            "module_dir": module_dir,
            "module_file": show.get("file"),
            "prefix": prefix,
            "spack_hash": prefix_hash.group("hash") if prefix_hash else None,
            "flavor": "/".join(entry["flavor"]) or None,
            "implies": {
                "mpi": mpi,
                "loads": [item for item in show["loads"] if item not in before],
                **({"load_first": before} if before else {}),
            },
            "variants": None,
            "loadable": not show.get("failed"),
        })
    return copies


# ---------------------------------------------------------------------------
# Discovery: the site's Spacks
# ---------------------------------------------------------------------------


def site_spack_roots() -> List[Dict[str, Any]]:
    """The site's Spacks: the ones JARVIS found, without those JARVIS owns."""
    from jarvis_cd.core.method_install import site_home
    from jarvis_cd.core.spack_install import discover_site_spacks

    owned = []
    for path in (site_home(), Path(os.environ.get("JARVIS_ROOT", "") or "~/.ppi-jarvis")):
        try:
            owned.append(str(Path(path).expanduser().resolve()))
        except (OSError, RuntimeError):
            continue
    roots = []
    for item in discover_site_spacks():
        root = str(item["root"])
        if item.get("source") == "resource_graph_site":
            continue
        if any(root == mine or root.startswith(mine + os.sep) for mine in owned):
            continue
        roots.append(item)
    return roots


def _spack_nodes(document: Any) -> List[Dict[str, Any]]:
    """The installed root specs of ``spack find --json`` (0.x lists, 1.x nodes)."""
    items = document if isinstance(document, list) else [document]
    roots: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        spec = item.get("spec") if isinstance(item.get("spec"), Mapping) else None
        nodes = spec.get("nodes") if spec else None
        if isinstance(nodes, list) and nodes:
            root = dict(nodes[0])
            by_hash = {node.get("hash"): node for node in nodes if isinstance(node, Mapping)}
            root["_deps"] = [
                {**dict(by_hash.get(dep.get("hash")) or {}), **dict(dep)}
                for dep in root.get("dependencies") or [] if isinstance(dep, Mapping)
            ]
            roots.append(root)
        elif item.get("name"):
            root = dict(item)
            root["_deps"] = [dict(dep) for dep in item.get("dependencies") or []
                             if isinstance(dep, Mapping)]
            roots.append(root)
    return roots


def parse_spack_find_d(text: str) -> Dict[str, Dict[str, Any]]:
    """``spack find -d --format '{hash} {name} {version} {prefix}'``, by hash."""
    found: Dict[str, Dict[str, Any]] = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 3 and re.fullmatch(r"[a-z0-9]{32}", parts[0]):
            found.setdefault(parts[0], {"name": parts[1], "version": parts[2],
                                        "prefix": parts[3] if len(parts) > 3 else None})
    return found


def _run_spack(executable: str, args: Sequence[str], root: Any = None) -> Tuple[int, str]:
    from jarvis_cd.core import spack_health

    try:
        completed = subprocess.run(
            [executable, *args], capture_output=True, text=True, timeout=_SPACK_TIMEOUT,
            stdin=subprocess.DEVNULL, env=spack_health.environment(root),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return completed.returncode, completed.stdout or ""


#: ``gate(spack_item) -> {"status": ..., "issue_id"?}`` (see spack_health.gate)
SpackGate = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def spack_copies(name: str, searched: List[str],
                 roots: Optional[Sequence[Mapping[str, Any]]] = None,
                 gate: Optional[SpackGate] = None,
                 blocked: Optional[List[str]] = None,
                 ) -> List[Dict[str, Any]]:
    """The installs of ``name`` in the site's Spacks (any version).

    With ``gate`` each Spack is probed first: a broken one (``spack_broken``,
    its issue id appended to ``blocked``) or a skipped one is not searched,
    so nothing from it is offered.
    """
    roots = site_spack_roots() if roots is None else roots
    if not roots:
        searched.append("site Spack: none found")
        return []
    copies: List[Dict[str, Any]] = []
    seen: set = set()
    for item in roots:
        root, executable = str(item["root"]), str(item.get("executable")
                                                 or Path(str(item["root"])) / "bin" / "spack")
        if gate is not None:
            state = gate(item)
            if state.get("status") == "broken":
                searched.append(f"site Spack {root}: not searched, it is broken "
                                f"(spack_broken {state.get('issue_id')})")
                if blocked is not None:
                    blocked.append(str(state.get("issue_id")))
                continue
            if state.get("status") == "skipped":
                searched.append(f"site Spack {root}: skipped (the site's spack_broken answer)")
                continue
        rc, output = _run_spack(executable, ["find", "--json", name], root)
        try:
            nodes = _spack_nodes(json.loads(output)) if rc == 0 and output.strip() else []
        except ValueError:
            nodes = []
        searched.append(f"site Spack {root}: `spack find --json {name}` "
                        + (f"found {len(nodes)}" if nodes else f"found none (exit {rc})"))
        if not nodes:
            continue
        _, detail = _run_spack(executable, ["find", "-d", "--format",
                                            "{hash} {name} {version} {prefix}", name], root)
        known = parse_spack_find_d(detail)
        for node in nodes:
            full_hash = str(node.get("hash") or "")
            if not full_hash or full_hash in seen:
                continue
            seen.add(full_hash)
            mpi = None
            deps = []
            for dep in node.get("_deps") or []:
                dep_name = str(dep.get("name") or "")
                info = known.get(str(dep.get("hash") or ""), {})
                version = dep.get("version") or info.get("version")
                virtuals = (dep.get("parameters") or {}).get("virtuals") or []
                record = {"name": dep_name, "version": version,
                          "hash": str(dep.get("hash") or "")[:7] or None}
                deps.append(record)
                if mpi is None and ("mpi" in virtuals or dep_name.lower() in _MPI_NAMES):
                    mpi = {**record, "prefix": info.get("prefix")}
            compiler_raw = node.get("compiler")
            compiler: Mapping[str, Any] = compiler_raw if isinstance(compiler_raw, Mapping) else {}
            parameters_raw = node.get("parameters")
            parameters: Mapping[str, Any] = (parameters_raw if isinstance(parameters_raw, Mapping)
                                             else {})
            copies.append({
                "source": "spack",
                "name": name,
                "version": str(node.get("version") or "") or None,
                "modules": [],
                "prefix": known.get(full_hash, {}).get("prefix"),
                "spack_hash": full_hash,
                "spack_root": root,
                "spack_executable": executable,
                "compiler": (f"{compiler.get('name')}@{compiler.get('version')}"
                             if compiler.get("name") else None),
                "variants": {key: value for key, value in parameters.items()
                             if not isinstance(value, list) or value},
                "implies": {"mpi": mpi, "depends_on": deps},
                "loadable": True,
            })
    return copies


# ---------------------------------------------------------------------------
# Discovery: the resource graph
# ---------------------------------------------------------------------------


def graph_copies(name: str, searched: List[str], path: Optional[Path] = None
                 ) -> List[Dict[str, Any]]:
    """The copies of ``name`` the resource graph's ``software`` section lists."""
    import yaml

    from jarvis_cd.core.install_placement import _graph_file

    try:
        graph_path = _graph_file(path)
        graph = yaml.safe_load(graph_path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, yaml.YAMLError):
        searched.append("resource graph: none readable")
        return []
    section = graph.get(GRAPH_SECTION) if isinstance(graph, dict) else None
    entries = [item for item in section or [] if isinstance(item, Mapping)
               and _names_match(str(item.get("name") or ""), name)] \
        if isinstance(section, list) else []
    searched.append(f"resource graph {graph_path}: `{GRAPH_SECTION}` lists "
                    f"{len(entries)} cop{'y' if len(entries) == 1 else 'ies'} of {name}")
    copies = []
    for item in entries:
        modules = item.get("modules") or []
        modules = [str(m) for m in (modules if isinstance(modules, list) else [modules])]
        implies_raw = item.get("implies")
        implies: Mapping[str, Any] = implies_raw if isinstance(implies_raw, Mapping) else {}
        mpi = implies.get("mpi")
        if isinstance(mpi, str):
            mpi = _mpi_of_module(mpi) or {"name": mpi, "version": None}
        modulepath = item.get("modulepath")
        variants_raw = item.get("variants")
        copies.append({
            "source": "resource_graph",
            "graph": str(graph_path),
            "name": name,
            "label": item.get("label"),
            "version": str(item.get("version")) if item.get("version") is not None else None,
            "modules": modules,
            "modulepath": modulepath if isinstance(modulepath, (str, list)) else None,
            "prefix": item.get("prefix"),
            "spack_hash": item.get("hash"),
            "spack_root": item.get("spack_root"),
            "variants": dict(variants_raw) if isinstance(variants_raw, Mapping) else None,
            "implies": {**dict(implies), "mpi": mpi},
            "basis": item.get("basis"),
            "loadable": True,
        })
    return copies


def find_site_copies(name: str, *, graph: Optional[Path] = None,
                     spack_roots: Optional[Sequence[Mapping[str, Any]]] = None,
                     gate: Optional[SpackGate] = None,
                     blocked: Optional[List[str]] = None,
                     ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Every copy of ``name`` the site has, and what was searched."""
    searched: List[str] = []
    found = graph_copies(name, searched, graph)
    modules = module_copies(name, searched)
    spacks = spack_copies(name, searched, spack_roots, gate, blocked)
    for copy in modules:
        twin = next((item for item in found if item.get("modules")
                     and item["modules"][-1] == copy["modules"][-1]), None)
        if twin is not None:
            twin.setdefault("discovered", []).append("module")
            continue
        found.append(copy)
    for copy in spacks:
        twin = next((item for item in found if (
            item.get("spack_hash") and copy["spack_hash"].startswith(str(item["spack_hash"]))
        ) or (item.get("prefix") and item.get("prefix") == copy.get("prefix"))), None)
        if twin is not None:
            # the module of a site Spack install: keep its variants and deps
            twin.setdefault("spack", {"root": copy["spack_root"],
                                      "hash": copy["spack_hash"]})
            if twin.get("variants") is None:
                twin["variants"] = copy["variants"]
            if not (twin.get("implies") or {}).get("mpi"):
                twin.setdefault("implies", {})["mpi"] = copy["implies"]["mpi"]
            twin["implies"].setdefault("depends_on", copy["implies"]["depends_on"])
            twin.setdefault("compiler", copy.get("compiler"))
            continue
        found.append(copy)
    return found, searched


# ---------------------------------------------------------------------------
# Fit and recommendation
# ---------------------------------------------------------------------------


def evaluate(copy: Mapping[str, Any], needs: Needs) -> Tuple[bool, str]:
    """Whether ``copy`` fits ``needs``, and why (or why not)."""
    bad: List[str] = []
    good: List[str] = []
    root = needs.root
    if root.versions:
        spec = f"@{','.join(root.versions)}"
        if not copy.get("version"):
            bad.append(f"its version is unknown, and the package needs {needs.name}{spec}")
        elif versions_satisfied(copy["version"], root.versions):
            good.append(f"version {copy['version']} satisfies {spec}")
        else:
            bad.append(f"version {copy['version']} is outside {spec}")
    else:
        good.append(f"version {copy.get('version') or 'unknown'} (any version fits)")
    variants = copy.get("variants")
    for key, wanted in root.variants.items():
        need = (("+" if wanted else "~") + key) if isinstance(wanted, bool) else f"{key}={wanted}"
        if not isinstance(variants, Mapping):
            bad.append(f"its variants can't be checked (the package needs {need})")
            continue
        have = variants.get(key)
        if isinstance(wanted, bool):
            ok = have is not None and bool(have) == wanted
        else:
            values = have if isinstance(have, list) else [have]
            ok = str(wanted) in [str(value) for value in values]
        if ok:
            good.append(f"it has {need}")
        else:
            bad.append(f"it has {key}={have!r}, the package needs {need}")
    mpi = (copy.get("implies") or {}).get("mpi")
    mpi_need = needs.mpi
    site_mpi = needs.site_mpi
    uses = f"it uses {mpi['name']}@{mpi.get('version')}" if mpi else ""
    if mpi_need is not None:
        wants_named = (mpi_need.name or "").lower() != "mpi"
        if not mpi:
            bad.append(f"no MPI was found for it, and the package needs ^{mpi_need.text()}")
        elif wants_named and str(mpi.get("name")).lower() != str(mpi_need.name).lower():
            bad.append(f"it uses {mpi['name']}, the package needs ^{mpi_need.text()}")
        elif not versions_satisfied(mpi.get("version"), mpi_need.versions):
            bad.append(f"{uses}, the package needs ^{mpi_need.text()}")
        else:
            good.append(uses)
    elif site_mpi is not None and mpi:
        if str(mpi.get("name")).lower() != str(site_mpi.name).lower():
            bad.append(f"it uses {mpi['name']}, the site's recorded MPI is {site_mpi.text()}")
        elif not versions_satisfied(mpi.get("version"), site_mpi.versions):
            bad.append(f"{uses}, the site's recorded MPI is {site_mpi.text()}")
        elif not _same_install(mpi, needs.site_mpi_prefix):
            bad.append(f"{uses} at {mpi.get('prefix')}, the site's recorded MPI is at "
                       f"{needs.site_mpi_prefix}")
        else:
            good.append(f"{uses}, the site's MPI")
    elif mpi:
        good.append(uses)
    deps = (copy.get("implies") or {}).get("depends_on")
    for dep in needs.deps:
        if dep is needs.mpi or not dep.name:
            continue
        if not isinstance(deps, list):
            bad.append(f"its dependency ^{dep.text()} can't be checked")
            continue
        have = next((item for item in deps if _names_match(str(item.get("name")), dep.name)),
                    None)
        if have is None:
            bad.append(f"it does not depend on {dep.name}")
        elif not versions_satisfied(have.get("version"), dep.versions):
            bad.append(f"it has {dep.name}@{have.get('version')}, the package needs "
                       f"^{dep.text()}")
    if root.unparsed:
        bad.append(f"part of the query can't be checked: {' '.join(root.unparsed)}")
    if not copy.get("loadable", True):
        bad.append("its module did not load in a login shell")
    if bad:
        return False, "; ".join(bad)
    return True, "; ".join(good)


_SOURCE_RANK = {"resource_graph": 0, "module": 1, "spack": 2}


def _label(copy: Mapping[str, Any]) -> str:
    if copy.get("label"):
        return str(copy["label"])
    version = copy.get("version") or "?"
    if copy["source"] == "spack":
        return (f"{copy['name']} {version} in the site Spack {copy.get('spack_root')} "
                f"(/{str(copy.get('spack_hash'))[:7]})")
    modules = list(copy.get("modules") or [])
    text = f"{copy['name']} {version} (module {modules[-1] if modules else '?'}"
    if len(modules) > 1:
        text += ", after " + " ".join(modules[:-1])
    return text + ")"


def _constraint(copy: Mapping[str, Any]) -> str:
    if copy["source"] == "spack":
        return f"spack:/{str(copy['spack_hash'])[:7]}"
    modules = list(copy.get("modules") or [])
    if modules:
        return f"module:{modules[-1]}"
    return f"graph:{copy['name']}@{copy.get('version')}"


def _does(copy: Mapping[str, Any]) -> str:
    if copy["source"] == "spack" and not copy.get("modules"):
        return (f"use this site install: `spack load /{str(copy['spack_hash'])[:7]}` "
                "with the site Spack when the package runs; JARVIS builds nothing")
    return (f"use the site's copy: `module load {' '.join(copy.get('modules') or [])}` "
            "when the package runs; JARVIS builds nothing")


def candidates_for(copies: Sequence[Mapping[str, Any]], needs: Needs,
                   unusable: Optional[Mapping[str, str]] = None,
                   ) -> Tuple[List[Dict[str, Any]], str, str]:
    """The issue's candidates, the recommended id, and why.

    ``unusable`` maps the constraint of a copy that did not load here to
    the error; such a copy is listed, but does not fit.
    """
    candidates: List[Dict[str, Any]] = []
    for index, copy in enumerate(copies, start=1):
        fits, why = evaluate(copy, needs)
        if unusable and _constraint(copy) in unusable:
            fits, why = False, f"it did not load on this host: {unusable[_constraint(copy)]}"
        candidate = {
            "id": f"site-{index}",
            "constraint": _constraint(copy),
            "source": copy["source"],
            "label": _label(copy),
            "version": copy.get("version"),
            "modules": list(copy.get("modules") or []),
            "implies": dict(copy.get("implies") or {}),
            "prefix": copy.get("prefix"),
            "fits": fits,
            "why": why,
            "does": _does(copy),
        }
        for key in ("modulepath", "module_dir", "spack_root", "spack_hash", "spack", "graph",
                    "basis", "compiler", "flavor", "module_file"):
            if copy.get(key) is not None:
                candidate[key] = copy[key]
        candidates.append(candidate)
    fitting = [c for c in candidates if c["fits"]]
    if fitting:
        # a person-confirmed copy first, then modules, then site Spack; newest first
        ranked = sorted(fitting, key=lambda c: _vkey(c.get("version") or ""), reverse=True)
        best = sorted(ranked, key=lambda c: _SOURCE_RANK.get(c["source"], 9))[0]
        recommended, reason = best["id"], f"{best['label']} fits: {best['why']}"
    else:
        recommended = "spack_build"
        reason = "no site copy fits the package's needs: " + "; ".join(
            f"{c['label']}: {c['why']}" for c in candidates)
    spack_why = ("build it with Spack, as the query asks"
                 if recommended == "spack_build" else
                 "a site copy fits; building with Spack is still possible")
    candidates.append({
        "id": "spack_build",
        "constraint": SPACK_BUILD,
        "source": "spack_build",
        "label": f"build {' '.join(shlex.split(needs.query))} with Spack",
        "version": None,
        "modules": [],
        "implies": {},
        "fits": True,
        "why": spack_why,
        "does": ("plan and build the query with Spack (find-first: what Spack already "
                 "has is reused, only missing nodes are built)"),
    })
    for candidate in candidates:
        candidate["recommended"] = candidate["id"] == recommended
    return candidates, recommended, reason


def site_copy_issue(package: str, needs: Needs, copies: Sequence[Mapping[str, Any]],
                    searched: Sequence[str], pkg_ids: Sequence[str],
                    decisions_path: Optional[Path] = None,
                    unusable: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """The answerable ``site_copy`` question for ``package`` (a Spack name)."""
    candidates, recommended, reason = candidates_for(copies, needs, unusable)
    marked = sorted(f"unusable:{c['constraint']}" for c in candidates
                    if unusable and c["constraint"] in unusable)
    labels = "; ".join(c["label"] for c in candidates if c["id"] != "spack_build")
    pick = next(c for c in candidates if c["id"] == recommended)
    count = len(candidates) - 1
    issue = make_issue(
        kind=KIND, package=package, subject=SUBJECT, scope="package",
        question=(
            f"This site already has {count} cop{'y' if count == 1 else 'ies'} of "
            f"{package}: {labels}. Use the site's copy, or build "
            f"{' '.join(shlex.split(needs.query))} with Spack? Recommended: "
            f"{pick['label']} ({reason})."
        ),
        candidates=candidates,
        evidence=list(searched),
        answerable=True,
        hint=("answers.json: {issue_id: '<candidate id or constraint>'} or {issue_id: "
              "{constraint, answered_by, basis}}; the answer is recorded for this site "
              f"and package; `jarvis ppl install --forget {package}` asks again"),
        identity_keys=([str(c["constraint"]) for c in candidates] + [f"needs:{needs.key()}"]
                       + marked),
    )
    issue.update(
        schema_site_copies=SCHEMA,
        method=METHOD,
        spec_name=package,
        query=needs.query,
        needs=needs.to_dict(),
        pkg_ids=list(pkg_ids),
        recommended=recommended,
        recommendation_reason=reason,
        searched=list(searched),
    )
    if decisions_path is not None:
        issue["decisions_path"] = str(decisions_path)
    return issue


def none_fact(package: str, needs: Needs, searched: Sequence[str],
              pkg_ids: Sequence[str]) -> Dict[str, Any]:
    """The ``site_copy_none`` fact: nothing found, Spack builds it."""
    fact = make_issue(
        kind=NONE_KIND, package=package, subject=SUBJECT, scope="package",
        question=(f"No site copy of {package} was found (modules, site Spack and "
                  f"resource graph searched); JARVIS installs "
                  f"{' '.join(shlex.split(needs.query))} with Spack."),
        detail=" | ".join(searched),
        evidence=list(searched),
        answerable=False,
    )
    fact.update(schema_site_copies=SCHEMA, fact=True, spec_name=package,
                query=needs.query, needs=needs.to_dict(), pkg_ids=list(pkg_ids),
                searched=list(searched), method="spack")
    return fact


# ---------------------------------------------------------------------------
# Resolution (called by Installer.install_all before the Spack group plans)
# ---------------------------------------------------------------------------


def _config(pkg_def: Mapping[str, Any]) -> Dict[str, Any]:
    config = pkg_def.get("config")
    return config if isinstance(config, dict) else {}


def search_enabled(pkg_def: Mapping[str, Any]) -> bool:
    if os.environ.get(SWITCH_ENV, "").strip().lower() in ("off", "0", "false", "no"):
        return False
    value = _config(pkg_def).get("install_site_search")
    return not (value is False or str(value).strip().lower() in ("off", "false", "0", "no"))


def needs_search(ppl, pkg_def: Mapping[str, Any]) -> bool:
    """A Spack package with a query, not yet resolved here, search on."""
    from jarvis_cd.core.installer import Installer

    config = _config(pkg_def)
    if Installer._resolve_install_method(ppl, dict(pkg_def)) != "spack":
        return False
    if not str(config.get("install_query") or config.get("install") or "").strip():
        return False
    if config.get("install_site_choice"):
        return False
    return search_enabled(pkg_def)


def _site_payload(candidate: Mapping[str, Any]) -> Dict[str, Any]:
    keys = ("source", "label", "version", "modules", "modulepath", "prefix", "spack_root",
            "spack_hash", "spack", "constraint", "implies", "graph")
    return {key: candidate[key] for key in keys if candidate.get(key) is not None}


def apply_choice(pkg_def: Dict[str, Any], choice: str, site: Optional[Mapping[str, Any]],
                 why: str, issue_id: Optional[str]) -> None:
    config = pkg_def.setdefault("config", {})
    previous = (config.get("install_site_choice") or {}).get("previous_method") \
        or config.get("install_method")
    if previous == METHOD:
        previous = None
    config["install_site_choice"] = {"choice": choice, "why": why, "issue_id": issue_id,
                                     "previous_method": previous}
    if choice != SPACK_BUILD and site is not None:
        config["install_method"] = METHOD
        config["install_site"] = dict(site)
    elif config.get("install_method") == METHOD:
        # a copy chosen earlier did not load: back to the Spack build
        config["install_method"] = previous or SPACK_BUILD
        config.pop("install_site", None)


def forget_choices(ppl, forgotten: Sequence[Mapping[str, Any]]) -> List[str]:
    """Undo the site-copy choices of forgotten answers in the pipeline's config.

    Returns the ``pkg_id`` of each package reset (it is asked again).
    """
    issue_ids = {str(entry.get("issue_id")) for entry in forgotten
                 if entry.get("kind") == KIND and entry.get("issue_id")}
    reset: List[str] = []
    for pkg_def in getattr(ppl, "packages", []) or []:
        config = _config(pkg_def)
        choice = config.get("install_site_choice")
        if not isinstance(choice, Mapping) or str(choice.get("issue_id")) not in issue_ids:
            continue
        config.pop("install_site_choice", None)
        config.pop("install_site", None)
        if config.get("install_method") == METHOD:
            config["install_method"] = choice.get("previous_method") or SPACK_BUILD
        reset.append(pkg_key(pkg_def))
    return reset


def _recorded(decisions: Decisions, package: str, needs: Needs) -> Optional[Dict[str, Any]]:
    for entry in decisions.package_entries(package):
        if entry.get("kind") == KIND and entry.get("needs") == needs.key():
            if entry["constraint"] == SPACK_BUILD or isinstance(entry.get("site"), Mapping):
                return entry
    return None


def pkg_key(pkg_def: Mapping[str, Any]) -> str:
    """The key the Spack installer files a package under (its ``pkg_id``)."""
    from jarvis_cd.core.install_placement import spec_name

    return str(pkg_def.get("pkg_id") or pkg_def.get("pkg_name") or spec_name(dict(pkg_def)))


def spack_gate(context, decisions: Decisions) -> SpackGate:
    """Probe each site Spack once before anything is taken from it."""
    from jarvis_cd.core import spack_health

    def gate(item: Mapping[str, Any]) -> Mapping[str, Any]:
        return spack_health.gate(item["root"], item.get("executable"), context, decisions)

    return gate


def _context_dict(context, name: str) -> Dict[str, Any]:
    value = getattr(context, name, None)
    if not isinstance(value, dict):
        value = {}
        setattr(context, name, value)
    return value


def _site_unusable(context) -> Dict[str, Any]:
    """constraint -> why the copy did not load here (this process)."""
    return _context_dict(context, "site_unusable")


def _site_loads(context) -> Dict[str, Any]:
    """pkg_id -> the env delta its site copy loaded (verified before the run)."""
    return _context_dict(context, "site_loads")


def _needs(query: str, site_mpi_entry: Mapping[str, Any]) -> Needs:
    site_key = str(site_mpi_entry.get("candidate_key") or "")
    return parse_needs(query, site_mpi_entry.get("constraint"),
                       site_key[len("prefix:"):] if site_key.startswith("prefix:") else None)


def _record_site_choice(decisions: Decisions, package: str, needs: Needs,
                        issue: Mapping[str, Any], answer: Mapping[str, Any],
                        site: Optional[Mapping[str, Any]]) -> None:
    """Replace the package's ``site_copy`` answer for ``needs`` with ``answer``."""
    entry: Dict[str, Any] = {
        "kind": KIND, "subject": SUBJECT, "issue_id": issue["issue_id"],
        "constraint": answer["constraint"], "needs": needs.key(),
        "answered_at": answer.get("answered_at") or _utc_now(),
        "via": issue.get("kind"),
    }
    for key in ("answered_by", "basis", "candidate_id"):
        if answer.get(key) is not None:
            entry[key] = answer[key]
    if site is not None:
        entry["site"] = dict(site)
    with decisions.locked():
        kept = [item for item in decisions.data["packages"].get(package, []) or []
                if not (isinstance(item, Mapping) and item.get("kind") == KIND
                        and item.get("needs") == needs.key())]
        decisions.data["packages"][package] = kept + [entry]
        decisions.save()


def _forget_site_choice(decisions: Decisions, package: str, needs: Needs) -> None:
    with decisions.locked():
        kept = [item for item in decisions.data["packages"].get(package, []) or []
                if not (isinstance(item, Mapping) and item.get("kind") == KIND
                        and item.get("needs") == needs.key())]
        if kept:
            decisions.data["packages"][package] = kept
        else:
            decisions.data["packages"].pop(package, None)
        decisions.save()


def resolve_site_copies(ppl, context) -> Dict[str, Any]:
    """Find site copies for each Spack package and apply the recorded answer.

    Returns ``{"packages": {pkg_id: summary}, "facts": [...], "held": [pkg_id]}``:
    ``held`` packages have an open ``site_copy`` question (raised on
    ``context``) and are not planned with Spack this run.
    """
    from jarvis_cd.core.installer import plan_only_active
    from jarvis_cd.core.install_placement import spec_name

    summary: Dict[str, Any] = {"schema_version": SCHEMA, "packages": {}, "facts": [],
                               "held": []}
    wanted = [pkg_def for pkg_def in getattr(ppl, "packages", []) or []
              if needs_search(ppl, pkg_def)]
    if not wanted:
        return summary
    decisions = Decisions(context.decisions_path)
    gate = spack_gate(context, decisions)
    unusable = _site_unusable(context)
    site_mpi_entry = decisions.site("mpi") or {}
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for pkg_def in wanted:
        config = _config(pkg_def)
        query = str(config.get("install_query") or config.get("install") or "").strip()
        groups.setdefault((spec_name(pkg_def), query), []).append(pkg_def)
    searches: Dict[str, Tuple[List[Dict[str, Any]], List[str], List[str]]] = {}
    for (package, query), pkg_defs in groups.items():
        pkg_ids = [pkg_key(p) for p in pkg_defs]
        needs = _needs(query, site_mpi_entry)
        recorded = _recorded(decisions, package, needs)
        if recorded is not None:
            context.note_decision(recorded, recorded, source="decisions_file",
                                  path=decisions.path, package=package)
            for pkg_def in pkg_defs:
                apply_choice(pkg_def, recorded["constraint"], recorded.get("site"),
                             "the site's recorded answer", recorded.get("issue_id"))
            for pkg_id in pkg_ids:
                summary["packages"][pkg_id] = {
                    "status": "recorded", "choice": recorded["constraint"],
                    "issue_id": recorded.get("issue_id"), "spec_name": package}
            continue
        if plan_only_active():
            for pkg_id in pkg_ids:
                summary["packages"][pkg_id] = {
                    "status": "not_searched", "spec_name": package,
                    "why": "plan only: no answer recorded and nothing is probed"}
            continue
        if package not in searches:
            context.log(f"[{_utc_now()}] site copies: searching for {package}\n")
            blocked: List[str] = []
            found, searched_where = find_site_copies(package, gate=gate, blocked=blocked)
            searches[package] = (found, searched_where, blocked)
        copies, searched, blocked = searches[package]
        if blocked:
            # a broken site Spack: nothing is recommended until spack_broken is answered
            summary["held"].extend(pkg_ids)
            for pkg_id in pkg_ids:
                summary["packages"][pkg_id] = {
                    "status": "blocked", "spec_name": package, "blocked_by": "spack_broken",
                    "issues": list(blocked)}
            continue
        if not copies:
            fact = none_fact(package, needs, searched, pkg_ids)
            summary["facts"].append(fact)
            facts = getattr(context, "site_copy_facts", None)
            # the config is left as it is: the Spack path runs as before
            for pkg_id in pkg_ids:
                if isinstance(facts, dict):
                    facts[pkg_id] = fact["question"]
                summary["packages"][pkg_id] = {
                    "status": "none_found", "spec_name": package, "fact": fact["issue_id"]}
            context.log(f"[{_utc_now()}] site copies: {fact['question']}\n")
            continue
        issue = site_copy_issue(package, needs, copies, searched, pkg_ids,
                                context.decisions_path, unusable)
        for entry in decisions.package_entries(package):
            if entry.get("kind") == KIND and entry.get("constraint"):
                issue["supersedes"] = entry.get("issue_id")
                issue["previous_answer"] = entry.get("constraint")
                stale = {"issue_id": entry.get("issue_id"), "subject": SUBJECT,
                         "scope": "package", "constraint": entry.get("constraint"),
                         "path": str(decisions.path), "answered_at": entry.get("answered_at"),
                         "superseded_by": issue["issue_id"],
                         "reason": "the package's needs changed, so the question changed"}
                if stale not in context.stale_decisions:
                    context.stale_decisions.append(stale)
                break
        answer = context.answer_for(issue)
        if answer is None:
            context.raise_issue(issue)
            summary["held"].extend(pkg_ids)
            for pkg_id in pkg_ids:
                summary["packages"][pkg_id] = {
                    "status": "question", "spec_name": package, "issue_id": issue["issue_id"],
                    "recommended": issue["recommended"]}
            continue
        chosen = next(c for c in issue["candidates"] if c["constraint"] == answer["constraint"])
        site = None if chosen["constraint"] == SPACK_BUILD else _site_payload(chosen)
        decisions.record(issue, answer, {"candidate_id": chosen["id"], "needs": needs.key(),
                                         "site": site})
        for pkg_def in pkg_defs:
            apply_choice(pkg_def, chosen["constraint"], site, "the person's answer",
                         issue["issue_id"])
        for pkg_id in pkg_ids:
            summary["packages"][pkg_id] = {
                "status": "answered", "choice": chosen["constraint"], "spec_name": package,
                "issue_id": issue["issue_id"]}
    return summary


# ---------------------------------------------------------------------------
# The ``site`` install method: load the copy into the pipeline's env
# ---------------------------------------------------------------------------


_LOAD_SCRIPT = r'''
uses="$1"; mods="$2"; spack="$3"; hash="$4"
env -0
printf '\0@@SPLIT@@\0'
for d in $uses; do module use "$d" >/dev/null 2>&1 || exit 4; done
if [ -n "$mods" ]; then module load $mods || exit 3; fi
if [ -n "$hash" ]; then
  out=$("$spack" load --sh "/$hash") || exit 5
  eval "$out" || exit 5
fi
env -0
'''

#: login-shell noise that is not part of what a module load sets
_SKIP_VARS = {"PWD", "OLDPWD", "SHLVL", "_", "PS1", "BASH_EXECUTION_STRING"}


def _parse_env0(blob: str) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for item in blob.split("\0"):
        name, sep, value = item.partition("=")
        if sep and name and not name.startswith("BASH_FUNC_"):
            env[name] = value
    return env


def load_env_delta(site: Mapping[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """What loading ``site`` (its modules, or ``spack load``) changes in the env."""
    from jarvis_cd.core import spack_health
    from jarvis_cd.core.installer import _env_delta

    modules = [str(m) for m in site.get("modules") or []]
    uses = site.get("modulepath") or []
    uses = [uses] if isinstance(uses, str) else [str(u) for u in uses]
    spack_hash = ""
    executable = ""
    root = None
    if not modules and site.get("spack_hash"):
        spack_hash = str(site["spack_hash"])
        root = site.get("spack_root") or None
        executable = str(Path(str(root)) / "bin" / "spack") if root else "spack"
    if not modules and not spack_hash:
        return None, "the site copy names no modules and no site Spack install"
    try:
        completed = subprocess.run(
            ["bash", "-lc", _LOAD_SCRIPT, "_", " ".join(uses), " ".join(modules),
             executable, spack_hash],
            capture_output=True, text=True, timeout=_LOGIN_TIMEOUT, stdin=subprocess.DEVNULL,
            env=spack_health.environment(root),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if completed.returncode != 0 or "\0@@SPLIT@@\0" not in completed.stdout:
        stage = {3: "module load", 4: "module use", 5: "spack load"}.get(
            completed.returncode, "the login shell")
        return None, (f"{stage} failed (exit {completed.returncode}): "
                      + (completed.stderr or "").strip()[-2000:])
    before_text, _, after_text = completed.stdout.partition("\0@@SPLIT@@\0")
    before, after = _parse_env0(before_text), _parse_env0(after_text)
    for name in _SKIP_VARS:
        before.pop(name, None)
        after.pop(name, None)
    return _env_delta(before, after), completed.stderr or ""


def apply_delta(env: Dict[str, str], delta: Mapping[str, Any]) -> List[str]:
    """Apply ``delta`` to the pipeline env (path lists are prepended)."""
    changed = []
    for name, change in delta.items():
        if "set" in change:
            env[name] = change["set"]
        else:
            added = list(change.get("prepend") or [])
            current = [p for p in str(env.get(name) or os.environ.get(name, "")).split(os.pathsep)
                       if p]
            env[name] = os.pathsep.join(added + [p for p in current if p not in added])
        changed.append(name)
    return sorted(changed)


def site_of(pkg_def: Mapping[str, Any]) -> Dict[str, Any]:
    """The site copy a ``site`` package loads (chosen, or set in its config)."""
    config = _config(pkg_def)
    site = dict(config.get("install_site") or {}) if isinstance(
        config.get("install_site"), Mapping) else {}
    if not site.get("modules") and config.get("install_modules"):
        modules = config["install_modules"]
        site["modules"] = shlex.split(modules) if isinstance(modules, str) else list(modules)
        site.setdefault("source", "config")
    if not site.get("modulepath") and config.get("install_modulepath"):
        site["modulepath"] = config["install_modulepath"]
    return site


UNUSABLE_KIND = "site_copy_unusable"
FORGET = "forget"
_MAX_REANSWERS = 4


_STAGE = re.compile(r"^[\w ]+ failed \(exit -?\d+\): ")


def _short(text: str) -> str:
    from jarvis_cd.core.spack_health import error_line

    return error_line(_STAGE.sub("", text or "")) or "no error output"


def load_site(site: Mapping[str, Any], context, decisions: Decisions
              ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Load ``site`` here: ``(delta, None)``, or ``(None, failure)``.

    ``failure`` is ``{"why", "detail", "blocked"?}``; ``blocked`` is the
    open ``spack_broken`` issue id when the copy's Spack is broken.
    """
    from jarvis_cd.core import spack_health

    root = site.get("spack_root") if not site.get("modules") and site.get("spack_hash") else None
    if root:
        state = spack_health.gate(root, None, context, decisions)
        if state["status"] == "broken":
            return None, {"why": f"the site Spack {root} is broken (spack_broken)",
                          "detail": state["issue"].get("detail"),
                          "blocked": state["issue_id"]}
        if state["status"] == "skipped":
            return None, {"why": (f"the site's answer to spack_broken skips the site Spack "
                                  f"{root}, which this copy is in"), "detail": None}
    prefix = site.get("prefix")
    if prefix and not os.path.isdir(str(prefix)):
        return None, {"why": f"its prefix {prefix} is not there on this host", "detail": None}
    delta, output = load_env_delta(site)
    if delta is not None:
        return delta, None
    modules = " ".join(site.get("modules") or [])
    if modules:
        why = f"`module load {modules}` failed in a login shell on this host: {_short(output)}"
    elif site.get("spack_hash"):
        why = (f"`spack load /{str(site['spack_hash'])[:7]}` with the site Spack "
               f"{site.get('spack_root') or '(on PATH)'} failed on this host: {_short(output)}")
    else:
        why = output
    return None, {"why": why, "detail": output}


def unusable_issue(pkg_def: Mapping[str, Any], site: Mapping[str, Any],
                   failure: Mapping[str, Any], context, decisions: Decisions,
                   ) -> Dict[str, Any]:
    """The ``site_copy_unusable`` issue: the chosen copy does not load here.

    Answerable when the copy was the person's choice: the site's other
    copies, the Spack build (with an ``install_query``) and ``forget``.
    A copy set in the pipeline's own config is a fact to fix there.
    """
    from jarvis_cd.core.install_placement import spec_name

    package = spec_name(dict(pkg_def))
    config = _config(pkg_def)
    query = str(config.get("install_query") or config.get("install") or "").strip()
    choice = config.get("install_site_choice")
    chosen = isinstance(choice, Mapping) and bool(choice.get("issue_id"))
    failing = str(site.get("constraint") or "")
    label = site.get("label") or failing or package
    candidates: List[Dict[str, Any]] = []
    recommended, reason = "", ""
    searched: List[str] = []
    needs = _needs(query or package, decisions.site("mpi") or {})
    if chosen:
        copies, searched = find_site_copies(package, gate=spack_gate(context, decisions))
        others = [c for c in copies if _constraint(c) != failing]
        candidates, recommended, reason = candidates_for(others, needs, _site_unusable(context))
        if not query:
            candidates = [c for c in candidates if c["id"] != "spack_build"]
        candidates.append({
            "id": FORGET, "constraint": FORGET, "source": "forget",
            "label": "forget the recorded choice and be asked again",
            "version": None, "modules": [], "implies": {}, "fits": True,
            "why": "the site_copy question is asked again, with this copy marked as not "
                   "loading here",
            "does": "drop the site's recorded site_copy answer for this package",
        })
        if not any(c["id"] == recommended for c in candidates):
            recommended = FORGET
            reason = "no other site copy fits, and the package has no Spack query"
        for candidate in candidates:
            candidate["recommended"] = candidate["id"] == recommended
        pick = next(c for c in candidates if c["id"] == recommended)
        question = (f"{package} uses the site's copy {label}, but {failure['why']}. Use "
                    "another of the site's copies, build it with Spack, or forget the choice "
                    f"and be asked again? Recommended: {pick['label']} ({reason}).")
        hint = ("answers.json: {issue_id: '<candidate id or constraint>'} or {issue_id: "
                "{constraint, answered_by, basis}}; a copy or the Spack build replaces the "
                "site's recorded site_copy answer for this package")
    else:
        question = (f"{package} uses the site's copy {label} set in the pipeline's config, "
                    f"but {failure['why']}.")
        hint = ("set the package's install_modules (and install_modulepath) to a copy that "
                "loads here, or its install_method to another method, in the pipeline")
    issue = make_issue(
        kind=UNUSABLE_KIND, package=package, subject=SUBJECT, scope="package",
        question=question, candidates=candidates, detail=str(failure.get("detail") or ""),
        evidence=list(searched), answerable=chosen, hint=hint,
        identity_keys=[f"failing:{failing or label}", f"needs:{needs.key()}",
                       *(str(c["constraint"]) for c in candidates)],
    )
    issue.update(schema_site_copies=SCHEMA, method=METHOD, spec_name=package, query=query,
                 needs=needs.to_dict(), pkg_ids=[pkg_key(pkg_def)], failing=failing,
                 failing_kind=("module" if site.get("modules") else
                               "spack" if site.get("spack_hash") else "other"),
                 why=failure["why"], site=dict(site))
    if chosen and isinstance(choice, Mapping):
        issue.update(recommended=recommended, recommendation_reason=reason,
                     replaces=choice.get("issue_id"))
    return issue


def verify_site_choices(ppl, context) -> Dict[str, Any]:
    """Load each ``site`` package's copy once, before anything is installed.

    A copy that loads is cached for :func:`site_run`. One that does not is
    the ``site_copy_unusable`` issue; its answer (another copy, the Spack
    build, or ``forget``) is applied here. Returns ``{"packages", "held",
    "resolved"}``: held packages wait for an answer this run.
    """
    from jarvis_cd.core.installer import Installer, plan_only_active
    from jarvis_cd.core.install_placement import spec_name

    summary: Dict[str, Any] = {"packages": {}, "held": [], "resolved": []}
    if plan_only_active():
        return summary
    decisions = Decisions(context.decisions_path)
    loads = _site_loads(context)
    for pkg_def in list(getattr(ppl, "packages", []) or []):
        key = pkg_key(pkg_def)
        for _ in range(_MAX_REANSWERS):
            if Installer._resolve_install_method(ppl, dict(pkg_def)) != METHOD:
                break
            site = site_of(pkg_def)
            delta, failure = load_site(site, context, decisions)
            if delta is not None:
                loads[key] = delta
                summary["packages"][key] = {"status": "loads",
                                            "choice": site.get("constraint")}
                break
            assert failure is not None
            if failure.get("blocked"):
                summary["held"].append(key)
                summary["packages"][key] = {"status": "blocked", "blocked_by": "spack_broken",
                                            "issues": [failure["blocked"]]}
                break
            issue = unusable_issue(pkg_def, site, failure, context, decisions)
            answer = context.answer_for(issue) if issue["answerable"] else None
            if answer is None:
                context.raise_issue(issue)
                summary["held"].append(key)
                summary["packages"][key] = {"status": "question", "issue_id": issue["issue_id"],
                                            "recommended": issue.get("recommended")}
                break
            package = spec_name(dict(pkg_def))
            needs = _needs(issue["query"] or package, decisions.site("mpi") or {})
            chosen = next(c for c in issue["candidates"]
                          if c["constraint"] == answer["constraint"])
            context.log(f"[{_utc_now()}] {UNUSABLE_KIND} {issue['issue_id']} answered: "
                        f"{chosen['constraint']}\n")
            summary["packages"][key] = {"status": "answered", "issue_id": issue["issue_id"],
                                        "choice": chosen["constraint"]}
            if chosen["constraint"] == FORGET:
                _forget_site_choice(decisions, package, needs)
                forget_choices(ppl, [{"kind": KIND, "issue_id": _config(pkg_def)
                                      .get("install_site_choice", {}).get("issue_id")}])
                _site_unusable(context)[issue["failing"]] = failure["why"]
                resolved = resolve_site_copies(ppl, context)
                summary["resolved"].append(resolved)
                if key in resolved["held"]:
                    summary["held"].append(key)
                    break
                continue
            site_payload = None if chosen["constraint"] == SPACK_BUILD else _site_payload(chosen)
            _record_site_choice(decisions, package, needs, issue, answer, site_payload)
            apply_choice(pkg_def, chosen["constraint"], site_payload,
                         f"the person's answer to {UNUSABLE_KIND}", issue["issue_id"])
        else:
            summary["held"].append(key)
            summary["packages"][key] = {"status": "unresolved",
                                        "why": f"{_MAX_REANSWERS} answers and still no copy "
                                               "that loads"}
    return summary


def site_run(ppl, pkg_list: Sequence[Mapping[str, Any]], context, mode: str) -> Dict[str, Any]:
    """Load each ``site`` package's copy into ``ppl.env``; nothing is built.

    The copy :func:`verify_site_choices` loaded is reused; one that does not
    load is the ``site_copy_unusable`` issue (the package waits for it).
    """
    from jarvis_cd.core.method_install import package_id

    result: Dict[str, Any] = {"method": METHOD, "mode": mode, "status": "satisfied",
                              "packages": {}}
    env = getattr(ppl, "env", None)
    loads = _site_loads(context)
    decisions: Optional[Decisions] = None
    for pkg_def in pkg_list:
        package = package_id(pkg_def)
        site = site_of(pkg_def)
        delta = loads.pop(pkg_key(pkg_def), None)
        if delta is None:
            what = " ".join(site.get("modules") or []) or f"/{site.get('spack_hash')}"
            context.log(f"[{_utc_now()}] {package}: loading the site's copy ({what})\n")
            decisions = decisions or Decisions(context.decisions_path)
            delta, failure = load_site(site, context, decisions)
            if delta is None:
                assert failure is not None
                issue_id = failure.get("blocked")
                if not issue_id:
                    issue = unusable_issue(pkg_def, site, failure, context, decisions)
                    context.raise_issue(issue)
                    issue_id = issue["issue_id"]
                result["status"] = "question"
                result["packages"][package] = {"status": "question", "issue_id": issue_id,
                                               "why": failure["why"]}
                continue
        changed = apply_delta(env, delta) if env is not None else sorted(delta)
        result["packages"][package] = {
            "status": "satisfied", "source": site.get("source"), "label": site.get("label"),
            "modules": list(site.get("modules") or []), "prefix": site.get("prefix"),
            "env_changed": changed,
        }
    return result
