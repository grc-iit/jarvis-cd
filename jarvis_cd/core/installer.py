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
    def install_all(ppl) -> None:
        """Run every installer that applies to ``ppl``."""
        grouped = Installer.get_installers(ppl)
        for method, pkg_list in grouped.items():
            installer = Installer.for_method(method)
            print(f"Installer '{method}': installing {len(pkg_list)} pkg(s)")
            installer.install(ppl, pkg_list)

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
        cmd = f"conda install -y {spec_str}"
        print(f"CondaInstaller: {cmd}")
        rc = _run_local(cmd)
        if rc != 0:
            raise RuntimeError(
                f"conda install failed (exit {rc}). Specs: {spec_str}"
            )
        ppl.env.update(dict(os.environ))


class SpackInstaller(Installer):
    """Install packages via ``spack install <spec ...>`` then capture the
    spack-load environment back into ``ppl.env``.
    """

    install_method = "spack"

    def install(self, ppl, pkg_list):
        from jarvis_cd.core.environment import EnvironmentManager

        specs = self._aggregate_queries(pkg_list)
        if not specs:
            print("SpackInstaller: no install_query entries; nothing to do")
            return
        specs_str = ' '.join(specs)

        spack_root = os.environ.get('SPACK_ROOT', '')
        spack_prefix = (
            f'. {spack_root}/share/spack/setup-env.sh && '
            if spack_root else ''
        )

        cmd = f'bash -c "{spack_prefix}spack install {specs_str}"'
        print(f"SpackInstaller: {cmd}")
        rc = _run_local(cmd)
        if rc != 0:
            raise RuntimeError(
                f"spack install failed (exit {rc}). Specs: {specs_str}"
            )

        env_manager = EnvironmentManager(ppl.jarvis)
        spack_env = env_manager.capture_spack_environment(specs)
        ppl.env.update(spack_env)
        print(f"SpackInstaller: merged {len(spack_env)} env vars from spack load")


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
