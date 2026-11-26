"""Does a Spack work on its own, before any package is involved?

A Spack can fail for reasons that have nothing to do with the package asked
for: a repo in the user's ``~/.spack/repos.yaml`` with an invalid package
directory, say, breaks the provider index, and then every ``spack load``,
``spack spec`` and concretize fails, while ``spack find`` still works.
Before JARVIS recommends a copy from a site Spack, loads one, or plans a
build with a Spack, it probes that Spack once (:data:`PROBE`, which builds
the provider index of every configured repo).

A failing probe is returned as it is: the ``failure`` record (stage
``spack_health``) with the Spack, the command, its exit code, the full
output in a file and the Spack's config scope directories. JARVIS does not
diagnose or work around it, and uses nothing from that Spack while it
fails. The caller reads the error and decides; Spack's own variables
(``SPACK_DISABLE_LOCAL_CONFIG``, ``SPACK_USER_CONFIG_PATH``) and
JARVIS's (``JARVIS_SPACK_ROOT``, ``JARVIS_SITE_SPACKS``) pass through to
every Spack command JARVIS runs. JARVIS never edits the person's
``~/.spack``.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

SCHEMA = "jarvis.spack-health.v1"
#: builds the provider index of every configured repo, needs no package
PROBE = ("providers", "mpi")
_TIMEOUT = 300
_CACHE_SECONDS = 600
#: ``off`` skips the probe (the unit tests' default)
SWITCH_ENV = "JARVIS_SPACK_HEALTH"

#: (root, environment digest) -> (monotonic time, probe result)
_PROBES: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
#: the variables that change which config a Spack command reads
_CONFIG_ENV = ("SPACK_DISABLE_LOCAL_CONFIG", "SPACK_USER_CONFIG_PATH",
               "SPACK_SYSTEM_CONFIG_PATH", "SPACK_ENV")


def reset() -> None:
    """Forget every probe made in this process (tests)."""
    _PROBES.clear()


def enabled() -> bool:
    return os.environ.get(SWITCH_ENV, "").strip().lower() not in ("off", "0", "false", "no")


def root_key(root: Any) -> str:
    try:
        return str(Path(str(root)).expanduser().resolve())
    except (OSError, RuntimeError):
        return str(root)


def environment(root: Any = None, base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The process environment for a command of the Spack at ``root``: the
    caller's, unchanged."""
    return dict(os.environ if base is None else base)


def _executable(root: str, executable: Optional[str]) -> str:
    return str(executable or Path(root) / "bin" / "spack")


def _run(executable: str, args: Sequence[str], env: Mapping[str, str],
         timeout: int = _TIMEOUT) -> Tuple[Optional[int], str, str]:
    try:
        completed = subprocess.run(
            [executable, *args], capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, env=dict(env), encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return None, "", f"timed out after {timeout} s"
    except OSError as exc:
        return None, "", str(exc)
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def error_line(text: str) -> str:
    """The error Spack reports (its last ``Error:`` line, else the last line)."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    errors = [line for line in lines if "Error:" in line or "Error " in line[:12]]
    line = (errors or lines or [""])[-1]
    if line.startswith("==> "):
        line = line[4:]
    return line[:500]


def probe(root: Any, executable: Optional[str] = None, *, fresh: bool = False
          ) -> Dict[str, Any]:
    """Run :data:`PROBE` with the Spack at ``root``; ``ok`` None is unknown.

    The result keeps the whole output (``output``) for the failure record.
    """
    key = root_key(root)
    env = environment(None)
    mode = ";".join(f"{name}={env.get(name, '')}" for name in _CONFIG_ENV)
    cached = _PROBES.get((key, mode))
    if cached and not fresh and time.monotonic() - cached[0] < _CACHE_SECONDS:
        return dict(cached[1])
    exe = _executable(key, executable)
    started = time.monotonic()
    rc, out, err = _run(exe, PROBE, env)
    result: Dict[str, Any] = {
        "schema_version": SCHEMA,
        "root": key,
        "executable": exe,
        "command": [exe, *PROBE],
        "config_env": {name: env[name] for name in _CONFIG_ENV if name in env},
        "exit": rc,
        # a timeout or a Spack that cannot run is not this check's to judge
        "ok": None if rc is None else rc == 0,
        "error": None if rc == 0 else (error_line(err or out) or f"exit {rc}"),
        "seconds": round(time.monotonic() - started, 3),
    }
    if rc not in (0, None):
        result["output"] = out + err
    _PROBES[(key, mode)] = (time.monotonic(), result)
    return dict(result)


def scope_paths(root: str) -> Dict[str, Path]:
    """The config scope directories of the Spack at ``root`` (as Spack 0.23)."""
    etc = Path(root) / "etc" / "spack"
    user = os.environ.get("SPACK_USER_CONFIG_PATH", "").strip() or "~/.spack"
    system = os.environ.get("SPACK_SYSTEM_CONFIG_PATH", "").strip() or "/etc/spack"
    return {"defaults": etc / "defaults", "site": etc,
            "user": Path(user).expanduser(), "system": Path(system).expanduser()}


def gate(root: Any, executable: Optional[str], context, *, packages: Sequence[str] = ()
         ) -> Dict[str, Any]:
    """Whether JARVIS may use the Spack at ``root``.

    Returns ``{"status": ...}``: ``off`` (``JARVIS_SPACK_HEALTH=off``),
    ``not_probed`` (plan only), ``ok``, ``unknown`` (the probe could not
    run), or ``broken`` with ``issue``, the raised failure record.
    """
    from jarvis_cd.core.installer import plan_only_active
    from jarvis_cd.core.spack_install import _utc_now, failure_record

    key = root_key(root)
    if not enabled():
        return {"status": "off", "root": key}
    if plan_only_active():
        return {"status": "not_probed", "root": key}
    result = probe(key, executable)
    if result.get("ok") is None:
        context.log(f"[{_utc_now()}] spack health: {key} not judged ({result.get('error')})\n")
        return {"status": "unknown", "root": key, "probe": result}
    if result["ok"]:
        return {"status": "ok", "root": key, "probe": result}
    issue = failure_record(
        context, stage="spack_health",
        doing=f"checking that the Spack at {key} works (before using it)",
        command=result["command"], exit_code=result["exit"],
        output=str(result.get("output") or ""), packages=packages,
        paths={"spack_root": key,
               "scopes": {name: str(path) for name, path in scope_paths(key).items()}},
        extra={"spack_root": key, "config_env": result.get("config_env") or {},
               "seconds": result.get("seconds")},
    )
    context.raise_issue(issue)
    return {"status": "broken", "root": key, "issue": issue, "issue_id": issue["issue_id"]}
