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

An engine that is installed but cannot run here (``podman info`` fails, or a
pull fails on its store) is ``container_engine_unusable``, with the engine's
own error; no answer fixes it.

Rootless podman whose default store is on a network file system gets a
node-local store (:func:`podman_store`); the image is then archived once on
the shared file system and each node loads it before it runs.

Pip installs into a venv and conda into an env, both under
``<jarvis root>/envs/<method>/<name>`` (the pipeline's name, or the packages'
``install_env``); the pipeline's env gets its ``bin`` first on ``PATH``.
"""

from __future__ import annotations

import errno
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
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from jarvis_cd.util import stall as _stall
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
               watched: bool = False) -> subprocess.CompletedProcess:
    """Run ``argv`` with stdout and stderr in the install log; return the result.

    Nothing has a time limit. ``watched`` (the probes) stops a command that
    stalls (:mod:`jarvis_cd.util.stall`), retries it once, and logs its
    progress and the stall; the result then has returncode 124 and the
    output so far.
    """
    context.log(f"[{_utc_now()}] $ {shlex.join([str(item) for item in argv])}\n")
    try:
        if watched:
            def say(text: str, **_: Any) -> None:
                context.log(f"[{_utc_now()}] {text}\n")

            result = _stall.run(argv, env=env, merge_stderr=True, say=say, echo=False)
        else:
            result = subprocess.run(
                [str(item) for item in argv], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, env=dict(env) if env is not None else None, check=False,
            )
    except OSError as exc:
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
    """Where JARVIS keeps its own Spack and Miniforge (see util.site_state).

    Never the directory of a site-named graph, which JARVIS only reads.
    """
    from jarvis_cd.util.site_state import site_home as state_home

    return state_home()


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
            result = _stall.run(
                [path, "-c", "import sys, venv, ensurepip; "
                 "print('%d.%d.%d' % sys.version_info[:3])"], what=f"{path} version probe")
        except OSError:
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
                        context, watched=True)
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
                            context, watched=True)
        try:
            return list(json.loads((result.stdout or "").strip().splitlines()[-1]))
        except (ValueError, IndexError):
            pass
    listed = run_logged([exe, "list", "-p", prefix, "--json"], context, watched=True)
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


def _uid() -> int:
    return getattr(os, "getuid", lambda: 0)()


def _node_tmp() -> Path:
    """This node's temp dir for podman's runtime dir and store.

    With no usable temp dir Python falls back to the working directory, which in
    a job is usually on the shared file system: refuse that rather than put the
    node-local store there.
    """
    path = tempfile.gettempdir()
    named = {os.path.abspath(value) for value in
             (os.environ.get(name, "") for name in ("TMPDIR", "TEMP", "TMP")) if value}
    if (path not in named | {"/tmp", "/var/tmp", "/usr/tmp"}
            and os.path.realpath(path) == os.path.realpath(os.getcwd())):
        raise FileNotFoundError(errno.ENOENT, "No usable temporary directory on this node "
                                "(Python fell back to the working directory)", path)
    return Path(path)


def _runtime_base() -> Path:
    return _node_tmp() / f"jarvis-podman-{_uid()}"


def _store_base() -> Path:
    return _node_tmp() / f"jarvis-podman-{_uid()}-store"


def podman_runtime_dir() -> Optional[str]:
    """Give rootless podman a runtime dir on hosts with no ``/run/user/<uid>``.

    Batch nodes often have no login session, so ``XDG_RUNTIME_DIR`` is unset
    or names a directory that does not exist; podman then cannot start. A
    private directory under the host's temp dir is used instead.
    """
    current = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if current and os.path.isdir(current) and os.access(current, os.W_OK):
        return None
    directory = _runtime_base()
    directory.mkdir(mode=0o700, exist_ok=True)
    os.environ["XDG_RUNTIME_DIR"] = str(directory)
    return str(directory)


#: file systems a podman image store must not sit on: rootless overlay cannot
#: pivot there, and the store's database records the host that made it
NETWORK_FS = frozenset({
    "nfs", "nfs4", "lustre", "gpfs", "beegfs", "fuse.beegfs", "cifs", "smb3", "smbfs",
    "fuse.sshfs", "panfs", "ceph", "fuse.ceph", "glusterfs", "fuse.glusterfs", "afs",
    "9p", "wekafs", "orangefs", "pvfs2", "fuse.weka",
})
STORE_ENV = "JARVIS_PODMAN_STORE"
"""``node`` always gives podman a node-local store, ``default`` never does."""
ARCHIVE_ENV = "JARVIS_CONTAINER_ARCHIVE"
"""The pipeline env's path to the image archive a node loads its image from."""


def _unescape_mount(text: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), text)


