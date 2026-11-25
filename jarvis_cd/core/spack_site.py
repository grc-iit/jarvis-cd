"""Spack site preparation: ``jarvis spack prepare``.

Prepares the jarvis Spack (``JARVIS_SPACK_ROOT``) for a site so installs
reuse what the site already has:

* ``upstreams.yaml``: the site Spack's install tree (and its module trees);
* ``config.yaml``, ``concretizer.yaml`` (``reuse: true``), ``bootstrap.yaml``;
* ``spack compiler find`` and ``spack external find --not-buildable`` for
  build tools (never an MPI);
* ``packages:mpi:buildable:false`` and the site's module MPIs as externals
  (``prefix`` + ``modules``), so the install's ``ambiguous_provider``
  question offers them;
* ``packages:all:compiler``: full toolchains (cc, cxx, fc) first, the one the
  upstream MPI was built with ahead. When several qualify and nobody chose,
  it is an ``ambiguous_compiler`` question (exit 3) instead of a guess.

Everything goes to the root's site scope ``<root>/etc/spack`` (the scope that
survives ``SPACK_DISABLE_LOCAL_CONFIG``). It is light, short work, safe on a
login node: a shallow clone when the root is absent, config writes,
``compiler find``, ``external find`` and a ``module load`` probe in a
throwaway login shell. It never installs or builds. The result is
``<root>/var/jarvis/site-prep.json`` (``jarvis.spack-site-prep.v1``), which
install records and provenance reference.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence

import yaml

from jarvis_cd.core.issues import JarvisIssueError, make_jarvis_issue
from jarvis_cd.core.spack_install import (
    BINDING_COMPILER_SOURCES,
    DECISION_NEEDED_EXIT_CODE,
    MPI_PROVIDERS,
    _atomic_write_text,
    _utc_now,
    compiler_requirement,
    make_issue,
    spack_checkout_version,
    spack_subprocess_environment,
)

SITE_PREP_SCHEMA = "jarvis.spack-site-prep.v1"
SITE_PREP_RECORD = Path("var") / "jarvis" / "site-prep.json"
COMPILER_CHOICE = Path("var") / "jarvis" / "compiler-choice"
#: ``build_jobs`` meaning "every CPU the build job may use"
ALL_CPUS_BUILD_JOBS = 1024

#: Spack release tags checked against github.com/spack/spack. A release
#: missing here is still cloned at its tag; the record says it is unpinned.
SPACK_RELEASE_PINS = {
    "0.21.3": "6467ddefa3aa41629f9ca04dffab24b01e7d20b2",
    "0.22.2": "594a376c521cc746978571b1181a47bbcff30a21",
    "0.22.5": "8c37ed4534414d99501a91e64e40718e271ff8a2",
    "0.23.0": "c6d4037758140fe15913c29e80cd1547f388ae51",
    "0.23.1": "2bfcc69fa870d3c6919be87593f22647981b648a",
    "1.0.2": "734c5db2121b01c373eed6538e452f18887e9e44",
    "1.1.0": "0c2be44e4ece21eb091ad5de4c97716b7c6d4c87",
}

#: executable-only build tools externalized by default; MPI never is
DEFAULT_BUILD_TOOLS = (
    "autoconf", "automake", "bison", "cmake", "diffutils", "findutils", "flex",
    "gawk", "git", "gmake", "libtool", "m4", "ninja", "perl", "pkgconf", "tar",
)
_COMPILER_FAMILIES = ("gcc", "clang", "llvm", "oneapi", "intel-oneapi-compilers",
                      "xl", "nag", "fj", "aocc")
_PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")
_RELEASE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")
_COMPILER = re.compile(r"^(?:auto|[a-z][a-z0-9_-]{0,49}(?:@=?[0-9][A-Za-z0-9._:-]{0,49})?)$")
_MODULE_VERSION = re.compile(r"^[0-9][A-Za-z0-9._-]*$")


def _refuse(kind: str, message: str, *, field: Optional[str] = None,
            **details: Any) -> JarvisIssueError:
    issue = make_jarvis_issue(
        kind=kind, subject="spack_site_prep", message=message,
        identity={key: str(value) for key, value in details.items()},
        field=field, details=details,
    )
    return JarvisIssueError([issue], document={"schema_version": SITE_PREP_SCHEMA})


def _version_key(version: str) -> tuple:
    return tuple(
        (0, int(part)) if part.isdigit() else (-1, part)
        for part in re.split(r"[.\-_]", version.lstrip("="))
    )


class _Spack:
    """Runs one Spack by path, isolated like every jarvis Spack call."""

    def __init__(self, root: Path, log: List[str]):
        self.executable = str(root / "bin" / "spack")
        self.root = root
        self.log = log

    def run(self, arguments: Sequence[str], timeout: int = 300) -> subprocess.CompletedProcess:
        with _jarvis_root(self.root):
            environment = spack_subprocess_environment()
        command = [self.executable, *arguments]
        self.log.append("$ " + shlex.join(command))
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=environment, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            self.log.append(f"timed out after {timeout}s")
            return subprocess.CompletedProcess(command, 124, "", "timeout")
        self.log.append(f"exit {completed.returncode}")
        return completed


@contextlib.contextmanager
def _jarvis_root(root: Path) -> Iterator[None]:
    previous = os.environ.get("JARVIS_SPACK_ROOT")
    os.environ["JARVIS_SPACK_ROOT"] = str(root)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("JARVIS_SPACK_ROOT", None)
        else:
            os.environ["JARVIS_SPACK_ROOT"] = previous


def _put(etc: Path, name: str, document: Mapping[str, Any], changes: List[str]) -> None:
    text = yaml.safe_dump(dict(document), sort_keys=False, default_flow_style=False)
    path = etc / name
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        changes.append(f"unchanged {name}")
        return
    _atomic_write_text(path, text)
    os.chmod(path, 0o644)
    changes.append(f"written {name}")


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return dict(loaded) if isinstance(loaded, Mapping) else {}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def site_install_tree(site: Path, log: List[str]) -> Path:
    """The site Spack's install tree (``spack.store.STORE.root``)."""
    completed = _Spack(site, log).run(
        ["python", "-c", "import spack.store; print(spack.store.STORE.root)"], timeout=60
    )
    lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
    if completed.returncode == 0 and lines and lines[-1].startswith("/"):
        return Path(lines[-1])
    return site / "opt" / "spack"


