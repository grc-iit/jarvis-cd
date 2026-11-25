"""Find-first pip, conda and container installs, and the install method choice.

Each method works like the Spack installer. ``check`` reports per package
whether what it needs is already there (``satisfied``) or raises a
``not_installed`` issue. ``install`` builds only what is missing, inside an
allocation (``ensure_may_build``), and a failure is a typed
:class:`~jarvis_cd.core.spack_install.InstallFailed`. Whatever JARVIS cannot
detect is a typed question answered through ``answers.json`` and recorded in
``decisions.yaml``, never a silent default:

* ``python_missing``: no Python that can make the pip venv (JARVIS's own
  interpreter comes first, as it is on every node JARVIS runs on);
* ``conda_select`` / ``conda_missing``: several condas, or none (then one
  candidate is Miniforge, installed into the site home once the person says so);
* ``container_engine`` / ``container_engine_missing``: several usable engines,
  or none;
* ``install_method``: a package lists several methods and more than one fits
  this site.

Pip and conda never bring an MPI: a query naming one is refused
(``mpi_from_package_manager``), so MPI codes reuse the site's Spack and modules.

Pip installs into a venv and conda into an env, both under
``<jarvis root>/envs/<method>/<name>`` (the pipeline's name, or the packages'
``install_env``); the pipeline's env gets its ``bin`` first on ``PATH``.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from jarvis_cd.core.spack_install import (
    MPI_PROVIDERS,
    Decisions,
    InstallFailed,
    _utc_now,
    make_issue,
)

METHODS = ("spack", "pip", "conda", "container")
MARKER_NAME = ".jarvis-install.json"
MINIFORGE_URL = (
    "https://github.com/conda-forge/miniforge/releases/latest/download/"
    "Miniforge3-{system}-{machine}.sh"
)

_ENV_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_NAME_AT_START = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_MPI_NAMES = {name.replace("_", "-") for name in MPI_PROVIDERS} | {
    "impi-rt",
    "mpich-abi",
    "msmpi",
    "mpi",
}
#: conda-forge's mpi4py depends on a conda MPI; pip's builds against the site's mpicc
_CONDA_MPI_EXTRA = {"mpi4py"}
_TAIL = 4000


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def run_logged(argv: Sequence[Any], context, *, env: Optional[Mapping[str, str]] = None,
               timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    """Run ``argv`` with stdout and stderr in the install log; return the result."""
    context.log(f"[{_utc_now()}] $ {shlex.join([str(item) for item in argv])}\n")
    try:
        result = subprocess.run(
            [str(item) for item in argv], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=dict(env) if env is not None else None, timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        context.log(f"[{_utc_now()}] failed to run: {exc}\n")
        return subprocess.CompletedProcess(list(argv), 127, str(exc), None)
    if result.stdout:
        context.log(result.stdout if result.stdout.endswith("\n") else result.stdout + "\n")
    return result


def _tail(text: Optional[str]) -> str:
    text = (text or "").strip()
    return text[-_TAIL:]


def _config(pkg_def: Mapping[str, Any]) -> Dict[str, Any]:
    config = pkg_def.get("config")
    return config if isinstance(config, dict) else {}


def package_id(pkg_def: Mapping[str, Any]) -> str:
    return str(pkg_def.get("pkg_id") or pkg_def.get("pkg_name") or "pkg")


def query_tokens(pkg_def: Mapping[str, Any]) -> List[str]:
    """The package's ``install_query`` (or legacy ``install``) as argv tokens."""
    config = _config(pkg_def)
    value = config.get("install_query") or config.get("install") or ""
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return shlex.split(str(value))


def _string_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item for item in re.split(r"[,\s]+", str(value or "")) if item]


def jarvis_root(ppl) -> Path:
    root = getattr(getattr(ppl, "jarvis", None), "jarvis_root", None)
    if root:
        return Path(root)
    return Path(os.environ.get("JARVIS_ROOT", "").strip() or "~/.ppi-jarvis").expanduser()


def site_home() -> Path:
    """The site record's directory (where JARVIS keeps its own Spack and Miniforge)."""
    graph = os.environ.get("JARVIS_RESOURCE_GRAPH", "").strip()
    if graph:
        return Path(graph).expanduser().resolve().parent
    return Path(os.environ.get("JARVIS_ROOT", "").strip() or "~/.ppi-jarvis").expanduser()


def env_prefix(ppl, method: str, pkg_list: Sequence[Mapping[str, Any]]) -> Path:
    """One venv or conda env per group of packages with the same method.

    ``JARVIS_INSTALL_ENV_DIR`` wins when set, as ``JARVIS_SPACK_ENV_DIR`` does
    for Spack: ``<dir>/<method>/<install_env or "env">``, shared by the check,
    the install and the run even when each has its own JARVIS root. Otherwise
    it is ``<jarvis root>/envs/<method>/<install_env or pipeline name>``.
    """
    names = {str(_config(pkg_def).get("install_env") or "").strip() for pkg_def in pkg_list}
    names.discard("")
    if len(names) > 1:
        raise ValueError(
            f"the {method} packages name different install_env values: {sorted(names)}")
    named = names.pop() if names else ""
    configured = os.environ.get("JARVIS_INSTALL_ENV_DIR", "").strip()
    name = named or ("env" if configured else str(getattr(ppl, "name", "") or "pipeline"))
    if not _ENV_NAME.match(name):
        raise ValueError(f"install_env {name!r} is not a plain directory name")
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured))) / method / name
    return jarvis_root(ppl) / "envs" / method / name