def mount_fstype(path: Path, mounts: str = "/proc/self/mounts") -> Optional[str]:
    """The file system type of the mount ``path`` (or its nearest existing parent) is on."""
    try:
        lines = Path(mounts).read_text().splitlines()
    except OSError:
        return None
    target = Path(path).expanduser()
    while not target.exists() and target.parent != target:
        target = target.parent
    text = str(target.resolve()) if target.exists() else str(target)
    best, fstype = "", None
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        point = _unescape_mount(parts[1])
        if (text == point or text.startswith(point.rstrip("/") + "/")) and len(point) >= len(best):
            best, fstype = point, parts[2]
    return fstype


def _storage_conf(base: Path) -> str:
    driver, mount_program = "overlay", None
    found = re.match(r"(\d+)\.(\d+)", platform.release())
    kernel = (int(found.group(1)), int(found.group(2))) if found else (0, 0)
    if kernel < (5, 13):
        # rootless native overlay needs Linux 5.13; before it, fuse-overlayfs or vfs
        mount_program = shutil.which("fuse-overlayfs")
        if mount_program is None:
            driver = "vfs"
    lines = [
        "# written by JARVIS: a node-local podman store (the default one is on a "
        "network file system)",
        "[storage]",
        f'driver = "{driver}"',
        f'graphroot = "{base / "storage"}"',
        f'runroot = "{base / "run"}"',
    ]
    if mount_program:
        lines += ["", "[storage.options.overlay]", f'mount_program = "{mount_program}"']
    return "\n".join(lines) + "\n"


def node_store_conf() -> Optional[str]:
    """The node-local store's ``storage.conf`` when JARVIS gave podman one, else None."""
    try:
        conf = str(_store_base() / "storage.conf")
    except OSError:
        return None
    return conf if os.environ.get("CONTAINERS_STORAGE_CONF", "").strip() == conf else None


def podman_store(mounts: str = "/proc/self/mounts") -> Optional[str]:
    """Give rootless podman a node-local image store when its default one is shared.

    The default store, ``$XDG_DATA_HOME/containers/storage``, is usually in the
    home directory. On an HPC site that is NFS (or Lustre, GPFS...): its
    database is bound to the host that made it ("database configuration
    mismatch" elsewhere), and rootless overlay cannot pivot on it. JARVIS then
    gives podman a store under the node's temp dir (``storage.conf`` there, and
    the same store as global flags through :func:`podman_wrapper`, since rootless
    podman 3.x ignores ``CONTAINERS_STORAGE_CONF``), and
    keeps an archive of each image on the shared file system that other nodes
    load (:func:`keep_archive`). A ``storage.conf`` the user set wins, and
    ``JARVIS_PODMAN_STORE`` (``node`` or ``default``) overrides the detection.
    """
    mode = os.environ.get(STORE_ENV, "").strip().lower()
    ours = node_store_conf()
    if not ours:
        if mode == "default" or os.environ.get("CONTAINERS_STORAGE_CONF", "").strip():
            return None
        if getattr(os, "geteuid", lambda: 0)() == 0:
            return None
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", "").strip()
                       or Path.home() / ".config")
    data_home = Path(os.environ.get("XDG_DATA_HOME", "").strip()
                     or Path.home() / ".local" / "share")
    if mode != "node" and not ours:
        if (config_home / "containers" / "storage.conf").exists():
            return None
        if mount_fstype(data_home / "containers" / "storage", mounts) not in NETWORK_FS:
            return None
    base = _store_base()
    base.mkdir(mode=0o700, exist_ok=True)
    conf = base / "storage.conf"
    text = _storage_conf(base)
    if not conf.exists() or conf.read_text() != text:
        conf.write_text(text)
    os.environ["CONTAINERS_STORAGE_CONF"] = str(conf)
    return str(conf)


#: OCI runtimes the podman wrapper tries, in order, when podman's default one is
#: not on this node (a login node may have crun while compute nodes only have runc)
OCI_RUNTIMES = ("crun", "runc", "youki", "krun", "runsc", "kata-runtime")