def login_module_path() -> str:
    """``$MODULEPATH`` as a login shell sets it."""
    try:
        completed = subprocess.run(
            ["bash", "-lc", 'printf "\\n@@MP=%s\\n" "${MODULEPATH-}"'],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    for line in completed.stdout.splitlines():
        if line.startswith("@@MP="):
            return line[5:]
    return ""


_MPI_PREFIX_SCRIPT = r'''
before=":$PATH:"
module use "$1" >/dev/null 2>&1
module load "$2" >/dev/null 2>&1 || exit 1
exe=$(command -v mpiexec || command -v mpirun) || exit 1
case "$exe" in /*/bin/mpiexec|/*/bin/mpirun) ;; *) exit 1 ;; esac
case "$before" in *":${exe%/*}:"*) exit 1 ;; esac
printf "\n@@PREFIX=%s\n" "${exe%/bin/*}"
'''


def module_mpi_prefix(directory: str, module: str) -> Optional[str]:
    """The prefix a module MPI puts first on PATH (a throwaway login shell)."""
    try:
        completed = subprocess.run(
            ["bash", "-lc", _MPI_PREFIX_SCRIPT, "_", directory, module],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    prefixes = [line[9:] for line in completed.stdout.splitlines() if line.startswith("@@PREFIX=")]
    return prefixes[-1] if prefixes and os.path.isdir(prefixes[-1]) else None


def module_mpis(module_paths: Sequence[str], skip: Sequence[Path],
                warnings: List[str]) -> List[Dict[str, str]]:
    """Site MPI modules (``<modulepath>/<mpi>/<version>[.lua]``) with prefixes.

    Spack's own module trees (``<root>/share/spack/...``) are skipped: the
    upstream already reaches them.
    """
    found: List[Dict[str, str]] = []
    skipped = [str(path / "share" / "spack") for path in skip]
    for directory in module_paths:
        directory = directory.rstrip("/")
        if not directory or not os.path.isdir(directory):
            continue
        if any(directory.startswith(prefix) for prefix in skipped):
            continue
        for name in MPI_PROVIDERS:
            mpi_dir = Path(directory) / name
            if not mpi_dir.is_dir():
                continue
            for entry in sorted(mpi_dir.iterdir()):
                version = entry.name[:-4] if entry.name.endswith(".lua") else entry.name
                if version.startswith(".") or version == "default":
                    continue
                if not _MODULE_VERSION.fullmatch(version):
                    continue
                module = f"{name}/{version}"
                prefix = module_mpi_prefix(directory, module)
                if prefix is None:
                    warnings.append(
                        f"MPI module {module} ({directory}): module load gave no "
                        "mpiexec prefix; not recorded"
                    )
                    continue
                found.append({"name": name, "version": version, "module": module,
                              "prefix": prefix, "modulepath": directory})
    return found


def toolchains(spack: _Spack, packages: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Compilers Spack knows: ``compilers:`` (0.x) or compiler externals (1.x)."""
    result: List[Dict[str, Any]] = []
    completed = spack.run(["config", "get", "compilers"], timeout=60)
    loaded = yaml.safe_load(completed.stdout or "") if completed.returncode == 0 else None
    entries = (loaded or {}).get("compilers") if isinstance(loaded, Mapping) else None
    for entry in entries or []:
        compiler = entry.get("compiler", {}) if isinstance(entry, Mapping) else {}
        if not compiler.get("spec"):
            continue
        name, _, version = str(compiler["spec"]).partition("@")
        paths = compiler.get("paths") or {}
        result.append({"spec": f"{name}@{version.lstrip('=')}", "cc": paths.get("cc"),
                       "cxx": paths.get("cxx"), "fc": paths.get("fc")})
    if not result:
        for name in ("gcc", "llvm", "intel-oneapi-compilers", "aocc"):
            for item in (packages.get(name) or {}).get("externals") or []:
                spec = str(item.get("spec", "")).split()[0] if item.get("spec") else ""
                paths = ((item.get("extra_attributes") or {}).get("compilers") or {})
                if spec:
                    result.append({"spec": spec.replace("@=", "@"), "cc": paths.get("c"),
                                   "cxx": paths.get("cxx"), "fc": paths.get("fortran")})
    unique: Dict[str, Dict[str, Any]] = {}
    for item in result:
        item["full"] = all(item.get(lang) for lang in ("cc", "cxx", "fc"))
        unique.setdefault(item["spec"], item)
    return list(unique.values())


def installed_mpis(spack: _Spack) -> List[Dict[str, Any]]:
    """Installed MPIs (local and upstream) with the compiler they used.

    One unconstrained ``spack find``, filtered to the MPI providers: naming
    each provider asks Spack about MPIs the site does not have, and a name
    the Spack release does not know fails the whole query.
    """
    mpis: List[Dict[str, Any]] = []
    completed = spack.run(
        ["find", "--format", "{name}@{version} {hash:7} {compiler} {prefix}"],
        timeout=60,
    )
    if completed.returncode != 0:
        return mpis
    for line in (completed.stdout or "").splitlines():
        parts = line.split()
        if len(parts) != 4 or "@" not in parts[0] or not parts[3].startswith("/"):
            continue
        name, _, version = parts[0].partition("@")
        if name not in MPI_PROVIDERS:
            continue
        mpis.append({"name": name, "version": version, "hash": parts[1],
                     "compiler": parts[2].replace("@=", "@"), "prefix": parts[3]})
    return mpis


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def _matching(request: str, chains: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    name, _, version = request.replace("@=", "@").partition("@")
    return [
        chain for chain in chains
        if chain["spec"].split("@")[0] == name
        and (not version or chain["spec"].split("@", 1)[1].startswith(version))
    ]


def compiler_policy(
    chains: List[Dict[str, Any]],
    mpis: Sequence[Mapping[str, Any]],
    request: str,
    choice_file: Optional[Path],
    answer: Optional[str],
) -> Dict[str, Any]:
    """Order the compilers and pick the preferred one (or ask)."""
    built = {mpi["compiler"] for mpi in mpis}
    for chain in chains:
        chain["built_upstream_mpi"] = chain["spec"] in built
    chains.sort(key=lambda chain: _version_key(chain["spec"].split("@", 1)[1]), reverse=True)
    chains.sort(key=lambda chain: (
        not chain["full"],
        not chain["built_upstream_mpi"],
        _COMPILER_FAMILIES.index(chain["spec"].split("@")[0])
        if chain["spec"].split("@")[0] in _COMPILER_FAMILIES else 99,
    ))
    chosen: Optional[Mapping[str, Any]] = None
    source = None
    candidates: List[str] = []
    warnings: List[str] = []
    if request == "auto":
        if choice_file is not None:
            choice_file.unlink(missing_ok=True)
        request = ""
    request = request or (answer or "")
    if request:
        found = _matching(request, chains)
        if len(found) != 1:
            names = ", ".join(chain["spec"] for chain in chains) or "none"
            what = "matches no" if not found else "matches more than one"
            raise _refuse(
                "compiler_not_found", f"compiler {request} {what} compiler Spack found "
                f"(found: {names})", field="--compiler", compiler=request,
            )
        chosen, source = found[0], ("answer" if answer and request == answer else "person")
        if choice_file is not None:
            _atomic_write_text(choice_file, chosen["spec"] + "\n")
    elif choice_file is not None and choice_file.is_file():
        earlier = choice_file.read_text(encoding="utf-8").strip()
        found = [chain for chain in chains if chain["spec"] == earlier]
        if found:
            chosen, source = found[0], "person_earlier"
        else:
            warnings.append(f"the earlier compiler choice {earlier} is gone; choosing again")
            choice_file.unlink(missing_ok=True)
    if chosen is None and chains:
        full = [chain for chain in chains if chain["full"]]
        qualifying = [chain for chain in full if chain["built_upstream_mpi"]] or full
        if len(qualifying) == 1:
            chosen = qualifying[0]
            source = "upstream_mpi" if chosen["built_upstream_mpi"] else "only_full_toolchain"
        elif qualifying:
            candidates = [chain["spec"] for chain in qualifying]
        else:
            chosen, source = chains[0], "no_full_toolchain"
            warnings.append("no compiler has cc, cxx and fc; Fortran packages cannot build")
    if chosen is not None and not chosen["full"] and source in ("person", "answer", "person_earlier"):
        warnings.append(f"{chosen['spec']} has no Fortran compiler; Fortran packages cannot build")
    order = [chosen["spec"]] + [c["spec"] for c in chains if c is not chosen] if chosen else []
    return {
        "chosen": chosen["spec"] if chosen else None,
        "source": source,
        "full_toolchain": bool(chosen and chosen["full"]),
        "order": order,
        "toolchains": chains,
        "choice_needed": bool(candidates),
        "candidates": candidates,
        "warnings": warnings,
    }


def ambiguous_compiler_issue(candidates: Sequence[str], chains: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    by_spec = {chain["spec"]: chain for chain in chains}
    return make_issue(
        kind="ambiguous_compiler",
        package="*",
        subject="compiler",
        scope="site",
        question="Several full compiler toolchains qualify; which should Spack prefer?",
        candidates=[
            {"id": f"compiler-{index + 1}", "constraint": spec,
             "built_upstream_mpi": bool(by_spec.get(spec, {}).get("built_upstream_mpi")),
             "cc": by_spec.get(spec, {}).get("cc"), "fc": by_spec.get(spec, {}).get("fc")}
            for index, spec in enumerate(candidates)
        ],
        evidence=["spack config get compilers",
                  "spack find --format '{name}@{version} {hash:7} {compiler} {prefix}' <mpis>"],
        answerable=True,
        hint="answer with --compiler <spec> or an answers.json entry for this issue_id",
    )


# ---------------------------------------------------------------------------
# Prepare
# ---------------------------------------------------------------------------


def _clone(root: Path, version: str, log: List[str]) -> None:
    if not root.parent.is_dir():
        root.parent.mkdir(parents=True, exist_ok=True)
    command = ["git", "-c", "advice.detachedHead=false", "clone", "--quiet", "--depth", "1",
               "--branch", f"v{version}", "https://github.com/spack/spack.git", str(root)]
    log.append("$ " + shlex.join(command))
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _refuse("spack_clone_failed", f"git clone of Spack v{version} into {root} "
                      f"failed: {error}", root=str(root), version=version)
    log.append(f"exit {completed.returncode}")
    if completed.returncode != 0:
        raise _refuse("spack_clone_failed", f"git clone of Spack v{version} into {root} "
                      f"failed: {completed.stderr.strip()[-512:]}", root=str(root),
                      version=version)


def _git_commit(root: Path) -> Optional[str]:
    try:
        completed = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                   capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() or None if completed.returncode == 0 else None


def prepare_site(
    *,
    root: Optional[str] = None,
    site_spack: Optional[str] = None,
    spack_version: Optional[str] = None,
    install_tree: Optional[str] = None,
    module_paths: Optional[Sequence[str]] = None,
    build_tools: Optional[Sequence[str]] = None,
    compiler: Optional[str] = None,
    build_jobs: Optional[int] = None,
    clone: bool = True,
    answers: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Prepare ``root`` (default ``JARVIS_SPACK_ROOT``); see the module doc.

    Returns the ``jarvis.spack-site-prep.v1`` record (also written to
    ``<root>/var/jarvis/site-prep.json``). ``record["exit_code"]`` is 3 when
    an ``ambiguous_compiler`` question is open. Refusals raise
    :class:`JarvisIssueError` (exit 5) and change nothing that was valid.
    """
    log: List[str] = []
    root_text = (root or os.environ.get("JARVIS_SPACK_ROOT", "")).strip()
    if not root_text:
        raise _refuse("spack_root_unset", "give --root or set JARVIS_SPACK_ROOT: the jarvis "
                      "Spack to prepare", field="JARVIS_SPACK_ROOT")
    spack_root = Path(os.path.expandvars(root_text)).expanduser().resolve()
    site: Optional[Path] = None
    site_version = None
    tree: Optional[Path] = None
    if site_spack:
        site = Path(os.path.expandvars(site_spack)).expanduser().resolve()
        if not (site / "bin" / "spack").is_file():
            raise _refuse("site_spack_invalid", f"no site Spack at {site} (no bin/spack)",
                          field="--site_spack", site_spack=str(site))
        if site == spack_root:
            raise _refuse("site_spack_invalid", "the site Spack cannot be the jarvis Spack "
                          "itself", field="--site_spack", site_spack=str(site))
        site_version = spack_checkout_version(site)
        tree = (Path(install_tree).expanduser() if install_tree
                else site_install_tree(site, log))
        if not (tree / ".spack-db" / "index.json").is_file():
            raise _refuse("site_install_tree_invalid", f"site install tree {tree} has no "
                          ".spack-db/index.json", field="--install_tree", tree=str(tree))
    version = spack_version or site_version
    if not version:
        raise _refuse("spack_version_unknown", "give --spack_version when the site has no "
                      "Spack (or its release cannot be read)", field="--spack_version")
    if not _RELEASE.fullmatch(version):
        raise _refuse("spack_version_invalid", f"bad Spack release {version}",
                      field="--spack_version", version=version)
    tools = list(DEFAULT_BUILD_TOOLS if build_tools is None else build_tools)
    for tool in tools:
        if not _PACKAGE_NAME.fullmatch(tool):
            raise _refuse("build_tool_invalid", f"build tool {tool!r} is not a Spack "
                          "package name", field="--build_tools", tool=tool)
        if tool in MPI_PROVIDERS or tool == "mpi":
            raise _refuse("build_tool_invalid", f"{tool} is an MPI; MPI is never "
                          "externalized from PATH", field="--build_tools", tool=tool)
    request = (compiler or "").strip()
    if request and not _COMPILER.fullmatch(request):
        raise _refuse("compiler_invalid", "compiler must be a Spack compiler spec such as "
                      "gcc@11.4.0, or auto", field="--compiler", compiler=request)
    jobs = ALL_CPUS_BUILD_JOBS if build_jobs is None else int(build_jobs)
    if not 1 <= jobs <= ALL_CPUS_BUILD_JOBS:
        raise _refuse("build_jobs_invalid", f"build_jobs must be 1..{ALL_CPUS_BUILD_JOBS}",
                      field="--build_jobs", build_jobs=jobs)

    if (spack_root / "bin" / "spack").is_file():
        have = spack_checkout_version(spack_root)
        if have != version:
            raise _refuse("spack_version_mismatch", f"the jarvis Spack at {spack_root} is "
                          f"{have}, not {version}; nothing changed", field="--spack_version",
                          root=str(spack_root), have=have, want=version)
        checkout = "present"
    elif spack_root.exists() and any(spack_root.iterdir()):
        raise _refuse("spack_root_not_spack", f"{spack_root} exists and is not a Spack "
                      "checkout; nothing changed", field="JARVIS_SPACK_ROOT",
                      root=str(spack_root))
    elif not clone:
        raise _refuse("spack_root_missing", f"{spack_root} has no Spack and --no_clone was "
                      "given", field="JARVIS_SPACK_ROOT", root=str(spack_root))
    else:
        _clone(spack_root, version, log)
        checkout = "cloned"
    commit = _git_commit(spack_root)
    pin = SPACK_RELEASE_PINS.get(version)
    if pin and commit and commit != pin:
        raise _refuse("spack_pin_mismatch", f"the jarvis Spack at {spack_root} is at "
                      f"{commit}, not the v{version} pin {pin}", root=str(spack_root),
                      commit=commit, pin=pin)

    etc = spack_root / "etc" / "spack"
    etc.mkdir(parents=True, exist_ok=True)
    changes: List[str] = []
    if site is not None and tree is not None:
        upstream: Dict[str, Any] = {"install_tree": str(tree)}
        modules = {kind: str(site / "share" / "spack" / sub)
                   for kind, sub in (("lmod", "lmod"), ("tcl", "modules"))
                   if (site / "share" / "spack" / sub).is_dir()}
        if modules:
            upstream["modules"] = modules
        _put(etc, "upstreams.yaml", {"upstreams": {"site": upstream}}, changes)
    _put(etc, "config.yaml", {"config": {
        "misc_cache": "$spack/var/jarvis/misc-cache",
        "test_stage": "$spack/var/jarvis/test-stage",
        "build_stage": ["$tempdir/$user/jarvis-spack-stage"],
        "build_jobs": jobs,
    }}, changes)
    _put(etc, "concretizer.yaml", {"concretizer": {"reuse": True}}, changes)
    _put(etc, "bootstrap.yaml", {"bootstrap": {
        "root": "$spack/var/jarvis/bootstrap", "trusted": {"spack-install": False},
    }}, changes)

    spack = _Spack(spack_root, log)
    found = spack.run(["compiler", "find", "--scope", "site"])
    if found.returncode != 0:
        raise _refuse("spack_compiler_find_failed", "spack compiler find failed: "
                      + (found.stderr or found.stdout or "").strip()[-512:],
                      root=str(spack_root))
    if tools:
        externals = spack.run(["external", "find", "--not-buildable", "--scope", "site", *tools])
        if externals.returncode != 0:
            raise _refuse("spack_external_find_failed", "spack external find failed: "
                          + (externals.stderr or externals.stdout or "").strip()[-512:],
                          root=str(spack_root))

    warnings: List[str] = []
    paths = list(module_paths) if module_paths else login_module_path().split(":")
    skip = [spack_root] + ([site] if site is not None else [])
    module_candidates = module_mpis(paths, skip, warnings)

    packages_path = etc / "packages.yaml"
    document = _load_yaml(packages_path)
    packages = dict(document.get("packages") or {})
    before = json.dumps(packages, sort_keys=True, default=str)
    policy_changes: List[str] = []
    mpi = dict(packages.get("mpi") or {})
    if mpi.get("buildable") is not False:
        mpi["buildable"] = False
        policy_changes.append("packages:mpi:buildable:false")
    packages["mpi"] = mpi
    added: List[str] = []
    for candidate in module_candidates:
        spec = f"{candidate['name']}@{candidate['version']}"
        settings = dict(packages.get(candidate["name"]) or {})
        externals = list(settings.get("externals") or [])
        same = [item for item in externals if str(item.get("spec", "")).split()[:1] == [spec]]
        if not same:
            externals.append({"spec": spec, "prefix": candidate["prefix"],
                              "modules": [candidate["module"]]})
            added.append(spec)
            policy_changes.append(f"packages:{candidate['name']}:externals {spec}")
        else:
            item = same[0]
            if not item.get("prefix"):
                item["prefix"] = candidate["prefix"]
                policy_changes.append(f"packages:{candidate['name']}:externals {spec} prefix")
            elif item["prefix"] != candidate["prefix"]:
                warnings.append(f"{spec} keeps prefix {item['prefix']}; module load gives "
                                f"{candidate['prefix']}")
            modules = list(item.get("modules") or [])
            if candidate["module"] not in modules:
                item["modules"] = modules + [candidate["module"]]
                policy_changes.append(f"packages:{candidate['name']}:externals {spec} modules")
        settings["externals"] = externals
        packages[candidate["name"]] = settings

    chains = toolchains(spack, packages)
    mpis = installed_mpis(spack)
    choice_file = spack_root / COMPILER_CHOICE
    probe = compiler_policy([dict(c) for c in chains], mpis, "", None, None)
    issue = None
    answer = None
    if probe["choice_needed"] and not request and not choice_file.is_file():
        issue = ambiguous_compiler_issue(probe["candidates"], probe["toolchains"])
        entry = (answers or {}).get(issue["issue_id"])
        if entry is not None:
            answer = str(entry.get("constraint"))
            if answer not in probe["candidates"]:
                raise _refuse("answer_invalid", f"answer for {issue['issue_id']} must be one "
                              "of the candidate constraints", field="answers.json")
            issue = None
    policy = compiler_policy(chains, mpis, request, choice_file, answer)
    warnings.extend(policy.pop("warnings"))
    if policy["choice_needed"] and issue is None:
        issue = ambiguous_compiler_issue(policy["candidates"], policy["toolchains"])
    everything = dict(packages.get("all") or {})
    if policy["order"] and list(everything.get("compiler") or []) != policy["order"]:
        everything["compiler"] = policy["order"]
        policy_changes.append("packages:all:compiler " + ",".join(policy["order"]))
    # A person's choice binds every build (a require); a jarvis pick only sets
    # the order. A require jarvis wrote earlier is dropped when the choice is
    # no longer the person's.
    try:
        previous = json.loads((spack_root / SITE_PREP_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = None
    previous_require = ((previous or {}).get("compiler") or {}).get("binding_require")
    binding = None
    if policy["chosen"] and policy["source"] in BINDING_COMPILER_SOURCES:
        binding = compiler_requirement(policy["chosen"], version)
    if binding is not None:
        if everything.get("require") != binding:
            everything["require"] = binding
            policy_changes.append("packages:all:require " + ",".join(binding))
    elif previous_require and everything.get("require") == previous_require:
        everything.pop("require")
        policy_changes.append("packages:all:require removed (no person choice)")
    policy["binding_require"] = binding
    if everything:
        packages["all"] = everything
    else:
        packages.pop("all", None)
    if json.dumps(packages, sort_keys=True, default=str) != before:
        document["packages"] = packages
        _atomic_write_text(packages_path, yaml.safe_dump(document, sort_keys=False))
        changes.append("written packages.yaml")
    else:
        changes.append("unchanged packages.yaml")

    record: Dict[str, Any] = {
        "schema_version": SITE_PREP_SCHEMA,
        "prepared_at": _utc_now(),
        "root": str(spack_root),
        "version": version,
        "commit": commit,
        "pinned": pin,
        "checkout": checkout,
        "config_scope": str(etc),
        "site_spack": str(site) if site else None,
        "site_version": site_version,
        "upstream_install_tree": str(tree) if tree else None,
        "build_jobs": jobs,
        "build_tools": tools,
        "changes": changes + policy_changes,
        "module_mpis": module_candidates,
        "mpi_externals_added": added,
        "installed_mpis": mpis,
        "compiler": policy,
        "warnings": warnings,
        "issues": [issue] if issue else [],
        "open_issues": [issue["issue_id"]] if issue else [],
        "environment": {"JARVIS_SPACK_ROOT": str(spack_root)},
        "commands": log,
        "exit_code": DECISION_NEEDED_EXIT_CODE if issue else 0,
        "status": "needs_decision" if issue else "prepared",
    }
    record_path = spack_root / SITE_PREP_RECORD
    record["record_path"] = str(record_path)
    _atomic_write_text(record_path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return record


def site_prep_record(root: Optional[str]) -> Optional[Dict[str, Any]]:
    """The root's ``site-prep.json`` summary, or None."""
    if not root:
        return None
    path = Path(root).expanduser() / SITE_PREP_RECORD
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {
        "path": str(path),
        "prepared_at": record.get("prepared_at"),
        "version": record.get("version"),
        "commit": record.get("commit"),
        "site_spack": record.get("site_spack"),
        "upstream_install_tree": record.get("upstream_install_tree"),
        "compiler": (record.get("compiler") or {}).get("chosen"),
        "compiler_source": (record.get("compiler") or {}).get("source"),
        "compiler_binding": (record.get("compiler") or {}).get("binding_require"),
        "status": record.get("status"),
    }
