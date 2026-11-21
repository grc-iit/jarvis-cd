"""
Installer base class + factory for Jarvis-CD.

An Installer turns the pipeline's packages into an installation plan and
executes it. The factory iterates a pipeline's packages, groups them by
each package's ``install_method`` (pip / conda / spack / container), and
hands each group to the matching Installer.

Installers update ``pipeline.env`` so subsequent ``run()`` calls see the
binaries / libraries the installer produced.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Any, Optional, Type


class Installer(ABC):
    """Base class for installation backends (pip / conda / spack / container)."""

    install_method: str = ""  # Subclasses set this to register with the factory.

    @abstractmethod
    def install(self, ppl, pkg_list: List[Dict[str, Any]]) -> None:
        """Run the installer over ``pkg_list`` (the packages from ``ppl``
        whose ``install_method`` matches this class) and update ``ppl.env``
        with the resulting environment.
        """

    @staticmethod
    def _resolve_install_method(ppl, pkg_def: Dict[str, Any]) -> Optional[str]:
        """Determine the install_method for ``pkg_def``.

        Resolution order:
        1. Explicit ``install_method`` in the package config.
        2. Pipeline ``base_deploy_mode`` — when 'container' or 'spack',
           it doubles as the default install_method so legacy YAMLs that
           only set the pipeline-level mode still install.
        3. None (no installer runs).
        """
        cfg = pkg_def.get('config', {}) or {}
        method = cfg.get('install_method')
        if method:
            return str(method)
        base_mode = getattr(ppl, 'base_deploy_mode', None)
        if base_mode in ('container', 'spack'):
            return base_mode
        return None

    @staticmethod
    def get_installers(ppl) -> Dict[str, List[Dict[str, Any]]]:
        """Group ``ppl.packages`` by install_method.

        :return: ``{install_method: [pkg_def, ...]}`` with stable per-method
                 ordering preserved from ``ppl.packages``. Packages with
                 no install_method are omitted.
        """
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for pkg_def in getattr(ppl, 'packages', []) or []:
            method = Installer._resolve_install_method(ppl, pkg_def)
            if not method:
                continue
            grouped.setdefault(method, []).append(pkg_def)
        return grouped

    @staticmethod
    def _registry() -> Dict[str, Type['Installer']]:
        """Map ``install_method`` string -> concrete Installer subclass.

        Built by walking ``Installer.__subclasses__()`` so adding a new
        backend is just defining a subclass with ``install_method``.
        """
        reg: Dict[str, Type[Installer]] = {}

        def _walk(cls: Type[Installer]):
            for sub in cls.__subclasses__():
                if sub.install_method:
                    reg[sub.install_method] = sub
                _walk(sub)

        _walk(Installer)
        return reg

    @staticmethod
    def for_method(method: str) -> 'Installer':
        """Instantiate the Installer subclass registered for ``method``."""
        reg = Installer._registry()
        if method not in reg:
            raise ValueError(
                f"No Installer registered for install_method '{method}'. "
                f"Known methods: {sorted(reg)}"
            )
        return reg[method]()

    @staticmethod
    def install_all(ppl, mode: str = "install", context=None) -> Dict[str, Any]:
        """Run (or, with ``mode='check'``, only verify) every installer.

        Returns a ``jarvis.install-report.v1`` document. Raises
        ``InstallDecisionNeeded`` (CLI exit 3) when any installer wrote an
        issue: a choice to make, or in check mode a package still missing.
        """
        from jarvis_cd.core.spack_install import (
            REPORT_SCHEMA,
            InstallDecisionNeeded,
        )

        if mode not in ("install", "check"):
            raise ValueError("install mode must be 'install' or 'check'")
        grouped = Installer.get_installers(ppl)
        report: Dict[str, Any] = {
            "schema_version": REPORT_SCHEMA,
            "pipeline_id": getattr(ppl, 'name', None),
            "mode": mode,
            "installers": {},
        }
        owns_context = False
        if context is None and grouped:
            context = install_context_for(ppl, mode)
            context.open()
            owns_context = True
        try:
            for method, pkg_list in grouped.items():
                installer = Installer.for_method(method)
                installer.context = context
                if mode == "check":
                    print(f"Installer '{method}': checking {len(pkg_list)} pkg(s)")
                    result = installer.check(ppl, pkg_list)
                else:
                    print(f"Installer '{method}': installing {len(pkg_list)} pkg(s)")
                    result = installer.install(ppl, pkg_list)
                report["installers"][method] = result or {"status": "done"}
        finally:
            if owns_context:
                context.close()
        if context is not None:
            report["install_dir"] = str(context.install_dir)
            report["log"] = str(context.log_path)
            report["issues_path"] = str(context.issues_path)
            report["issues"] = list(context.issues)
            report["open_issues"] = [issue["issue_id"] for issue in context.issues]
            report["applied_decisions"] = list(context.applied_decisions)
            report["stale_decisions"] = list(context.stale_decisions)
            report["decisions_path"] = str(context.decisions_path)
            if mode == "check":
                try:
                    report["last_install"] = read_last_install(ppl)
                except Exception as exc:  # reporting must never fail a check
                    report["last_install"] = {"error": str(exc)[:512]}
            report["answer_with"] = {
                "format": "answers.json: {issue_id: constraint | "
                          "{constraint, answered_by, ask_id}}",
                "flag": "--answers <answers.json>",
                "env": "JARVIS_INSTALL_ANSWERS",
                "replay": "a recorded answer applies to the same issue_id; MPI "
                          "questions are keyed on each candidate's stable key "
                          "(its prefix), so the same MPIs listed differently keep "
                          "the issue_id. A changed candidate set gets a new "
                          "issue_id; an answer may name a candidate by "
                          "constraint, alias, id or key",
            }
        report["satisfied"] = not (context is not None and context.issues) and all(
            result.get("status") in ("done", "satisfied", "installed", "unchecked")
            for result in report["installers"].values()
        )
        if context is not None and context.issues:
            raise InstallDecisionNeeded(context.issues, context.issues_path, report)
        return report

    #: set by ``install_all`` before ``install``/``check`` run
    context = None

    def _ensure_may_build(self, ppl, what: str, packages=()) -> None:
        """Refuse heavy work on the submitting host (see ``ensure_may_build``)."""
        context = self.context or install_context_for(ppl, "install")
        context.ensure_may_build(what, packages)

    def check(self, ppl, pkg_list: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Verify without building. Backends without a check report 'unchecked'."""
        return {"status": "unchecked", "packages": [p.get('pkg_id') for p in pkg_list]}

    # ------------------------------------------------------------------
    # helpers shared by concrete installers
    # ------------------------------------------------------------------

    @staticmethod
    def _aggregate_queries(pkg_list: List[Dict[str, Any]]) -> List[str]:
        """Pull ``install_query`` (or legacy ``install``) from each pkg_def.
        Empty / missing queries are skipped.
        """
        queries = []
        for pkg_def in pkg_list:
            cfg = pkg_def.get('config', {}) or {}
            query = cfg.get('install_query') or cfg.get('install') or ''
            if query:
                queries.append(str(query).strip())
        return queries