def prepend_path(ppl, directory: Path, **variables: str) -> None:
    """Put ``directory`` first on the pipeline's ``PATH`` and set ``variables``."""
    env = getattr(ppl, "env", None)
    if env is None:
        return
    current = str(env.get("PATH") or os.environ.get("PATH", ""))
    parts = [part for part in current.split(os.pathsep) if part and part != str(directory)]
    env["PATH"] = os.pathsep.join([str(directory), *parts])
    env.update(variables)


def mpi_named(method: str, tokens: Sequence[str]) -> Optional[str]:
    """The MPI a pip or conda query names, if any."""
    for token in tokens:
        text = token.split("::")[-1].strip()
        if text.startswith("-"):
            continue
        match = _NAME_AT_START.match(text)
        if match is None:
            continue
        name = match.group(0).lower().replace("_", "-")
        if name in _MPI_NAMES or (method == "conda" and name in _CONDA_MPI_EXTRA):
            return name
    return None


def _issue(kind: str, package: str, subject: str, question: str, *, method: str,
           scope: str = "package", candidates: Sequence[Mapping[str, Any]] = (),
           detail: str = "", evidence: Sequence[str] = (), answerable: bool = False,
           hint: Optional[str] = None) -> Dict[str, Any]:
    issue = make_issue(kind=kind, package=package, subject=subject, scope=scope,
                       question=question, candidates=candidates, detail=detail,
                       evidence=evidence, answerable=answerable, hint=hint)
    issue["method"] = method
    return issue


def not_installed_issue(method: str, package: str, subject: str, missing: Sequence[str],
                        env: Path) -> Dict[str, Any]:
    issue = _issue(
        "not_installed", package, subject,
        f"{package} needs {len(missing)} {method} package(s) installed into {env} "
        "before it can run.",
        method=method, detail=", ".join(missing),
        hint="run `jarvis ppl install` (e.g. as a 1-node +submit job)",
    )
    issue["missing"] = list(missing)[:64]
    return issue


def failed(context, kind: str, method: str, package: str, subject: str, question: str,
           output: Optional[str], hint: str) -> InstallFailed:
    """Raise-ready typed failure, with its issue in ``issues.jsonl``.

    It carries what Spack's typed failures carry (message, remedy, stage,
    packages, log, log_tail, host), so readers treat every method alike.
    """
    issue = _issue(kind, package, subject, question, method=method, detail=_tail(output),
                   hint=hint)
    issue.update(message=question, remedy=hint, stage="install", packages=[package],
                 log=str(context.log_path), log_tail=_tail(output), host=socket.gethostname())
    context.raise_issue(issue)
    return InstallFailed([issue], context.issues_path)


def mpi_refused(context, method: str, package: str, name: str) -> Dict[str, Any]:
    issue = _issue(
        "mpi_from_package_manager", package, name,
        f"{package} asks {method} for the MPI {name!r}. MPI comes from the site's Spack "
        f"and modules, so JARVIS does not install one with {method}.",
        method=method,
        hint="install the package with Spack (depend on the mpi virtual), or drop the MPI "
             "from its install_query",
    )
    context.raise_issue(issue)
    return issue


def resolve_choice(context, decisions: Decisions, issue: Dict[str, Any],
                   validate: Optional[Callable[[str], None]] = None
                   ) -> Optional[Dict[str, Any]]:
    """The answer to ``issue``: recorded, else from ``answers.json``, else None.

    A recorded answer applies only to the same ``issue_id`` (the question did
    not change); an older one is reported in ``stale_decisions``. An answer
    from ``answers.json`` is validated and recorded. With no answer the issue
    is raised.
    """
    if issue["scope"] == "site":
        recorded = decisions.site(issue["subject"])
        entries = [recorded] if recorded else []
    else:
        entries = [entry for entry in decisions.package_entries(issue["package"])
                   if entry.get("subject") == issue["subject"]]
    for entry in entries:
        if entry.get("issue_id") == issue["issue_id"] and entry.get("constraint"):
            context.note_decision(issue, entry, source="decisions_file", path=decisions.path)
            return entry
    for entry in entries:
        if not entry.get("constraint"):
            continue
        stale = {
            "issue_id": entry.get("issue_id"),
            "subject": issue["subject"],
            "scope": issue["scope"],
            "constraint": entry.get("constraint"),
            "path": str(decisions.path),
            "answered_at": entry.get("answered_at"),
            "superseded_by": issue["issue_id"],
            "reason": "the candidates changed, so the question changed",
        }
        if stale not in context.stale_decisions:
            context.stale_decisions.append(stale)
        issue["supersedes"] = entry.get("issue_id")
        issue["previous_answer"] = entry.get("constraint")
        break
    answer = context.answer_for(issue)
    if answer is not None:
        if validate is not None:
            validate(str(answer["constraint"]))
        decisions.record(issue, answer, {"candidate_id": answer.get("candidate_id")})
        return answer
    context.raise_issue(issue)
    return None