_PODMAN_WRAPPER = r"""#!/bin/sh
# written by JARVIS: podman with this node's own image store and an OCI runtime
# this node has. Rootless podman 3.x ignores CONTAINERS_STORAGE_CONF, so the
# store is given as global flags; the runtime is probed once per node.
here=$(cd "$(dirname "$0")" && pwd -P)
real=
old_ifs=$IFS
IFS=:
for dir in $PATH; do
  [ -n "$dir" ] && [ -x "$dir/podman" ] && [ ! -d "$dir/podman" ] || continue
  [ "$(cd "$dir" 2>/dev/null && pwd -P)" = "$here" ] && continue
  real=$dir/podman
  break
done
IFS=$old_ifs
if [ -z "$real" ]; then
  echo "JARVIS podman wrapper: no podman on PATH besides $here" >&2
  exit 127
fi
cache=@CACHE@
mkdir -p -m 700 "$cache"
store=@STORE@
# With JARVIS's node-local store every session on this node uses one runtime
# dir: the OCI runtime keeps container state there, so an exec over ssh (which
# has /run/user/<uid>) must find what a batch step (which has none) started.
# That dir has no user bus, so the cgroups are podman's own (cgroupfs), as a
# batch step falls back to anyway: systemd's would fail over sd-bus.
pinned=
if [ -n "$store" ] || [ -z "${XDG_RUNTIME_DIR:-}" ] || [ ! -w "$XDG_RUNTIME_DIR" ]; then
  mkdir -p -m 700 @RUNTIME@
  XDG_RUNTIME_DIR=@RUNTIME@
  export XDG_RUNTIME_DIR
  pinned=yes
fi
driver=
option=
if [ -n "$store" ]; then
  mkdir -p -m 700 "$store/storage" "$store/run" "$XDG_RUNTIME_DIR/libpod/tmp"
  driver=overlay
  release=$(uname -r)
  major=${release%%.*}
  minor=${release#*.}
  minor=${minor%%[!0-9]*}
  # rootless native overlay needs Linux 5.13; before it, fuse-overlayfs or vfs
  if [ "${major:-0}" -lt 5 ] || { [ "$major" -eq 5 ] && [ "${minor:-0}" -lt 13 ]; }; then
    if mount_program=$(command -v fuse-overlayfs); then
      option=overlay.mount_program=$mount_program
    else
      driver=vfs
    fi
  fi
fi
oci=
podman_() {
  if [ -n "$pinned" ]; then set -- --cgroup-manager cgroupfs "$@"; fi
  if [ -n "$oci" ]; then set -- --runtime "$oci" "$@"; fi
  if [ -n "$option" ]; then set -- --storage-opt "$option" "$@"; fi
  if [ -n "$store" ]; then
    set -- --root "$store/storage" --runroot "$store/run" --storage-driver "$driver" \
      --tmpdir "$XDG_RUNTIME_DIR/libpod/tmp" "$@"
  fi
  exec "$real" "$@"
}
if [ -f "$cache/oci-runtime" ]; then
  oci=$(cat "$cache/oci-runtime")
else
  probe=$(podman_ info --format '{{.Host.OCIRuntime.Name}}' 2>&1)
  status=$?
  found=
  if [ "$status" -eq 0 ]; then
    found=yes
  else
    case $probe in
      *"OCI runtime"*"not found"*|*"no valid executable found for OCI runtime"*)
        for name in @RUNTIMES@; do
          path=$(command -v "$name" 2>/dev/null) || path=
          if [ -z "$path" ]; then
            for dir in /usr/sbin /sbin /usr/local/sbin /usr/libexec/podman; do
              if [ -x "$dir/$name" ]; then path=$dir/$name; break; fi
            done
          fi
          [ -n "$path" ] || continue
          oci=$path
          if (podman_ info >/dev/null 2>&1); then found=yes; break; fi
          oci=
        done ;;
    esac
  fi
  if [ -n "$found" ]; then
    printf '%s' "$oci" > "$cache/oci-runtime.$$" && mv -f "$cache/oci-runtime.$$" "$cache/oci-runtime"
  fi
fi
podman_ "$@"
"""


def podman_wrapper_dir() -> Path:
    """The node-local directory of the ``podman`` wrapper (first on PATH)."""
    return _store_base() / "bin"


def podman_wrapper_text() -> str:
    """The wrapper: the real podman with the node-local store (when JARVIS gives
    one) as global flags, and ``--runtime`` when the default OCI runtime is not
    on this node. It finds the real podman and the runtime on whichever node it
    runs, so the same text serves every node. With the node-local store it also
    pins the runtime dir, so a session with ``/run/user/<uid>`` (ssh) and one
    without it (a batch step) see the same containers."""
    conf = node_store_conf()
    store = shlex.quote(str(Path(conf).parent)) if conf else "''"
    return (_PODMAN_WRAPPER
            .replace("@CACHE@", shlex.quote(str(_store_base())))
            .replace("@RUNTIME@", shlex.quote(str(_runtime_base())))
            .replace("@STORE@", store)
            .replace("@RUNTIMES@", " ".join(OCI_RUNTIMES)))


def podman_wrapper() -> Optional[str]:
    """Write the wrapper and put its directory first on this process's PATH.

    Every podman call JARVIS makes or starts (pull, save, load, inspect,
    build, podman-compose) then carries the store and the runtime.
    """
    if os.name != "posix":
        return None
    directory = podman_wrapper_dir()
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    script = directory / "podman"
    text = podman_wrapper_text()
    if not script.exists() or script.read_text() != text:
        partial = directory / f".podman.{os.getpid()}"
        partial.write_text(text)
        partial.chmod(0o755)
        os.replace(partial, script)
    entries = [entry for entry in os.environ.get("PATH", "").split(os.pathsep)
               if entry and entry != str(directory)]
    os.environ["PATH"] = os.pathsep.join([str(directory), *entries])
    return str(script)