# ---------------------------------------------------------------------------
# Concrete installers
# ---------------------------------------------------------------------------


def _run_local(cmd: str) -> int:
    """Run ``cmd`` via the project's Exec on localhost; return exit code."""
    from jarvis_cd.shell import Exec, LocalExecInfo
    result = Exec(cmd, LocalExecInfo()).run()
    return result.exit_code.get('localhost', 1)


class PipInstaller(Installer):
    """Install packages via ``pip install <install_query ...>``.

    Each package's ``install_query`` is treated as one or more pip
    requirement specs (anything ``pip install`` accepts).
    """

    install_method = "pip"

    def install(self, ppl, pkg_list):
        queries = self._aggregate_queries(pkg_list)
        if not queries:
            print("PipInstaller: no install_query entries; nothing to do")
            return
        spec_str = ' '.join(queries)
        self._ensure_may_build(ppl, "pip install", queries)
        cmd = f"python3 -m pip install {spec_str}"
        print(f"PipInstaller: {cmd}")
        rc = _run_local(cmd)
        if rc != 0:
            raise RuntimeError(
                f"pip install failed (exit {rc}). Specs: {spec_str}"
            )
        # pip writes into the active interpreter; the host environment
        # already covers it. Merge current os.environ so downstream
        # exec_info inherits any updated PATH / VIRTUAL_ENV.
        ppl.env.update(dict(os.environ))


class CondaInstaller(Installer):
    """Install packages via ``conda install -y <install_query ...>``.

    All packages are installed into the currently-active conda env.
    Set ``CONDA_PREFIX`` in the pipeline env to pick a different one.
    """

    install_method = "conda"

    def install(self, ppl, pkg_list):
        queries = self._aggregate_queries(pkg_list)
        if not queries:
            print("CondaInstaller: no install_query entries; nothing to do")
            return
        spec_str = ' '.join(queries)
        self._ensure_may_build(ppl, "conda install", queries)
        cmd = f"conda install -y {spec_str}"
        print(f"CondaInstaller: {cmd}")
        rc = _run_local(cmd)
        if rc != 0:
            raise RuntimeError(
                f"conda install failed (exit {rc}). Specs: {spec_str}"
            )
        ppl.env.update(dict(os.environ))


