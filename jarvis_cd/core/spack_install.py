"""Find-first Spack installation for Jarvis-CD pipelines.

The planner resolves each package's ``install_query`` with ``spack spec -I``
before building anything. Nodes that Spack already has (local ``[+]``,
upstream ``[^]`` or external ``[e]``) are reused; only missing nodes are
built. Choices JARVIS must not make silently (several installed MPIs, a
variant conflict) become machine-readable issues in ``issues.jsonl`` and the
run ends with :data:`DECISION_NEEDED_EXIT_CODE`, so a caller can ask a person
and re-run with an ``answers.json``.

Files, all under one install directory (``<execution>/install`` inside an
execution, else ``<pipeline shared>/install``):

``install.log``          append-only log of every Spack command and its output
``issues.jsonl``         ``jarvis.install-issue.v1`` lines from the latest run
``install-record.json``  ``jarvis.install-record.v1`` resolved hashes per package
``decisions.yaml``       recorded answers; always ``<pipeline shared>/install``
                         so runs reuse them (``JARVIS_SPACK_DECISIONS`` overrides)
``spack.lock``           copy of the env lock (``<pipeline shared>/spack-env``,
                         or ``JARVIS_SPACK_ENV_DIR`` when set)
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Callable, Dict, List, Mapping, Optional, Sequence

import yaml

from jarvis_cd.core.issues import JarvisIssueError, make_jarvis_issue


ISSUE_SCHEMA = "jarvis.install-issue.v1"
RECORD_SCHEMA = "jarvis.install-record.v1"
#: ``spack -C`` scope (under ``<shared>/install``) listing ``install_repos``.
REPO_SCOPE_NAME = "spack-scope"
REPORT_SCHEMA = "jarvis.install-report.v1"
DECISIONS_SCHEMA = "jarvis.install-decisions.v1"
PROGRESS_SCHEMA = "jarvis.install.progress.v1"

#: exit status of ``jarvis`` when an install needs a decision or (in check
#: mode) an install before it can continue. Read ``issues.jsonl``.
DECISION_NEEDED_EXIT_CODE = 3

MPI_PROVIDERS = (
    "openmpi",
    "mpich",
    "mvapich2",
    "mvapich",
    "intel-oneapi-mpi",
    "intel-mpi",
    "cray-mpich",
    "hpcx-mpi",
    "mpt",
    "spectrum-mpi",
)

_STATUS = {"[+]": "installed", "[^]": "upstream", "[e]": "external", " - ": "missing"}
_SPEC_LINE = re.compile(
    r"^(?P<status>\[[+^e]\]| - )\s+(?P<hash>[a-z0-9]{7,32})\s+"
    r"(?P<indent>\s*)(?P<caret>\^?)(?P<spec>[A-Za-z0-9].*?)\s*$"
)
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
_FIND_LINE = re.compile(
    r"^(?P<name>[a-z0-9][a-z0-9_-]*)@(?P<version>\S+)\s+"
    r"(?P<hash>[a-z0-9]{32})\s+(?P<prefix>/\S.*?)"
    r"(?:\s+external=(?P<external>\S*))?\s*$"
)
#: ``spack find`` format for MPI discovery; ``{external_path}`` is empty for
#: a real install and set for an external, even one at an install's prefix.
_MPI_FIND_FORMAT = "{name}@{version} {hash} {prefix} external={external_path}"
_MPI_FIND_FORMAT_LEGACY = "{name}@{version} {hash} {prefix}"
_MAX_ISSUE_TEXT = 4096


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class InstallDecisionNeeded(RuntimeError):
    """An install stopped because a person or caller has to decide.

    ``issues_path`` holds one ``jarvis.install-issue.v1`` line per question.
    The CLI exits with :data:`DECISION_NEEDED_EXIT_CODE`.
    """

    exit_code = DECISION_NEEDED_EXIT_CODE

    def __init__(
        self,
        issues: Sequence[Mapping[str, Any]],
        issues_path: Path,
        report: Optional[Mapping[str, Any]] = None,
    ):
        kinds = sorted({str(issue.get("kind")) for issue in issues})
        super().__init__(
            f"install needs {len(issues)} decision(s) ({', '.join(kinds)}); "
            f"see {issues_path}"
        )
        self.issues = [dict(issue) for issue in issues]
        self.issues_path = Path(issues_path)
        self.report = dict(report) if report is not None else None


#: exit status of ``jarvis`` when a build was refused because it would run on
#: the submitting host (e.g. a login node) instead of inside an allocation.
BUILD_REFUSED_EXIT_CODE = 4

#: set to 1/true/yes to allow builds outside an allocation (a workstation)
ALLOW_LOCAL_BUILD_ENV = "JARVIS_ALLOW_LOCAL_BUILD"

#: batch-job identity variables, in the order they are reported
_ALLOCATION_VARIABLES = (
    ("slurm", "SLURM_JOB_ID"),
    ("pbs", "PBS_JOBID"),
    ("lsf", "LSB_JOBID"),
    ("flux", "FLUX_JOB_ID"),
)

#: scheduler clients whose presence marks a host as a cluster login node
SCHEDULER_CLIENTS = ("sbatch", "qsub", "bsub", "flux")


def current_allocation() -> Optional[str]:
    """``<scheduler>:<job id>`` when this process runs inside a batch job."""
    for scheduler, variable in _ALLOCATION_VARIABLES:
        value = os.environ.get(variable, "").strip()
        if value:
            return f"{scheduler}:{value}"
    return None


def local_build_allowed() -> bool:
    """Whether the caller opted in to builds outside an allocation."""
    return os.environ.get(ALLOW_LOCAL_BUILD_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def host_is_cluster_login() -> bool:
    """True when a scheduler client is on PATH, i.e. this host submits jobs."""
    return any(shutil.which(client) for client in SCHEDULER_CLIENTS)


class BuildOutsideAllocation(RuntimeError):
    """A build, ``spack install`` or env concretize was refused on this host.

    Site login nodes may only do light work (submit, read records,
    ``spack find``, ``spack spec``). Run the install as a job with
    ``jarvis ppl install ... +submit``, or opt in with
    ``jarvis ppl install +allow_local`` / ``JARVIS_ALLOW_LOCAL_BUILD=1``.
    The CLI exits with :data:`BUILD_REFUSED_EXIT_CODE`.
    """

    exit_code = BUILD_REFUSED_EXIT_CODE

    def __init__(self, what: str, packages: Sequence[str] = ()):
        self.what = what
        self.packages = [str(package) for package in packages]
        super().__init__(
            f"refusing to {what} outside a scheduler allocation on this host; "
            "run it as a job with `jarvis ppl install [yaml FILE] +submit`, or "
            f"allow it explicitly with `+allow_local` / {ALLOW_LOCAL_BUILD_ENV}=1"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "build_outside_allocation",
            "what": self.what,
            "packages": list(self.packages),
            "exit_code": self.exit_code,
            "message": str(self),
        }


#: exit status of ``jarvis`` when an install ran and Spack failed. The typed
#: failure (``compiler_unavailable``, ``fetch_failed``, ``build_failed``, …)
#: is in ``issues.jsonl`` and the report's ``issues[]``.
INSTALL_FAILED_EXIT_CODE = 6


class InstallFailed(JarvisIssueError):
    """``spack install`` (or the env concretize) failed; ``issues`` say why."""

    def __init__(
        self,
        issues: Sequence[Mapping[str, Any]],
        issues_path: Path,
        report: Optional[Mapping[str, Any]] = None,
    ):
        super().__init__(issues, exit_code=INSTALL_FAILED_EXIT_CODE)
        self.issues_path = Path(issues_path)
        self.report = dict(report) if report is not None else None

    def to_dict(self) -> Dict[str, Any]:
        document: Dict[str, Any] = dict(self.report or {})
        document.setdefault("schema_version", REPORT_SCHEMA)
        document["satisfied"] = False
        issues = list(document.get("issues") or self.issues)
        document.update(
            failed=True,
            exit_code=self.exit_code,
            failures=[issue.get("issue_id") for issue in self.issues],
            issues=issues,
            open_issues=[issue.get("issue_id") for issue in issues],
            issues_path=str(self.issues_path),
        )
        return document


# ---------------------------------------------------------------------------
# Spack invocation
# ---------------------------------------------------------------------------


def spack_root() -> Optional[str]:
    """The explicit Spack root: ``JARVIS_SPACK_ROOT``, else ``SPACK_ROOT``."""
    for name in ("JARVIS_SPACK_ROOT", "SPACK_ROOT"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def explicit_spack_root() -> Optional[str]:
    """``JARVIS_SPACK_ROOT``: the one Spack jarvis calls, by path."""
    value = os.environ.get("JARVIS_SPACK_ROOT", "").strip()
    return value or None


def spack_executable() -> str:
    """Shell word for the Spack command.

    With ``JARVIS_SPACK_ROOT`` this is ``<root>/bin/spack`` by absolute
    path, so the ``spack`` on PATH (or a shell function) is never used.
    Otherwise it is ``spack`` (the ``SPACK_ROOT`` shell function when
    :func:`spack_shell_prefix` sources it, else the one on PATH).
    """
    root = explicit_spack_root()
    if root is None:
        return "spack"
    executable = Path(root).expanduser().resolve() / "bin" / "spack"
    if not executable.is_file():
        raise RuntimeError(f"JARVIS_SPACK_ROOT has no {executable}")
    return shlex.quote(str(executable))


def spack_shell_prefix() -> str:
    """Shell prefix that sources ``$SPACK_ROOT/share/spack/setup-env.sh``.

    Empty with ``JARVIS_SPACK_ROOT``: that Spack is called by path.
    """
    if explicit_spack_root() is not None:
        return ""
    root = spack_root()
    if root is None:
        return ""
    setup = Path(root) / "share" / "spack" / "setup-env.sh"
    return f". {shlex.quote(str(setup))} && "


def spack_command(arguments: Sequence[str]) -> str:
    """Bash command line that runs Spack with ``arguments``."""
    return spack_shell_prefix() + spack_executable() + (
        " " + shlex.join(arguments) if arguments else ""
    )


def spack_user_config_path() -> Optional[str]:
    """``JARVIS_SPACK_CONFIG``, exported as ``SPACK_USER_CONFIG_PATH``.

    Kept for compatibility only. Jarvis always sets
    ``SPACK_DISABLE_LOCAL_CONFIG=1`` alongside it, and Spack then drops the
    user scope (and the system scope) entirely, so this directory is never
    read as configuration. Site config belongs in ``<root>/etc/spack``.
    """
    config = os.environ.get("JARVIS_SPACK_CONFIG", "").strip()
    return config or None


def spack_local_config_disabled() -> bool:
    """Whether jarvis isolates Spack (``JARVIS_SPACK_ROOT`` or ``_CONFIG``)."""
    return explicit_spack_root() is not None or spack_user_config_path() is not None


def spack_subprocess_environment() -> Dict[str, str]:
    """Process environment for Spack.

    With ``JARVIS_SPACK_ROOT`` (or ``JARVIS_SPACK_CONFIG``) the user's
    ``~/.spack`` and the machine's ``/etc/spack`` never leak in:
    ``SPACK_DISABLE_LOCAL_CONFIG=1`` removes Spack's user and system scopes,
    and any active environment (``SPACK_ENV``) is dropped. What remains is
    the root's ``etc/spack/defaults`` and its site scope ``etc/spack``, plus
    the ``-C`` and ``-e`` scopes jarvis passes (see
    :func:`spack_config_scopes`).
    """
    environment = dict(os.environ)
    if spack_local_config_disabled():
        environment["SPACK_DISABLE_LOCAL_CONFIG"] = "1"
        config = spack_user_config_path()
        if config:
            # Harmless: ignored by Spack while local config is disabled.
            environment["SPACK_USER_CONFIG_PATH"] = config
        for name in ("SPACK_ENV", "SPACK_ENV_VIEW", "SPACK_OLD_PS1"):
            environment.pop(name, None)
    return environment


def _resolved_spack_root() -> Optional[Path]:
    """The Spack prefix jarvis runs: the explicit root, ``SPACK_ROOT``, or PATH."""
    root = explicit_spack_root() or spack_root()
    if root:
        return Path(root).expanduser().resolve()
    found = shutil.which("spack")
    if found is None:
        return None
    return Path(os.path.realpath(found)).parent.parent


def _spack_platform() -> str:
    if sys.platform.startswith("darwin"):
        return "darwin"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


def spack_config_scopes(
    command_line: Sequence[str] = (),
    env_dir: Optional[str | Path] = None,
) -> List[Dict[str, Any]]:
    """Configuration scopes the Spack jarvis runs reads, lowest precedence first.

    Mirrors ``spack.config.create`` (Spack 0.23): ``defaults``, ``system``
    (skipped with ``SPACK_DISABLE_LOCAL_CONFIG``), ``site`` =
    ``<root>/etc/spack``, ``user`` (skipped likewise), each with its
    ``<platform>`` subdirectory; then jarvis's ``-C`` scopes and the
    ``-e`` environment manifest. Python entry-point scopes are not listed.
    """
    disabled = spack_local_config_disabled() or "SPACK_DISABLE_LOCAL_CONFIG" in os.environ
    root = _resolved_spack_root()
    etc = root / "etc" / "spack" if root is not None else None
    directories: List[tuple[str, Optional[Path]]] = [
        ("defaults", etc / "defaults" if etc is not None else None)
    ]
    if not disabled:
        system = os.environ.get("SPACK_SYSTEM_CONFIG_PATH", "").strip() or "/etc/spack"
        directories.append(("system", Path(system)))
    directories.append(("site", etc))
    if not disabled:
        user = os.environ.get("SPACK_USER_CONFIG_PATH", "").strip() or "~/.spack"
        directories.append(("user", Path(user).expanduser()))
    platform = _spack_platform()
    scopes: List[Dict[str, Any]] = []
    for name, path in directories:
        scopes.append(
            {"name": name, "path": str(path) if path else None,
             "exists": bool(path and path.is_dir())}
        )
        if path is not None and (path / platform).is_dir():
            scopes.append(
                {"name": f"{name}/{platform}", "path": str(path / platform), "exists": True}
            )
    for scope in command_line:
        scopes.append(
            {"name": "command_line", "path": str(scope), "exists": Path(scope).is_dir()}
        )
    if env_dir:
        manifest = Path(env_dir) / "spack.yaml"
        scopes.append(
            {"name": "environment", "path": str(manifest), "exists": manifest.is_file()}
        )
    return scopes


# ---------------------------------------------------------------------------
# Missing Spack: a typed ``spack_missing`` issue with ``site_spacks``
# ---------------------------------------------------------------------------

#: os.pathsep list of Spack roots a site offers (checked first)
SITE_SPACKS_ENV = "JARVIS_SITE_SPACKS"
#: well-known Spack locations probed (cheap ``is_file`` checks only)
_WELL_KNOWN_SPACKS = (
    "/opt/spack",
    "/usr/local/spack",
    "/opt/apps/spack",
    "/apps/spack",
    "/sw/spack",
    "/shared/spack",
    "~/spack",
)
_SPACK_VERSION_LINE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.M)
_SPACK_VERSION_INFO = re.compile(r"^spack_version_info\s*=\s*\(([^)]*)\)", re.M)


def spack_unavailable_reason() -> Optional[str]:
    """Why jarvis has no Spack to run, or None when it has one.

    Light: file checks and a PATH lookup, nothing is executed.
    """
    root = explicit_spack_root()
    if root is not None:
        executable = Path(root).expanduser().resolve() / "bin" / "spack"
        return None if executable.is_file() else f"JARVIS_SPACK_ROOT={root} has no bin/spack"
    root = spack_root()
    if root is not None:
        setup = Path(root).expanduser() / "share" / "spack" / "setup-env.sh"
        return None if setup.is_file() else f"SPACK_ROOT={root} has no share/spack/setup-env.sh"
    if shutil.which("spack") is not None:
        return None
    return "no JARVIS_SPACK_ROOT, no SPACK_ROOT and no spack on PATH"


def spack_checkout_version(root: Path) -> Optional[str]:
    """A Spack checkout's release, read from its source (nothing is run)."""
    init = Path(root) / "lib" / "spack" / "spack" / "__init__.py"
    try:
        text = init.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _SPACK_VERSION_LINE.search(text)
    if match:
        return match.group(1)
    match = _SPACK_VERSION_INFO.search(text)
    if match:
        parts = [part.strip().strip("'\"") for part in match.group(1).split(",")]
        return ".".join(part for part in parts if part) or None
    return None