def prepare_engine(engine: str) -> None:
    """Set up what ``engine`` needs on this host before it runs (podman: runtime
    dir, store, and the wrapper that applies them with an OCI runtime it has)."""
    if engine == "podman":
        podman_runtime_dir()
        podman_store()
        podman_wrapper()


def _prepare_error(engine: str) -> Optional[str]:
    """None when :func:`prepare_engine` worked, else why (a full or read-only temp dir)."""
    try:
        prepare_engine(engine)
    except OSError as exc:
        # not tempfile.gettempdir(): it raises itself when no temp dir is usable
        return (f"JARVIS could not set up {engine}'s runtime dir, store or wrapper in "
                f"this node's temp dir on {socket.gethostname()}: {exc}")
    return None


def podman_node_script(directory: Path) -> Path:
    """Write a wrapper that gives podman on another node what it has here.

    ``sh <script> podman ...`` makes the runtime dir when the node has none,
    writes the same node-local ``storage.conf`` and the podman wrapper (the
    store as global flags, an OCI runtime that node has), puts the wrapper
    first on PATH, then runs the command.
    """
    runtime = _runtime_base()
    lines = [
        "#!/bin/sh",
        "# written by JARVIS: podman's runtime dir, image store and OCI runtime on this node",
        "set -e",
        'if [ -z "${XDG_RUNTIME_DIR:-}" ] || [ ! -w "${XDG_RUNTIME_DIR}" ]; then',
        f"  mkdir -p -m 700 {shlex.quote(str(runtime))}",
        f"  export XDG_RUNTIME_DIR={shlex.quote(str(runtime))}",
        "fi",
    ]
    conf = node_store_conf()
    if conf:
        lines += [
            f"mkdir -p -m 700 {shlex.quote(str(Path(conf).parent))}",
            f"cat > {shlex.quote(conf)} <<'JARVIS_EOF'",
            Path(conf).read_text().rstrip("\n"),
            "JARVIS_EOF",
            f"export CONTAINERS_STORAGE_CONF={shlex.quote(conf)}",
        ]
    wrapper_dir = shlex.quote(str(podman_wrapper_dir()))
    lines += [
        f"mkdir -p -m 700 {wrapper_dir}",
        f"cat > {wrapper_dir}/.podman.$$ <<'JARVIS_EOF'",
        podman_wrapper_text().rstrip("\n"),
        "JARVIS_EOF",
        f"chmod 755 {wrapper_dir}/.podman.$$",
        f"mv -f {wrapper_dir}/.podman.$$ {wrapper_dir}/podman",
        f'PATH={wrapper_dir}:"$PATH"',
        "export PATH",
    ]
    lines.append('exec "$@"')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "podman-node.sh"
    script.write_text("\n".join(lines) + "\n")
    script.chmod(0o755)
    return script


def _engine_field(executable: str, template: str) -> Optional[str]:
    try:
        result = _stall.run([executable, "info", "--format", template],
                            what=f"{executable} info")
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def engine_error(executable: str) -> Optional[str]:
    """None when ``<engine> info`` works here, else the engine's own error."""
    try:
        result = _stall.run([executable, "info"], what=f"{executable} info")
    except OSError as exc:
        return f"`{executable} info` did not run: {exc}"
    if result.returncode == 0:
        return None
    stalled = _stall.stall_error(result)
    if stalled is not None:
        return f"`{executable} info` {stalled}" + (
            f"; it printed: {_tail(result.stderr)}" if (result.stderr or "").strip() else "")
    return _tail(result.stderr) or f"`{executable} info` exited {result.returncode}"


_ENGINE_FAULTS = (
    (re.compile(r"No space left on device|Read-only file system|No usable temporary "
                r"directory|could not set up|runroot is longer than", re.I),
     "this node's temp dir cannot hold podman's node-local store (full, read-only, missing, "
     "or a path too long for podman): free it, or set TMPDIR to a short node-local path "
     "with space"),
    (re.compile(r"database configuration mismatch|pivot|graphroot|overlay", re.I),
     "podman's image store cannot work here: it is on a network file system, or another "
     "host made it. Set JARVIS_PODMAN_STORE=node so JARVIS gives each node its own store, "
     "or point CONTAINERS_STORAGE_CONF at a store on local disk"),
    (re.compile(r"subuid|subgid|newuidmap|insufficient uids|lchown", re.I),
     "rootless podman has no subordinate ids for this user here: ask the site to add the "
     "user to /etc/subuid and /etc/subgid on every node"),
    (re.compile(r"OCI runtime.*not found|no valid executable found for OCI runtime", re.I),
     "this node has no OCI runtime podman can use (JARVIS tried "
     + ", ".join(OCI_RUNTIMES) + "): ask the site to install one on every node, or set "
     "[engine] runtime in containers.conf"),
)
_ENGINE_FAULT = re.compile("|".join(pattern.pattern for pattern, _ in _ENGINE_FAULTS), re.I)