class SpackInstaller(Installer):
    """Find-first Spack installer.

    Each package's ``install_query`` is resolved with ``spack spec -I``;
    nodes Spack already has (local, upstream or external) are reused and only
    missing ones are built. Choices JARVIS must not make (several installed
    MPIs, variant conflicts) become ``issues.jsonl`` questions. The resolved
    root hashes are recorded and loaded with ``spack load /<hash>``. See
    ``jarvis_cd.core.spack_install``.
    """

    install_method = "spack"

    def _run(self, ppl, pkg_list, mode: str) -> Dict[str, Any]:
        from jarvis_cd.core.environment import EnvironmentManager
        from jarvis_cd.core.spack_install import (
            mpi_environment,
            run_spack_install,
            spack_env_enabled,
        )

        context = self.context
        owns_context = context is None
        if owns_context:
            context = install_context_for(ppl, mode)
            context.open()
        env_dir = spack_env_dir_for(ppl) if spack_env_enabled() else None
        try:
            pkg_list = _with_absolute_repos(ppl, pkg_list)
            record = run_spack_install(pkg_list, context, env_dir=env_dir)
        finally:
            if owns_context:
                context.close()
        statuses = {
            name: entry.get("status") for name, entry in record["packages"].items()
        }
        for name, entry in record["packages"].items():
            status = entry.get("status")
            line = f"SpackInstaller: {name}: {status}"
            if entry.get("root"):
                line += f" /{entry['root']['hash']}"
            if entry.get("missing"):
                line += f" (missing {len(entry['missing'])})"
            print(line)
        failed = [name for name, status in statuses.items() if status == "failed"]
        if failed:
            details = "; ".join(
                f"{name}: {record['packages'][name].get('error', '')}" for name in failed
            )
            raise RuntimeError(
                f"spack install failed for {', '.join(failed)}. {details} "
                f"(log: {context.log_path})"
            )
        hashes = [
            "/" + (entry.get("lock_hash") or entry["root"]["hash"])
            for entry in record["packages"].values()
            if entry.get("status") in ("satisfied", "installed") and entry.get("root")
        ]
        mpi_env: Dict[str, str] = {}
        if hashes and not context.issues:
            env_manager = EnvironmentManager(ppl.jarvis)
            spack_env = env_manager.capture_spack_environment(
                hashes,
                config_scopes=[record["spack_scope"]] if record.get("spack_scope") else (),
            )
            ppl.env.update(spack_env)
            print(f"SpackInstaller: merged {len(spack_env)} env vars from spack load")
            mpi_env = mpi_environment(record)
            for name in ("JARVIS_MPI_NAME", "JARVIS_MPI_SPEC", "JARVIS_MPI_TYPE",
                         "JARVIS_MPI_PREFIX"):
                ppl.env.pop(name, None)
            ppl.env.update(mpi_env)
            prefix = mpi_env.get("JARVIS_MPI_PREFIX")
            if prefix:
                # Externals (site MPIs) are not put on PATH by spack load.
                mpi_bin = os.path.join(prefix, "bin")
                path = [p for p in ppl.env.get("PATH", "").split(os.pathsep) if p]
                if mpi_bin not in path:
                    ppl.env["PATH"] = os.pathsep.join([mpi_bin, *path])
            if mpi_env:
                print(f"SpackInstaller: MPI {mpi_env.get('JARVIS_MPI_SPEC')}"
                      f" ({mpi_env.get('JARVIS_MPI_TYPE', 'probe')})")
        if context.issues:
            overall = "needs_decision"
        elif any(status == "missing" for status in statuses.values()):
            overall = "missing"
        elif any(status == "installed" for status in statuses.values()):
            overall = "installed"
        else:
            overall = "satisfied"
        return {
            "status": overall,
            "record": str(context.record_path),
            "spack_env": record.get("spack_env"),
            "mpi_env": mpi_env or None,
            "packages": {
                name: {
                    key: entry.get(key)
                    for key in ("status", "spec", "root", "lock_hash", "missing",
                                "built", "mpi")
                    if entry.get(key) is not None
                }
                for name, entry in record["packages"].items()
            },
        }

    def install(self, ppl, pkg_list):
        return self._run(ppl, pkg_list, "install")

    def check(self, ppl, pkg_list):
        return self._run(ppl, pkg_list, "check")


def _with_absolute_repos(ppl, pkg_list):
    """Copies of ``pkg_list`` whose ``install_repos`` are absolute paths.

    Relative entries are under the package's own directory (the one holding
    its ``pkg.py``), so a package can ship its Spack recipes.
    """
    from jarvis_cd.core.spack_install import _package_repos, resolve_package_repos

    result = []
    for pkg_def in pkg_list:
        repos = _package_repos(pkg_def)
        if not repos:
            result.append(pkg_def)
            continue
        package_dir = None
        if any(not os.path.isabs(os.path.expanduser(os.path.expandvars(r))) for r in repos):
            instance = ppl._load_package_instance(pkg_def, ppl.env)
            package_dir = Path(instance.pkg_dir) if instance.pkg_dir else None
        copy = dict(pkg_def)
        copy["config"] = dict(pkg_def.get("config") or {})
        copy["config"]["install_repos"] = resolve_package_repos(repos, package_dir)
        result.append(copy)
    return result


def spack_env_dir_for(ppl) -> Path:
    """The pipeline's Spack environment directory.

    ``JARVIS_SPACK_ENV_DIR`` wins when set (check, install and the run's
    load all use it), so several pipelines can share one env, e.g. one per
    set of package digests; writes are serialized by a flock on the dir.
    Otherwise it is ``<pipeline shared>/spack-env``, in the pipeline's
    persistent shared dir (not the execution's), so an install job's
    ``spack.lock`` is what later run jobs load.
    """
    configured = os.environ.get("JARVIS_SPACK_ENV_DIR", "").strip()
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured)))
    return Path(ppl.jarvis.get_pipeline_shared_dir(ppl.name)) / "spack-env"


def default_require_allocation(ppl) -> bool:
    """Whether builds for ``ppl`` must happen inside an allocation.

    True for scheduled pipelines (a ``scheduler:`` block) and on hosts that
    have a scheduler client on PATH (a site login node), so install-on-load
    never builds on the submitting host. A workstation without either is
    unaffected.
    """
    from jarvis_cd.core.spack_install import host_is_cluster_login

    return bool(getattr(ppl, 'scheduler', None)) or host_is_cluster_login()


LAST_INSTALL_NAME = "last-install.json"
LAST_INSTALL_SCHEMA = "jarvis.install-pointer.v1"


def shared_install_dir(ppl) -> Path:
    """``<pipeline shared>/install``, as the submitting host sees it.

    Inside a scheduler job the execution lives at
    ``<pipeline shared>/executions/<id>``, so that path is derived from the
    execution root (the job and the submitting host agree on it).
    """
    execution_root = getattr(ppl, '_execution_root', None)
    if execution_root is not None:
        return Path(execution_root).parent.parent / "install"
    return Path(ppl.jarvis.get_pipeline_shared_dir(ppl.name)) / "install"