def discover_site_spacks() -> List[Dict[str, Any]]:
    """Spack checkouts this host offers, as ``spack_missing`` candidates.

    Sources, in order: ``JARVIS_SITE_SPACKS``, the resource graph's site dir
    (``<dir of JARVIS_RESOURCE_GRAPH>/spack``), well-known locations, and
    the parent of a broken ``JARVIS_SPACK_ROOT``/``SPACK_ROOT``. Each one
    must have ``bin/spack``; its release is read from the source tree.
    """
    probes: List[tuple[str, str]] = []
    for item in os.environ.get(SITE_SPACKS_ENV, "").split(os.pathsep):
        if item.strip():
            probes.append((item.strip(), SITE_SPACKS_ENV))
    graph = os.environ.get("JARVIS_RESOURCE_GRAPH", "").strip()
    if graph:
        probes.append((str(Path(graph).expanduser().parent / "spack"), "resource_graph_site"))
    for item in _WELL_KNOWN_SPACKS:
        probes.append((item, "well_known"))
    for name in ("JARVIS_SPACK_ROOT", "SPACK_ROOT"):
        value = os.environ.get(name, "").strip()
        if value:
            probes.append((str(Path(value).expanduser().parent / "spack"), f"{name}_sibling"))
    found: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw, source in probes:
        try:
            root = Path(os.path.expandvars(raw)).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        key = str(root)
        if key in seen or not (root / "bin" / "spack").is_file():
            continue
        seen.add(key)
        version = spack_checkout_version(root)
        found.append(
            {
                "id": f"spack-{len(found) + 1}",
                "constraint": key,
                "root": key,
                "version": version,
                "executable": str(root / "bin" / "spack"),
                "source": source,
                "use": f"JARVIS_SPACK_ROOT={key}",
                "prepare": f"jarvis spack prepare --site_spack {shlex.quote(key)}",
            }
        )
        if len(found) >= 16:
            break
    return found


def spack_missing_issue(reason: str, site_spacks: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """The ``spack_missing`` install issue (site scope, subject ``spack``)."""
    issue = make_issue(
        kind="spack_missing",
        package="*",
        subject="spack",
        scope="site",
        question=(
            "Which Spack should jarvis use on this site?"
            if site_spacks
            else "No Spack was found on this site; which Spack root should jarvis use?"
        ),
        candidates=site_spacks,
        detail=reason,
        evidence=[
            "JARVIS_SPACK_ROOT, SPACK_ROOT, `command -v spack`",
            f"{SITE_SPACKS_ENV}, <resource graph dir>/spack, "
            + ", ".join(_WELL_KNOWN_SPACKS),
        ],
        answerable=True,
        hint=(
            "answer with a site_spacks root (recorded as the site decision `spack` "
            "and used as JARVIS_SPACK_ROOT), or set JARVIS_SPACK_ROOT; "
            "`jarvis spack prepare --site_spack <root>` makes a jarvis Spack "
            "chained to a site one"
        ),
    )
    issue["site_spacks"] = [dict(item) for item in site_spacks]
    return issue


def ensure_spack_available(context: "InstallContext", decisions: "Decisions") -> Optional[Dict[str, Any]]:
    """Make sure jarvis has a Spack; else raise the ``spack_missing`` issue.

    A recorded answer (site decision ``spack``) is replayed only for the
    exact same issue_id; an answer from ``answers.json`` is recorded. Either
    one becomes ``JARVIS_SPACK_ROOT`` for this process (so the run's
    ``spack load`` and the provenance use it too). Returns the open issue,
    or None when Spack is available.
    """
    reason = spack_unavailable_reason()
    if reason is None:
        return None
    issue = spack_missing_issue(reason, discover_site_spacks())
    answer: Optional[Mapping[str, Any]] = None
    recorded = decisions.site("spack")
    if recorded is not None and recorded.get("constraint"):
        if recorded.get("issue_id") == issue["issue_id"]:
            answer = recorded
            context.note_decision(issue, recorded, source="decisions_file",
                                  path=decisions.path)
        else:
            stale = {
                "issue_id": recorded.get("issue_id"),
                "subject": "spack",
                "scope": "site",
                "constraint": recorded.get("constraint"),
                "path": str(decisions.path),
                "answered_at": recorded.get("answered_at"),
                "superseded_by": issue["issue_id"],
                "reason": "the Spack candidates changed, so the question changed",
            }
            if stale not in context.stale_decisions:
                context.stale_decisions.append(stale)
            issue["supersedes"] = recorded.get("issue_id")
            issue["previous_answer"] = recorded.get("constraint")
    if answer is None:
        answer = context.answer_for(issue)
        if answer is not None:
            root = Path(str(answer["constraint"])).expanduser()
            if not (root / "bin" / "spack").is_file():
                raise ValueError(f"spack_missing answer {root} has no bin/spack")
            decisions.record(issue, answer)
    if answer is None:
        context.raise_issue(issue)
        return issue
    root = str(Path(str(answer["constraint"])).expanduser().resolve())
    os.environ["JARVIS_SPACK_ROOT"] = root
    context.log(f"[{_utc_now()}] spack_missing answered: JARVIS_SPACK_ROOT={root}\n")
    return None


class SpackRunner:
    """Run ``spack`` through bash with the selected root, logging everything."""

    def __init__(self, log: Optional[IO[str]] = None):
        self.log = log
        self.commands: List[str] = []
        #: ``-C`` scopes (e.g. the pipeline's ``repos.yaml``) put on every
        #: command except ``-e DIR`` ones, whose manifest lists the repos.
        self.config_scopes: List[str] = []

    def _log(self, text: str) -> None:
        if self.log is not None:
            self.log.write(text)
            self.log.flush()

    def run(
        self, arguments: Sequence[str], *, stream: bool = False
    ) -> subprocess.CompletedProcess[str]:
        """Run one Spack command; ``stream`` copies output line by line."""
        if self.config_scopes and list(arguments[:1]) != ["-e"]:
            arguments = [
                *(token for scope in self.config_scopes for token in ("-C", scope)),
                *arguments,
            ]
        command = spack_executable() + " " + shlex.join(arguments)
        self.commands.append(command)
        script = spack_command(arguments)
        self._log(f"[{_utc_now()}] $ {command}\n")
        environment = spack_subprocess_environment()
        if stream:
            process = subprocess.Popen(
                ["bash", "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
            )
            lines: List[str] = []
            assert process.stdout is not None
            for line in process.stdout:
                lines.append(line)
                self._log(line)
            return_code = process.wait()
            output = "".join(lines)
            completed = subprocess.CompletedProcess(
                ["bash", "-c", script], return_code, output, ""
            )
        else:
            completed = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                check=False,
            )
            self._log(completed.stdout)
            self._log(completed.stderr)
        self._log(f"[{_utc_now()}] exit {completed.returncode}\n")
        if completed.returncode == 127 and "spack" in (
            (completed.stderr or "") + (completed.stdout or "")
        ):
            raise RuntimeError(
                "spack is not available; set JARVIS_SPACK_ROOT to a Spack checkout"
            )
        return completed


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpecNode:
    """One node of a ``spack spec -I -l`` tree."""

    status: str
    hash: str
    name: str
    spec: str
    depth: int

    @property
    def present(self) -> bool:
        return self.status != "missing"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "hash": self.hash,
            "status": self.status,
            "spec": self.spec[:512],
        }


def parse_spec_tree(text: str) -> List[SpecNode]:
    """Parse the concretized tree printed by ``spack spec -I -l``."""
    lines = text.splitlines()
    start = 0
    for index, line in enumerate(lines):
        if line.strip().lower() == "concretized":
            start = index + 1
    nodes: List[SpecNode] = []
    seen: set[str] = set()
    for line in lines[start:]:
        match = _SPEC_LINE.match(line)
        if match is None:
            continue
        spec = match.group("spec")
        name_match = _NAME.match(spec)
        if name_match is None:
            continue
        node_hash = match.group("hash")
        if node_hash in seen:
            continue
        seen.add(node_hash)
        nodes.append(
            SpecNode(
                status=_STATUS[match.group("status")],
                hash=node_hash,
                name=name_match.group(0),
                spec=spec,
                depth=0 if not match.group("caret") else 1 + len(match.group("indent")) // 4,
            )
        )
    return nodes


def classify_concretize_failure(output: str) -> str:
    """Map a failed ``spack spec`` into an issue kind."""
    lowered = output.lower()
    if "conflict" in lowered:
        return "variant_conflict"
    if (
        "unknownpackageerror" in lowered
        or "unknown package" in lowered
        or "no such package" in lowered
        or re.search(r"package '?[a-z0-9_.-]+'? not found", lowered)
        or "repository is not registered" in lowered
    ):
        return "no_recipe"
    return "concretize_failed"


def _bounded(text: str, limit: int = _MAX_ISSUE_TEXT) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return "[truncated] " + text[-limit:]


def _error_excerpt(output: str) -> str:
    """The Spack error lines, or the tail of the output."""
    errors = [line for line in output.splitlines() if "error" in line.lower()]
    return _bounded("\n".join(errors) if errors else output, 2048)


# ---------------------------------------------------------------------------
# Typed install failures
# ---------------------------------------------------------------------------

_COMPILER_ACCESS = re.compile(
    r"CompilerAccessError: Compiler '(?P<spec>[^']+)' has executables that are "
    r"missing or are not executable: \[(?P<paths>[^\]]*)\]"
)
_INSTALLING = re.compile(r"==> Installing (?P<node>\S+)")
_BUILD_LOG = re.compile(r"See build log for details:\s*\n\s*(?P<path>/\S+)")
_FETCH_FAILURE = re.compile(
    r"FetchError|All fetchers failed|Failed to fetch|Manual download is required|"
    r"ChecksumError|NoDigestError|Could not resolve host",
    re.IGNORECASE,
)
_BUILD_FAILURE = re.compile(
    r"ProcessError|InstallError|BuildError|Command exited with status|"
    r"failed to install|build failed",
    re.IGNORECASE,
)


def _log_tail(text: str, lines: int = 40) -> str:
    return _bounded("\n".join(text.splitlines()[-lines:]), 4096)