def engine_remedy(engine: str, error: str) -> str:
    for pattern, remedy in _ENGINE_FAULTS:
        if pattern.search(error):
            return remedy
    return (f"fix {engine} on this host (its own error is in the detail), or name "
            "another container_engine in the pipeline")


def unusable_issue(engine: str, error: str, evidence: Sequence[str] = ()) -> Dict[str, Any]:
    """``container_engine_unusable``: the engine is installed but cannot run here."""
    host = socket.gethostname()
    cause = ("JARVIS cannot set it up here" if "could not set up" in error
             else f"`{engine} info` fails")
    question = f"{engine} is installed on {host} but cannot run: {cause}."
    hint = engine_remedy(engine, error)
    issue = _issue("container_engine_unusable", "*", "container_engine", question,
                   method="container", scope="site", detail=_tail(error),
                   evidence=[f"`{engine} info` on {host}", *evidence], hint=hint)
    issue.update(message=question, remedy=hint, host=host, engine=engine)
    return issue


def engine_on_path(engine: str) -> Optional[str]:
    """``engine`` as this host has it: JARVIS's own podman wrapper (first on
    PATH once prepared) does not count, so a node without podman says so."""
    found = shutil.which(engine)
    if engine != "podman" or not found:
        return found
    try:
        wrapper_dir = podman_wrapper_dir().resolve()
    except OSError:
        return found
    if Path(found).resolve().parent != wrapper_dir:
        return found
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or Path(entry).resolve() == wrapper_dir:
            continue
        candidate = Path(entry) / engine
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _detect() -> Tuple[List[Dict[str, Any]], List[str], List[Dict[str, str]]]:
    found: List[Dict[str, Any]] = []
    excluded: List[str] = []
    broken: List[Dict[str, str]] = []
    apptainer = shutil.which("apptainer")
    if apptainer:
        found.append({"id": "apptainer", "constraint": "apptainer", "executable": apptainer,
                      "source": apptainer, "does": "runs as this user"})
    podman = engine_on_path("podman")
    if podman:
        error = _prepare_error("podman")
        podman = shutil.which("podman") or podman  # the wrapper, now first on PATH
        error = error or engine_error(podman)
        rootless = None if error else _engine_field(podman, "{{.Host.Security.Rootless}}")
        if error:
            broken.append({"engine": "podman", "executable": podman, "error": error})
            excluded.append(f"podman at {podman}: `podman info` failed: "
                            f"{error.splitlines()[-1] if error.splitlines() else error}")
        elif rootless == "true" or getattr(os, "geteuid", lambda: 1)() == 0:
            found.append({"id": "podman", "constraint": "podman", "executable": podman,
                          "rootless": rootless == "true", "source": podman,
                          "does": "rootless" if rootless == "true" else "runs as root"})
        else:
            excluded.append(f"podman at {podman}: not rootless")
    docker = shutil.which("docker")
    if docker:
        options = _engine_field(docker, "{{.SecurityOptions}}")
        if options and "rootless" in options:
            found.append({"id": "docker", "constraint": "docker", "executable": docker,
                          "rootless": True, "source": docker, "does": "rootless daemon"})
        else:
            excluded.append(f"docker at {docker}: a shared rootful daemon (or `docker info` "
                            "failed); used only when the pipeline names it")
    return found, excluded, broken


def detect_engines() -> Tuple[List[Dict[str, Any]], List[str]]:
    """Container engines this user can run, and why others were left out.

    Apptainer runs as the user. Podman counts when it runs rootless and
    ``podman info`` works. Docker counts only when its daemon is rootless: a
    shared rootful daemon is left out unless the pipeline names
    ``container_engine: docker``.
    """
    found, excluded, _ = _detect()
    return found, excluded


def resolve_engine(ppl, context) -> Optional[str]:
    """The pipeline's container engine, or None after a typed question or issue."""
    explicit = getattr(ppl, "container_engine_source", "yaml") == "yaml"
    engine = str(getattr(ppl, "container_engine", "") or "")
    if explicit and engine:
        if engine_on_path(engine) is None:
            context.raise_issue(_issue(
                "container_engine_missing", "*", "container_engine",
                f"The pipeline names container_engine {engine!r}, which this host "
                f"({socket.gethostname()}) does not have.",
                method="container", scope="site", evidence=[f"`command -v {engine}`"],
                hint="install the engine or name another container_engine"))
            return None
        setup_error = _prepare_error(engine)
        error = setup_error or (engine_error(engine) if engine == "podman" else None)
        if error:
            context.raise_issue(unusable_issue(engine, error))
            return None
        return engine
    found, excluded, broken = _detect()
    evidence = ["apptainer, rootless podman, rootless docker on PATH", *excluded]
    if len(found) == 1:
        chosen = str(found[0]["constraint"])
        context.log(f"[{_utc_now()}] container engine {chosen}: the only one this user "
                    "can run here\n")
    elif not found and broken:
        context.raise_issue(unusable_issue(broken[0]["engine"], broken[0]["error"],
                                           evidence))
        return None
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
    prepare_engine(chosen)
    ppl.container_engine = chosen
    ppl.container_engine_source = "detected"
    return chosen


