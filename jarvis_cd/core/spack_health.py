"""Does a Spack work on its own, before any package is involved?

A Spack can fail for reasons that have nothing to do with the package asked
for: a repo in the user's ``~/.spack/repos.yaml`` with an invalid package
directory, say, breaks the provider index, and then every ``spack load``,
``spack spec`` and concretize fails, while ``spack find`` still works.
Before JARVIS recommends a copy from a site Spack, loads one, or plans a
build with a Spack, it probes that Spack once (:data:`PROBE`, which builds
the provider index of every configured repo).

A failing probe is the typed, answerable ``spack_broken`` issue (site
scope, subject ``spack:<root>``). It names the Spack, the error, and what
causes it when that can be isolated: each repo the user and system scopes
add is probed alone, so the issue names the failing repo and the scope file
that lists it. Its candidates:

* ``drop_failing_repos``: JARVIS runs its commands for that Spack with its
  own copy of the failing scope (the user or system scope), the failing
  repos removed from its ``repos.yaml`` and everything else kept, by
  pointing ``SPACK_USER_CONFIG_PATH`` / ``SPACK_SYSTEM_CONFIG_PATH`` at the
  copy (offered, and recommended, only when the failure is confined to
  named repos and the probe passes with the copy). The copy lives under the
  site home (``JARVIS_SPACK_SCOPE_DIR``), is named by its content and is
  rebuilt from the person's scope each time, so later edits there apply;
* ``no_user_config``: JARVIS runs its commands for that Spack with
  ``SPACK_DISABLE_LOCAL_CONFIG=1`` (offered only when the probe passes that
  way). It says what this leaves out: the user and system scopes, with
  their repos, compilers, mirrors and package preferences;
* ``skip``: JARVIS does not use that Spack: it offers no copies from it,
  and builds with its own Spack instead (one under the site home that
  reuses that Spack's installs as an upstream);
* ``recheck``: the person fixes it; JARVIS probes again (nothing recorded).

The answer is recorded per site in the decisions store, as the other site
answers are. JARVIS never edits the person's ``~/.spack``.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

SCHEMA = "jarvis.spack-health.v1"
KIND = "spack_broken"
DROP_REPOS = "drop_failing_repos"
NO_USER_CONFIG = "no_user_config"
SKIP = "skip"
RECHECK = "recheck"
#: builds the provider index of every configured repo, needs no package
PROBE = ("providers", "mpi")
_TIMEOUT = 300
_CACHE_SECONDS = 600
_MAX_REPO_PROBES = 8
_ISOLATION_DROPS = ("user", "system")
#: ``off`` skips the probe (the unit tests' default)
SWITCH_ENV = "JARVIS_SPACK_HEALTH"
#: where JARVIS writes its copies of a scope without the failing repos
SCOPE_DIR_ENV = "JARVIS_SPACK_SCOPE_DIR"
#: the variable that moves each scope ``drop_failing_repos`` can rewrite
_SCOPE_ENV = {"user": "SPACK_USER_CONFIG_PATH", "system": "SPACK_SYSTEM_CONFIG_PATH"}
#: scope subdirectories that hold caches or state, not config
_SCOPE_SKIP_DIRS = {"cache", "bootstrap", "reports", "git_repos", "test", "environments",
                    "package_repos", "repos"}

#: Spack roots whose commands run without the user and system config
_ISOLATED: set = set()
#: Spack roots whose commands run with JARVIS's scope copies: root -> {var: dir}
_FILTERED: Dict[str, Dict[str, str]] = {}
#: Spack roots the site's answer skips
_SKIPPED: set = set()
#: (root, mode) -> (monotonic time, probe result); mode is plain, isolated or the scopes
_PROBES: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
#: root -> (monotonic time, diagnosis)
_DIAGNOSES: Dict[str, Tuple[float, Dict[str, Any]]] = {}

_BLAME = re.compile(r"^(?P<file>/\S+?):(?P<line>\d+)\s+-\s+(?P<repo>\S.*?)\s*$")
_YAML_FILE = re.compile(r"(/[^\s:'\"()]+\.ya?ml)")
_NAMESPACE = re.compile(r"^\s*namespace:\s*['\"]?([^'\"\s#]+)", re.M)
_ADDRESS = re.compile(r"0x[0-9a-fA-F]+")


def reset() -> None:
    """Forget every probe and answer applied in this process (tests)."""
    _ISOLATED.clear()
    _FILTERED.clear()
    _SKIPPED.clear()
    _PROBES.clear()
    _DIAGNOSES.clear()


def enabled() -> bool:
    return os.environ.get(SWITCH_ENV, "").strip().lower() not in ("off", "0", "false", "no")


def root_key(root: Any) -> str:
    try:
        return str(Path(str(root)).expanduser().resolve())
    except (OSError, RuntimeError):
        return str(root)


def subject_for(root: Any) -> str:
    """The decisions-store subject of a Spack's ``spack_broken`` answer."""
    return f"spack:{root_key(root)}"