def compiler_question_candidates(
    usable: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """``compiler_unavailable`` candidates: the build host's usable compilers,
    full toolchains (cc, cxx and fc) first."""
    ordered = sorted(usable, key=lambda item: not item.get("full"))
    return [
        {
            "id": f"compiler-{index + 1}",
            "constraint": item["spec"],
            "full": bool(item.get("full")),
            **{lang: (item.get("paths") or {}).get(lang) for lang in ("cc", "cxx", "fc")},
        }
        for index, item in enumerate(ordered)
    ]


def compiler_unavailable_issue(
    compiler: str,
    missing: Sequence[str],
    *,
    host: str,
    usable: Sequence[Mapping[str, Any]],
    origin: str,
    evidence: Sequence[str] = (),
) -> Dict[str, Any]:
    """The ``compiler_unavailable`` question (site scope, ``subject: compiler``).

    Its ``issue_id`` digests only the usable candidate set, so the answer (a
    usable compiler, applied as a binding ``require``) is replayed while the
    build hosts offer the same compilers.
    """
    candidates = compiler_question_candidates(usable)
    missing_text = ", ".join(missing) or "not configured"
    issue = make_issue(
        kind="compiler_unavailable",
        package="*",
        subject="compiler",
        scope="site",
        question=(
            f"Compiler {compiler} cannot run on the build host {host} "
            f"(missing or not executable: {missing_text}). Which compiler "
            "should builds use?"
        ),
        candidates=candidates,
        detail=f"{compiler}: {missing_text}",
        evidence=list(evidence),
        answerable=bool(candidates),
        hint=(
            "answer with one of the candidates; it becomes a binding "
            "packages:all:require for every build"
            if candidates
            else "make a compiler available on the build nodes (module or "
            "path) and re-run `jarvis spack prepare` inside a job"
        ),
    )
    issue.update(
        message=issue["question"],
        compiler=compiler,
        host=host,
        missing_executables=list(missing),
        usable_compilers=[item["constraint"] for item in candidates],
        origin=origin,
        field="compiler",
        remedy=(
            "answer this issue with a usable compiler (answers.json), or run "
            "`jarvis spack prepare --compiler <spec>` with a compiler the build "
            "nodes have"
        ),
    )
    return issue


def classify_install_failure(
    output: str,
    *,
    stage: str,
    packages: Sequence[str],
    usable_compilers: Sequence[Mapping[str, Any]] = (),
    log_path: Optional[Path] = None,
    command: Optional[str] = None,
) -> Dict[str, Any]:
    """Map a failed ``spack install``/``concretize`` to one typed issue.

    ``compiler_unavailable`` (``CompilerAccessError``), ``fetch_failed``,
    ``build_failed``, a concretize kind (``variant_conflict``, ``no_recipe``,
    ``concretize_failed``) for ``stage='concretize'``, else
    ``install_failed``. Every one carries ``host``, ``stage``,
    ``failed_node`` (the last ``==> Installing`` line), ``log`` and
    ``log_tail``.
    """
    host = socket.gethostname()
    node = None
    for match in _INSTALLING.finditer(output):
        node = match.group("node")
    package = packages[0] if len(packages) == 1 else "*"
    excerpt = _error_excerpt(output)
    evidence = [item for item in (command, f"log: {log_path}" if log_path else None) if item]
    access = _COMPILER_ACCESS.search(output)
    if access is not None:
        compiler = access.group("spec").replace("@=", "@")
        missing = re.findall(r"'([^']+)'", access.group("paths"))
        # Spack could not run it here, whatever the earlier check found.
        usable = [item for item in usable_compilers
                  if not _same_compiler(str(item.get("spec")), compiler)]
        issue = compiler_unavailable_issue(
            compiler, missing, host=host, usable=usable,
            origin=f"spack {stage}", evidence=evidence,
        )
    else:
        if stage == "concretize":
            kind = classify_concretize_failure(output)
            question = f"Spack could not concretize the environment for {', '.join(packages)}."
            remedy = "answer with a spec constraint, or change install_query"
        elif _FETCH_FAILURE.search(output):
            kind = "fetch_failed"
            question = f"Spack could not download sources while installing {', '.join(packages)}."
            remedy = ("check the build node's network/proxy, or put the source in a "
                      "Spack mirror and re-run")
        elif _BUILD_FAILURE.search(output):
            kind = "build_failed"
            question = f"A Spack build failed while installing {', '.join(packages)}."
            remedy = "read build_log; change the spec (variants, versions) or fix the recipe"
        else:
            kind = "install_failed"
            question = f"spack {stage} failed for {', '.join(packages)}."
            remedy = "read log_tail and install.log"
        issue = make_issue(
            kind=kind,
            package=package,
            subject=node or ", ".join(packages),
            scope="package",
            question=question,
            detail=excerpt,
            evidence=evidence,
            answerable=kind in ("variant_conflict", "concretize_failed"),
        )
        issue.update(message=question, remedy=remedy)
    build_log = _BUILD_LOG.search(output)
    issue.update(
        stage=stage,
        packages=list(packages),
        failed_node=node,
        log=str(log_path) if log_path else None,
        log_tail=_log_tail(output),
    )
    if build_log is not None:
        issue["build_log"] = build_log.group("path")
    if "host" not in issue:
        issue["host"] = host
    return issue


# ---------------------------------------------------------------------------
# Compilers on the build host
# ---------------------------------------------------------------------------

BUILD_COMPILERS_SCHEMA = "jarvis.build-compilers.v1"
BUILD_COMPILERS_NAME = "build-compilers.json"
#: ``spack -C`` scope (under the install dir) with the compiler exclusion
#: and the binding compiler ``require``
COMPILER_SCOPE_NAME = "spack-compiler-scope"
#: site-prep compiler sources that are a person's choice, hence binding
BINDING_COMPILER_SOURCES = ("person", "answer", "person_earlier")
_COMPILER_PACKAGES = ("gcc", "llvm", "intel-oneapi-compilers", "aocc")
_COMPILER_LANGUAGES = ("cc", "cxx", "f77", "fc")


def _executable_problem(path: str) -> Optional[str]:
    text = str(path)
    if not os.path.isabs(text):
        return None if shutil.which(text) else "not on PATH"
    if not os.path.exists(text):
        return "missing"
    if not os.path.isfile(text) or not os.access(text, os.X_OK):
        return "not executable"
    return None


def configured_compilers(runner: "SpackRunner") -> List[Dict[str, Any]]:
    """Compilers the Spack config lists: ``compilers:`` (0.x) or the compiler
    externals in ``packages:`` (1.x), each with its raw config ``entry``."""
    found: List[Dict[str, Any]] = []
    completed = runner.run(["config", "get", "compilers"])
    loaded = yaml.safe_load(completed.stdout or "") if completed.returncode == 0 else None
    entries = loaded.get("compilers") if isinstance(loaded, Mapping) else None
    for entry in entries or []:
        compiler = entry.get("compiler") if isinstance(entry, Mapping) else None
        if not isinstance(compiler, Mapping) or not compiler.get("spec"):
            continue
        paths = compiler.get("paths") or {}
        found.append({
            "spec": str(compiler["spec"]).replace("@=", "@"),
            "paths": {lang: str(paths[lang]) for lang in _COMPILER_LANGUAGES
                      if paths.get(lang)},
            "modules": [str(item) for item in compiler.get("modules") or []],
            "source": "compilers",
            "entry": json.loads(json.dumps(entry, default=str)),
        })
    if found:
        return found
    completed = runner.run(["config", "get", "packages"])
    loaded = yaml.safe_load(completed.stdout or "") if completed.returncode == 0 else None
    packages = loaded.get("packages") if isinstance(loaded, Mapping) else None
    for name in _COMPILER_PACKAGES:
        settings = (packages or {}).get(name) or {}
        for item in settings.get("externals") or []:
            if not isinstance(item, Mapping) or not item.get("spec"):
                continue
            attributes = (item.get("extra_attributes") or {}).get("compilers") or {}
            paths = {"cc": attributes.get("c"), "cxx": attributes.get("cxx"),
                     "fc": attributes.get("fortran")}
            found.append({
                "spec": str(item["spec"]).split()[0].replace("@=", "@"),
                "paths": {lang: str(path) for lang, path in paths.items() if path},
                "modules": [str(module) for module in item.get("modules") or []],
                "source": f"packages:{name}",
                "entry": json.loads(json.dumps(item, default=str)),
            })
    return found


def verify_compilers(compilers: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Check every compiler's executables on this host, as Spack's
    ``CompilerAccessError`` would (each configured path must be executable).

    A compiler loaded through ``modules`` is not checked (its paths exist
    only after the module load) and counts as usable.
    """
    checked: List[Dict[str, Any]] = []
    for compiler in compilers:
        item = dict(compiler)
        paths = item.get("paths") or {}
        item["full"] = all(paths.get(lang) for lang in ("cc", "cxx", "fc"))
        if item.get("modules"):
            item.update(verified=False, usable=True, missing=[],
                        note="loaded by modules; not checked")
        else:
            problems = {path: _executable_problem(path) for path in paths.values()}
            item["missing"] = [path for path, problem in problems.items() if problem]
            item["problems"] = {path: problem for path, problem in problems.items()
                                if problem}
            item.update(verified=True, usable=not item["missing"])
        checked.append(item)
    return {
        "schema_version": BUILD_COMPILERS_SCHEMA,
        "host": socket.gethostname(),
        "checked_at": _utc_now(),
        "allocation": current_allocation(),
        "compilers": checked,
        "usable": [item["spec"] for item in checked if item["usable"]],
        "unusable": [
            {"spec": item["spec"], "missing": item["missing"]}
            for item in checked if not item["usable"]
        ],
    }


def find_compiler(
    compilers: Sequence[Mapping[str, Any]], request: str
) -> Optional[Mapping[str, Any]]:
    """The compiler ``request`` (``gcc@11.4.0``, ``gcc@11``, ``gcc``) names;
    a usable one first."""
    name, _, version = request.replace("@=", "@").lstrip("%").partition("@")
    found = [
        item for item in compilers
        if item["spec"].split("@")[0] == name
        and (not version or item["spec"].partition("@")[2].startswith(version))
    ]
    found.sort(key=lambda item: not item.get("usable"))
    return found[0] if found else None


_NODE_COMPILER = re.compile(
    r"%(?:\[[^\]]*\]\s*)?(?:[a-z]+(?:,[a-z]+)*=)?"
    r"(?P<spec>[a-z][a-z0-9_-]*@=?[0-9][A-Za-z0-9._-]*)"
)


def node_compiler(spec: str) -> Optional[str]:
    """The compiler (``gcc@12.3.0``) a ``spack spec`` node line names."""
    match = _NODE_COMPILER.search(spec)
    return match.group("spec").replace("@=", "@") if match else None


def _same_compiler(found: Optional[str], spec: str) -> bool:
    return bool(found) and (found == spec or found.startswith(spec + "."))


def unusable_compiler_issue(
    nodes: Sequence["SpecNode"],
    *,
    unusable: Sequence[Mapping[str, Any]],
    usable: Sequence[Mapping[str, Any]],
    host: str,
    origin: str,
    evidence: Sequence[str] = (),
) -> Optional[Dict[str, Any]]:
    """``compiler_unavailable`` when a node to build uses a compiler the
    build host cannot run, or ``None``.

    ``to_build`` lists those nodes; ``reused_specs`` lists the installed and
    upstream nodes built with that compiler (often ``gcc-runtime`` and the
    libraries that pulled the rest of the DAG onto it).
    """
    bad = {str(item["spec"]): item for item in unusable}

    def hit(node: "SpecNode") -> Optional[str]:
        found = node_compiler(node.spec)
        return next((spec for spec in bad if _same_compiler(found, spec)), None)

    to_build = [(node, hit(node)) for node in nodes if not node.present]
    to_build = [(node, spec) for node, spec in to_build if spec]
    if not to_build:
        return None
    compiler = to_build[0][1]
    reused = [node for node in nodes if node.present and hit(node)]
    issue = compiler_unavailable_issue(
        compiler, list(bad[compiler].get("missing") or []), host=host,
        usable=usable, origin=origin, evidence=evidence,
    )
    names = ", ".join(sorted({node.name for node in reused})) or "none"
    issue["message"] = (
        f"{issue['question']} {len(to_build)} node(s) would be built with "
        f"{compiler}; reused specs built with it: {names}."
    )
    issue["to_build"] = [
        {**node.to_dict(), "compiler": spec} for node, spec in to_build[:64]
    ]
    issue["reused_specs"] = [
        {**node.to_dict(), "compiler": node_compiler(node.spec)} for node in reused[:64]
    ]
    return issue


def reuse_filters_supported(spack_version: Optional[str]) -> bool:
    """``concretizer:reuse:from`` with ``exclude`` exists from Spack 0.22."""
    if not spack_version:
        return True
    parts = [int(part) for part in re.findall(r"\d+", spack_version)[:2]]
    return len(parts) < 2 or tuple(parts) >= (0, 22)


def compiler_requirement(spec: str, spack_version: Optional[str]) -> List[str]:
    """``packages:all:require`` entries that bind every build to ``spec``."""
    if spack_version and spack_version.split(".")[0].isdigit() and int(spack_version.split(".")[0]) >= 1:
        return [f"%[when=%c] c={spec}", f"%[when=%cxx] cxx={spec}"]
    return [f"%{spec}"]


def compiler_scope_sections(
    document: Mapping[str, Any],
    required: Optional[str],
    spack_version: Optional[str] = None,
) -> Dict[str, Any]:
    """Config sections that keep unusable compilers out and bind ``required``.

    Keys ending in ``:`` are Spack overrides (``compilers::``), so lower
    scopes (the site's ``compilers.yaml``) cannot add the unusable compilers
    back. Installed and upstream specs built with an unusable compiler are
    not reused either (``concretizer:reuse:from`` excludes
    ``%<compiler>``): reusing one (``gcc-runtime``, a library) would pull the
    new builds of its DAG onto that compiler. They are rebuilt instead.
    """
    compilers = list(document.get("compilers") or [])
    usable = [item for item in compilers if item.get("usable")]
    unusable = [item for item in compilers if not item.get("usable")]
    sections: Dict[str, Any] = {}
    if unusable and reuse_filters_supported(spack_version):
        exclude = [f"%{item['spec']}" for item in unusable]
        sections["concretizer"] = {
            "reuse:": {
                "roots": True,
                "from": [
                    {"type": "local", "exclude": list(exclude)},
                    {"type": "buildcache", "exclude": list(exclude)},
                    {"type": "external"},
                ],
            }
        }
    if unusable and usable:
        if any(item["source"] == "compilers" for item in unusable):
            sections["compilers:"] = [
                item["entry"] for item in usable if item["source"] == "compilers"
            ]
        for name in sorted({
            item["source"].split(":", 1)[1]
            for item in unusable if item["source"].startswith("packages:")
        }):
            sections.setdefault("packages", {})[name] = {
                "externals:": [
                    item["entry"] for item in usable
                    if item["source"] == f"packages:{name}"
                ]
            }
    if required:
        sections.setdefault("packages", {}).setdefault("all", {})["require:"] = (
            compiler_requirement(required, spack_version)
        )
    return sections


def write_compiler_scope(scope_dir: Path, sections: Mapping[str, Any]) -> Path:
    """A ``spack -C`` scope holding ``sections`` (``compilers.yaml``,
    ``packages.yaml``, ``concretizer.yaml``); files for absent sections are
    removed."""
    scope_dir.mkdir(parents=True, exist_ok=True)
    files = {
        f"{name}.yaml": {key: value for key, value in sections.items()
                         if key.rstrip(":") == name}
        for name in ("compilers", "packages", "concretizer")
    }
    for name, content in files.items():
        path = scope_dir / name
        if content:
            text = yaml.safe_dump(content, sort_keys=True)
            try:
                current = path.read_text(encoding="utf-8")
            except OSError:
                current = None
            if current != text:
                _atomic_write_text(path, text)
        elif path.exists():
            path.unlink()
    return scope_dir


def _site_build_compilers_path() -> Optional[Path]:
    root = explicit_spack_root()
    return Path(root) / "var" / "jarvis" / BUILD_COMPILERS_NAME if root else None


def apply_build_compilers(
    planner: "SpackPlanner",
    context: "InstallContext",
    record: Dict[str, Any],
) -> Dict[str, Any]:
    """Verify the compilers where the build runs; keep unusable ones out.

    In install mode the compilers are checked on this host (the build job's
    node) and the result is written to ``<install>/build-compilers.json`` and
    ``<JARVIS_SPACK_ROOT>/var/jarvis/build-compilers.json``. A check (on a
    login node) reuses that last build-host result instead of trusting the
    login node's compilers. The binding compiler is a person's choice: the
    site prep's ``--compiler``/``ambiguous_compiler`` answer, or an answer to
    ``compiler_unavailable``. Returns ``{"sections", "blocked"}``: ``blocked``
    is the ``compiler_unavailable`` issue to raise if anything must be built.
    """
    state: Dict[str, Any] = {"sections": {}, "blocked": None}
    document: Optional[Dict[str, Any]] = None
    if context.mode == "install":
        document = verify_compilers(configured_compilers(planner.runner))
        document["source"] = "verified"
        text = json.dumps(document, indent=2, sort_keys=True) + "\n"
        _atomic_write_text(context.install_dir / BUILD_COMPILERS_NAME, text)
        site_path = _site_build_compilers_path()
        if site_path is not None:
            try:
                _atomic_write_text(site_path, text)
            except OSError as error:
                context.log(f"WARNING: could not write {site_path}: {error}\n")
    else:
        site_path = _site_build_compilers_path()
        try:
            document = json.loads(site_path.read_text(encoding="utf-8")) if site_path else None
            document["source"] = "recorded"
            document["path"] = str(site_path)
        except (OSError, ValueError, TypeError):
            document = None
    if document is None:
        record["build_compilers"] = {"source": None}
        return state
    compilers = document.get("compilers") or []
    usable = [item for item in compilers if item.get("usable")]
    planner.usable_compilers = [dict(item) for item in usable]
    planner.unusable_compilers = [
        {"spec": item["spec"], "missing": list(item.get("missing") or [])}
        for item in compilers if not item.get("usable")
    ]
    planner.build_host = str(document.get("host"))
    site = record.get("spack_site_prep") or {}
    required: Optional[str] = None
    required_source: Optional[str] = None
    if site.get("compiler") and site.get("compiler_source") in BINDING_COMPILER_SOURCES:
        required, required_source = str(site["compiler"]), f"site_prep:{site['compiler_source']}"
    question = compiler_unavailable_issue(
        "?", [], host=str(document.get("host")), usable=usable, origin="replay"
    ) if usable else None
    recorded = planner.decisions.site("compiler")
    if recorded is not None and recorded.get("kind") != "compiler_unavailable":
        recorded = None
    if recorded is not None:
        if (question is not None and recorded.get("issue_id") == question["issue_id"]
                and find_compiler(usable, str(recorded.get("constraint")))):
            required, required_source = str(recorded["constraint"]), "decisions_file"
            context.note_decision(question, recorded, source="decisions_file",
                                  path=planner.decisions.path)
        else:
            stale = {
                "issue_id": recorded.get("issue_id"),
                "subject": "compiler",
                "scope": "site",
                "constraint": recorded.get("constraint"),
                "path": str(planner.decisions.path),
                "answered_at": recorded.get("answered_at"),
                "superseded_by": question["issue_id"] if question else None,
                "reason": "the build host's usable compilers changed",
            }
            if stale not in context.stale_decisions:
                context.stale_decisions.append(stale)
    if question is not None:
        answer = context.answer_for(question)
        if answer is not None and find_compiler(usable, str(answer["constraint"])):
            planner.decisions.record(question, answer)
            required, required_source = str(answer["constraint"]), "answers_file"
    if required:
        chosen = find_compiler(compilers, required)
        if chosen is None or not chosen.get("usable"):
            issue = compiler_unavailable_issue(
                required,
                list(chosen.get("missing") or []) if chosen else [],
                host=str(document.get("host")),
                usable=usable,
                origin=f"build host check ({document['source']})",
                evidence=["spack config get compilers",
                          str(context.install_dir / BUILD_COMPILERS_NAME)],
            )
            if recorded is not None and recorded.get("issue_id") == issue["issue_id"]:
                issue["previous_answer"] = recorded.get("constraint")
            answer = context.answer_for(issue)
            if answer is not None:
                planner.decisions.record(issue, answer)
                required, required_source = str(answer["constraint"]), "answers_file"
            else:
                state["blocked"] = issue
                required = None
    root = explicit_spack_root()
    sections = compiler_scope_sections(
        document, required, spack_checkout_version(Path(root)) if root else None
    )
    scope = None
    if sections or (context.install_dir / COMPILER_SCOPE_NAME).exists():
        scope = write_compiler_scope(context.install_dir / COMPILER_SCOPE_NAME, sections)
    if sections:
        planner.runner.config_scopes.append(str(scope))
    state["sections"] = sections
    excluding = "compilers:" in sections or any(
        "externals:" in value for value in sections.get("packages", {}).values()
    )
    record["build_compilers"] = {
        "source": document["source"],
        "host": document.get("host"),
        "checked_at": document.get("checked_at"),
        "allocation": document.get("allocation"),
        "usable": [item["spec"] for item in usable],
        "unusable": [
            {"spec": item["spec"], "missing": item.get("missing") or []}
            for item in compilers if not item.get("usable")
        ],
        "excluded": [item["spec"] for item in compilers if not item.get("usable")]
        if excluding else [],
        "reuse_excluded": next(
            (list(entry.get("exclude") or []) for entry in
             sections.get("concretizer", {}).get("reuse:", {}).get("from", [])
             if entry.get("type") == "local"),
            [],
        ),
        "required": {
            "spec": required,
            "source": required_source,
            "require": sections["packages"]["all"]["require:"],
        } if required else None,
        "blocked_by": state["blocked"]["issue_id"] if state["blocked"] else None,
        "scope": str(scope) if sections else None,
        "path": document.get("path") or str(context.install_dir / BUILD_COMPILERS_NAME),
    }
    return state


# ---------------------------------------------------------------------------
# Issues, answers and decisions
# ---------------------------------------------------------------------------


def _issue_identity(
    *,
    kind: str,
    subject: str,
    package: Optional[str],
    constraints: Sequence[str],
    detail: str,
    identity_keys: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    identity: Dict[str, Any] = {
        "kind": kind,
        "subject": subject,
        "package": package,
        "candidates": sorted(constraints),
        "detail": detail,
    }
    if identity_keys is not None:
        identity["candidates"] = sorted(str(key) for key in identity_keys)
        identity["identity"] = CANDIDATE_KEY_IDENTITY
    return identity


def _issue_digest(identity: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


#: marks issue ids computed from stable candidate keys (not constraints)
CANDIDATE_KEY_IDENTITY = "candidate-key.v1"
_PREFIX_HASH = re.compile(r"-([a-z0-9]{32})$")


def mpi_candidate_key(
    name: str,
    version: str,
    prefix: Optional[str],
    modules: Sequence[str] = (),
    spec: Optional[str] = None,
) -> str:
    """A key naming one MPI no matter how Spack lists it.

    The same install can show up as a ``packages.yaml`` external that is not
    in ``spack find`` yet, then as an external hash once something was built
    against it, and (via an upstream) as a real install hash. All three share
    the prefix, so the prefix is the key; a module-only external (no prefix)
    is keyed on its name, version and modules.
    """
    if modules and not prefix:
        return f"modules:{name}@{version}:{','.join(sorted(modules))}"
    if prefix:
        return "prefix:" + posixpath.normpath(str(prefix))
    return f"spec:{spec or name + '@' + version}"


def mpi_candidate_id(name: str, version: str, key: str) -> str:
    """A readable id derived only from the stable key (see :func:`mpi_candidate_key`)."""
    if key.startswith("prefix:"):
        base = posixpath.basename(key[len("prefix:"):])
        match = _PREFIX_HASH.search(base)
        if match is not None and base[: match.start()]:
            return f"{base[: match.start()]}-{match.group(1)[:7]}"
    if key.startswith("modules:"):
        return f"{name}-{version}-external"
    return f"{name}-{version}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:7]}"


def matching_candidates(
    candidates: Sequence[Mapping[str, Any]], value: Any
) -> List[Mapping[str, Any]]:
    """Every candidate ``value`` names (constraint, alias, id or key)."""
    text = str(value).strip()
    found: List[Mapping[str, Any]] = []
    for candidate in candidates:
        for field_name in ("constraint", "aliases", "id", "key"):
            current = candidate.get(field_name)
            values = current if isinstance(current, (list, tuple)) else [current]
            if any(item is not None and str(item) == text for item in values):
                found.append(candidate)
                break
    return found


def match_candidate(
    candidates: Sequence[Mapping[str, Any]], value: Any
) -> Optional[Mapping[str, Any]]:
    """The candidate ``value`` names: its constraint, an alias, its id or key."""
    text = str(value).strip()
    for field_name in ("constraint", "aliases", "id", "key"):
        for candidate in candidates:
            current = candidate.get(field_name)
            values = current if isinstance(current, (list, tuple)) else [current]
            if any(item is not None and str(item) == text for item in values):
                return candidate
    return None


def make_issue(
    *,
    kind: str,
    package: str,
    subject: str,
    scope: str,
    question: str,
    candidates: Sequence[Mapping[str, Any]] = (),
    detail: str = "",
    evidence: Sequence[str] = (),
    answerable: bool,
    hint: Optional[str] = None,
    identity_keys: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Build one ``jarvis.install-issue.v1`` record with a stable id.

    The id digests the kind, subject, package (for package scope) and the
    candidate set, so the same situation keeps its id and a changed candidate
    set asks a new question. With ``identity_keys`` the candidate set is
    identified by those stable keys (e.g. an MPI's prefix) instead of each
    candidate's constraint, which can change when Spack lists the same MPI
    differently (an external entering ``spack find``).
    """
    identity = _issue_identity(
        kind=kind,
        subject=subject,
        package=package if scope == "package" else None,
        constraints=[str(item.get("constraint")) for item in candidates],
        detail=detail if not candidates else "",
        identity_keys=identity_keys,
    )
    issue: Dict[str, Any] = {
        "schema": ISSUE_SCHEMA,
        "issue_id": _issue_digest(identity),
        "kind": kind,
        "package": package,
        "subject": subject,
        "scope": scope,
        "question": question,
        "candidates": [dict(item) for item in candidates],
        "answerable": answerable,
        "evidence": [_bounded(item, 1024) for item in evidence],
        "created_at": _utc_now(),
    }
    if detail:
        issue["detail"] = _bounded(detail)
    if hint:
        issue["hint"] = hint
    return issue


def _validated_constraint(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise ValueError("an install answer constraint must be a non-empty string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("an install answer constraint cannot contain control text")
    shlex.split(value)
    return value.strip()


def load_answers(path: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    """Read ``answers.json``: ``{issue_id: constraint | {constraint, ...}}``."""
    if path is None:
        return {}
    with Path(path).open("r", encoding="utf-8") as stream:
        raw = json.load(stream)
    if not isinstance(raw, dict):
        raise ValueError("answers.json must map issue ids to constraints")
    answers: Dict[str, Dict[str, Any]] = {}
    for issue_id, value in raw.items():
        if not isinstance(issue_id, str) or not issue_id.startswith("sha256:"):
            raise ValueError(f"answers.json key {issue_id!r} is not an issue id")
        entry = dict(value) if isinstance(value, Mapping) else {"constraint": value}
        entry["constraint"] = _validated_constraint(entry.get("constraint"))
        answers[issue_id] = {
            key: entry[key]
            for key in ("constraint", "answered_by", "ask_id")
            if entry.get(key) is not None
        }
    return answers


class Decisions:
    """Recorded answers, keyed by scope: ``site`` subjects and per package."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: Dict[str, Any] = {
            "schema_version": DECISIONS_SCHEMA,
            "site": {},
            "packages": {},
        }
        if self.path.is_file():
            with self.path.open("r", encoding="utf-8") as stream:
                loaded = yaml.safe_load(stream) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"{self.path} must be a mapping")
            self.data["site"] = dict(loaded.get("site") or {})
            self.data["packages"] = {
                str(name): list(entries or [])
                for name, entries in (loaded.get("packages") or {}).items()
            }

    def site(self, subject: str) -> Optional[Dict[str, Any]]:
        value = self.data["site"].get(subject)
        return dict(value) if isinstance(value, Mapping) else None

    def package_constraints(self, package: str) -> List[str]:
        return [str(entry["constraint"]) for entry in self.package_entries(package)]

    def package_entries(self, package: str) -> List[Dict[str, Any]]:
        return [
            dict(entry)
            for entry in self.data["packages"].get(package, [])
            if isinstance(entry, Mapping) and entry.get("constraint")
        ]

    def record(
        self,
        issue: Mapping[str, Any],
        answer: Mapping[str, Any],
        extra: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Store ``answer`` for ``issue``.

        ``extra`` adds fields such as the chosen ``candidate_key``; an
        ``answered_at`` already on ``answer`` (a re-keyed replay) is kept.
        """
        entry = {
            "kind": issue["kind"],
            "subject": issue["subject"],
            "issue_id": issue["issue_id"],
            "constraint": answer["constraint"],
            "answered_at": answer.get("answered_at") or _utc_now(),
        }
        for key in ("answered_by", "ask_id"):
            if answer.get(key) is not None:
                entry[key] = answer[key]
        for key, value in (extra or {}).items():
            if value is not None:
                entry[key] = value
        if issue["scope"] == "site":
            self.data["site"][issue["subject"]] = entry
        else:
            entries = [
                existing
                for existing in self.data["packages"].get(issue["package"], [])
                if existing.get("issue_id") != issue["issue_id"]
            ]
            entries.append(entry)
            self.data["packages"][issue["package"]] = entries
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self.path, yaml.safe_dump(self.data, sort_keys=True))

    def entries(self) -> List[Dict[str, Any]]:
        """Every recorded answer, with its ``scope`` and ``package``."""
        found: List[Dict[str, Any]] = []
        for subject, entry in sorted(self.data["site"].items()):
            if isinstance(entry, Mapping):
                found.append({**dict(entry), "scope": "site", "subject": entry.get("subject", subject)})
        for package, items in sorted(self.data["packages"].items()):
            for entry in items or []:
                if isinstance(entry, Mapping):
                    found.append({**dict(entry), "scope": "package", "package": package})
        return found

    @staticmethod
    def _matches(entry: Mapping[str, Any], selector: str) -> bool:
        if selector == "all":
            return True
        if selector.startswith("sha256:") and len(selector) >= len("sha256:") + 8:
            ids = [entry.get("issue_id"), entry.get("rekeyed_from")]
            return any(isinstance(item, str) and item.startswith(selector) for item in ids)
        values = [entry.get("constraint"), entry.get("candidate_id"),
                  entry.get("candidate_key"), entry.get("answered_constraint")]
        if entry.get("scope") == "site":
            values.append(entry.get("subject"))
        else:
            values.append(entry.get("package"))
        return selector in [str(value) for value in values if value is not None]

    def forget(self, selector: str) -> List[Dict[str, Any]]:
        """Remove the recorded answers ``selector`` names and save.

        ``selector`` is ``all``, an ``issue_id`` (or a prefix of at least 8
        hex digits, also matching ``rekeyed_from``), a site subject
        (``mpi``, ``compiler``, ``spack``), a package name, or a chosen
        ``constraint``, ``candidate_id`` or ``candidate_key``.
        """
        selector = str(selector).strip()
        removed: List[Dict[str, Any]] = []
        for subject in list(self.data["site"]):
            entry = self.data["site"][subject]
            view = {**dict(entry or {}), "scope": "site",
                    "subject": (entry or {}).get("subject", subject)}
            if self._matches(view, selector):
                removed.append(view)
                del self.data["site"][subject]
        for package in list(self.data["packages"]):
            kept = []
            for entry in self.data["packages"][package] or []:
                view = {**dict(entry or {}), "scope": "package", "package": package}
                if self._matches(view, selector):
                    removed.append(view)
                else:
                    kept.append(entry)
            if kept:
                self.data["packages"][package] = kept
            else:
                del self.data["packages"][package]
        if removed:
            self.save()
        return removed


FORGET_SCHEMA = "jarvis.install-forget.v1"


def forget_decisions(decisions_path: Path, selectors: Sequence[str]) -> Dict[str, Any]:
    """Forget recorded answers; the ``jarvis.install-forget.v1`` report.

    Raises :class:`JarvisIssueError` (``decision_not_found``, exit 5) when a
    selector matches nothing, before anything is removed.
    """
    decisions = Decisions(Path(decisions_path))
    before = decisions.entries()
    unmatched = [
        selector for selector in selectors
        if not any(Decisions._matches(entry, str(selector).strip()) for entry in before)
    ]
    if unmatched:
        raise JarvisIssueError([make_jarvis_issue(
            kind="decision_not_found",
            subject="install_decisions",
            message=(
                f"No recorded decision matches {', '.join(map(repr, unmatched))} "
                f"in {decisions_path}."
            ),
            field="forget",
            remedy="name an issue_id, a site subject (mpi, compiler, spack), a "
            "package, or a recorded constraint/candidate; or use `all`",
            details={
                "unmatched": list(unmatched),
                "decisions_path": str(decisions_path),
                "recorded": [_decision_summary(entry) for entry in before],
            },
        )])
    forgotten: List[Dict[str, Any]] = []
    for selector in selectors:
        for entry in decisions.forget(str(selector)):
            forgotten.append({**_decision_summary(entry), "selector": selector,
                              "forgotten_at": _utc_now()})
    return {
        "schema_version": FORGET_SCHEMA,
        "decisions_path": str(decisions_path),
        "forgotten": forgotten,
        "remaining": [_decision_summary(entry) for entry in decisions.entries()],
    }


def _decision_summary(entry: Mapping[str, Any]) -> Dict[str, Any]:
    keys = ("scope", "subject", "package", "kind", "issue_id", "constraint",
            "candidate_id", "candidate_key", "answered_by", "answered_at",
            "rekeyed_from")
    return {key: entry.get(key) for key in keys if entry.get(key) is not None}


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


@dataclass
class InstallContext:
    """Where one install run reads answers and writes its log and results."""

    install_dir: Path
    mode: str = "install"
    answers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    decisions_path: Optional[Path] = None
    progress: Optional[Callable[..., None]] = None
    #: refuse builds unless inside an allocation (or explicitly allowed)
    require_allocation: bool = False
    #: the answers.json the answers came from (for decision provenance)
    answers_path: Optional[Path] = None

    def __post_init__(self) -> None:
        if self.mode not in {"install", "check"}:
            raise ValueError("install mode must be 'install' or 'check'")
        self.install_dir = Path(self.install_dir)
        if self.decisions_path is None:
            configured = os.environ.get("JARVIS_SPACK_DECISIONS", "").strip()
            self.decisions_path = (
                Path(configured) if configured else self.install_dir / "decisions.yaml"
            )
        self.issues: List[Dict[str, Any]] = []
        self.used_answers: List[str] = []
        #: every recorded answer this run applied, with where it came from
        self.applied_decisions: List[Dict[str, Any]] = []
        #: recorded answers not replayed because their question changed
        self.stale_decisions: List[Dict[str, Any]] = []
        self._log_stream: Optional[IO[str]] = None

    @property
    def log_path(self) -> Path:
        return self.install_dir / "install.log"

    @property
    def issues_path(self) -> Path:
        return self.install_dir / "issues.jsonl"

    @property
    def record_path(self) -> Path:
        return self.install_dir / "install-record.json"

    def open(self) -> "InstallContext":
        """Create the directory, truncate issues and open the append-only log."""
        self.install_dir.mkdir(parents=True, exist_ok=True)
        self.issues_path.write_text("", encoding="utf-8")
        self._log_stream = self.log_path.open("a", encoding="utf-8")
        self.log(f"=== jarvis install ({self.mode}) started {_utc_now()} ===\n")
        return self

    def close(self) -> None:
        if self._log_stream is not None:
            self.log(f"=== jarvis install ({self.mode}) ended {_utc_now()} ===\n")
            self._log_stream.close()
            self._log_stream = None

    @property
    def log_stream(self) -> Optional[IO[str]]:
        return self._log_stream

    def log(self, text: str) -> None:
        if self._log_stream is not None:
            self._log_stream.write(text)
            self._log_stream.flush()

    def report_progress(self, **fields: Any) -> None:
        if self.progress is not None:
            self.progress(**fields)

    def ensure_may_build(self, what: str, packages: Sequence[str] = ()) -> None:
        """Raise :class:`BuildOutsideAllocation` for heavy work off-allocation.

        Called right before anything heavy (``spack install``, an env
        concretize, a container or pip/conda build), so find-first runs that
        have nothing to build never trip it.
        """
        if not self.require_allocation:
            return
        allocation = current_allocation()
        if allocation is not None:
            return
        if local_build_allowed():
            self.log(f"[{_utc_now()}] {ALLOW_LOCAL_BUILD_ENV} set: {what} locally\n")
            return
        refused = BuildOutsideAllocation(what, packages)
        self.log(f"[{_utc_now()}] REFUSED: {refused}\n")
        self.report_progress(phase="refused", current_package=None)
        raise refused

    def raise_issue(self, issue: Dict[str, Any]) -> None:
        if any(existing["issue_id"] == issue["issue_id"] for existing in self.issues):
            return
        self.issues.append(issue)
        with self.issues_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(issue, sort_keys=True, separators=(",", ":")) + "\n")
        self.log(f"ISSUE {issue['kind']} {issue['issue_id']}: {issue['question']}\n")

    def answer_for(self, issue: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """The answers-file answer for ``issue``, or ``None``.

        For a question with candidates the answer may name a candidate by
        any of its ``aliases`` (constraints it was or is listed under), its
        ``id`` or its ``key``; it is normalized to the candidate's current
        ``constraint`` and carries ``candidate_key``/``candidate_id``.
        """
        answer = self.answers.get(str(issue["issue_id"]))
        if answer is None:
            return None
        candidates = issue.get("candidates") or []
        if candidates:
            chosen = match_candidate(candidates, answer["constraint"])
            if chosen is None:
                raise ValueError(
                    f"answer for {issue['issue_id']} must be one of the candidate "
                    "constraints (or a candidate id or key)"
                )
            answer = dict(answer)
            if answer["constraint"] != chosen.get("constraint"):
                answer["answered_constraint"] = answer["constraint"]
            answer["constraint"] = str(chosen.get("constraint"))
            for field_name, key in (("candidate_key", "key"), ("candidate_id", "id")):
                if chosen.get(key) is not None:
                    answer[field_name] = chosen[key]
        self.used_answers.append(str(issue["issue_id"]))
        self.note_decision(
            issue,
            answer,
            source="answers_file",
            path=self.answers_path,
            recorded_to=self.decisions_path,
        )
        return answer

    def note_decision(
        self,
        issue: Mapping[str, Any],
        answer: Mapping[str, Any],
        *,
        source: str,
        path: Optional[Path] = None,
        recorded_to: Optional[Path] = None,
        package: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record that a stored answer was applied (``applied_decisions``)."""
        entry: Dict[str, Any] = {
            "issue_id": issue.get("issue_id"),
            "kind": issue.get("kind"),
            "subject": issue.get("subject"),
            "scope": issue.get("scope") or ("package" if package else "site"),
            "package": package if package is not None else issue.get("package"),
            "constraint": answer.get("constraint"),
            "source": source,
            "path": str(path) if path is not None else None,
        }
        if recorded_to is not None:
            entry["recorded_to"] = str(recorded_to)
        for key in ("answered_at", "answered_by", "ask_id", "candidate_key",
                    "candidate_id", "answered_constraint"):
            if answer.get(key) is not None:
                entry[key] = answer[key]
        key = (entry["issue_id"], entry["package"], entry["constraint"])
        for existing in self.applied_decisions:
            if (existing["issue_id"], existing["package"], existing["constraint"]) == key:
                return existing
        self.applied_decisions.append(entry)
        return entry


def _node_is_version(node: "SpecNode", name: str, version: str) -> bool:
    """Whether a concretized node is ``name@version`` (for hashless externals)."""
    if node.name != name:
        return False
    match = re.search(r"@=?([^\s%+~^/]+)", node.spec)
    return match is not None and match.group(1) == version


def _query_pins_mpi(tokens: Sequence[str]) -> bool:
    text = " ".join(tokens)
    return any(
        re.search(rf"\^\s*{re.escape(provider)}(?![a-z0-9_-])", text)
        for provider in MPI_PROVIDERS
    )


class SpackPlanner:
    """Resolve, reuse and (in install mode) build each package's spec."""

    def __init__(self, context: InstallContext, runner: Optional[SpackRunner] = None):
        self.context = context
        self.runner = runner or SpackRunner(context.log_stream)
        self.decisions = Decisions(context.decisions_path)  # type: ignore[arg-type]
        self._mpi_candidates: Optional[List[Dict[str, Any]]] = None
        #: the build host's usable compilers (``apply_build_compilers``)
        self.usable_compilers: List[Dict[str, Any]] = []
        #: ``[{spec, missing}]`` the build host cannot run, and that host
        self.unusable_compilers: List[Dict[str, Any]] = []
        self.build_host: Optional[str] = None

    def compiler_guard(
        self, nodes: Sequence[SpecNode], origin: str, evidence: Sequence[str] = ()
    ) -> Optional[Dict[str, Any]]:
        """``compiler_unavailable`` if a node to build uses an unusable compiler."""
        if not self.unusable_compilers:
            return None
        return unusable_compiler_issue(
            nodes, unusable=self.unusable_compilers, usable=self.usable_compilers,
            host=self.build_host or socket.gethostname(), origin=origin,
            evidence=evidence,
        )

    # -- discovery ---------------------------------------------------------

    def mpi_candidates(self) -> List[Dict[str, Any]]:
        """Installed, upstream and used-external MPIs, merged by prefix.

        A site often registers its MPI install a second time as a
        ``packages.yaml`` external at the same prefix. Both records are kept
        as ``hashes`` of one candidate, but the constraint pins the real
        install's hash: that is the one upstream dependents (adios2, hdf5,
        ...) link, so pinning the external would force them to rebuild. The
        external hash is pinned only when no real install shares the prefix.
        """
        if self._mpi_candidates is not None:
            return self._mpi_candidates
        by_prefix: Dict[str, Dict[str, Any]] = {}
        for provider in MPI_PROVIDERS:
            completed = self.runner.run(
                ["find", "--format", _MPI_FIND_FORMAT, provider]
            )
            if completed.returncode != 0 and "No package matches" not in (
                (completed.stdout or "") + (completed.stderr or "")
            ):
                completed = self.runner.run(
                    ["find", "--format", _MPI_FIND_FORMAT_LEGACY, provider]
                )
            if completed.returncode != 0:
                continue
            for line in completed.stdout.splitlines():
                match = _FIND_LINE.match(line.strip())
                if match is None:
                    continue
                prefix = match.group("prefix").strip()
                short = match.group("hash")[:7]
                external = match.group("external")
                if external is None:
                    # Legacy format: the default install layout ends the
                    # prefix with the full hash; anything else stays unknown.
                    is_external = (
                        False
                        if prefix.rstrip("/").endswith(match.group("hash"))
                        else None
                    )
                else:
                    is_external = external not in ("", "None")
                record = {
                    "hash": short,
                    "external": is_external,
                    "version": match.group("version"),
                }
                existing = by_prefix.setdefault(
                    posixpath.normpath(prefix),
                    {
                        "name": match.group("name"),
                        "version": match.group("version"),
                        "prefix": prefix,
                        "records": [],
                    },
                )
                existing["records"].append(record)
        candidates = []
        for candidate in by_prefix.values():
            records = candidate.pop("records")
            # Stable sort: real installs first, then unknown, then externals.
            records.sort(key=lambda item: {False: 0, None: 1, True: 2}[item["external"]])
            pinned = records[0]
            short = pinned["hash"]
            candidate["version"] = pinned["version"]
            key = mpi_candidate_key(candidate["name"], candidate["version"],
                                    candidate["prefix"])
            candidate.update(
                {
                    "key": key,
                    "id": mpi_candidate_id(candidate["name"], candidate["version"], key),
                    "constraint": f"^{candidate['name']}/{short}",
                    "hashes": [item["hash"] for item in records],
                    "external_hashes": [
                        item["hash"] for item in records if item["external"]
                    ],
                    "aliases": [f"^{candidate['name']}/{item['hash']}" for item in records],
                }
            )
            others = records[1:]
            candidate["source"] = (
                f"spack find: {candidate['prefix']}"
                + (" (external only)" if pinned["external"] else "")
                + (
                    " (also registered as "
                    + ", ".join(
                        "/" + item["hash"] + (" external" if item["external"] else "")
                        for item in others
                    )
                    + ", same prefix)"
                    if others
                    else ""
                )
            )
            candidates.append(candidate)
        candidates.extend(self._configured_mpi_externals(candidates))
        candidates.sort(key=lambda item: item["id"])
        self._mpi_candidates = candidates
        return candidates

    def _configured_mpi_externals(
        self, found: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """MPI externals from ``packages.yaml`` that ``spack find`` can't show.

        Spack lists an external only once something has been installed
        against it, so a site's module MPI (``externals: [{spec:
        mpich@4.1.1, modules: [mpich/4.1.1]}]``) is otherwise invisible. An
        external matching a found install (same prefix, or same name and
        version for a module-only one) is merged into it instead.
        """
        completed = self.runner.run(["config", "get", "packages"])
        if completed.returncode != 0:
            return []
        try:
            document = yaml.safe_load(completed.stdout) or {}
        except yaml.YAMLError:
            return []
        packages = document.get("packages", document) if isinstance(document, dict) else {}
        if not isinstance(packages, dict):
            return []
        extra: List[Dict[str, Any]] = []
        for provider in MPI_PROVIDERS:
            settings = packages.get(provider)
            if not isinstance(settings, dict):
                continue
            for external in settings.get("externals") or []:
                if not isinstance(external, dict) or not external.get("spec"):
                    continue
                spec = " ".join(str(external["spec"]).split())
                match = re.match(r"([a-z0-9][a-z0-9_-]*)@=?([^\s%+~^]+)", spec)
                if match is None or match.group(1) != provider:
                    continue
                version = match.group(2)
                prefix = external.get("prefix")
                modules = [str(m) for m in external.get("modules") or []]
                origin = "packages.yaml external " + (
                    f"prefix {prefix}" if prefix else f"modules {', '.join(modules)}"
                )
                twin = next(
                    (
                        item for item in found
                        if (
                            prefix
                            and item.get("prefix")
                            and posixpath.normpath(str(item["prefix"]))
                            == posixpath.normpath(str(prefix))
                        )
                        or (
                            not prefix
                            and item["name"] == provider
                            and item["version"] == version
                        )
                    ),
                    None,
                )
                if twin is None:
                    # Several packages.yaml externals at one prefix (e.g. two
                    # module names for the same install) are one candidate.
                    own_key = mpi_candidate_key(
                        provider, version, str(prefix) if prefix else None, modules, spec
                    )
                    twin = next((item for item in extra if item["key"] == own_key), None)
                if twin is not None:
                    twin["source"] += f"; {origin}"
                    alias = f"^{spec}"
                    if alias not in twin.setdefault("aliases", []):
                        twin["aliases"].append(alias)
                    if modules:
                        merged = list(twin.get("modules") or [])
                        merged += [m for m in modules if m not in merged]
                        twin["modules"] = merged
                    if not prefix and twin.get("hashes"):
                        # Keep the key the module-only external had before it
                        # entered ``spack find`` (find adds a derived prefix).
                        twin["key"] = mpi_candidate_key(provider, version, None, modules)
                        twin["id"] = mpi_candidate_id(provider, version, twin["key"])
                    continue
                key = mpi_candidate_key(
                    provider, version, str(prefix) if prefix else None, modules, spec
                )
                extra.append(
                    {
                        "id": mpi_candidate_id(provider, version, key),
                        "key": key,
                        "constraint": f"^{spec}",
                        "aliases": [f"^{spec}"],
                        "name": provider,
                        "version": version,
                        "prefix": str(prefix) if prefix else None,
                        "modules": modules,
                        "hashes": [],
                        "source": f"{origin} (not yet in spack find)",
                    }
                )
        return extra

    def _spec(self, tokens: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return self.runner.run(["spec", "-I", "-l", *tokens])

    # -- per package -------------------------------------------------------

    def plan_package(self, package: str, query: str) -> Dict[str, Any]:
        """Resolve one package; returns its install-record entry."""
        base_tokens = shlex.split(query)
        package_entries = self.decisions.package_entries(package)
        constraints = [str(item["constraint"]) for item in package_entries]
        for item in package_entries:
            self.context.note_decision(
                item, item, source="decisions_file",
                path=self.decisions.path, package=package,
            )
        entry: Dict[str, Any] = {
            "query": query,
            "status": "resolving",
            "constraints": list(constraints),
            "mpi": None,
        }
        nodes: Optional[List[SpecNode]] = None
        for _attempt in range(4):
            tokens = [*base_tokens, *(t for c in constraints for t in shlex.split(c))]
            completed = self._spec(tokens)
            output = (completed.stdout or "") + (completed.stderr or "")
            if completed.returncode == 0:
                nodes = parse_spec_tree(completed.stdout)
                if nodes:
                    break
                output = "spack spec printed no concretized tree\n" + output
            kind = classify_concretize_failure(output)
            issue = self._concretize_issue(kind, package, tokens, output)
            answer = self.context.answer_for(issue) if issue["answerable"] else None
            if answer is None:
                self.context.raise_issue(issue)
                entry["status"] = "needs_decision"
                entry["issue_id"] = issue["issue_id"]
                return entry
            self.decisions.record(issue, answer)
            constraints.append(answer["constraint"])
            entry["constraints"] = list(constraints)
        if nodes is None:
            raise RuntimeError(f"{package}: spack spec did not converge")

        tokens = [*base_tokens, *(t for c in constraints for t in shlex.split(c))]
        mpi_nodes = [node for node in nodes if node.name in MPI_PROVIDERS]
        # A spec whose root is itself an MPI names its provider; there is no choice to ask.
        root_is_mpi = nodes[0].name in MPI_PROVIDERS
        if mpi_nodes and not root_is_mpi and not _query_pins_mpi(tokens):
            pin, source = self._choose_mpi(package, mpi_nodes[0])
            if pin is None and source.startswith("needs_decision:"):
                entry["status"] = "needs_decision"
                entry["issue_id"] = source.split(":", 1)[1]
                return entry
            if pin is not None:
                tokens = [*tokens, *shlex.split(pin)]
                constraints.append(pin)
                entry["constraints"] = list(constraints)
                completed = self._spec(tokens)
                output = (completed.stdout or "") + (completed.stderr or "")
                nodes = parse_spec_tree(completed.stdout) if completed.returncode == 0 else []
                if not nodes:
                    issue = self._concretize_issue(
                        classify_concretize_failure(output), package, tokens, output
                    )
                    self.context.raise_issue(issue)
                    entry["status"] = "needs_decision"
                    entry["issue_id"] = issue["issue_id"]
                    return entry
            mpi_node = next(node for node in nodes if node.name in MPI_PROVIDERS)
            entry["mpi"] = self._mpi_entry(mpi_node, source)
        elif mpi_nodes:
            entry["mpi"] = self._mpi_entry(mpi_nodes[0], "query")
        self._fill_nodes(entry, nodes, tokens)
        issue = self.compiler_guard(
            nodes, origin="spack spec", evidence=[f"spack spec -I -l {shlex.join(tokens)}"]
        )
        if issue is not None:
            self.context.raise_issue(issue)
            entry["status"] = "needs_decision"
            entry["issue_id"] = issue["issue_id"]
        return entry

    def _mpi_entry(self, node: SpecNode, source: str) -> Dict[str, Any]:
        """The MPI a package resolved to, with its prefix when Spack has it."""
        entry: Dict[str, Any] = {"name": node.name, "hash": node.hash, "source": source}
        short = node.hash[:7]
        for candidate in self.mpi_candidates():
            if any(h.startswith(short) or short.startswith(h) for h in candidate["hashes"]) or (
                not candidate["hashes"]
                and _node_is_version(node, candidate["name"], candidate["version"])
            ):
                entry["version"] = candidate["version"]
                entry["prefix"] = candidate["prefix"]
                entry["candidate_id"] = candidate.get("id")
                entry["candidate_key"] = candidate.get("key")
                if candidate.get("modules"):
                    entry["modules"] = candidate["modules"]
                break
        return entry

    def _fill_nodes(
        self, entry: Dict[str, Any], nodes: Sequence[SpecNode], tokens: Sequence[str]
    ) -> None:
        root = nodes[0]
        missing = [node for node in nodes if not node.present]
        counts: Dict[str, int] = {}
        for node in nodes:
            counts[node.status] = counts.get(node.status, 0) + 1
        entry.update(
            {
                "spec": shlex.join(tokens),
                "root": {"name": root.name, "hash": root.hash, "spec": root.spec[:512]},
                "nodes_total": len(nodes),
                "nodes_by_status": counts,
                "missing": [node.to_dict() for node in missing],
                "status": "satisfied" if not missing else "missing",
            }
        )

    def _choose_mpi(
        self, package: str, concretized: SpecNode
    ) -> tuple[Optional[str], str]:
        candidates = self.mpi_candidates()
        recorded = self.decisions.site("mpi")
        if len(candidates) == 1:
            return candidates[0]["constraint"], "auto_single_candidate"
        if not candidates:
            return None, "concretizer"
        options = []
        for candidate in candidates:
            current = (
                concretized.hash[:7] in candidate["hashes"]
                if candidate["hashes"]
                else _node_is_version(concretized, candidate["name"], candidate["version"])
            )
            options.append(
                {
                    key: candidate[key]
                    for key in ("id", "key", "constraint", "aliases", "source",
                                "prefix", "hashes")
                }
                | ({"modules": candidate["modules"]} if candidate.get("modules") else {})
                | {
                    "implies": (
                        "the concretizer's current choice"
                        if current
                        else "re-resolves dependents against this MPI"
                    )
                }
            )
        keys = sorted(str(candidate["key"]) for candidate in candidates)
        issue = make_issue(
            kind="ambiguous_provider",
            package=package,
            subject="mpi",
            scope="site",
            question="Which MPI should packages use on this site?",
            candidates=options,
            evidence=[
                "spack find --format '{name}@{version} {hash} {prefix}' "
                + " ".join(MPI_PROVIDERS),
                "spack config get packages (MPI externals)",
                f"spack spec -I -l (concretizer picked {concretized.name}/{concretized.hash})",
            ],
            answerable=True,
            identity_keys=keys,
        )
        if recorded is not None and recorded.get("constraint"):
            # A recorded answer is replayed only for the same question (the
            # same candidate set, identified by stable keys), applying the
            # chosen candidate's current constraint. A changed set has a new
            # issue_id and is asked again.
            resolved = self._resolve_recorded_mpi(recorded, issue, options)
            if resolved is not None:
                chosen, resolved_by = resolved
                answer = dict(recorded)
                answer["constraint"] = str(chosen["constraint"])
                answer["candidate_key"] = chosen["key"]
                answer["candidate_id"] = chosen["id"]
                applied = self.context.note_decision(
                    issue, answer, source="decisions_file",
                    path=self.decisions.path,
                )
                applied["resolved_by"] = resolved_by
                if recorded.get("constraint") != answer["constraint"]:
                    applied["recorded_constraint"] = recorded.get("constraint")
                if recorded.get("issue_id") != issue["issue_id"]:
                    applied["recorded_issue_id"] = recorded.get("issue_id")
                if (
                    recorded.get("issue_id") != issue["issue_id"]
                    or recorded.get("constraint") != answer["constraint"]
                    or recorded.get("candidate_key") != chosen["key"]
                ):
                    # Re-key the stored answer (keeping who answered and when).
                    self.decisions.record(
                        issue,
                        answer,
                        extra={
                            "candidate_key": chosen["key"],
                            "candidate_id": chosen["id"],
                            "candidate_keys": keys,
                            "rekeyed_from": (
                                recorded.get("issue_id")
                                if recorded.get("issue_id") != issue["issue_id"]
                                else recorded.get("rekeyed_from")
                            ),
                            "rekeyed_at": _utc_now(),
                        },
                    )
                    applied["recorded_to"] = str(self.decisions.path)
                return answer["constraint"], "decision"
            stale = {
                "issue_id": recorded.get("issue_id"),
                "subject": "mpi",
                "scope": "site",
                "constraint": recorded.get("constraint"),
                "candidate_key": recorded.get("candidate_key"),
                "path": str(self.decisions.path),
                "answered_at": recorded.get("answered_at"),
                "superseded_by": issue["issue_id"],
                "reason": (
                    "the candidate set changed, so the question changed"
                    if recorded.get("candidate_keys") is not None or recorded.get("candidate_key")
                    else "recorded before candidate keys, and its constraint names no "
                    "single current candidate"
                ),
            }
            previous = None
            if recorded.get("candidate_key"):
                previous = match_candidate(options, recorded["candidate_key"])
            if previous is None:
                previous = match_candidate(options, recorded["constraint"])
            recorded_keys = recorded.get("candidate_keys")
            if isinstance(recorded_keys, list):
                stale["added_candidates"] = sorted(set(keys) - set(recorded_keys))
                stale["removed_candidates"] = sorted(set(recorded_keys) - set(keys))
            if stale not in self.context.stale_decisions:
                self.context.stale_decisions.append(stale)
            issue["supersedes"] = recorded.get("issue_id")
            issue["previous_answer"] = recorded.get("constraint")
            if previous is not None:
                # The earlier choice is still on offer: a caller can preselect it.
                issue["previous_candidate_id"] = previous["id"]
                issue["previous_candidate_key"] = previous["key"]
        answer = self.context.answer_for(issue)
        if answer is None:
            self.context.raise_issue(issue)
            return None, f"needs_decision:{issue['issue_id']}"
        self.decisions.record(
            issue,
            answer,
            extra={
                "candidate_key": answer.get("candidate_key"),
                "candidate_id": answer.get("candidate_id"),
                "candidate_keys": keys,
            },
        )
        return str(answer["constraint"]), "decision"

    def _resolve_recorded_mpi(
        self,
        recorded: Mapping[str, Any],
        issue: Mapping[str, Any],
        options: Sequence[Mapping[str, Any]],
    ) -> Optional[tuple[Mapping[str, Any], str]]:
        """The candidate a recorded MPI answer chose, if its question still stands.

        ``issue_id``: same candidate keys; the answer is found by its
        ``candidate_key`` or by any alias of its constraint. ``legacy_issue_id``:
        a record written before candidate keys (no ``candidate_keys``). Its
        constraint-based id can't be recomputed reliably (older listings
        offered same-prefix externals as separate candidates), so it is
        accepted once when its constraint names exactly one current
        candidate; the caller then re-keys it.
        """
        def chosen() -> Optional[Mapping[str, Any]]:
            if recorded.get("candidate_key"):
                found = match_candidate(options, recorded["candidate_key"])
                if found is not None:
                    return found
            return match_candidate(options, recorded["constraint"])

        if recorded.get("issue_id") == issue["issue_id"]:
            candidate = chosen()
            return (candidate, "issue_id") if candidate is not None else None
        if recorded.get("candidate_keys") is None and not recorded.get("candidate_key"):
            named = matching_candidates(options, recorded["constraint"])
            if len(named) == 1:
                return named[0], "legacy_issue_id"
        return None

    def _concretize_issue(
        self, kind: str, package: str, tokens: Sequence[str], output: str
    ) -> Dict[str, Any]:
        excerpt = _error_excerpt(output)
        spec = shlex.join(tokens)
        if kind == "variant_conflict":
            return make_issue(
                kind=kind,
                package=package,
                subject=spec,
                scope="package",
                question=(
                    f"Spack reports a conflict for {package} ({spec}). "
                    "Which constraint should be added to resolve it?"
                ),
                detail=excerpt,
                evidence=[f"spack spec -I -l {spec}"],
                answerable=True,
                hint="answer with a spec constraint (e.g. '~cuda') appended to the query",
            )
        if kind == "no_recipe":
            return make_issue(
                kind=kind,
                package=package,
                subject=spec,
                scope="package",
                question=f"Spack has no recipe for {package} ({spec}).",
                detail=excerpt,
                evidence=[f"spack spec -I -l {spec}"],
                answerable=False,
                hint="add a recipe repo through install_repos or change install_query",
            )
        return make_issue(
            kind="concretize_failed",
            package=package,
            subject=spec,
            scope="package",
            question=f"Spack could not concretize {package} ({spec}).",
            detail=excerpt,
            evidence=[f"spack spec -I -l {spec}"],
            answerable=True,
            hint="answer with a spec constraint appended to the query",
        )

    def install_package(self, package: str, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Build only the missing nodes of a resolved entry, then re-verify."""
        tokens = shlex.split(entry["spec"])
        missing_before = [item["name"] for item in entry["missing"]]
        completed = self.runner.run(["install", "--fail-fast", *tokens], stream=True)
        if completed.returncode != 0:
            entry["status"] = "failed"
            entry["error"] = _error_excerpt(completed.stdout or "")
            issue = classify_install_failure(
                completed.stdout or "",
                stage="install",
                packages=[package],
                usable_compilers=self.usable_compilers,
                log_path=self.context.log_path,
                command=self.runner.commands[-1] if self.runner.commands else None,
            )
            self.context.raise_issue(issue)
            entry["issue_id"] = issue["issue_id"]
            entry["failure_kind"] = issue["kind"]
            return entry
        verify = self._spec(tokens)
        nodes = parse_spec_tree(verify.stdout) if verify.returncode == 0 else []
        if not nodes:
            entry["status"] = "failed"
            entry["error"] = "could not re-verify the installed spec"
            return entry
        self._fill_nodes(entry, nodes, tokens)
        entry["built"] = missing_before
        if entry["status"] == "satisfied":
            entry["status"] = "installed"
        else:
            entry["status"] = "failed"
            entry["error"] = "spack install succeeded but nodes are still missing"
        return entry

    def refresh_mpi(self, entries: Sequence[Dict[str, Any]]) -> None:
        """Fill the prefix of MPIs that were only just built."""
        if not any(
            isinstance(entry.get("mpi"), dict) and not entry["mpi"].get("prefix")
            for entry in entries
        ):
            return
        self._mpi_candidates = None
        for entry in entries:
            mpi = entry.get("mpi")
            if isinstance(mpi, dict) and not mpi.get("prefix"):
                node = SpecNode("installed", mpi["hash"], mpi["name"], mpi["name"], 1)
                entry["mpi"] = self._mpi_entry(node, mpi.get("source", "query"))


# ---------------------------------------------------------------------------
# Per-pipeline Spack environment (spack.yaml + spack.lock)
# ---------------------------------------------------------------------------

ENV_SCHEMA = "jarvis.spack-env.v1"

#: flock file inside the env dir. Held exclusively around every env write
#: (manifest, concretize, install, lock copy) and shared while a check or
#: load reads the lock, so pipelines sharing one ``JARVIS_SPACK_ENV_DIR``
#: serialize instead of corrupting ``spack.lock``.
ENV_LOCK_NAME = ".jarvis-env.lock"


@contextlib.contextmanager
def spack_env_lock(
    directory: Path,
    *,
    exclusive: bool,
    on_wait: Optional[Callable[[], None]] = None,
):
    """Hold ``<directory>/.jarvis-env.lock``; ``on_wait`` runs once if blocked."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(directory / ENV_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o664)
    try:
        if os.name == "nt":
            import msvcrt

            waited = False
            while True:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if not waited and on_wait is not None:
                        on_wait()
                    waited = True
                    time.sleep(0.5)
            try:
                yield
            finally:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            try:
                fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if on_wait is not None:
                    on_wait()
                fcntl.flock(descriptor, operation)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def spack_env_enabled() -> bool:
    """``JARVIS_SPACK_ENV=off`` falls back to one ``spack install`` per package."""
    value = os.environ.get("JARVIS_SPACK_ENV", "").strip().lower()
    return value not in ("off", "0", "false", "no", "none")


class SpackEnvironment:
    """An anonymous Spack environment directory (one per pipeline by
    default, or shared through ``JARVIS_SPACK_ENV_DIR``).

    ``spack.yaml`` lists each resolved package spec as a root (``unify:
    false`` so every root concretizes as the planner resolved it, ``reuse:
    true`` so installed/upstream/external nodes are kept, no view).
    ``spack.lock`` written by ``spack -e DIR concretize`` is the install
    record: runs load the lock's root hashes, so every execution uses the
    exact same builds until the pipeline's specs change.
    """

    def __init__(self, directory: Path, runner: SpackRunner,
                 repos: Sequence[str] = (),
                 sections: Optional[Mapping[str, Any]] = None):
        self.directory = Path(directory)
        self.runner = runner
        self.repos = [str(repo) for repo in repos]
        #: extra manifest sections (the build-host compiler scope); ``None``
        #: means "keep whatever the manifest has" (a check reads the lock
        #: the install job concretized, whatever compilers it excluded)
        self.sections = (
            json.loads(json.dumps(dict(sections))) if sections is not None else None
        )

    @property
    def manifest_path(self) -> Path:
        return self.directory / "spack.yaml"

    @property
    def lock_path(self) -> Path:
        return self.directory / "spack.lock"

    @staticmethod
    def manifest_for(
        specs: Sequence[str],
        repos: Sequence[str] = (),
        sections: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        document: Dict[str, Any] = {
            "spack": {
                "specs": list(specs),
                "view": False,
                "concretizer": {"reuse": True, "unify": False},
            }
        }
        if repos:
            document["spack"]["repos"] = list(repos)
        for key, value in (sections or {}).items():
            if key == "concretizer" and isinstance(value, Mapping):
                merged = dict(document["spack"]["concretizer"])
                for name, setting in value.items():
                    merged.pop(name.rstrip(":"), None)
                    merged[name] = setting
                document["spack"]["concretizer"] = merged
            else:
                document["spack"][key] = value
        return document

    def _manifest(self) -> Optional[Mapping[str, Any]]:
        try:
            document = yaml.safe_load(self.manifest_path.read_text(encoding="utf-8"))
            section = document["spack"]
        except (OSError, yaml.YAMLError, KeyError, TypeError):
            return None
        return section if isinstance(section, Mapping) else None

    def manifest_specs(self) -> Optional[List[str]]:
        section = self._manifest()
        specs = section.get("specs") if section is not None else None
        return [str(spec) for spec in specs] if isinstance(specs, list) else None

    def manifest_repos(self) -> List[str]:
        section = self._manifest()
        repos = section.get("repos") if section is not None else None
        return [str(repo) for repo in repos] if isinstance(repos, list) else []

    def _manifest_matches(self, specs: Sequence[str]) -> bool:
        if self.manifest_specs() != list(specs) or self.manifest_repos() != self.repos:
            return False
        if self.sections is None:
            return True
        expected = self.manifest_for(specs, self.repos, self.sections)["spack"]
        return json.loads(json.dumps(self._manifest(), default=str)) == json.loads(
            json.dumps(expected, default=str)
        )

    def lock_roots(self) -> Optional[List[Dict[str, str]]]:
        try:
            document = json.loads(self.lock_path.read_text(encoding="utf-8"))
            roots = document["roots"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if not isinstance(roots, list) or not all(
            isinstance(root, dict) and isinstance(root.get("hash"), str) for root in roots
        ):
            return None
        return [{"hash": root["hash"], "spec": str(root.get("spec", ""))} for root in roots]

    def current_roots(self, specs: Sequence[str]) -> Optional[List[Dict[str, str]]]:
        """Lock roots when the lock was concretized from exactly ``specs``
        (and this environment's recipe repos and compiler sections)."""
        if not self._manifest_matches(specs):
            return None
        roots = self.lock_roots()
        if roots is None or len(roots) != len(specs):
            return None
        return roots

    def write_manifest(self, specs: Sequence[str]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if not self._manifest_matches(specs):
            sections = self.sections if self.sections is not None else {}
            _atomic_write_text(
                self.manifest_path,
                "# Generated by jarvis ppl install; edit the pipeline instead.\n"
                + yaml.safe_dump(
                    self.manifest_for(specs, self.repos, sections), sort_keys=False
                ),
            )

    def concretize(self) -> subprocess.CompletedProcess[str]:
        return self.runner.run(
            ["-e", str(self.directory), "concretize", "-f"], stream=True
        )

    def install(self) -> subprocess.CompletedProcess[str]:
        return self.runner.run(
            ["-e", str(self.directory), "install", "--fail-fast"], stream=True
        )

    def describe(self, roots: Optional[Sequence[Mapping[str, str]]]) -> Dict[str, Any]:
        lock_sha256 = None
        if roots is not None and self.lock_path.is_file():
            lock_sha256 = hashlib.sha256(self.lock_path.read_bytes()).hexdigest()
        return {
            "schema_version": ENV_SCHEMA,
            "dir": str(self.directory),
            "manifest": str(self.manifest_path),
            "lock": str(self.lock_path) if roots is not None else None,
            "lock_sha256": lock_sha256,
            "roots": [dict(root) for root in roots] if roots is not None else [],
            "repos": list(self.repos),
            "status": "locked" if roots is not None else "unlocked",
        }


def _sync_spack_environment(
    planner: SpackPlanner,
    context: InstallContext,
    record: Dict[str, Any],
    resolved: Sequence[tuple[str, Dict[str, Any]]],
    env_dir: Path,
    sections: Optional[Mapping[str, Any]] = None,
) -> None:
    """Build the missing nodes through the pipeline env; record its lock.

    A lock concretized from the same specs is reused without re-running
    Spack, so a run's find-first load is just ``spack load /<lock hash>``.

    Everything runs under :func:`spack_env_lock` (exclusive when it may
    build, shared otherwise), so several pipelines may share one env dir:
    a second install waits, re-verifies what the first one built and
    reuses its lock instead of building again.
    """
    environment = SpackEnvironment(
        env_dir, planner.runner, record.get("spack_repos") or (), sections=sections
    )
    building = context.mode == "install" and not context.issues
    if not building and not Path(env_dir).is_dir():
        record["spack_env"] = environment.describe(None)
        return
    waited: List[bool] = []

    def on_wait() -> None:
        waited.append(True)
        message = (
            f"waiting for {Path(env_dir) / ENV_LOCK_NAME}: another jarvis "
            "operation is using this Spack environment"
        )
        context.log(f"[{_utc_now()}] {message}\n")
        print(f"SpackInstaller: {message}", flush=True)
        context.report_progress(phase="waiting_env_lock", current_package=None)

    with spack_env_lock(env_dir, exclusive=building, on_wait=on_wait):
        if waited:
            context.log(f"[{_utc_now()}] acquired {Path(env_dir) / ENV_LOCK_NAME}\n")
        _sync_spack_environment_locked(
            planner, context, record, resolved, env_dir, environment,
            building=building, waited=bool(waited),
        )


def _sync_spack_environment_locked(
    planner: SpackPlanner,
    context: InstallContext,
    record: Dict[str, Any],
    resolved: Sequence[tuple[str, Dict[str, Any]]],
    env_dir: Path,
    environment: "SpackEnvironment",
    *,
    building: bool,
    waited: bool,
) -> None:
    # Manifest entries are Spack spec syntax, not shell words: unquote them.
    specs = [" ".join(shlex.split(entry["spec"])) for _package, entry in resolved]
    roots = environment.current_roots(specs)
    missing = [
        (package, entry) for package, entry in resolved if entry["status"] == "missing"
    ]
    if building and waited and missing:
        # The holder may have built these nodes while this install waited.
        still_missing = []
        for package, entry in missing:
            verify = planner._spec(shlex.split(entry["spec"]))
            nodes = parse_spec_tree(verify.stdout) if verify.returncode == 0 else []
            if nodes:
                planner._fill_nodes(entry, nodes, shlex.split(entry["spec"]))
            if entry["status"] == "missing":
                still_missing.append((package, entry))
        missing = still_missing
        planner.refresh_mpi([entry for _package, entry in resolved])
    if building and (roots is None or missing):
        context.ensure_may_build(
            f"concretize and install the Spack environment {env_dir}",
            [item["name"] for _p, e in missing for item in e["missing"]],
        )
        context.report_progress(
            phase="building",
            current_package=None,
            missing=[item["name"] for _p, e in missing for item in e["missing"]][:32],
        )
        environment.write_manifest(specs)
        completed = environment.concretize()
        stage = "concretize"
        if completed.returncode == 0:
            guard = planner.compiler_guard(
                parse_spec_tree(completed.stdout or ""), origin="spack concretize",
                evidence=[f"spack -e {env_dir} concretize -f"],
            )
            if guard is not None:
                # Nothing is built with a compiler the build host cannot run.
                context.raise_issue(guard)
                for _package, entry in missing or resolved:
                    entry["status"] = "needs_decision"
                    entry["issue_id"] = guard["issue_id"]
                record["spack_env"] = environment.describe(None)
                return
            stage = "install"
            completed = environment.install()
        if completed.returncode != 0:
            error = _error_excerpt(completed.stdout or "")
            failing = missing or resolved
            issue = classify_install_failure(
                completed.stdout or "",
                stage=stage,
                packages=[package for package, _entry in failing],
                usable_compilers=planner.usable_compilers,
                log_path=context.log_path,
                command=planner.runner.commands[-1] if planner.runner.commands else None,
            )
            context.raise_issue(issue)
            for _package, entry in failing:
                entry["status"] = "failed"
                entry["error"] = f"spack -e {env_dir}: {error}"
                entry["issue_id"] = issue["issue_id"]
                entry["failure_kind"] = issue["kind"]
            record["spack_env"] = environment.describe(None)
            return
        roots = environment.current_roots(specs)
        for package, entry in missing:
            missing_before = [item["name"] for item in entry["missing"]]
            verify = planner._spec(shlex.split(entry["spec"]))
            nodes = parse_spec_tree(verify.stdout) if verify.returncode == 0 else []
            if not nodes:
                entry["status"] = "failed"
                entry["error"] = "could not re-verify the installed spec"
                continue
            planner._fill_nodes(entry, nodes, shlex.split(entry["spec"]))
            entry["built"] = missing_before
            if entry["status"] == "satisfied":
                entry["status"] = "installed"
            else:
                entry["status"] = "failed"
                entry["error"] = "spack install succeeded but nodes are still missing"
        planner.refresh_mpi([entry for _package, entry in resolved])
    if roots is not None:
        for (_package, entry), root in zip(resolved, roots):
            entry["lock_hash"] = root["hash"]
        try:
            context.install_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(
                context.install_dir / "spack.lock",
                environment.lock_path.read_text(encoding="utf-8"),
            )
        except OSError:
            pass
    record["spack_env"] = environment.describe(roots)


# ---------------------------------------------------------------------------
# MPI selection for launchers
# ---------------------------------------------------------------------------

#: Spack MPI package -> ``jarvis_cd.shell.ExecType`` value used to build
#: the launcher. MPIs not listed are detected with ``mpiexec --version``.
MPI_EXEC_TYPES = {
    "openmpi": "openmpi",
    "hpcx-mpi": "openmpi",
    "mpich": "mpich",
    "mvapich2": "mpich",
    "mvapich": "mpich",
    "intel-oneapi-mpi": "intel_mpi",
    "intel-mpi": "intel_mpi",
    "cray-mpich": "cray_mpich",
}


def mpi_environment(record: Mapping[str, Any]) -> Dict[str, str]:
    """``JARVIS_MPI_*`` variables for the MPI the pipeline's packages chose.

    Launchers read them (see ``jarvis_cd.shell.exec_factory``): the type
    skips ``mpiexec --version`` probing, and the prefix selects
    ``<prefix>/bin/mpiexec`` and OpenMPI's ``--prefix`` so remote daemons
    started over ssh find the same installation. Returns ``{}`` when no
    package uses MPI or packages disagree.
    """
    chosen = [
        entry["mpi"]
        for entry in record.get("packages", {}).values()
        if isinstance(entry, Mapping)
        and entry.get("status") in ("satisfied", "installed")
        and isinstance(entry.get("mpi"), Mapping)
    ]
    if not chosen:
        return {}
    first = chosen[0]
    if any(str(mpi.get("hash", ""))[:7] != str(first.get("hash", ""))[:7] for mpi in chosen):
        return {}
    name = str(first.get("name"))
    version = first.get("version")
    environment = {
        "JARVIS_MPI_NAME": name,
        "JARVIS_MPI_SPEC": f"{name}{'@' + str(version) if version else ''}/{first.get('hash')}",
    }
    if name in MPI_EXEC_TYPES:
        environment["JARVIS_MPI_TYPE"] = MPI_EXEC_TYPES[name]
    if first.get("prefix"):
        environment["JARVIS_MPI_PREFIX"] = str(first["prefix"])
    return environment


def _package_query(pkg_def: Mapping[str, Any]) -> str:
    config = pkg_def.get("config", {}) or {}
    return str(config.get("install_query") or config.get("install") or "").strip()


def _package_repos(pkg_def: Mapping[str, Any]) -> List[str]:
    """``install_repos`` of one package, as given (see :func:`resolve_package_repos`)."""
    config = pkg_def.get("config", {}) or {}
    repos = config.get("install_repos") or []
    if isinstance(repos, str):
        repos = [repos]
    return [str(repo) for repo in repos if str(repo).strip()]


def resolve_package_repos(
    repos: Sequence[str], package_dir: Optional[Path]
) -> List[str]:
    """Absolute Spack repo dirs: relative entries are under ``package_dir``."""
    resolved = []
    for repo in repos:
        path = Path(os.path.expandvars(str(repo))).expanduser()
        if not path.is_absolute():
            if package_dir is None:
                raise RuntimeError(
                    f"install_repos entry {repo!r} is relative but the package "
                    "directory is unknown"
                )
            path = Path(package_dir) / path
        path = path.resolve()
        if not (path / "repo.yaml").is_file():
            raise RuntimeError(f"install_repos entry {repo!r}: {path} has no repo.yaml")
        resolved.append(str(path))
    return resolved


def pipeline_repos(pkg_list: Sequence[Mapping[str, Any]]) -> List[str]:
    """Every package's (already absolute) ``install_repos``, deduplicated."""
    repos: List[str] = []
    for pkg_def in pkg_list:
        for repo in _package_repos(pkg_def):
            if repo not in repos:
                repos.append(repo)
    return repos


def write_repo_scope(scope_dir: Path, repos: Sequence[str]) -> Path:
    """A ``spack -C`` scope directory whose ``repos.yaml`` lists ``repos``.

    Commands outside the pipeline env (``spec``, ``find``, ``load``) see the
    recipes through it, so nothing is added to any global Spack config.
    """
    scope_dir.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump({"repos": list(repos)}, sort_keys=False)
    path = scope_dir / "repos.yaml"
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = None
    if current != text:
        _atomic_write_text(path, text)
    return scope_dir


def run_spack_install(
    pkg_list: Sequence[Mapping[str, Any]],
    context: InstallContext,
    runner: Optional[SpackRunner] = None,
    env_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Plan and, in install mode, build every Spack package in ``pkg_list``.

    With ``env_dir`` the missing nodes are built through that per-pipeline
    Spack environment and its ``spack.lock`` becomes the install record
    (see :class:`SpackEnvironment`); without it each package gets its own
    ``spack install``.

    Returns the ``jarvis.install-record.v1`` document (also written to
    ``install-record.json``). Issues are appended to ``context.issues``; the
    caller decides whether to raise :class:`InstallDecisionNeeded`.
    """
    planner = SpackPlanner(context, runner)
    if any(_package_query(pkg_def) for pkg_def in pkg_list):
        missing = ensure_spack_available(context, planner.decisions)
        if missing is not None:
            record = {
                "schema_version": RECORD_SCHEMA,
                "mode": context.mode,
                "spack_root": None,
                "spack_missing": missing["issue_id"],
                "decisions": str(context.decisions_path),
                "started_at": _utc_now(),
                "finished_at": _utc_now(),
                "packages": {
                    str(d.get("pkg_id") or d.get("pkg_name") or "pkg"): {
                        "status": "blocked", "blocked_by": "spack_missing",
                    }
                    for d in pkg_list
                },
                "packages_total": len(pkg_list),
                "issues": [issue["issue_id"] for issue in context.issues],
                "applied_decisions": list(context.applied_decisions),
                "stale_decisions": list(context.stale_decisions),
            }
            _atomic_write_text(
                context.record_path, json.dumps(record, indent=2, sort_keys=True) + "\n"
            )
            return record
    repos = pipeline_repos(pkg_list)
    record: Dict[str, Any] = {
        "schema_version": RECORD_SCHEMA,
        "mode": context.mode,
        "spack_root": explicit_spack_root() or spack_root(),
        "spack_config": spack_user_config_path(),
        "spack_local_config_disabled": spack_local_config_disabled(),
        "spack_config_scopes": [],
        "spack_executable": spack_executable(),
        "spack_repos": repos,
        "spack_scope": None,
        "decisions": str(context.decisions_path),
        "started_at": _utc_now(),
        "packages": {},
    }
    if repos:
        scope = write_repo_scope(context.install_dir / REPO_SCOPE_NAME, repos)
        record["spack_scope"] = str(scope)
        planner.runner.config_scopes = [str(scope)]
    record["spack_config_scopes"] = spack_config_scopes(
        planner.runner.config_scopes, env_dir
    )
    from jarvis_cd.core.spack_site import site_prep_record

    record["spack_site_prep"] = site_prep_record(explicit_spack_root())
    compiler_state = apply_build_compilers(planner, context, record)
    record["spack_config_scopes"] = spack_config_scopes(
        planner.runner.config_scopes, env_dir
    )
    packages = [
        (str(pkg_def.get("pkg_id") or pkg_def.get("pkg_name") or "pkg"), pkg_def)
        for pkg_def in pkg_list
    ]
    total = len(packages)
    resolved: List[tuple[str, Dict[str, Any]]] = []
    to_build: List[tuple[int, str, Dict[str, Any]]] = []
    for index, (package, pkg_def) in enumerate(packages):
        query = _package_query(pkg_def)
        context.report_progress(
            phase="resolving", current_package=package, packages_done=index
        )
        if not query:
            record["packages"][package] = {"status": "no_query"}
            continue
        entry = planner.plan_package(package, query)
        if entry["status"] == "missing":
            if context.mode == "check":
                context.raise_issue(
                    make_issue(
                        kind="not_installed",
                        package=package,
                        subject=entry["spec"],
                        scope="package",
                        question=(
                            f"{package} needs {len(entry['missing'])} Spack "
                            "package(s) built before it can run."
                        ),
                        candidates=[],
                        detail=", ".join(
                            f"{item['name']}/{item['hash']}" for item in entry["missing"]
                        ),
                        answerable=False,
                        hint="run `jarvis ppl install` (e.g. as a 1-node +submit job)",
                    )
                )
            elif env_dir is None:
                to_build.append((index, package, entry))
        if entry["status"] in ("satisfied", "missing"):
            resolved.append((package, entry))
        record["packages"][package] = entry
        context.report_progress(
            phase="resolving",
            current_package=None,
            packages_done=index + 1,
            missing=[item["name"] for item in entry.get("missing", [])][:32],
        )
    blocked = compiler_state.get("blocked")
    if blocked is not None:
        if any(entry.get("status") == "missing" for _package, entry in resolved):
            # Nothing is built with a compiler the build hosts cannot run.
            context.raise_issue(blocked)
            to_build = []
        else:
            record.setdefault("warnings", []).append(
                f"binding compiler {blocked['compiler']} is not usable on "
                f"{blocked['host']}; nothing needed building"
            )
    try:
        if to_build and not context.issues:
            context.ensure_may_build(
                "spack install",
                [item["name"] for _i, _p, e in to_build for item in e["missing"]],
            )
            for index, package, entry in to_build:
                context.report_progress(
                    phase="building",
                    current_package=package,
                    packages_done=index,
                    missing=[item["name"] for item in entry["missing"]][:32],
                )
                record["packages"][package] = planner.install_package(package, entry)
        if env_dir is not None and resolved:
            _sync_spack_environment(
                planner, context, record, resolved, Path(env_dir),
                sections=compiler_state["sections"] if context.mode == "install" else None,
            )
    except BuildOutsideAllocation as refused:
        record["refused"] = refused.to_dict()
        record["finished_at"] = _utc_now()
        record["packages_total"] = total
        record["issues"] = [issue["issue_id"] for issue in context.issues]
        _atomic_write_text(
            context.record_path, json.dumps(record, indent=2, sort_keys=True) + "\n"
        )
        raise
    record["finished_at"] = _utc_now()
    record["packages_total"] = total
    record["issues"] = [issue["issue_id"] for issue in context.issues]
    record["used_answers"] = list(context.used_answers)
    record["applied_decisions"] = list(context.applied_decisions)
    record["stale_decisions"] = list(context.stale_decisions)
    _atomic_write_text(
        context.record_path, json.dumps(record, indent=2, sort_keys=True) + "\n"
    )
    return record