def engine_fault(context, engine: str, package: str, output: Optional[str]) -> None:
    """Raise ``container_engine_unusable`` when a failed pull or build was the engine's fault."""
    if engine == "apptainer":
        return
    error = engine_error(engine)
    text = error or (output or "")
    if error is None and not _ENGINE_FAULT.search(text):
        return
    host = socket.gethostname()
    raise failed(context, "container_engine_unusable", "container", package, engine,
                 f"{engine} is installed on {host} but cannot run images here.",
                 text, engine_remedy(engine, text))


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
    """Whether ``image`` is in this host's own store (a SIF file for apptainer)."""
    from jarvis_cd.core.pkg import Pkg

    if engine == "apptainer":
        containers = ppl.jarvis.get_containers_dir()
        return (Path(containers) / f"{ppl.name}.sif").exists()
    return Pkg._image_exists(engine, image)


def archives_images(engine: str) -> bool:
    """Whether this host saves its images to the shared FS: podman with a node-local store."""
    return engine == "podman" and node_store_conf() is not None


def image_archive(ppl, pkg_list: Sequence[Mapping[str, Any]]) -> Path:
    """``<container env>/image.tar``, shared by the check, the install and the run jobs."""
    return env_prefix(ppl, "container", pkg_list) / "image.tar"


def archived(archive: Path, image: str) -> bool:
    """Whether ``archive`` holds ``image`` (its ``image.json`` says so)."""
    try:
        meta = json.loads(archive.with_suffix(".json").read_text())
    except (OSError, ValueError):
        return False
    return archive.is_file() and isinstance(meta, dict) and meta.get("image") == image


def archive_of(ppl, pkg_list: Sequence[Mapping[str, Any]], engine: str,
               image: str) -> Optional[Path]:
    """The shared archive of ``image`` when one is there, whatever this host's store.

    The install may have run on a node with a node-local store while this host
    (a login node serving the home locally, say) keeps the default store: the
    image is then in the archive, not here, and each run node loads it.
    Only podman loads archives (:meth:`Pipeline._load_archived_image`).
    """
    if engine != "podman":
        return None
    archive = image_archive(ppl, pkg_list)
    return archive if archived(archive, image) else None


def keep_archive(ppl, pkg_list, engine: str, image: str, context) -> Optional[Path]:
    """The archive of ``image`` the run nodes load, or None when there is none.

    A matching archive is used whatever this host's store is. A new one is saved
    (once) only from a node-local store (:func:`archives_images`): other stores
    are where the run nodes look already.
    """
    existing = archive_of(ppl, pkg_list, engine, image)
    if existing is not None:
        return existing
    if not archives_images(engine):
        return None
    archive = image_archive(ppl, pkg_list)
    if not image_present(ppl, engine, image):
        return None
    archive.parent.mkdir(parents=True, exist_ok=True)
    partial = archive.with_suffix(".tar.part")
    saved = run_logged([engine, "save", "-o", partial, image], context)
    if saved.returncode != 0 or not partial.is_file():
        partial.unlink(missing_ok=True)
        raise failed(context, "container_archive_failed", "container",
                     str(pkg_list[0].get("pkg_id") or "container") if pkg_list
                     else "container", str(archive),
                     f"{engine} could not save {image} to {archive}, so other nodes "
                     "cannot load it.", saved.stdout,
                     "check the free space and write access of the container env dir "
                     "(JARVIS_INSTALL_ENV_DIR), then install again")
    os.replace(partial, archive)
    archive.with_suffix(".json").write_text(json.dumps({
        "image": image, "engine": engine, "saved_at": _utc_now(),
        "host": socket.gethostname(), "bytes": archive.stat().st_size,
    }, indent=2) + "\n")
    context.log(f"[{_utc_now()}] archived {image} to {archive} for the other nodes\n")
    return archive


def load_archived(engine: str, image: str, archive: Optional[str]) -> bool:
    """Load ``image`` into this host's store from ``archive`` when it is not there yet."""
    from jarvis_cd.core.pkg import Pkg

    if Pkg._image_exists(engine, image):
        return True
    if not archive or not archived(Path(archive), image):
        return False
    print(f"Loading container image {image} from {archive}")
    result = subprocess.run([engine, "load", "-i", str(archive)], check=False)
    return result.returncode == 0 and Pkg._image_exists(engine, image)