def write_last_install(ppl, context, *, execution_id=None, phase: str) -> Optional[Path]:
    """Point ``<shared>/install/last-install.json`` at the record an install wrote.

    An install job writes under ``<execution>/install``; a later check
    (``<shared>/install``) reads the pointer to report what that job did.
    """
    import json
    from jarvis_cd.core.spack_install import _atomic_write_text, _utc_now

    if context is None or context.mode != "install":
        return None
    document = {
        "schema_version": LAST_INSTALL_SCHEMA,
        "pipeline_id": getattr(ppl, 'name', None),
        "execution_id": execution_id,
        "phase": phase,
        "install_dir": str(context.install_dir),
        "record": str(context.record_path),
        "log": str(context.log_path),
        "issues": str(context.issues_path),
        "decisions": str(context.decisions_path),
        "written_at": _utc_now(),
    }
    path = shared_install_dir(ppl) / LAST_INSTALL_NAME
    try:
        _atomic_write_text(path, json.dumps(document, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        context.log(f"WARNING: could not write {path}: {exc}\n")
        return None
    return path


def read_last_install(ppl) -> Optional[Dict[str, Any]]:
    """The last install's pointer plus a summary of the record it wrote.

    Without a pointer (an install from an older jarvis) the newest
    ``<shared>/executions/*/install/install-record.json`` in install mode is
    used. Returns ``None`` when no install has run.
    """
    import json

    shared = shared_install_dir(ppl)
    pointer_path = shared / LAST_INSTALL_NAME
    pointer: Optional[Dict[str, Any]] = None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        pointer["pointer"] = str(pointer_path)
    except (OSError, ValueError):
        pointer = None
    if not isinstance(pointer, dict):
        newest = None
        for record_path in (shared.parent / "executions").glob("*/install/install-record.json"):
            try:
                document = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if document.get("mode") != "install":
                continue
            stamp = str(document.get("finished_at") or document.get("started_at") or "")
            if newest is None or stamp > newest[0]:
                newest = (stamp, record_path)
        if newest is None:
            return None
        record_path = newest[1]
        pointer = {
            "schema_version": LAST_INSTALL_SCHEMA,
            "execution_id": record_path.parent.parent.name,
            "install_dir": str(record_path.parent),
            "record": str(record_path),
            "log": str(record_path.parent / "install.log"),
            "pointer": None,
            "found_by": "scan",
        }
    try:
        record = json.loads(Path(pointer["record"]).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        pointer["record_readable"] = False
        return pointer
    pointer["record_readable"] = True
    pointer["finished_at"] = record.get("finished_at")
    pointer["packages"] = {
        name: {
            key: value
            for key, value in {
                "status": entry.get("status"),
                "spec": entry.get("spec"),
                "mpi": (
                    {k: entry["mpi"].get(k) for k in
                     ("name", "hash", "prefix", "candidate_id", "candidate_key")}
                    if isinstance(entry.get("mpi"), dict) else None
                ),
            }.items()
            if value is not None
        }
        for name, entry in (record.get("packages") or {}).items()
        if isinstance(entry, dict)
    }
    pointer["applied_decisions"] = record.get("applied_decisions") or []
    pointer["open_issues"] = record.get("issues") or []
    if record.get("spack_env"):
        pointer["spack_env"] = record.get("spack_env")
    return pointer


def install_context_for(ppl, mode: str = "install", require_allocation=None):
    """Build the install context (log, issues, record) for ``ppl``.

    Inside a scheduler execution the files live under
    ``<execution>/install``; otherwise under ``<pipeline shared>/install``.
    ``decisions.yaml`` always lives in ``<pipeline shared>/install`` (unless
    ``JARVIS_SPACK_DECISIONS`` is set) so an answer given to an install job
    (e.g. the MPI) is reused by every later run job of the pipeline.
    ``JARVIS_INSTALL_ANSWERS`` names an ``answers.json`` to apply.
    Every install also points ``<pipeline shared>/install/last-install.json``
    at the record it wrote, which a later check reports as ``last_install``.
    ``require_allocation`` (default :func:`default_require_allocation`)
    refuses builds outside a batch job.
    """
    from jarvis_cd.core.spack_install import InstallContext, load_answers

    shared_dir = shared_install_dir(ppl)
    execution_root = getattr(ppl, '_execution_root', None)
    if execution_root is not None:
        install_dir = Path(execution_root) / "install"
    else:
        install_dir = shared_dir
    configured = os.environ.get("JARVIS_SPACK_DECISIONS", "").strip()
    decisions_path = (
        Path(configured) if configured else shared_dir / "decisions.yaml"
    )
    answers_path = os.environ.get("JARVIS_INSTALL_ANSWERS", "").strip()
    return InstallContext(
        install_dir=install_dir,
        mode=mode,
        answers=load_answers(Path(answers_path)) if answers_path else {},
        answers_path=Path(answers_path) if answers_path else None,
        decisions_path=decisions_path,
        progress=getattr(ppl, '_install_progress', None),
        require_allocation=(
            default_require_allocation(ppl)
            if require_allocation is None
            else bool(require_allocation)
        ),
    )


class ContainerInstaller(Installer):
    """Install packages by building (or pulling) one pipeline deploy
    container, then generating the compose / pipeline-yaml artifacts the
    runtime needs.

    Per-package deploy images live under the shared ``containers_dir`` so
    other pipelines pick them up from cache; the merged deploy image lives
    under the per-pipeline name.
    """

    install_method = "container"

    def install(self, ppl, pkg_list):
        # When a prebuilt deploy URI is configured, pull it and we're done.
        if getattr(ppl, 'container_uri', ''):
            if not self._ensure_container_uri_available(ppl):
                raise RuntimeError(
                    f"container_uri '{ppl.container_uri}' is not available "
                    f"locally and could not be pulled with engine "
                    f"'{ppl.container_engine}'."
                )
            ppl.container_image = ppl.container_uri
            print(f"ContainerInstaller: using prebuilt deploy container: "
                  f"{ppl.container_uri}")
        elif not ppl.container_image:
            self._build_pipeline_container(ppl)
            ppl.container_image = ppl.name

        # Save side-effects (container_image was just set) and emit the
        # runtime artifacts (compose file / container-side pipeline yaml).
        ppl.save()
        if ppl.is_containerized():
            print("ContainerInstaller: generating container configuration files")
            ppl._generate_pipeline_container_yaml()
            if ppl.container_engine != 'apptainer':
                ppl._generate_pipeline_compose_file()

    # ------------------------------------------------------------------
    # Image lookup / pull
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_container_uri_available(ppl) -> bool:
        """Make ``ppl.container_uri`` usable as the deploy image.

        Checks for the image locally; if absent, pulls/imports it.
        For apptainer we pull into ``<containers_dir>/<name>.sif`` so
        downstream apptainer exec finds it at the standard path.
        """
        from jarvis_cd.shell import Exec, LocalExecInfo
        from jarvis_cd.core.pkg import Pkg

        if ppl.container_engine == 'apptainer':
            containers_dir = ppl.jarvis.get_containers_dir()
            sif_path = containers_dir / f'{ppl.name}.sif'
            if sif_path.exists():
                return True
            containers_dir.mkdir(parents=True, exist_ok=True)
            print(f"ContainerInstaller: pulling apptainer image "
                  f"{ppl.container_uri} -> {sif_path}")
            pull_cmd = f"apptainer pull {sif_path} {ppl.container_uri}"
            result = Exec(pull_cmd, LocalExecInfo()).run()
            return result.exit_code.get('localhost', 1) == 0

        engine = ppl.container_engine
        if Pkg._image_exists(engine, ppl.container_uri):
            return True
        print(f"ContainerInstaller: pulling container image "
              f"{ppl.container_uri}")
        result = Exec(f"{engine} pull {ppl.container_uri}",
                      LocalExecInfo()).run()
        return result.exit_code.get('localhost', 1) == 0

    # ------------------------------------------------------------------
    # Per-engine build
    # ------------------------------------------------------------------

    def _build_pipeline_container(self, ppl):
        """Build the pipeline's deploy image.

        Flow:
        1. Skip when the deploy image is already cached.
        2. Apptainer: build directly from a ``.def`` (no docker intermediate).
        3. Otherwise: start a build container, run each pkg's build.sh in
           it, commit it, then build per-package deploy images and merge
           them into one pipeline image.
        """
        from jarvis_cd.shell import Exec, LocalExecInfo
        from jarvis_cd.core.pkg import Pkg

        deploy_image_name = ppl.name
        pipeline_shared_dir = ppl.jarvis.get_pipeline_shared_dir(ppl.name)
        pipeline_shared_dir.mkdir(parents=True, exist_ok=True)
        containers_dir = ppl.jarvis.get_containers_dir()
        containers_dir.mkdir(parents=True, exist_ok=True)

        all_cached = all(
            pkg_def.get('config', {}).get('container_cache', True)
            for pkg_def in ppl.packages
        )
        if all_cached and Pkg._image_exists(
                ppl.container_engine, deploy_image_name,
                sif_dir=str(containers_dir)):
            print(f"ContainerInstaller: deploy image '{deploy_image_name}' "
                  "already exists, skipping build")
            return

        self._ensure_may_build(
            ppl, f"build the deploy container '{deploy_image_name}'",
            [pkg_def.get('pkg_id') for pkg_def in ppl.packages])
        if ppl.container_engine == 'apptainer':
            self._build_apptainer_native(
                ppl, deploy_image_name, pipeline_shared_dir, containers_dir)
            return

        build_engine = ppl.container_engine
        build_container_name = f'jarvis-build-{ppl.name}'
        build_image_name = f'jarvis-build-{ppl.name}'
        base_image = ppl.container_base

        print(f"ContainerInstaller: starting build container from {base_image}")
        start_cmd = (
            f"{build_engine} run -d --name {build_container_name} "
            f"--network=host {base_image} sleep infinity"
        )
        result = Exec(start_cmd, LocalExecInfo()).run()
        if result.exit_code.get('localhost', 1) != 0:
            raise RuntimeError(
                f"Failed to start build container '{build_container_name}'"
            )

        try:
            has_build_content = self._run_phase1_builds(
                ppl, build_engine, build_container_name,
                pipeline_shared_dir,
            )
            if has_build_content:
                print(f"ContainerInstaller: committing build container as "
                      f"{build_image_name}")
                commit_cmd = (
                    f"{build_engine} commit {build_container_name} "
                    f"{build_image_name}"
                )
                result = Exec(commit_cmd, LocalExecInfo()).run()
                if result.exit_code.get('localhost', 1) != 0:
                    raise RuntimeError(
                        f"Failed to commit build container as "
                        f"'{build_image_name}'"
                    )
        finally:
            Exec(f"{build_engine} rm -f {build_container_name}",
                 LocalExecInfo(hide_output=True)).run()

        self._run_phase2_deploy(
            ppl, build_engine, build_image_name, deploy_image_name,
            pipeline_shared_dir, containers_dir,
        )

    # ------------------------------------------------------------------
    # Phase 1: per-package build.sh inside a single build container
    # ------------------------------------------------------------------

    def _run_phase1_builds(self, ppl, build_engine, build_container_name,
                           pipeline_shared_dir) -> bool:
        from jarvis_cd.shell import Exec, LocalExecInfo
        from jarvis_cd.core.pkg import Pkg

        has_build_content = False
        for pkg_def in ppl.packages:
            pkg_instance = ppl._load_package_instance(pkg_def, ppl.env)
            pkg_instance.config['deploy_mode'] = pkg_def.get(
                'config', {}).get('deploy_mode', 'default')

            build_result = pkg_instance._build_phase()
            if not build_result:
                continue
            script_content, build_suffix = build_result
            if not script_content:
                continue

            pkg_instance._build_suffix = build_suffix
            pkg_name_raw = pkg_def['pkg_type'].split('.')[-1].replace('_', '-')
            pkg_deploy_name = f'jarvis-deploy-{pkg_name_raw}'
            if build_suffix:
                pkg_deploy_name = f'{pkg_deploy_name}-{build_suffix}'
            use_cache = pkg_def.get('config', {}).get('container_cache', True)

            if use_cache and Pkg._image_exists(build_engine, pkg_deploy_name):
                print(f"ContainerInstaller: injecting cached '{pkg_deploy_name}'"
                      f" into build container")
                temp_name = f'jarvis-inject-{pkg_name_raw}'
                Exec(f"{build_engine} rm -f {temp_name}",
                     LocalExecInfo(hide_output=True)).run()
                result = Exec(
                    f"{build_engine} create --name {temp_name} {pkg_deploy_name}",
                    LocalExecInfo(hide_output=True)).run()
                if result.exit_code.get('localhost', 1) != 0:
                    raise RuntimeError(
                        f"Failed to create inject container from "
                        f"'{pkg_deploy_name}'"
                    )
                for src_path in ['/usr/local', '/opt']:
                    pipe_cmd = (
                        f"set -e; "
                        f"tarfile=$(mktemp /tmp/jarvis-inject-XXXXXX.tar); "
                        f"trap 'rm -f \"$tarfile\"' EXIT; "
                        f"{build_engine} cp {temp_name}:{src_path}/. - "
                        f"  > \"$tarfile\" 2>/dev/null; "
                        f"if [ \"$(tar tf \"$tarfile\" 2>/dev/null "
                        f"          | wc -l)\" -eq 0 ]; then "
                        f"  exit 0; "
                        f"fi; "
                        f"cat \"$tarfile\" | {build_engine} cp - "
                        f"{build_container_name}:{src_path}"
                    )
                    result = Exec(pipe_cmd,
                                  LocalExecInfo(hide_output=True)).run()
                    if result.exit_code.get('localhost', 1) != 0:
                        raise RuntimeError(
                            f"Failed to inject '{pkg_deploy_name}' "
                            f"({src_path}) into build container"
                        )
                Exec(f"{build_engine} rm {temp_name}",
                     LocalExecInfo(hide_output=True)).run()
                Exec(f"{build_engine} exec {build_container_name} ldconfig",
                     LocalExecInfo(hide_output=True)).run()
                has_build_content = True
                continue

            has_build_content = True
            pkg_name = pkg_def['pkg_name']

            script_path = pipeline_shared_dir / f'build-{pkg_name}.sh'
            with open(script_path, 'w') as f:
                f.write(script_content)

            pkg_ctx_dir = f'/tmp/pkg-ctx-{pkg_name}'
            Exec(
                f"{build_engine} exec {build_container_name} "
                f"mkdir -p {pkg_ctx_dir}",
                LocalExecInfo(hide_output=True),
            ).run()
            Exec(
                f"{build_engine} cp {script_path} "
                f"{build_container_name}:{pkg_ctx_dir}/build.sh",
                LocalExecInfo(),
            ).run()
            pkg_dir = (Path(pkg_instance.pkg_dir)
                       if pkg_instance.pkg_dir else None)
            if pkg_dir and pkg_dir.is_dir():
                skip = {
                    'pkg.py', '__pycache__', '__init__.py',
                    'build.sh', 'Dockerfile.deploy',
                    'README.md', 'README.MD',
                    'INSTALL.md', 'INSTALL.MD',
                    'USE.md', 'USE.MD',
                }
                for entry in sorted(pkg_dir.iterdir()):
                    if entry.name in skip:
                        continue
                    if entry.suffix in ('.pyc', '.pyo'):
                        continue
                    Exec(
                        f"{build_engine} cp {entry} "
                        f"{build_container_name}:{pkg_ctx_dir}/",
                        LocalExecInfo(hide_output=True),
                    ).run()

            print(f"ContainerInstaller: building {pkg_name} in container")
            exec_cmd = (
                f"{build_engine} exec -w {pkg_ctx_dir} "
                f"{build_container_name} "
                f"bash {pkg_ctx_dir}/build.sh"
            )
            result = Exec(exec_cmd, LocalExecInfo()).run()
            if result.exit_code.get('localhost', 1) != 0:
                raise RuntimeError(
                    f"Build script failed for '{pkg_name}'. "
                    f"Script: {script_path}"
                )
            print(f"ContainerInstaller: build complete: {pkg_name}")

        return has_build_content

    # ------------------------------------------------------------------
    # Phase 2: per-package deploy Dockerfiles + merged pipeline image
    # ------------------------------------------------------------------

    def _run_phase2_deploy(self, ppl, build_engine, build_image_name,
                           deploy_image_name, pipeline_shared_dir,
                           containers_dir):
        from jarvis_cd.shell import Exec, LocalExecInfo
        from jarvis_cd.core.pkg import Pkg, Library

        per_pkg_deploy_images: List[str] = []

        for pkg_def in ppl.packages:
            pkg_instance = ppl._load_package_instance(pkg_def, ppl.env)
            pkg_instance.config['deploy_mode'] = pkg_def.get(
                'config', {}).get('deploy_mode', 'default')

            build_result = pkg_instance._build_phase()
            build_suffix = (
                build_result[1]
                if build_result and len(build_result) > 1 else ''
            )
            pkg_instance._build_suffix = ''

            deploy_result = pkg_instance._build_deploy_phase()
            if not deploy_result:
                continue
            deploy_content, deploy_suffix = deploy_result
            if not deploy_content:
                continue

            effective_suffix = build_suffix or deploy_suffix

            pkg_name = pkg_def['pkg_type'].split('.')[-1].replace('_', '-')
            pkg_deploy_name = f'jarvis-deploy-{pkg_name}'
            if effective_suffix:
                pkg_deploy_name = f'{pkg_deploy_name}-{effective_suffix}'

            deploy_content = deploy_content.replace(
                pkg_instance.build_image_name(), build_image_name)

            use_cache = pkg_def.get('config', {}).get('container_cache', True)
            if use_cache and Pkg._image_exists(build_engine, pkg_deploy_name):
                print(f"ContainerInstaller: deploy image '{pkg_deploy_name}' "
                      "cached, skipping build")
            else:
                deploy_df_path = (
                    pipeline_shared_dir / f'deploy-{pkg_name}.Dockerfile'
                )
                with open(deploy_df_path, 'w') as f:
                    f.write(deploy_content)
                print(f"ContainerInstaller: building per-package deploy image: "
                      f"{pkg_deploy_name}")
                build_cmd = (
                    f"{build_engine} build --network=host -t {pkg_deploy_name} "
                    f"-f {deploy_df_path} {pipeline_shared_dir}"
                )
                result = Exec(build_cmd, LocalExecInfo()).run()
                if result.exit_code.get('localhost', 1) != 0:
                    raise RuntimeError(
                        f"Failed to build deploy image '{pkg_deploy_name}'"
                    )

            if not isinstance(pkg_instance, Library):
                per_pkg_deploy_images.append(pkg_deploy_name)

        if not per_pkg_deploy_images:
            print("ContainerInstaller: no deploy Dockerfile content from any package")
            return

        # Always build a final image that ends in a fresh `ldconfig`, even
        # for the single-package case. Merely tagging the per-package image
        # is not enough: a package's own Dockerfile.deploy may COPY shared
        # libraries (e.g. libhdf5*.so* for IOR) into /usr/local/lib whose
        # `RUN ldconfig` layer can be served stale from Docker's build cache,
        # leaving the libs present on disk but absent from /etc/ld.so.cache
        # — so the binary aborts at runtime with "cannot open shared object
        # file". Re-running ldconfig here (with --no-cache on this short
        # final stage) guarantees the cache reflects the libraries actually
        # present in the merged image.
        lines = [f"FROM {per_pkg_deploy_images[0]}"]
        for img in per_pkg_deploy_images[1:]:
            lines.append(f"COPY --from={img} /usr/local /usr/local")
            lines.append(
                f"COPY --from={img} /usr/lib/x86_64-linux-gnu "
                f"/usr/lib/x86_64-linux-gnu"
            )
            lines.append(f"COPY --from={img} /opt /opt")
        lines.append("RUN ldconfig")
        lines.append('CMD ["/bin/bash"]')
        deploy_dockerfile = "\n".join(lines)

        deploy_dockerfile_path = pipeline_shared_dir / 'deploy.Dockerfile'
        with open(deploy_dockerfile_path, 'w') as f:
            f.write(deploy_dockerfile)

        print(f"ContainerInstaller: building merged deploy image: "
              f"{deploy_image_name}")
        deploy_cmd = (
            f"{build_engine} build --no-cache --network=host "
            f"-t {deploy_image_name} "
            f"-f {deploy_dockerfile_path} {pipeline_shared_dir}"
        )
        result = Exec(deploy_cmd, LocalExecInfo()).run()
        if result.exit_code.get('localhost', 1) != 0:
            raise RuntimeError(
                f"Failed to build deploy image '{deploy_image_name}'"
            )

        if ppl.container_engine == 'apptainer':
            sif_path = containers_dir / f'{deploy_image_name}.sif'
            print(f"ContainerInstaller: converting to Apptainer SIF: {sif_path}")
            from jarvis_cd.shell.container_compose_exec import ApptainerBuildExec
            ApptainerBuildExec(
                deploy_image_name, str(sif_path),
                LocalExecInfo(), source='docker-daemon'
            ).run()
            print(f"ContainerInstaller: apptainer SIF ready: {sif_path}")
        else:
            print(f"ContainerInstaller: deploy image ready: {deploy_image_name}")

        Exec(f"{build_engine} rmi {build_image_name}",
             LocalExecInfo(hide_output=True)).run()

    # ------------------------------------------------------------------
    # Apptainer-native build (no docker intermediate)
    # ------------------------------------------------------------------

    @staticmethod
    def _dockerfile_deploy_to_post(dockerfile: str):
        """Translate a single-stage view of a Dockerfile.deploy into bash
        for an Apptainer ``%post``. See pipeline.py history for the long
        version of this comment.

        :return: (post_script, env_lines)
        """
        physical = dockerfile.splitlines()
        logical = []
        buf = ''
        for line in physical:
            if line.rstrip().endswith('\\'):
                buf += line.rstrip()[:-1]
                continue
            buf += line
            logical.append(buf)
            buf = ''
        if buf:
            logical.append(buf)

        post_lines = []
        env_lines = []
        DROP = {'FROM', 'COPY', 'CMD', 'ENTRYPOINT', 'EXPOSE', 'LABEL',
                'ARG', 'USER', 'HEALTHCHECK', 'SHELL', 'VOLUME',
                'ONBUILD', 'STOPSIGNAL'}
        for line in logical:
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue
            head, _, rest = stripped.partition(' ')
            head_upper = head.upper()
            if head_upper in DROP:
                continue
            if head_upper == 'RUN':
                post_lines.append(rest)
            elif head_upper == 'WORKDIR':
                post_lines.append(f'mkdir -p {rest} && cd {rest}')
            elif head_upper == 'ENV':
                if '=' in rest:
                    for tok in rest.split():
                        if '=' in tok:
                            k, _, v = tok.partition('=')
                            env_lines.append(f'export {k}={v}')
                else:
                    k, _, v = rest.partition(' ')
                    env_lines.append(f'export {k}={v}')
        return '\n'.join(post_lines), env_lines

    def _build_apptainer_native(self, ppl, deploy_image_name,
                                pipeline_shared_dir, containers_dir):
        from jarvis_cd.shell import Exec, LocalExecInfo

        base_image = ppl.container_base
        def_path = pipeline_shared_dir / f'{deploy_image_name}.def'
        sif_path = containers_dir / f'{deploy_image_name}.sif'

        AUX_SKIP = {
            'pkg.py', '__pycache__', '__init__.py',
            'build.sh', 'Dockerfile.deploy',
            'README.md', 'README.MD',
            'INSTALL.md', 'INSTALL.MD',
            'USE.md', 'USE.MD',
        }
        build_scripts = []
        deploy_runtime = []
        files_lines = []
        cleanup_dirs = []
        env_paths = []
        env_extra = []
        for pkg_def in ppl.packages:
            pkg_instance = ppl._load_package_instance(pkg_def, ppl.env)
            pkg_instance.config['deploy_mode'] = pkg_def.get(
                'config', {}).get('deploy_mode', 'default')
            pkg_name = pkg_def['pkg_name']

            build_result = pkg_instance._build_phase()
            if build_result:
                script_content, _ = build_result
                if script_content:
                    script_path = pipeline_shared_dir / f'build-{pkg_name}.sh'
                    with open(script_path, 'w') as f:
                        f.write(script_content)
                    pkg_ctx_dir = f'/opt/pkg-ctx-{pkg_name}'
                    files_lines.append(
                        f'{script_path} {pkg_ctx_dir}/build.sh')
                    pkg_dir = (Path(pkg_instance.pkg_dir)
                               if pkg_instance.pkg_dir else None)
                    if pkg_dir and pkg_dir.is_dir():
                        for entry in sorted(pkg_dir.iterdir()):
                            if entry.name in AUX_SKIP:
                                continue
                            if entry.suffix in ('.pyc', '.pyo'):
                                continue
                            files_lines.append(
                                f'{entry} {pkg_ctx_dir}/{entry.name}')
                    build_scripts.append(f'# --- Build: {pkg_name} ---')
                    build_scripts.append(f'cd {pkg_ctx_dir}')
                    build_scripts.append(f'bash {pkg_ctx_dir}/build.sh')
                    cleanup_dirs.append(pkg_ctx_dir)

            deploy_result = pkg_instance._build_deploy_phase()
            if deploy_result:
                df_content, _ = deploy_result
                if df_content:
                    runtime_sh, env_lines = self._dockerfile_deploy_to_post(
                        df_content)
                    if runtime_sh.strip():
                        deploy_runtime.append(
                            f'# --- Deploy runtime: {pkg_name} ---')
                        deploy_runtime.append(runtime_sh)
                    env_extra.extend(env_lines)

            pkg_short = pkg_def['pkg_type'].split('.')[-1]
            env_paths.append(f'/opt/{pkg_short}/install/bin')

        if not build_scripts and not deploy_runtime:
            print("ContainerInstaller: no build scripts from any package")
            return

        env_path_str = ':'.join(env_paths + ['$PATH'])
        env_ld_str = ':'.join(
            f'/opt/{p["pkg_type"].split(".")[-1]}/install/lib'
            for p in ppl.packages) + ':$LD_LIBRARY_PATH'

        def_content = f"Bootstrap: docker\nFrom: {base_image}\n\n"
        if files_lines:
            def_content += "%files\n"
            for line in files_lines:
                def_content += f"    {line}\n"
            def_content += "\n"
        def_content += "%post\n"
        def_content += "export DEBIAN_FRONTEND=noninteractive\n"
        for var in ('http_proxy', 'https_proxy', 'HTTP_PROXY',
                    'HTTPS_PROXY', 'no_proxy', 'NO_PROXY'):
            val = os.environ.get(var)
            if val:
                def_content += f"export {var}={val}\n"
        def_content += '\n'.join(build_scripts)
        if deploy_runtime:
            def_content += "\n\n# === Deploy runtime layer ===\n"
            def_content += '\n'.join(deploy_runtime)
        if cleanup_dirs:
            def_content += "\n\n# Drop per-package staging dirs from the SIF.\n"
            for d in cleanup_dirs:
                def_content += f"rm -rf {d}\n"
        def_content += "\n\n%environment\n"
        def_content += f"export PATH={env_path_str}\n"
        def_content += f"export LD_LIBRARY_PATH={env_ld_str}\n"
        for line in env_extra:
            def_content += f"{line}\n"

        with open(def_path, 'w') as f:
            f.write(def_content)

        print(f"ContainerInstaller: building Apptainer SIF from "
              f"definition: {def_path}")
        build_cmd = f"apptainer build --fakeroot {sif_path} {def_path}"
        result = Exec(build_cmd, LocalExecInfo()).run()
        if result.exit_code.get('localhost', 1) != 0:
            raise RuntimeError(
                f"Apptainer build failed. Definition: {def_path}"
            )
        print(f"ContainerInstaller: apptainer SIF ready: {sif_path}")