def isolated(root: Any) -> bool:
    """Whether the site's answer runs this Spack without the user config."""
    return root is not None and root_key(root) in _ISOLATED


def skipped(root: Any) -> bool:
    return root is not None and root_key(root) in _SKIPPED


def filtered_scopes(root: Any) -> Optional[Dict[str, str]]:
    """The scope copies the site's ``drop_failing_repos`` answer runs ``root`` with."""
    if root is None:
        return None
    scopes = _FILTERED.get(root_key(root))
    return dict(scopes) if scopes else None


def isolate_environment(environment: Dict[str, str]) -> Dict[str, str]:
    environment["SPACK_DISABLE_LOCAL_CONFIG"] = "1"
    for name in ("SPACK_ENV", "SPACK_ENV_VIEW", "SPACK_OLD_PS1"):
        environment.pop(name, None)
    return environment


def scoped_environment(environment: Dict[str, str], scopes: Mapping[str, str]
                       ) -> Dict[str, str]:
    """Point Spack's user/system scopes at JARVIS's copies (``scopes``: var -> dir)."""
    environment.update(scopes)
    for name in ("SPACK_ENV", "SPACK_ENV_VIEW", "SPACK_OLD_PS1"):
        environment.pop(name, None)
    return environment


def apply(environment: Dict[str, str], root: Any) -> Dict[str, str]:
    """Apply the site's answer for the Spack at ``root`` to ``environment``."""
    if isolated(root):
        return isolate_environment(environment)
    scopes = filtered_scopes(root)
    if scopes:
        return scoped_environment(environment, scopes)
    return environment