def _engine_env(ppl, archive: Optional[Path] = None) -> None:
    env = getattr(ppl, "env", None)
    if env is None:
        return
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime:
        env["XDG_RUNTIME_DIR"] = runtime
    conf = node_store_conf()
    if conf:
        env["CONTAINERS_STORAGE_CONF"] = conf
    if archive is not None:
        env[ARCHIVE_ENV] = str(archive)


def engine_command(engine: str) -> str:
    """The command that runs ``engine`` on a pipeline node.

    For podman it is the node wrapper's absolute path (the run writes it on
    every node), so a command reached through ssh gets the node's store and
    OCI runtime and not the default store under the home.
    """
    if engine != "podman" or os.name != "posix":
        return engine
    try:
        wrapper = podman_wrapper_dir() / "podman"
    except OSError:
        return engine
    return shlex.quote(str(wrapper)) if wrapper.is_file() else engine


def _podman_has_compose() -> bool:
    try:
        result = _stall.run(["podman", "compose", "--help"], what="podman compose --help")
    except OSError:
        return False
    return result.returncode == 0


def compose_command(engine: str) -> str:
    """The compose command for ``engine``, runnable on every pipeline node.

    ``podman-compose`` is installed with JARVIS, in its own environment's
    ``bin``, which is not on a node's PATH: it is named by absolute path (the
    environment is on the shared file system the nodes run JARVIS from). Then
    ``podman-compose`` on PATH, then ``podman compose``. With none, the start
    is refused with a typed ``container_compose_missing`` issue.
    """
    if engine != "podman":
        return "docker compose"
    found = [Path(sys.executable).parent / "podman-compose"]
    on_path = shutil.which("podman-compose")
    if on_path:
        found.append(Path(on_path))
    for candidate in found:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return shlex.quote(str(candidate.absolute()))
    if _podman_has_compose():
        return "podman compose"
    from jarvis_cd.core.issues import JarvisIssueError, make_jarvis_issue

    raise JarvisIssueError([make_jarvis_issue(
        kind="container_compose_missing", subject="podman-compose",
        message=(f"JARVIS starts podman containers with podman-compose, which is not in "
                 f"JARVIS's environment ({Path(sys.executable).parent}) or on PATH, and "
                 "this podman has no `podman compose`."),
        identity={"python": sys.executable},
        remedy=("install podman-compose into JARVIS's Python environment "
                f"(`{sys.executable} -m pip install podman-compose`), then run again"))])


#: What JARVIS needs in a deploy image: a shell for the entrypoint, sshd for the
#: SSH-ready containers, ssh for MPI between them, and bash for ``exec``.
_READY_PROBE = (
    'missing=; '
    'for tool in bash ssh ssh-keygen; do command -v "$tool" >/dev/null 2>&1 '
    '|| missing="$missing $tool"; done; '
    '{ command -v sshd >/dev/null 2>&1 || [ -x /usr/sbin/sshd ]; } || missing="$missing sshd"; '
    'echo "JARVIS_MISSING:$missing"'
)

_READY_SCRIPT = r"""# written by JARVIS: what it needs to run {image} as SSH-ready containers
set -e
if command -v apk >/dev/null 2>&1; then
  apk add --no-cache bash openssh-server openssh-client openssh-keygen
elif command -v apt-get >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    bash openssh-server openssh-client
  rm -rf /var/lib/apt/lists/*
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y bash openssh-server openssh-clients; dnf clean all
elif command -v microdnf >/dev/null 2>&1; then
  microdnf install -y bash openssh-server openssh-clients; microdnf clean all
elif command -v yum >/dev/null 2>&1; then
  yum install -y bash openssh-server openssh-clients; yum clean all
elif command -v zypper >/dev/null 2>&1; then
  zypper --non-interactive install bash openssh
else
  echo "JARVIS: {image} has no package manager JARVIS knows (apk, apt-get, dnf, microdnf, yum, zypper) to add:{missing}" >&2
  exit 3
fi
mkdir -p /run/sshd /var/run/sshd /root/.ssh
ssh-keygen -A
"""


def ready_image(image: str) -> str:
    """The local name of ``image`` with what JARVIS adds to run it."""
    ref = image.split("@", 1)[0]
    name, tag = ref, "latest"
    if ":" in ref.rsplit("/", 1)[-1]:
        name, tag = ref.rsplit(":", 1)
    name = re.sub(r"[^a-z0-9._/-]+", "-", name.lower()).strip("/-") or "image"
    tag = re.sub(r"[^A-Za-z0-9._-]+", "-", tag)[:128] or "latest"
    return f"localhost/jarvis-ready/{name}:{tag}"