def _decisions(context) -> Decisions:
    return Decisions(context.decisions_path)


def _read_marker(prefix: Path) -> Dict[str, Any]:
    try:
        value = json.loads((prefix / MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_marker(prefix: Path, method: str, packages: Mapping[str, Sequence[str]],
                  **fields: Any) -> None:
    marker = _read_marker(prefix)
    installed = dict(marker.get("packages") or {})
    installed.update({name: list(tokens) for name, tokens in packages.items()})
    marker.update(fields, schema="jarvis.method-env.v1", method=method,
                  packages=installed, updated_at=_utc_now())
    (prefix / MARKER_NAME).write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n",
                                      encoding="utf-8")


def _marker_has(prefix: Path, package: str, tokens: Sequence[str]) -> bool:
    return list((_read_marker(prefix).get("packages") or {}).get(package) or []) == list(tokens)


# ---------------------------------------------------------------------------
# pip
# ---------------------------------------------------------------------------

_PIP_PROBE = r"""
import json, sys
try:
    from pip._vendor.packaging.requirements import Requirement
except Exception:
    from packaging.requirements import Requirement
from importlib import metadata
out = []
for text in json.loads(sys.argv[1]):
    entry = {"requirement": text}
    try:
        req = Requirement(text)
    except Exception as exc:
        entry.update(status="unverifiable", reason=str(exc)[:200])
        out.append(entry)
        continue
    if req.url:
        entry.update(status="unverifiable", reason="a URL requirement")
        out.append(entry)
        continue
    try:
        version = metadata.version(req.name)
    except metadata.PackageNotFoundError:
        entry["status"] = "missing"
    else:
        entry["version"] = version
        ok = not req.specifier or req.specifier.contains(version, prereleases=True)
        entry["status"] = "satisfied" if ok else "mismatch"
    out.append(entry)
print(json.dumps(out))
"""


def jarvis_python() -> Optional[str]:
    """The interpreter JARVIS itself runs on, outside any venv it runs in.

    It is on every host JARVIS runs on (login and compute alike), unlike a
    ``python3`` on ``PATH``, which differs from node to node.
    """
    base = getattr(sys, "_base_executable", None) or sys.executable
    return os.path.realpath(base) if base else None


def find_pythons() -> List[Dict[str, Any]]:
    """Pythons that can make a venv with pip (``venv`` and ``ensurepip``).

    The one JARVIS runs on comes first (``source: jarvis``), then those on ``PATH``.
    """
    found: List[Dict[str, Any]] = []
    seen: set = set()
    own = jarvis_python()
    named = [(own, "jarvis")] if own else []
    named += [(shutil.which(name), name) for name in
              ("python3", "python3.13", "python3.12", "python3.11", "python3.10",
               "python3.9")]
    for path, name in named:
        if not path:
            continue
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        try:
            result = subprocess.run(
                [path, "-c", "import sys, venv, ensurepip; "
                 "print('%d.%d.%d' % sys.version_info[:3])"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0:
            continue
        found.append({"id": f"python-{len(found) + 1}", "constraint": path, "name": name,
                      "version": result.stdout.strip(), "source": "PATH"})
    return found


def _validate_python(value: str) -> None:
    path = shutil.which(value) or value
    if not (Path(path).is_file() and os.access(path, os.X_OK)):
        raise ValueError(f"python_missing answer {value!r} is not an executable Python")


def pip_python(ppl, pkg_list, context, prefix: Path) -> Optional[str]:
    """The Python the venv is made with, or None after raising ``python_missing``."""
    for pkg_def in pkg_list:
        configured = str(_config(pkg_def).get("install_python") or "").strip()
        if configured:
            return configured
    if (prefix / "bin" / "python").exists():
        return str(prefix / "bin" / "python")
    pythons = find_pythons()
    named = next((item for item in pythons if item["name"] in ("jarvis", "python3")), None)
    if named is not None:
        return str(named["constraint"])
    issue = _issue(
        "python_missing", "*", "python",
        "Which Python should JARVIS make the pip venv with on this site?"
        if pythons else
        "No python3 that can make a venv was found on this site; which Python should "
        "JARVIS use for pip packages?",
        method="pip", scope="site", candidates=pythons, answerable=True,
        evidence=["the Python JARVIS runs on, then python3, python3.13 ... python3.9 on "
                  "PATH, each with venv and ensurepip"],
        hint="answer with a Python executable (recorded as the site decision `python`), "
             "or set install_python on the package",
    )
    answer = resolve_choice(context, _decisions(context), issue, _validate_python)
    return str(answer["constraint"]) if answer else None


def _pip_probe(prefix: Path, requirements: Sequence[str], context) -> List[Dict[str, Any]]:
    python = prefix / "bin" / "python"
    if not python.exists():
        return [{"requirement": item, "status": "missing", "reason": "no venv yet"}
                for item in requirements]
    result = run_logged([python, "-c", _PIP_PROBE, json.dumps(list(requirements))],
                        context, timeout=120)
    try:
        return list(json.loads((result.stdout or "").strip().splitlines()[-1]))
    except (ValueError, IndexError):
        return [{"requirement": item, "status": "unverifiable",
                 "reason": "the venv's Python could not read its packages"}
                for item in requirements]


def _requirements(tokens: Sequence[str]) -> List[str]:
    """The requirement tokens of a pip query (options and their values left out)."""
    found: List[str] = []
    skip = False
    for token in tokens:
        if skip:
            skip = False
            continue
        if token.startswith("-"):
            skip = "=" not in token and token not in ("--pre", "--upgrade", "-U",
                                                      "--no-deps", "--user")
            continue
        found.append(token)
    return found


def _package_state(prefix: Path, package: str, tokens: Sequence[str],
                   probed: List[Dict[str, Any]]) -> Dict[str, Any]:
    missing = [item["requirement"] for item in probed
               if item.get("status") in ("missing", "mismatch")]
    unverifiable = [item["requirement"] for item in probed
                    if item.get("status") == "unverifiable"]
    options = any(token.startswith("-") for token in tokens)
    if not missing and (unverifiable or options) and not _marker_has(prefix, package, tokens):
        # JARVIS cannot read these from the env, so it trusts only its own record
        missing = unverifiable or list(tokens)
    status = "missing" if missing else "satisfied"
    return {"status": status, "query": list(tokens), "requirements": probed,
            "missing": missing}


def _pip_states(prefix: Path, pkg_list, context) -> Dict[str, Dict[str, Any]]:
    states: Dict[str, Dict[str, Any]] = {}
    for pkg_def in pkg_list:
        tokens = query_tokens(pkg_def)
        if not tokens:
            states[package_id(pkg_def)] = {"status": "no_query"}
            continue
        probed = _pip_probe(prefix, _requirements(tokens), context)
        states[package_id(pkg_def)] = _package_state(prefix, package_id(pkg_def), tokens,
                                                     probed)
    return states


def pip_run(ppl, pkg_list, context, mode: str) -> Dict[str, Any]:
    """Check (``mode='check'``) or install the pip packages into the venv."""
    prefix = env_prefix(ppl, "pip", pkg_list)
    result: Dict[str, Any] = {"method": "pip", "env": str(prefix), "packages": {}}
    refused = False
    for pkg_def in pkg_list:
        name = mpi_named("pip", query_tokens(pkg_def))
        if name is not None:
            mpi_refused(context, "pip", package_id(pkg_def), name)
            refused = True
    if refused:
        result["status"] = "refused"
        return result
    states = _pip_states(prefix, pkg_list, context)
    result["packages"] = states
    todo = [pkg_def for pkg_def in pkg_list
            if states[package_id(pkg_def)]["status"] == "missing"]
    if not todo:
        result["status"] = "satisfied"
        prepend_path(ppl, prefix / "bin", VIRTUAL_ENV=str(prefix))
        return result
    # the Python question comes before any install job, so ask it in check mode too
    python = pip_python(ppl, pkg_list, context, prefix)
    if mode == "check":
        for pkg_def in todo:
            state = states[package_id(pkg_def)]
            context.raise_issue(not_installed_issue(
                "pip", package_id(pkg_def), " ".join(state["query"]), state["missing"],
                prefix))
        result["status"] = "missing"
        return result
    if python is None:
        result["status"] = "needs_decision"
        return result
    result["python"] = python
    names = [package_id(pkg_def) for pkg_def in todo]
    context.ensure_may_build("pip install", names)
    if not (prefix / "bin" / "python").exists():
        prefix.parent.mkdir(parents=True, exist_ok=True)
        context.report_progress(phase="building", current_package=names[0])
        made = run_logged([python, "-m", "venv", prefix], context)
        if made.returncode != 0:
            raise failed(context, "venv_failed", "pip", names[0], str(prefix),
                         f"JARVIS could not make the pip venv {prefix} with {python}.",
                         made.stdout, "answer python_missing with another Python, or set "
                         "install_python on the package")
    for pkg_def in todo:
        package = package_id(pkg_def)
        tokens = query_tokens(pkg_def)
        context.report_progress(phase="building", current_package=package)
        ran = run_logged([prefix / "bin" / "python", "-m", "pip", "install",
                          "--disable-pip-version-check", "--no-input", *tokens], context)
        if ran.returncode != 0:
            raise failed(context, "pip_install_failed", "pip", package, " ".join(tokens),
                         f"pip could not install {' '.join(tokens)} for {package}.",
                         ran.stdout, "fix the package's install_query, or read the install "
                         "log for the network or build error")
        _write_marker(prefix, "pip", {package: tokens}, python=python)
    states = _pip_states(prefix, pkg_list, context)
    result["packages"] = states
    still = [name for name, state in states.items() if state["status"] == "missing"]
    if still:
        raise failed(context, "pip_install_failed", "pip", still[0], str(prefix),
                     f"pip finished but {', '.join(still)} still miss requirements.",
                     json.dumps(states[still[0]].get("requirements"))[:_TAIL],
                     "read the install log: pip may have installed another version")
    result["status"] = "installed"
    prepend_path(ppl, prefix / "bin", VIRTUAL_ENV=str(prefix))
    return result


# ---------------------------------------------------------------------------
# conda
# ---------------------------------------------------------------------------

_CONDA_PROBE = r"""
import json, sys
from conda.core.prefix_data import PrefixData
from conda.models.match_spec import MatchSpec
data = PrefixData(sys.argv[1])
out = []
for text in json.loads(sys.argv[2]):
    try:
        found = list(data.query(MatchSpec(text)))
    except Exception as exc:
        out.append({"requirement": text, "status": "unverifiable", "reason": str(exc)[:200]})
        continue
    out.append({"requirement": text, "status": "satisfied" if found else "missing",
                "version": str(found[0].version) if found else None})
print(json.dumps(out))
"""


def miniforge_prefix() -> Path:
    return site_home() / "miniforge3"


def find_condas() -> List[Dict[str, Any]]:
    """The conda executables this host has (``CONDA_EXE``, ``PATH``, usual homes)."""
    probes: List[Tuple[str, str]] = []
    for name in ("CONDA_EXE", "MAMBA_EXE"):
        value = os.environ.get(name, "").strip()
        if value:
            probes.append((value, name))
    for name in ("conda", "mamba", "micromamba"):
        path = shutil.which(name)
        if path:
            probes.append((path, "PATH"))
    probes.append((str(miniforge_prefix() / "bin" / "conda"), "jarvis_miniforge"))
    for home in ("~/miniforge3", "~/mambaforge", "~/miniconda3", "~/anaconda3"):
        probes.append((str(Path(home).expanduser() / "bin" / "conda"), "home"))
    found: List[Dict[str, Any]] = []
    seen: set = set()
    for raw, source in probes:
        path = Path(raw).expanduser()
        if not (path.is_file() and os.access(path, os.X_OK)):
            continue
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        found.append({"id": f"conda-{len(found) + 1}", "constraint": str(path),
                      "kind": path.name, "root": str(path.parent.parent),
                      "prefix": str(path.parent.parent), "source": source})
        if len(found) >= 8:
            break
    return found


def _validate_conda(value: str) -> None:
    if Path(value) == miniforge_prefix() / "bin" / "conda":
        return
    if not (Path(value).is_file() and os.access(value, os.X_OK)):
        raise ValueError(f"conda answer {value!r} is not a conda executable")


def resolve_conda(pkg_list, context) -> Optional[str]:
    """The conda executable, or None after raising ``conda_select``/``conda_missing``.

    The answer may be the Miniforge candidate, which is installed into the
    site home the first time an install needs it.
    """
    for pkg_def in pkg_list:
        configured = str(_config(pkg_def).get("install_conda") or "").strip()
        if configured:
            return configured
    condas = find_condas()
    if len(condas) == 1:
        return str(condas[0]["constraint"])
    if condas:
        issue = _issue(
            "conda_select", "*", "conda", "Which conda should JARVIS use on this site?",
            method="conda", scope="site", candidates=condas, answerable=True,
            evidence=["CONDA_EXE, MAMBA_EXE, conda/mamba/micromamba on PATH, "
                      f"{miniforge_prefix()}, ~/miniforge3, ~/mambaforge, ~/miniconda3, "
                      "~/anaconda3"],
            hint="answer with a conda executable (recorded as the site decision `conda`)",
        )
    else:
        prefix = miniforge_prefix()
        machine = platform.machine() or "x86_64"
        system = platform.system() or "Linux"
        candidate = {
            "id": "miniforge", "constraint": str(prefix / "bin" / "conda"),
            "root": str(prefix), "action": "install_miniforge",
            "url": MINIFORGE_URL.format(system=system, machine=machine),
            "summary": f"install Miniforge (conda-forge's conda) into {prefix}",
            "does": f"installs Miniforge (conda-forge's conda) into {prefix} in the install job",
        }
        issue = _issue(
            "conda_missing", "*", "conda",
            f"No conda was found on this site. May JARVIS install Miniforge into {prefix} "
            "(the site home) for the conda packages?",
            method="conda", scope="site", candidates=[candidate], answerable=True,
            evidence=["CONDA_EXE, MAMBA_EXE, conda/mamba/micromamba on PATH, "
                      f"{prefix}, ~/miniforge3, ~/mambaforge, ~/miniconda3, ~/anaconda3"],
            hint="answer `miniforge` to install it (inside the install job), or set "
                 "install_conda on the package",
        )
    answer = resolve_choice(context, _decisions(context), issue, _validate_conda)
    return str(answer["constraint"]) if answer else None


def install_miniforge(context, exe: Path) -> None:
    """Install Miniforge so ``exe`` (``<prefix>/bin/conda``) exists."""
    prefix = exe.parent.parent
    context.ensure_may_build("install Miniforge", ["miniforge"])
    context.report_progress(phase="building", current_package="miniforge")
    url = MINIFORGE_URL.format(system=platform.system() or "Linux",
                               machine=platform.machine() or "x86_64")
    prefix.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(prefix.parent)) as scratch:
        installer = Path(scratch) / "miniforge.sh"
        context.log(f"[{_utc_now()}] downloading {url}\n")
        try:
            with urllib.request.urlopen(url, timeout=300) as response, \
                    installer.open("wb") as stream:
                shutil.copyfileobj(response, stream)
        except OSError as exc:
            raise failed(context, "conda_bootstrap_failed", "conda", "*", "conda",
                         f"JARVIS could not download Miniforge from {url}.", str(exc),
                         "check the host's network or proxy, or name a conda with "
                         "install_conda") from exc
        ran = run_logged(["bash", installer, "-b", "-p", prefix], context)
    if ran.returncode != 0 or not exe.is_file():
        raise failed(context, "conda_bootstrap_failed", "conda", "*", "conda",
                     f"The Miniforge installer did not finish in {prefix}.", ran.stdout,
                     "read the install log, remove the partial prefix, then install again")


def _conda_probe(exe: Path, prefix: Path, specs: Sequence[str], context
                 ) -> List[Dict[str, Any]]:
    if not (prefix / "conda-meta").is_dir():
        return [{"requirement": item, "status": "missing", "reason": "no env yet"}
                for item in specs]
    python = exe.parent / "python"
    if python.exists():
        result = run_logged([python, "-c", _CONDA_PROBE, prefix, json.dumps(list(specs))],
                            context, timeout=120)
        try:
            return list(json.loads((result.stdout or "").strip().splitlines()[-1]))
        except (ValueError, IndexError):
            pass
    listed = run_logged([exe, "list", "-p", prefix, "--json"], context, timeout=120)
    try:
        names = {str(item.get("name")) for item in json.loads(listed.stdout or "[]")}
    except ValueError:
        names = set()
    probed = []
    for item in specs:
        match = _NAME_AT_START.match(item.split("::")[-1])
        name = match.group(0) if match else item
        probed.append({"requirement": item,
                       "status": "unverifiable" if name in names else "missing",
                       "reason": "this conda reads names only"})
    return probed


def conda_run(ppl, pkg_list, context, mode: str) -> Dict[str, Any]:
    """Check or install the conda packages into the env."""
    prefix = env_prefix(ppl, "conda", pkg_list)
    result: Dict[str, Any] = {"method": "conda", "env": str(prefix), "packages": {}}
    refused = False
    for pkg_def in pkg_list:
        name = mpi_named("conda", query_tokens(pkg_def))
        if name is not None:
            mpi_refused(context, "conda", package_id(pkg_def), name)
            refused = True
    if refused:
        result["status"] = "refused"
        return result
    conda = resolve_conda(pkg_list, context)
    if conda is None:
        result["status"] = "needs_decision"
        return result
    exe = Path(conda)
    result["conda"] = conda
    states: Dict[str, Dict[str, Any]] = {}

    def probe_all() -> None:
        for pkg_def in pkg_list:
            tokens = query_tokens(pkg_def)
            if not tokens:
                states[package_id(pkg_def)] = {"status": "no_query"}
                continue
            probed = (_conda_probe(exe, prefix, _requirements(tokens), context)
                      if exe.is_file() else
                      [{"requirement": item, "status": "missing", "reason": "no conda yet"}
                       for item in _requirements(tokens)])
            states[package_id(pkg_def)] = _package_state(prefix, package_id(pkg_def),
                                                         tokens, probed)

    probe_all()
    result["packages"] = states
    todo = [pkg_def for pkg_def in pkg_list if states[package_id(pkg_def)]["status"] == "missing"]
    if not todo:
        result["status"] = "satisfied"
        prepend_path(ppl, prefix / "bin", CONDA_PREFIX=str(prefix))
        return result
    if mode == "check":
        for pkg_def in todo:
            state = states[package_id(pkg_def)]
            missing = list(state["missing"])
            if not exe.is_file():
                missing = ["miniforge", *missing]
            context.raise_issue(not_installed_issue(
                "conda", package_id(pkg_def), " ".join(state["query"]), missing, prefix))
        result["status"] = "missing"
        return result
    names = [package_id(pkg_def) for pkg_def in todo]
    if not exe.is_file():
        install_miniforge(context, exe)
    context.ensure_may_build("conda install", names)
    for pkg_def in todo:
        package = package_id(pkg_def)
        tokens = query_tokens(pkg_def)
        channels: List[str] = []
        for channel in _string_list(_config(pkg_def).get("install_channels")):
            channels += ["-c", channel]
        if channels:
            channels.append("--override-channels")
        verb = "install" if (prefix / "conda-meta").is_dir() else "create"
        prefix.parent.mkdir(parents=True, exist_ok=True)
        context.report_progress(phase="building", current_package=package)
        ran = run_logged([exe, verb, "-y", "-p", prefix, *channels, *tokens], context)
        if ran.returncode != 0:
            raise failed(context, "conda_install_failed", "conda", package, " ".join(tokens),
                         f"conda could not install {' '.join(tokens)} for {package}.",
                         ran.stdout, "fix the package's install_query or install_channels, "
                         "or read the install log")
        _write_marker(prefix, "conda", {package: tokens}, conda=str(exe))
    probe_all()
    still = [name for name, state in states.items() if state["status"] == "missing"]
    if still:
        raise failed(context, "conda_install_failed", "conda", still[0], str(prefix),
                     f"conda finished but {', '.join(still)} still miss packages.",
                     json.dumps(states[still[0]].get("requirements"))[:_TAIL],
                     "read the install log")
    result["status"] = "installed"
    prepend_path(ppl, prefix / "bin", CONDA_PREFIX=str(prefix))
    return result


# ---------------------------------------------------------------------------
# containers
# ---------------------------------------------------------------------------


def podman_runtime_dir() -> Optional[str]:
    """Give rootless podman a runtime dir on hosts with no ``/run/user/<uid>``.

    Batch nodes often have no login session, so ``XDG_RUNTIME_DIR`` is unset
    or names a directory that does not exist; podman then cannot start. A
    private directory under the host's temp dir is used instead.
    """
    current = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if current and os.path.isdir(current) and os.access(current, os.W_OK):
        return None
    uid = getattr(os, "getuid", lambda: 0)()
    directory = Path(tempfile.gettempdir()) / f"jarvis-podman-{uid}"
    directory.mkdir(mode=0o700, exist_ok=True)
    os.environ["XDG_RUNTIME_DIR"] = str(directory)
    return str(directory)


def _engine_field(executable: str, template: str) -> Optional[str]:
    try:
        result = subprocess.run([executable, "info", "--format", template],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def detect_engines() -> Tuple[List[Dict[str, Any]], List[str]]:
    """Container engines this user can run, and why others were left out.

    Apptainer runs as the user. Podman counts when it runs rootless. Docker
    counts only when its daemon is rootless: a shared rootful daemon is left
    out unless the pipeline names ``container_engine: docker``.
    """
    found: List[Dict[str, Any]] = []
    excluded: List[str] = []
    apptainer = shutil.which("apptainer")
    if apptainer:
        found.append({"id": "apptainer", "constraint": "apptainer", "executable": apptainer,
                      "source": apptainer, "does": "runs as this user"})
    podman = shutil.which("podman")
    if podman:
        podman_runtime_dir()
        rootless = _engine_field(podman, "{{.Host.Security.Rootless}}")
        if rootless == "true" or getattr(os, "geteuid", lambda: 1)() == 0:
            found.append({"id": "podman", "constraint": "podman", "executable": podman,
                          "rootless": rootless == "true", "source": podman,
                          "does": "rootless" if rootless == "true" else "runs as root"})
        else:
            excluded.append(f"podman at {podman}: `podman info` failed or is not rootless")
    docker = shutil.which("docker")
    if docker:
        options = _engine_field(docker, "{{.SecurityOptions}}")
        if options and "rootless" in options:
            found.append({"id": "docker", "constraint": "docker", "executable": docker,
                          "rootless": True, "source": docker, "does": "rootless daemon"})
        else:
            excluded.append(f"docker at {docker}: a shared rootful daemon (or `docker info` "
                            "failed); used only when the pipeline names it")
    return found, excluded


def resolve_engine(ppl, context) -> Optional[str]:
    """The pipeline's container engine, or None after a typed question."""
    explicit = getattr(ppl, "container_engine_source", "yaml") == "yaml"
    engine = str(getattr(ppl, "container_engine", "") or "")
    if explicit and engine:
        if engine == "podman":
            podman_runtime_dir()
        if shutil.which(engine) is None:
            context.raise_issue(_issue(
                "container_engine_missing", "*", "container_engine",
                f"The pipeline names container_engine {engine!r}, which this host does "
                "not have.",
                method="container", scope="site", evidence=[f"`command -v {engine}`"],
                hint="install the engine or name another container_engine"))
            return None
        return engine
    found, excluded = detect_engines()
    evidence = ["apptainer, rootless podman, rootless docker on PATH", *excluded]
    if len(found) == 1:
        chosen = str(found[0]["constraint"])
        context.log(f"[{_utc_now()}] container engine {chosen}: the only one this user "
                    "can run here\n")
    elif not found:
        context.raise_issue(_issue(
            "container_engine_missing", "*", "container_engine",
            "This host has no container engine JARVIS can run as this user.",
            method="container", scope="site", evidence=evidence,
            hint="install apptainer or rootless podman, or name container_engine in the "
                 "pipeline"))
        return None
    else:
        issue = _issue(
            "container_engine", "*", "container_engine",
            "Which container engine should JARVIS use on this site?",
            method="container", scope="site", candidates=found, answerable=True,
            evidence=evidence,
            hint="answer with an engine (recorded as the site decision `container_engine`)")
        answer = resolve_choice(context, _decisions(context), issue)
        if answer is None:
            return None
        chosen = str(answer["constraint"])
    if chosen == "podman":
        podman_runtime_dir()
    ppl.container_engine = chosen
    ppl.container_engine_source = "detected"
    return chosen


def container_image(ppl, pkg_list, context) -> Tuple[bool, str]:
    """``(ok, uri)``: the prebuilt image (``container_uri``, else the
    ``install_query`` of packages that name ``install_method: container``
    themselves), ``""`` when the pipeline builds its own."""
    uri = str(getattr(ppl, "container_uri", "") or "")
    if uri:
        return True, uri
    images = sorted({" ".join(query_tokens(pkg_def)) for pkg_def in pkg_list
                     if _config(pkg_def).get("install_method") == "container"} - {""})
    if len(images) > 1:
        context.raise_issue(_issue(
            "container_images_differ", "*", "container_uri",
            "The pipeline's container packages name different images, and JARVIS runs "
            "one image per pipeline.",
            method="container", scope="site", detail=", ".join(images),
            hint="give every container package the same image, or set container_uri"))
        return False, ""
    if images:
        ppl.container_uri = images[0]
        return True, images[0]
    return True, ""


def image_present(ppl, engine: str, image: str) -> bool:
    from jarvis_cd.core.pkg import Pkg

    if engine == "apptainer":
        containers = ppl.jarvis.get_containers_dir()
        return (Path(containers) / f"{ppl.name}.sif").exists()
    return Pkg._image_exists(engine, image)


def _engine_env(ppl) -> None:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime and getattr(ppl, "env", None) is not None:
        ppl.env["XDG_RUNTIME_DIR"] = runtime


def method_fits(method: str) -> Tuple[bool, str]:
    """Whether ``method`` can install on this host now, and why."""
    if method == "spack":
        from jarvis_cd.core.spack_install import discover_site_spacks, spack_unavailable_reason

        reason = spack_unavailable_reason()
        if reason is None:
            return True, "Spack is available"
        # spack_missing then offers these, or replays the site's recorded answer
        site_spacks = discover_site_spacks()
        if site_spacks:
            return True, "site Spack " + ", ".join(str(item["root"]) for item in site_spacks)
        return False, reason
    if method == "pip":
        pythons = find_pythons()
        return (bool(pythons), f"python3 {pythons[0]['version']}" if pythons
                else "no python3 with venv and ensurepip")
    if method == "conda":
        condas = find_condas()
        return (bool(condas), condas[0]["constraint"] if condas
                else "no conda; JARVIS can install Miniforge")
    if method == "container":
        found, excluded = detect_engines()
        return (bool(found), ", ".join(item["id"] for item in found) if found
                else "; ".join(excluded) or "no container engine")
    return False, f"unknown method {method!r}"


def method_options(pkg_def: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The package's ``install_methods`` alternatives, validated."""
    raw = _config(pkg_def).get("install_methods") or []
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{package_id(pkg_def)}: install_methods must be a list")
    options: List[Dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            item = {"method": item}
        if not isinstance(item, Mapping) or item.get("method") not in METHODS:
            raise ValueError(f"{package_id(pkg_def)}: each install_methods entry needs a "
                             f"method among {', '.join(METHODS)}")
        if any(option["method"] == item["method"] for option in options):
            raise ValueError(f"{package_id(pkg_def)}: install_methods lists "
                             f"{item['method']} twice")
        options.append(dict(item))
    return options


def needs_method_choice(pkg_def: Mapping[str, Any]) -> bool:
    config = _config(pkg_def)
    return not config.get("install_method") and bool(config.get("install_methods"))


_OPTION_FIELDS = {"query": "install_query", "repos": "install_repos",
                  "channels": "install_channels", "python": "install_python",
                  "conda": "install_conda", "env": "install_env"}


def apply_method(pkg_def: Dict[str, Any], option: Mapping[str, Any], why: str) -> None:
    config = pkg_def.setdefault("config", {})
    config["install_method"] = option["method"]
    for key, field_name in _OPTION_FIELDS.items():
        if option.get(key) not in (None, ""):
            config[field_name] = option[key]
    if option["method"] == "container":
        config["deploy_mode"] = "container"
    config["install_method_choice"] = {"method": option["method"], "why": why}


def resolve_method_choices(ppl, context) -> Dict[str, Any]:
    """Pick each package's method from its ``install_methods``.

    One declared method is used. When several are declared, the ones that
    fit this host (their tool is there) are offered: one that fits is used,
    and several are an answerable ``install_method`` question (package
    scope, recorded). When none fits every declared method is offered.
    """
    chosen: Dict[str, Any] = {}
    decisions: Optional[Decisions] = None
    for pkg_def in getattr(ppl, "packages", []) or []:
        if not needs_method_choice(pkg_def):
            continue
        package = package_id(pkg_def)
        options = method_options(pkg_def)
        if len(options) == 1:
            apply_method(pkg_def, options[0], "the only method the package declares")
            chosen[package] = options[0]["method"]
            continue
        fits = {option["method"]: method_fits(option["method"]) for option in options}
        fitting = [option for option in options if fits[option["method"]][0]]
        evidence = [f"{method}: {'fits' if ok else 'does not fit'} ({why})"
                    for method, (ok, why) in fits.items()]
        if len(fitting) == 1:
            why = f"the only declared method that fits this host ({fits[fitting[0]['method']][1]})"
            context.log(f"[{_utc_now()}] {package}: {fitting[0]['method']}, {why}\n")
            apply_method(pkg_def, fitting[0], why)
            chosen[package] = fitting[0]["method"]
            continue
        offered = fitting or options
        candidates = [
            {"id": option["method"], "constraint": option["method"],
             "query": option.get("query"), "fits": fits[option["method"]][0],
             "why": fits[option["method"]][1],
             "does": f"{option.get('query') or option['method']}: {fits[option['method']][1]}"}
            for option in offered
        ]
        issue = _issue(
            "install_method", package, "install_method",
            f"{package} can be installed with {', '.join(c['id'] for c in candidates)} "
            "on this site. Which should JARVIS use?",
            method="choice", candidates=candidates, evidence=evidence, answerable=True,
            hint="answer with one method (recorded for this package)",
        )
        if decisions is None:
            decisions = _decisions(context)
        answer = resolve_choice(context, decisions, issue)
        if answer is None:
            continue
        option = next(option for option in offered if option["method"] == answer["constraint"])
        apply_method(pkg_def, option, "the person's answer")
        chosen[package] = option["method"]
    return chosen

