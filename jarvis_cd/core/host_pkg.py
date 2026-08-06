"""
Host prerequisite packages for Jarvis-CD.

A ``host_pkgs`` entry declares software that must exist on the **host
baremetal** for jarvis itself to function -- as opposed to ``pkgs``, which
declares the workload jarvis deploys. The motivating case is a
containerized pipeline: jarvis shells out to ``apptainer build`` on the
host, so apptainer has to be on the host's PATH *before* the pipeline
starts. Without a declaration, a missing apptainer surfaces late as an
opaque ``apptainer: command not found`` from inside the container build,
long after jarvis has created directories and started packages.

YAML shape (top level of a pipeline, or inside a pipeline test's
``config:`` block)::

    host_pkgs:
      - install_method: spack
        install_query: apptainer

``install_method`` selects a backend (spack / pip / conda);
``install_query`` is the string that backend uses to install the
software, and doubles as the string used to look it up.

Two things happen at check time, in :meth:`HostPkg.check_all`:

1. **Verify.** Every declared package is probed on the host. Anything
   missing raises :class:`HostPkgError` naming the exact command to run.
   This is a check, not an install -- ``spack install apptainer`` can run
   for hours, which is not something a pipeline launch should do behind
   the user's back.
2. **Activate.** Packages that *are* present contribute their environment
   (e.g. the PATH entry ``spack load apptainer`` produces) to the
   process environment, so the subprocesses jarvis spawns to do the
   containerizing actually find them.

On (2): the activation is written into ``os.environ``, not just the
pipeline's ``env`` dict. That is deliberate. ``LocalExec.run`` falls back
to a bare ``os.environ`` whenever its ``ExecInfo`` carries no explicit
env, and the container build path (``ContainerInstaller``, the apptainer
``instance start``/``build`` calls) constructs plain ``LocalExecInfo()``
at a couple dozen call sites. Threading an env dict through all of them
would be invasive and would silently miss any new call site; putting the
activation in the process environment covers every subprocess jarvis
spawns, present and future, which is exactly the scope a *host*
prerequisite has.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple, Type

# A POSIX environment variable name. Used to reject the noise that shows
# up in `env` output but is not a variable an inherited subprocess should
# see -- most importantly exported bash *functions*, which `env` prints as
# `BASH_FUNC_name%%=() { ...` with the body spanning further lines. Left
# unfiltered, each body line parses as its own bogus variable and the
# function itself lands in os.environ, where it is both useless to a
# non-bash child and a well-known injection surface.
_ENV_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

# Shell bookkeeping that changes on every invocation and means nothing to
# a child process. Carrying these would make an activation look "dirty"
# on every probe and would stomp the child's own values.
_ENV_NOISE = frozenset({'_', 'SHLVL', 'PWD', 'OLDPWD', 'BASH_EXECUTION_STRING'})


class HostPkgError(RuntimeError):
    """A declared host prerequisite is missing, or the declaration is
    malformed. Carries an actionable message naming the fix."""


# How long a single probe / activation subprocess may run. `spack load`
# on a cold cache is slow but not minutes-slow; a hung probe should not
# wedge the pipeline launch indefinitely.
PROBE_TIMEOUT = 120


class HostPkg(ABC):
    """Base class for host-prerequisite backends.

    Subclasses set :attr:`install_method` to register themselves with the
    factory, mirroring how :class:`jarvis_cd.core.installer.Installer`
    registers install backends. The two hierarchies are deliberately
    separate: an Installer *installs the workload*, a HostPkg *verifies
    the host can run jarvis at all*.
    """

    install_method: str = ""  # Subclasses set this to register.

    # Probe results are memoized per (method, query) for the life of the
    # process. A sweep re-loads the same pipeline YAML once per
    # combination, and each load would otherwise re-run the same
    # `spack find` subprocess -- 24 combinations meant 24 identical
    # probes before this cache.
    _probe_cache: Dict[Tuple[str, str], Tuple[bool, Dict[str, str]]] = {}

    @abstractmethod
    def is_installed(self, query: str) -> bool:
        """True when ``query`` is already available on this host."""

    @abstractmethod
    def activate(self, query: str) -> Dict[str, str]:
        """Environment variables that make ``query`` usable by
        subprocesses. Called only after :meth:`is_installed` returns True.
        Returning an empty dict is valid (nothing to add to the env).
        """

    @abstractmethod
    def install_hint(self, query: str) -> str:
        """The command a user should run to satisfy this prerequisite.
        Shown verbatim in the error raised for a missing package.
        """

    # ------------------------------------------------------------------
    # registry
    # ------------------------------------------------------------------

    @staticmethod
    def _registry() -> Dict[str, Type['HostPkg']]:
        """Map ``install_method`` -> concrete HostPkg subclass, built by
        walking subclasses so a new backend is just a new subclass."""
        reg: Dict[str, Type[HostPkg]] = {}

        def _walk(cls: Type[HostPkg]):
            for sub in cls.__subclasses__():
                if sub.install_method:
                    reg[sub.install_method] = sub
                _walk(sub)

        _walk(HostPkg)
        return reg

    @staticmethod
    def for_method(method: str) -> 'HostPkg':
        """Instantiate the backend registered for ``method``."""
        reg = HostPkg._registry()
        if method not in reg:
            raise HostPkgError(
                f"Unknown host_pkgs install_method '{method}'. "
                f"Known methods: {', '.join(sorted(reg))}."
            )
        return reg[method]()

    # ------------------------------------------------------------------
    # parsing / validation
    # ------------------------------------------------------------------

    @staticmethod
    def parse(raw: Any) -> List[Dict[str, str]]:
        """Normalize and validate a raw ``host_pkgs`` YAML value.

        Validation is strict and happens at load time rather than at use
        time: a typo'd key in a prerequisite block should fail the
        pipeline immediately, not silently skip the check and resurface
        as a missing binary halfway through a container build.

        :param raw: the YAML value (``None`` / list of dicts)
        :return: list of ``{'install_method': ..., 'install_query': ...}``
        :raises HostPkgError: on any malformed entry
        """
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise HostPkgError(
                f"'host_pkgs' must be a list of "
                f"{{install_method, install_query}} entries, got "
                f"{type(raw).__name__}."
            )

        known = sorted(HostPkg._registry())
        parsed: List[Dict[str, str]] = []
        for idx, entry in enumerate(raw):
            where = f"host_pkgs[{idx}]"
            if not isinstance(entry, dict):
                raise HostPkgError(
                    f"{where} must be a mapping with 'install_method' and "
                    f"'install_query', got {type(entry).__name__}."
                )

            unknown_keys = set(entry) - {'install_method', 'install_query'}
            if unknown_keys:
                raise HostPkgError(
                    f"{where} has unknown key(s) "
                    f"{sorted(unknown_keys)}. Supported keys: "
                    "install_method, install_query."
                )

            method = str(entry.get('install_method') or '').strip()
            query = str(entry.get('install_query') or '').strip()
            if not method:
                raise HostPkgError(
                    f"{where} is missing 'install_method'. "
                    f"Supported methods: {', '.join(known)}.")
            if not query:
                raise HostPkgError(
                    f"{where} (install_method: {method}) is missing "
                    "'install_query' -- the string used to install the "
                    "software, e.g. 'apptainer'.")
            if method not in known:
                raise HostPkgError(
                    f"{where} has unknown install_method '{method}'. "
                    f"Known methods: {', '.join(known)}.")

            parsed.append(
                {'install_method': method, 'install_query': query})
        return parsed

    # ------------------------------------------------------------------
    # check + activate
    # ------------------------------------------------------------------

    @staticmethod
    def resolve(method: str, query: str) -> Tuple[bool, Dict[str, str]]:
        """Probe one prerequisite, memoized.

        :return: ``(installed, env)``; ``env`` is empty when not installed.
        """
        key = (method, query)
        if key in HostPkg._probe_cache:
            return HostPkg._probe_cache[key]

        backend = HostPkg.for_method(method)
        installed = backend.is_installed(query)
        env = backend.activate(query) if installed else {}
        HostPkg._probe_cache[key] = (installed, env)
        return installed, env

    @staticmethod
    def clear_cache() -> None:
        """Drop memoized probe results. Tests use this; so should any
        caller that installs a prerequisite mid-process and wants the
        next check to see it."""
        HostPkg._probe_cache.clear()

    @staticmethod
    def check_all(host_pkgs: List[Dict[str, str]],
                  env: Optional[Dict[str, str]] = None,
                  context: str = "") -> Dict[str, str]:
        """Verify every declared prerequisite and activate it.

        :param host_pkgs: parsed entries (see :meth:`parse`)
        :param env: optional pipeline env dict to merge activations into,
            in addition to ``os.environ``
        :param context: pipeline / test name, for the error message
        :return: the merged activation environment
        :raises HostPkgError: naming every missing package and its fix
        """
        if not host_pkgs:
            return {}

        merged: Dict[str, str] = {}
        missing: List[Tuple[Dict[str, str], str]] = []

        for spec in host_pkgs:
            method = spec['install_method']
            query = spec['install_query']
            installed, activation = HostPkg.resolve(method, query)
            if not installed:
                backend = HostPkg.for_method(method)
                missing.append((spec, backend.install_hint(query)))
                continue
            merged.update(activation)

        if missing:
            where = f" for '{context}'" if context else ""
            lines = [
                f"Missing {len(missing)} required host package(s){where}. "
                "These must be installed on the host baremetal before "
                "jarvis can run this pipeline:"
            ]
            for spec, hint in missing:
                lines.append(
                    f"  - {spec['install_query']} "
                    f"(install_method: {spec['install_method']})\n"
                    f"      install it with: {hint}")
            raise HostPkgError('\n'.join(lines))

        HostPkg.apply_env(merged, env)
        return merged

    @staticmethod
    def apply_env(activation: Dict[str, str],
                  env: Optional[Dict[str, str]] = None) -> None:
        """Publish an activation into the process environment.

        Writes to ``os.environ`` so that every subprocess jarvis spawns
        inherits it -- including the container-build calls that construct
        a bare ``LocalExecInfo()`` and therefore run under an unmodified
        ``os.environ``. See this module's docstring for why the scope is
        process-global rather than per-ExecInfo.
        """
        if not activation:
            return
        for key, value in activation.items():
            os.environ[key] = str(value)
        if env is not None:
            env.update({k: str(v) for k, v in activation.items()})


# ---------------------------------------------------------------------------
# Concrete backends
# ---------------------------------------------------------------------------


def _run_capture(script: str) -> subprocess.CompletedProcess:
    """Run ``script`` under bash, capturing output.

    Uses subprocess directly rather than the project's ``Exec``: probes
    run before the pipeline env exists, must stay quiet on the console,
    and need the raw stdout to parse an environment out of.
    """
    return subprocess.run(
        ['bash', '-c', script],
        capture_output=True, text=True, timeout=PROBE_TIMEOUT,
        env=os.environ.copy(),
    )


def _parse_env0(stdout: str) -> Dict[str, str]:
    """Parse the output of ``env -0`` into a dict.

    NUL-separated rather than newline-separated so that values which
    themselves contain newlines stay in one piece. Entries whose name is
    not a plain identifier are dropped -- see :data:`_ENV_NAME_RE`.
    """
    parsed: Dict[str, str] = {}
    for record in stdout.split('\0'):
        if not record or '=' not in record:
            continue
        key, _, value = record.partition('=')
        if key in _ENV_NOISE or not _ENV_NAME_RE.match(key):
            continue
        parsed[key] = value
    return parsed


class SpackHostPkg(HostPkg):
    """Host prerequisite provided by spack.

    ``install_query`` is a spack spec (``apptainer``, ``apptainer@1.3.6``,
    ``hdf5+mpi``). Presence is probed with ``spack location -i <spec>``,
    which succeeds only for a spec that is actually *installed* --
    ``spack find`` also matches specs that are merely known to spack.
    """

    install_method = "spack"

    @staticmethod
    def _setup_prefix() -> str:
        """Shell prefix that makes ``spack`` callable.

        Sources ``setup-env.sh`` when SPACK_ROOT is set; otherwise relies
        on spack already being on PATH (the shell-function form installed
        by a user's rc files is not visible to a non-interactive bash, so
        SPACK_ROOT is the reliable route).
        """
        spack_root = os.environ.get('SPACK_ROOT', '')
        if spack_root:
            return f'. {spack_root}/share/spack/setup-env.sh >/dev/null 2>&1 && '
        return ''

    def _spack_available(self) -> bool:
        """True when a spack command can be reached at all."""
        if shutil.which('spack'):
            return True
        spack_root = os.environ.get('SPACK_ROOT', '')
        return bool(spack_root) and os.path.exists(
            os.path.join(spack_root, 'share', 'spack', 'setup-env.sh'))

    def is_installed(self, query: str) -> bool:
        if not self._spack_available():
            return False
        try:
            result = _run_capture(
                f'{self._setup_prefix()}spack location -i {query}')
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
        return result.returncode == 0

    def activate(self, query: str) -> Dict[str, str]:
        """Capture the environment ``spack load <spec>`` produces.

        Only variables the load actually changed are returned, so an
        activation cannot smuggle unrelated host state (or a stale
        working directory) into the pipeline environment.
        """
        try:
            result = _run_capture(
                f'{self._setup_prefix()}spack load {query} && env -0')
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return {}
        if result.returncode != 0:
            return {}

        loaded = _parse_env0(result.stdout)
        return {
            key: value for key, value in loaded.items()
            if os.environ.get(key) != value
        }

    def install_hint(self, query: str) -> str:
        return f"spack install {query}"


class PipHostPkg(HostPkg):
    """Host prerequisite provided by pip.

    ``install_query`` is a pip requirement spec. Presence is probed with
    ``pip show`` against the distribution name, which is the leading
    token of the spec (``ruamel.yaml>=0.17`` -> ``ruamel.yaml``).
    """

    install_method = "pip"

    @staticmethod
    def _dist_name(query: str) -> str:
        name = query.strip()
        for sep in ('===', '==', '>=', '<=', '~=', '!=', '>', '<', '[', ';'):
            name = name.split(sep, 1)[0]
        return name.strip()

    def is_installed(self, query: str) -> bool:
        dist = self._dist_name(query)
        if not dist:
            return False
        try:
            result = _run_capture(
                f'python3 -m pip show {dist}')
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
        return result.returncode == 0

    def activate(self, query: str) -> Dict[str, str]:
        # pip installs into the interpreter jarvis is already running
        # under, so the host environment already resolves it.
        return {}

    def install_hint(self, query: str) -> str:
        return f"python3 -m pip install {query}"


class CondaHostPkg(HostPkg):
    """Host prerequisite provided by conda, checked against the currently
    active environment. ``install_query`` is a conda package spec."""

    install_method = "conda"

    @staticmethod
    def _pkg_name(query: str) -> str:
        return query.strip().split('=')[0].strip()

    def is_installed(self, query: str) -> bool:
        if not shutil.which('conda'):
            return False
        name = self._pkg_name(query)
        if not name:
            return False
        try:
            # `conda list <name>` exits 0 even with no match, so match on
            # a real output row instead of the exit code.
            result = _run_capture(f'conda list --no-pip {name}')
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
        if result.returncode != 0:
            return False
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.split()[0] == name:
                return True
        return False

    def activate(self, query: str) -> Dict[str, str]:
        # Packages land in the already-active conda env; nothing to add.
        return {}

    def install_hint(self, query: str) -> str:
        return f"conda install -y {query}"