def environment(root: Any = None, base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The process environment for a command of the Spack at ``root``."""
    return apply(dict(os.environ if base is None else base), root)


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


def probe(root: Any, executable: Optional[str] = None, *, isolate: bool = False,
          fresh: bool = False, scopes: Optional[Mapping[str, str]] = None
          ) -> Dict[str, Any]:
    """Run :data:`PROBE` with the Spack at ``root``; ``ok`` None is unknown.

    ``isolate`` runs it with ``SPACK_DISABLE_LOCAL_CONFIG=1``; ``scopes``
    (var -> dir) runs it with JARVIS's scope copies instead.
    """
    key = root_key(root)
    mode = ("isolated" if isolate else
            "scopes:" + ",".join(f"{k}={v}" for k, v in sorted(scopes.items())) if scopes
            else "plain")
    cached = _PROBES.get((key, mode))
    if cached and not fresh and time.monotonic() - cached[0] < _CACHE_SECONDS:
        return dict(cached[1])
    env = environment(None)
    if isolate:
        isolate_environment(env)
    elif scopes:
        scoped_environment(env, scopes)
    exe = _executable(key, executable)
    started = time.monotonic()
    rc, out, err = _run(exe, PROBE, env)
    result: Dict[str, Any] = {
        "schema_version": SCHEMA,
        "root": key,
        "executable": exe,
        "command": f"spack {' '.join(PROBE)}",
        "isolated": isolate,
        "exit": rc,
        # a timeout or a Spack that cannot run is not this check's to judge
        "ok": None if rc is None else rc == 0,
        "error": None if rc == 0 else (error_line(err or out) or f"exit {rc}"),
        "seconds": round(time.monotonic() - started, 3),
    }
    if scopes:
        result["scopes"] = dict(scopes)
    if rc not in (0, None):
        result["output_tail"] = (err or out)[-2000:]
    _PROBES[(key, mode)] = (time.monotonic(), result)
    return dict(result)


def _under(path: str, directory: Optional[Path]) -> bool:
    if directory is None:
        return False
    try:
        Path(path).resolve().relative_to(directory.expanduser().resolve())
    except (ValueError, OSError, RuntimeError):
        return False
    return True


def scope_paths(root: str) -> Dict[str, Path]:
    """The config scope directories of the Spack at ``root`` (as Spack 0.23)."""
    etc = Path(root) / "etc" / "spack"
    user = os.environ.get("SPACK_USER_CONFIG_PATH", "").strip() or "~/.spack"
    system = os.environ.get("SPACK_SYSTEM_CONFIG_PATH", "").strip() or "/etc/spack"
    return {"defaults": etc / "defaults", "site": etc,
            "user": Path(user).expanduser(), "system": Path(system).expanduser()}


def scope_of(path: str, root: str) -> str:
    paths = scope_paths(root)
    for name in ("defaults", "site", "user", "system"):
        if _under(path, paths[name]):
            return name
    return "other"


def config_repos(root: str, executable: Optional[str] = None) -> List[Dict[str, Any]]:
    """``spack config blame repos``: each repo, the file that lists it, its scope."""
    rc, out, _ = _run(_executable(root, executable), ["config", "blame", "repos"],
                      environment(None), timeout=120)
    repos: List[Dict[str, Any]] = []
    if rc != 0:
        return repos
    for line in out.splitlines():
        match = _BLAME.match(line.strip())
        if match is None:
            continue
        repo = match.group("repo").strip().strip("'\"")
        repos.append({"repo": repo, "file": match.group("file"),
                      "line": int(match.group("line")),
                      "scope": scope_of(match.group("file"), root),
                      "namespace": repo_namespace(repo)})
    return repos


def repo_namespace(repo: str) -> Optional[str]:
    try:
        text = (Path(os.path.expandvars(repo)).expanduser() / "repo.yaml").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _NAMESPACE.search(text)
    return match.group(1) if match else None


def dropped_scopes(root: str) -> List[Dict[str, Any]]:
    """What ``SPACK_DISABLE_LOCAL_CONFIG=1`` leaves out: the user and system scopes."""
    dropped = []
    for name in _ISOLATION_DROPS:
        path = scope_paths(root)[name]
        try:
            files = sorted(item.name for item in path.iterdir()
                           if item.suffix in (".yaml", ".yml") and item.is_file())
        except OSError:
            continue
        dropped.append({"scope": name, "path": str(path), "files": files})
    return dropped


def _probe_repo_alone(root: str, executable: Optional[str], repo: str
                      ) -> Tuple[Optional[int], str]:
    """The probe with only ``repo`` added to the Spack's own scopes."""
    scope = tempfile.mkdtemp(prefix="jarvis-spack-probe-")
    try:
        Path(scope, "repos.yaml").write_text(f"repos:\n- {repo}\n", encoding="utf-8")
        env = isolate_environment(environment(None))
        rc, out, err = _run(_executable(root, executable), ["-C", scope, *PROBE], env)
        return rc, error_line(err or out)
    finally:
        shutil.rmtree(scope, ignore_errors=True)


def scope_dir() -> Path:
    """Where JARVIS keeps its scope copies (``JARVIS_SPACK_SCOPE_DIR``, else the site home)."""
    configured = os.environ.get(SCOPE_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    from jarvis_cd.core.method_install import site_home

    return site_home() / "spack-scopes"


def _canon(path: Any, root: str, base: Path) -> str:
    text = str(path).strip().strip("'\"")
    text = text.replace("${spack}", root).replace("$spack", root)
    text = os.path.expanduser(os.path.expandvars(text))
    if not os.path.isabs(text):
        text = str(base / text)
    return os.path.normpath(text)


def _drop_from_repos_yaml(text: str, drop: Sequence[str], root: str, base: Path
                          ) -> Optional[str]:
    """``repos.yaml`` without the repos in ``drop``; None when none of them is listed."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    targets = {_canon(item, root, base) for item in drop}
    removed = False
    for key in [k for k in data if str(k).rstrip(":") == "repos"]:
        value = data[key]
        if isinstance(value, list):
            kept = [item for item in value if _canon(item, root, base) not in targets]
            removed = removed or len(kept) != len(value)
            data[key] = kept
        elif isinstance(value, dict):
            kept_map = {name: item for name, item in value.items()
                        if not (isinstance(item, (str, Path))
                                and _canon(item, root, base) in targets)}
            removed = removed or len(kept_map) != len(value)
            data[key] = kept_map
    return yaml.safe_dump(data, sort_keys=False) if removed else None


def _scope_files(source: Path) -> Dict[str, bytes]:
    """The config files of a scope directory: its YAML, and its platform subdirectories'."""
    files: Dict[str, bytes] = {}
    if not source.is_dir():
        return files
    for item in sorted(source.iterdir()):
        try:
            if item.is_file() and item.suffix in (".yaml", ".yml"):
                files[item.name] = item.read_bytes()
            elif item.is_dir() and item.name not in _SCOPE_SKIP_DIRS:
                for sub in sorted(item.iterdir()):
                    if sub.is_file() and sub.suffix in (".yaml", ".yml"):
                        files[f"{item.name}/{sub.name}"] = sub.read_bytes()
        except OSError:
            continue
    return files


def build_scopes(root: Any, repos: Sequence[Mapping[str, Any]]
                 ) -> Optional[Dict[str, str]]:
    """JARVIS's copies of the scopes listing ``repos``, with those repos removed.

    Returns ``{SPACK_USER_CONFIG_PATH|SPACK_SYSTEM_CONFIG_PATH: dir}``, or
    None when a repo is not in the user or system scope, or a ``repos.yaml``
    that lists it cannot be rewritten. The person's scope is only read.
    Each copy is named by its content, so concurrent jobs share one.
    """
    key = root_key(root)
    if not repos:
        return None
    paths = scope_paths(key)
    by_scope: Dict[str, List[Mapping[str, Any]]] = {}
    for repo in repos:
        if repo.get("scope") not in _SCOPE_ENV or not repo.get("file"):
            return None
        by_scope.setdefault(str(repo["scope"]), []).append(repo)
    result: Dict[str, str] = {}
    for scope, items in sorted(by_scope.items()):
        source = paths[scope]
        files = _scope_files(source)
        for file_path in sorted({str(item["file"]) for item in items}):
            try:
                relative = Path(file_path).resolve().relative_to(source.resolve()).as_posix()
            except (ValueError, OSError, RuntimeError):
                return None
            if relative not in files:
                return None
            drop = [str(item["repo"]) for item in items if str(item["file"]) == file_path]
            text = _drop_from_repos_yaml(files[relative].decode("utf-8", "replace"), drop,
                                         key, Path(file_path).parent)
            if text is None:
                return None
            files[relative] = text.encode("utf-8")
        digest = hashlib.sha256()
        for name, content in sorted(files.items()):
            digest.update(name.encode() + b"\0" + hashlib.sha256(content).digest())
        target = scope_dir() / f"{scope}-{digest.hexdigest()[:16]}"
        if not target.is_dir():
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
            try:
                for name, content in files.items():
                    (staging / name).parent.mkdir(parents=True, exist_ok=True)
                    (staging / name).write_bytes(content)
                (staging / ".jarvis-scope").write_text(
                    f"JARVIS's copy of the {scope} scope {source} for the Spack {key}, "
                    "without: " + ", ".join(str(item["repo"]) for item in items) + "\n",
                    encoding="utf-8")
                try:
                    os.replace(staging, target)
                except OSError:
                    if not target.is_dir():
                        raise
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        result[_SCOPE_ENV[scope]] = str(target)
    return result


def diagnose(root: Any, executable: Optional[str], failure: Mapping[str, Any]
             ) -> Dict[str, Any]:
    """What breaks the Spack: the failing repo and its scope file, when isolable."""
    key = root_key(root)
    cached = _DIAGNOSES.get(key)
    if cached and time.monotonic() - cached[0] < _CACHE_SECONDS:
        return dict(cached[1])
    already_isolated = "SPACK_DISABLE_LOCAL_CONFIG" in os.environ
    repos = config_repos(key, executable)
    without = None if already_isolated else probe(key, executable, isolate=True)
    broken: List[Dict[str, Any]] = []
    probed = 0
    if without is not None and without.get("ok"):
        suspects = [repo for repo in repos if repo["scope"] in (*_ISOLATION_DROPS, "other")]
        for repo in suspects[:_MAX_REPO_PROBES]:
            probed += 1
            rc, error = _probe_repo_alone(key, executable, repo["repo"])
            if rc not in (0, None):
                broken.append({**repo, "error": error})
    files = sorted(set(_YAML_FILE.findall(str(failure.get("output_tail") or ""))))
    without_broken = None
    if broken and without is not None:
        try:
            scopes = build_scopes(key, broken)
        except OSError as exc:
            scopes = None
            without_broken = {"ok": None, "error": f"could not write the scope copy: {exc}"}
        if scopes:
            without_broken = probe(key, executable, scopes=scopes)
    diagnosis = {
        "repos": repos,
        "broken_repos": broken,
        "without_user_config": without,
        "without_failing_repos": without_broken,
        "error_files": files,
        "dropped_scopes": dropped_scopes(key) if without and without.get("ok") else [],
        "repos_probed_alone": probed,
    }
    _DIAGNOSES[key] = (time.monotonic(), diagnosis)
    return dict(diagnosis)


def _cause(root: str, diagnosis: Mapping[str, Any]) -> str:
    broken = diagnosis.get("broken_repos") or []
    without = diagnosis.get("without_user_config")
    parts = []
    if broken:
        parts.append("; ".join(
            f"the repo {item['repo']}"
            + (f" (namespace {item['namespace']})" if item.get("namespace") else "")
            + f", listed in {item['file']}:{item['line']} ({item['scope']} scope), "
              f"fails alone with: {item['error']}"
            for item in broken))
    elif diagnosis.get("error_files"):
        parts.append("Spack names " + ", ".join(diagnosis["error_files"]))
    if without is None:
        parts.append("SPACK_DISABLE_LOCAL_CONFIG is already set, so the cause is in the "
                     f"Spack's own scopes ({root}/etc/spack) or a -C scope")
    elif without.get("ok"):
        parts.append("without the user and system config (SPACK_DISABLE_LOCAL_CONFIG=1) "
                     "the same probe passes")
    else:
        parts.append("it fails even without the user and system config, so the cause is "
                     f"in the Spack's own scopes ({root}/etc/spack): {without.get('error')}")
    return "; ".join(parts)


def _leaves_out(root: str, diagnosis: Mapping[str, Any]) -> Dict[str, Any]:
    dropped = list(diagnosis.get("dropped_scopes") or [])
    repos = [repo for repo in diagnosis.get("repos") or []
             if repo["scope"] in _ISOLATION_DROPS]
    text = (f"Spack reads only {root}/etc/spack/defaults and {root}/etc/spack for these "
            "commands; it leaves out ")
    if dropped:
        text += " and ".join(
            f"the {item['scope']} scope {item['path']}"
            + (f" ({', '.join(item['files'])})" if item["files"] else "")
            for item in dropped)
    else:
        text += "the user scope (~/.spack) and the system scope (/etc/spack)"
    text += (": the repos, compilers, mirrors, package preferences and config settings "
             "listed there are not used by JARVIS's commands for this Spack")
    if repos:
        names = ", ".join(repo.get("namespace") or repo["repo"] for repo in repos)
        text += (f". Repos left out: {names}; installs built from their packages "
                 "cannot be loaded or reused this way")
        failing = {item["repo"] for item in diagnosis.get("broken_repos") or []}
        good = [repo.get("namespace") or repo["repo"] for repo in repos
                if repo["repo"] not in failing]
        if failing and good:
            text += (f", including {', '.join(good)}, which work: only "
                     + ", ".join(item.get("namespace") or item["repo"]
                                 for item in diagnosis.get("broken_repos") or [])
                     + " fails")
    text += (". Your own config is not edited, and your own Spack commands still "
             "read it.")
    return {"text": text, "scopes": dropped, "repos": repos}


def _drops_only(root: str, diagnosis: Mapping[str, Any]) -> Dict[str, Any]:
    broken = list(diagnosis.get("broken_repos") or [])
    scopes = (diagnosis.get("without_failing_repos") or {}).get("scopes") or {}
    kept = [repo for repo in diagnosis.get("repos") or []
            if repo["scope"] in {item["scope"] for item in broken}
            and repo["repo"] not in {item["repo"] for item in broken}]
    text = ("JARVIS's commands for this Spack leave out only "
            + "; ".join(f"the repo {item['repo']}"
                        + (f" (namespace {item['namespace']})" if item.get("namespace") else "")
                        + f", listed in {item['file']}:{item['line']}" for item in broken)
            + ". Everything else in the "
            + " and ".join(sorted({f"{item['scope']} scope" for item in broken}))
            + " is kept: its other config files")
    if kept:
        text += ", and the repos " + ", ".join(repo.get("namespace") or repo["repo"]
                                               for repo in kept)
    text += (". Installs built from the left-out repos' packages cannot be loaded or "
             "reused this way. JARVIS reads your scope and writes its own copy ("
             + ", ".join(f"{var}={path}" for var, path in sorted(scopes.items()))
             + "), rebuilt from your scope each time; your own config is not edited, "
             "and your own Spack commands still read it.")
    return {"text": text, "repos": broken, "kept_repos": kept}


def broken_issue(root: Any, executable: Optional[str], failure: Mapping[str, Any],
                 diagnosis: Mapping[str, Any], *, builds_with: bool = False
                 ) -> Dict[str, Any]:
    """The answerable ``spack_broken`` issue for the Spack at ``root``."""
    from jarvis_cd.core.spack_install import make_issue

    key = root_key(root)
    without = diagnosis.get("without_user_config") or {}
    without_broken = diagnosis.get("without_failing_repos") or {}
    drops = bool(without_broken.get("ok") and without_broken.get("scopes"))
    candidates: List[Dict[str, Any]] = []
    if drops:
        only = _drops_only(key, diagnosis)
        names = ", ".join(item.get("namespace") or item["repo"] for item in only["repos"])
        candidates.append({
            "id": DROP_REPOS, "constraint": DROP_REPOS,
            "label": f"run JARVIS's Spack commands for {key} without the failing repo(s) {names}",
            "does": ("JARVIS points "
                     + " and ".join(sorted(without_broken["scopes"]))
                     + " at its own copy of your scope with only the failing repos removed, "
                     "on its find, load and build commands for this Spack (recorded for "
                     "this site)"),
            "leaves_out": only["text"],
            "leaves_out_repos": [{k: item.get(k) for k in ("repo", "namespace", "file",
                                                          "line", "scope")}
                                 for item in only["repos"]],
            "keeps_repos": [{k: item.get(k) for k in ("repo", "namespace", "file", "line",
                                                     "scope")} for item in only["kept_repos"]],
            "scopes": dict(without_broken["scopes"]),
            "fits": True,
            "why": (f"`spack {' '.join(PROBE)}` passes with JARVIS's scope copy without "
                    f"{names}"),
        })
    if without.get("ok"):
        leaves_out = _leaves_out(key, diagnosis)
        candidates.append({
            "id": NO_USER_CONFIG, "constraint": NO_USER_CONFIG,
            "label": f"run JARVIS's Spack commands for {key} without your user config",
            "does": ("JARVIS sets SPACK_DISABLE_LOCAL_CONFIG=1 on its find, load and build "
                     "commands for this Spack (recorded for this site)"),
            "leaves_out": leaves_out["text"],
            "leaves_out_scopes": leaves_out["scopes"],
            "leaves_out_repos": leaves_out["repos"],
            "fits": True,
            "why": f"`spack {' '.join(PROBE)}` passes with SPACK_DISABLE_LOCAL_CONFIG=1",
        })
    candidates.append({
        "id": SKIP, "constraint": SKIP,
        "label": f"skip the Spack at {key}",
        "does": ("JARVIS offers no site copies from this Spack (the modules, other site "
                 "Spacks and the resource graph are still searched)"
                 + ("; it builds with its own Spack under the site home, which reuses "
                    "this Spack's installs as an upstream" if builds_with else "")
                 + " (recorded for this site)"),
        "fits": True,
        "why": "nothing from a broken Spack is used",
    })
    candidates.append({
        "id": RECHECK, "constraint": RECHECK,
        "label": "I'll fix it myself; check again",
        "does": (f"nothing is recorded: JARVIS runs `spack {' '.join(PROBE)}` again "
                 "now, and asks again while it still fails"),
        "fits": True,
        "why": "the person repairs their own config",
    })
    recommended = DROP_REPOS if drops else NO_USER_CONFIG if without.get("ok") else SKIP
    if recommended == DROP_REPOS:
        reason = ("the failure is confined to the named repos, and without only them the "
                  "Spack works with the rest of your config, its other repos included")
    elif recommended == NO_USER_CONFIG:
        reason = ("the Spack works without the user and system config, and JARVIS then "
                  "never reads the broken part")
    else:
        reason = "the Spack fails in its own scopes, so it cannot be used as it is"
    for candidate in candidates:
        candidate["recommended"] = candidate["id"] == recommended
    rc = failure.get("exit")
    error = str(failure.get("error") or "")
    cause = _cause(key, diagnosis)
    choices = ("Leave out only the failing repos, run JARVIS's Spack commands without "
               "your user config, " if drops else
               "Run JARVIS's Spack commands without your user config, "
               if without.get("ok") else "")
    issue = make_issue(
        kind=KIND, package="*", subject=subject_for(key), scope="site",
        question=(
            f"The Spack at {key} fails on its own, before any package: "
            f"`spack {' '.join(PROBE)}` exits {rc}: {error}. Cause: {cause}. JARVIS "
            "recommends nothing from this Spack and plans nothing with it until this "
            f"is resolved. {choices}"
            + ("skip this Spack" if choices else "Skip this Spack")
            + ", or fix it yourself and check again? Recommended: "
            f"{next(c['label'] for c in candidates if c['id'] == recommended)} ({reason})."
        ),
        candidates=candidates,
        detail=str(failure.get("output_tail") or error)[-2000:],
        evidence=[
            f"{failure.get('executable')} {' '.join(PROBE)} -> exit {rc}",
            f"SPACK_DISABLE_LOCAL_CONFIG=1 {' '.join(PROBE)} -> "
            + (f"exit {without.get('exit')}" if without else "not run (already set)"),
            f"spack config blame repos: {len(diagnosis.get('repos') or [])} repo(s)",
            f"repos probed alone: {diagnosis.get('repos_probed_alone') or 0}",
            *([f"scope copy without the failing repos {' '.join(PROBE)} -> "
               + (f"exit {without_broken.get('exit')}" if without_broken.get("exit")
                  is not None else str(without_broken.get("error")))]
              if without_broken else []),
        ],
        answerable=True,
        hint=("answers.json: {issue_id: '" + "' | '".join(c["constraint"] for c in candidates)
              + "'} or {issue_id: {constraint, answered_by, basis}}; "
              + ", ".join(c["constraint"] for c in candidates if c["constraint"] != RECHECK)
              + " are recorded for this site, recheck probes again"),
        identity_keys=[key, _ADDRESS.sub("0x", error),
                       *sorted(item["repo"] for item in diagnosis.get("broken_repos") or []),
                       *(c["constraint"] for c in candidates)],
    )
    broken = diagnosis.get("broken_repos") or []
    issue.update(
        schema_spack_health=SCHEMA,
        spack_root=key,
        spack_executable=failure.get("executable"),
        probe={key_: failure.get(key_) for key_ in ("command", "exit", "error", "seconds")},
        without_user_config=({key_: without.get(key_) for key_ in ("exit", "ok", "error")}
                             if without else None),
        without_failing_repos=({key_: without_broken.get(key_)
                                for key_ in ("exit", "ok", "error", "scopes")}
                               if without_broken else None),
        failing_repos=[{k: item.get(k) for k in ("repo", "namespace", "file", "line",
                                                 "scope", "error")} for item in broken],
        failing_scope=broken[0]["scope"] if broken else None,
        failing_file=broken[0]["file"] if broken else None,
        cause=cause,
        recommended=recommended,
        recommendation_reason=reason,
        builds_with=builds_with,
    )
    return issue


def gate(root: Any, executable: Optional[str], context, decisions, *,
         builds_with: bool = False) -> Dict[str, Any]:
    """Whether JARVIS may use the Spack at ``root``; raises ``spack_broken`` if not.

    Returns ``{"status": ...}``: ``ok``, ``unknown`` (the probe could not
    run), ``not_probed`` (plan only), ``isolated`` (the answer runs it
    without the user config), ``filtered`` (the answer runs it with
    JARVIS's scope copy without the failing repos), ``skipped`` (the answer
    skips it), or ``broken`` with the open ``issue``.
    """
    from jarvis_cd.core.installer import plan_only_active
    from jarvis_cd.core.spack_install import _utc_now

    key = root_key(root)
    subject = subject_for(key)
    _ISOLATED.discard(key)
    _FILTERED.pop(key, None)
    _SKIPPED.discard(key)
    recorded = decisions.site(subject)
    if recorded is not None and (recorded.get("kind") != KIND or not recorded.get("constraint")):
        recorded = None
    if not enabled() and recorded is None:
        return {"status": "off", "root": key}
    if recorded is not None and recorded["constraint"] == SKIP:
        _SKIPPED.add(key)
        context.note_decision({"issue_id": recorded.get("issue_id"), "kind": KIND,
                               "subject": subject, "scope": "site"}, recorded,
                              source="decisions_file", path=decisions.path)
        return {"status": "skipped", "root": key, "issue_id": recorded.get("issue_id")}
    recorded_scopes = None
    if recorded is not None and recorded["constraint"] == DROP_REPOS:
        try:
            recorded_scopes = build_scopes(key, recorded.get("dropped_repos") or [])
        except OSError as exc:
            context.log(f"[{_utc_now()}] spack health: could not write the scope copy for "
                        f"{key}: {exc}\n")
    if plan_only_active() or not enabled():
        if recorded is not None and recorded["constraint"] == NO_USER_CONFIG:
            _ISOLATED.add(key)
            return {"status": "isolated", "root": key, "issue_id": recorded.get("issue_id")}
        if recorded_scopes and recorded is not None:
            _FILTERED[key] = recorded_scopes
            return {"status": "filtered", "root": key, "issue_id": recorded.get("issue_id"),
                    "scopes": recorded_scopes}
        return {"status": "not_probed", "root": key}
    if recorded_scopes and recorded is not None:
        again = probe(key, executable, scopes=recorded_scopes)
        if again.get("ok") is not False:
            _FILTERED[key] = recorded_scopes
            context.note_decision({"issue_id": recorded.get("issue_id"), "kind": KIND,
                                   "subject": subject, "scope": "site"}, recorded,
                                  source="decisions_file", path=decisions.path)
            return {"status": "filtered", "root": key, "issue_id": recorded.get("issue_id"),
                    "scopes": recorded_scopes}
    if recorded is not None and recorded["constraint"] == NO_USER_CONFIG:
        without = probe(key, executable, isolate=True)
        if without.get("ok") is not False:
            _ISOLATED.add(key)
            context.note_decision({"issue_id": recorded.get("issue_id"), "kind": KIND,
                                   "subject": subject, "scope": "site"}, recorded,
                                  source="decisions_file", path=decisions.path)
            return {"status": "isolated", "root": key, "issue_id": recorded.get("issue_id")}
    result = probe(key, executable)
    if result.get("ok") is None:
        context.log(f"[{_utc_now()}] spack health: {key} not judged ({result.get('error')})\n")
        return {"status": "unknown", "root": key, "probe": result}
    if result["ok"]:
        return {"status": "ok", "root": key, "probe": result}
    issue = broken_issue(key, executable, result, diagnose(key, executable, result),
                         builds_with=builds_with)
    if recorded is not None and recorded.get("issue_id") != issue["issue_id"]:
        stale = {"issue_id": recorded.get("issue_id"), "subject": subject, "scope": "site",
                 "constraint": recorded.get("constraint"), "path": str(decisions.path),
                 "answered_at": recorded.get("answered_at"),
                 "superseded_by": issue["issue_id"],
                 "reason": "the Spack fails differently now, so the question changed"}
        if stale not in context.stale_decisions:
            context.stale_decisions.append(stale)
        issue["supersedes"] = recorded.get("issue_id")
        issue["previous_answer"] = recorded.get("constraint")
    answer = context.answer_for(issue)
    if answer is not None:
        choice = str(answer["constraint"])
        if choice == RECHECK:
            again = probe(key, executable, fresh=True)
            if again.get("ok") is not False:
                _DIAGNOSES.pop(key, None)
                context.log(f"[{_utc_now()}] spack health: {key} checked again and "
                            "passes\n")
                return {"status": "ok", "root": key, "probe": again}
            issue["rechecked_at"] = _utc_now()
            issue["recheck_error"] = again.get("error")
        elif choice == DROP_REPOS and any(
                c["constraint"] == DROP_REPOS for c in issue["candidates"]):
            candidate = next(c for c in issue["candidates"] if c["constraint"] == DROP_REPOS)
            decisions.record(issue, answer, {"spack_root": key,
                                             "dropped_repos": candidate["leaves_out_repos"]})
            _FILTERED[key] = dict(candidate["scopes"])
            context.log(f"[{_utc_now()}] spack_broken answered: {key} runs with JARVIS's "
                        "scope copy without "
                        + ", ".join(item["repo"] for item in candidate["leaves_out_repos"])
                        + f" ({candidate['scopes']})\n")
            return {"status": "filtered", "root": key, "issue_id": issue["issue_id"],
                    "scopes": dict(candidate["scopes"])}
        elif choice == NO_USER_CONFIG and any(
                c["constraint"] == NO_USER_CONFIG for c in issue["candidates"]):
            decisions.record(issue, answer, {"spack_root": key})
            _ISOLATED.add(key)
            context.log(f"[{_utc_now()}] spack_broken answered: {key} runs with "
                        "SPACK_DISABLE_LOCAL_CONFIG=1\n")
            return {"status": "isolated", "root": key, "issue_id": issue["issue_id"]}
        elif choice == SKIP:
            decisions.record(issue, answer, {"spack_root": key})
            _SKIPPED.add(key)
            context.log(f"[{_utc_now()}] spack_broken answered: {key} is skipped\n")
            return {"status": "skipped", "root": key, "issue_id": issue["issue_id"]}
    context.raise_issue(issue)
    return {"status": "broken", "root": key, "issue": issue, "issue_id": issue["issue_id"]}