def deploy_marker(ppl, pkg_list: Sequence[Mapping[str, Any]]) -> Path:
    """``<container env>/deploy.json``: which image runs for the pulled one."""
    return env_prefix(ppl, "container", pkg_list) / "deploy.json"


def deploy_image(ppl, pkg_list: Sequence[Mapping[str, Any]], source: str) -> Optional[str]:
    """The image that runs for ``source``, once an install made it ready, else None."""
    try:
        meta = json.loads(deploy_marker(ppl, pkg_list).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or meta.get("source") != source:
        return None
    image = meta.get("image")
    return image if isinstance(image, str) and image else None


def _missing_tools(engine: str, image: str, context) -> Tuple[Optional[List[str]], str]:
    """``(missing, output)``: the tools ``image`` lacks, None when it cannot run a shell."""
    probe = run_logged([engine, "run", "--rm", "--network=host", "--entrypoint", "/bin/sh",
                        image, "-c", _READY_PROBE], context)
    for line in (probe.stdout or "").splitlines():
        if line.startswith("JARVIS_MISSING:"):
            return [str(tool) for tool in line.split(":", 1)[1].split()], probe.stdout or ""
    return None, probe.stdout or ""


def _commit_ready(engine: str, image: str, derived: str, script: str, context):
    """Run ``script`` as root in a container of ``image`` and commit it as ``derived``.

    The compose file sets the entrypoint and command, so the committed ones
    (``/bin/sh -c <script>``) never run.
    """
    name = f"jarvis-ready-{os.getpid()}-{int(time.time())}"
    try:
        made = run_logged([engine, "run", "--name", name, "--user", "0", "--network=host",
                           "--entrypoint", "/bin/sh", image, "-c", script], context)
        if made.returncode != 0:
            return made
        committed = run_logged([engine, "commit", name, derived], context)
        if committed.returncode != 0:
            committed.stdout = (made.stdout or "") + (committed.stdout or "")
        return committed
    finally:
        run_logged([engine, "rm", "-f", name], context)


def make_ready(ppl, pkg_list, engine: str, image: str, context) -> str:
    """The image that runs for ``image``: itself when it has what JARVIS needs
    (sh, bash, sshd, ssh), else a local image built on it that adds them.

    It runs where the install runs (the image is in this store or its archive),
    and records the answer in :func:`deploy_marker` for the check and the runs.
    A gap JARVIS cannot fill is a typed ``container_image_not_ready`` failure
    before any run.
    """
    package = str(pkg_list[0].get("pkg_id") or "container") if pkg_list else "container"
    if not image_present(ppl, engine, image):
        archive = archive_of(ppl, pkg_list, engine, image)
        if archive is not None:
            load_archived(engine, image, str(archive))
    remedy = ("use an image that has /bin/sh, bash and sshd (openssh-server), or let "
              "this node reach the image's package mirror, then install again")
    missing, output = _missing_tools(engine, image, context)
    if missing is None:
        raise failed(context, "container_image_not_ready", "container", package, image,
                     f"{engine} could not run /bin/sh in {image}, and JARVIS needs a "
                     "shell in it to start sshd.", output, remedy)
    deploy = image
    if missing:
        derived = ready_image(image)
        context.ensure_may_build(f"add {', '.join(missing)} to the container image {image}",
                                 [p.get("pkg_id") for p in pkg_list])
        context.report_progress(phase="building", current_package=derived)
        build_dir = env_prefix(ppl, "container", pkg_list) / "ready"
        build_dir.mkdir(parents=True, exist_ok=True)
        script = _READY_SCRIPT.format(image=image, missing=" " + " ".join(missing))
        (build_dir / "ready.sh").write_text(script)
        context.log(f"[{_utc_now()}] {image} lacks {', '.join(missing)}; making "
                    f"{derived} with them\n")
        # `run` then `commit`, not `build`: a build's RUN step needs more of the
        # host (rootless podman 3 asks systemd for its cgroup there and fails
        # without a user session) than the plain `run` the containers use
        built = _commit_ready(engine, image, derived, script, context)
        still, more = (None, "")
        if built.returncode == 0:
            still, more = _missing_tools(engine, derived, context)
        if built.returncode != 0 or still:
            engine_fault(context, engine, package, built.stdout)
            raise failed(context, "container_image_not_ready", "container", package, image,
                         f"{image} lacks {', '.join(missing)}, which JARVIS needs to run it "
                         f"on the nodes, and {engine} could not build {derived} with them.",
                         (built.stdout or "") + more, remedy)
        deploy = derived
    marker = deploy_marker(ppl, pkg_list)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({
        "source": image, "image": deploy, "added": missing, "engine": engine,
        "host": socket.gethostname(), "made_at": _utc_now(),
    }, indent=2) + "\n")
    return deploy


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
            f"{pkg_def.get('pkg_type') or package} can be installed with "
            f"{', '.join(c['id'] for c in candidates)} on this site. Which should JARVIS use?",
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

