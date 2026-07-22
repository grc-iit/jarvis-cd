"""
Base package classes for Jarvis-CD.
Provides the consolidated Pkg class and its subclasses for Services, Applications, and Interceptors.
"""

import os
import sys
import time
import inspect
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Any, Callable, List, Mapping, Optional, cast
from jarvis_cd.core.config import Jarvis
from jarvis_cd.util.hostfile import Hostfile

if TYPE_CHECKING:
    from jarvis_cd.deployment import PackageDeploymentContract
    from jarvis_cd.artifacts import (
        ArtifactObservation,
        ArtifactReporter,
        PackageArtifactProvider,
    )
    from jarvis_cd.progress import (
        PackageProgressProvider,
        ProgressObservation,
        ProgressReporter,
    )


class _PackageProgressLineCallback:
    """Serialize one provider stream and finalize it exactly once at EOF."""

    def __init__(
        self,
        provider: Optional["PackageProgressProvider"],
        reporter: "ProgressReporter",
        *,
        progress_path: Path,
        execution_id: str,
        package_name: str,
        package_id: str,
    ) -> None:
        self._provider = provider
        self._reporter = reporter
        self._progress_path = progress_path
        self._expected_identity = (execution_id, package_name, package_id)
        self._lock = threading.Lock()
        self._finalized = False
        self._source: Optional[str] = None

    def __call__(self, stream_name: str, line: str) -> None:
        """Interpret one stdout line and persist validated observations."""
        if stream_name != "stdout":
            return
        with self._lock:
            if self._finalized:
                raise RuntimeError("package progress callback is already finalized")
            from jarvis_cd.progress import (
                PROGRESS_LINE_PREFIX,
                ProgressStore,
                event_from_progress_line,
            )

            if line.strip().startswith(PROGRESS_LINE_PREFIX):
                if self._source == "provider":
                    raise ValueError(
                        "package progress cannot mix provider and structured sources"
                    )
                event = event_from_progress_line(line)
                if event is None:
                    raise ValueError("structured package progress line was empty")
                identity = (
                    event.execution_id,
                    event.package_name,
                    event.package_id,
                )
                if identity != self._expected_identity:
                    raise ValueError("structured package progress identity mismatch")
                ProgressStore(self._progress_path).append(event)
                self._source = "structured_stdout"
                return
            if self._source == "structured_stdout" or self._provider is None:
                return
            observations = self._provider.observe_progress(line)
            if observations:
                self._source = "provider"
                self._persist(observations)

    def finalize(self) -> None:
        """Flush the provider's final fragment once after output capture closes."""
        self.finalize_process(None)

    def finalize_process(self, return_code: int | None) -> None:
        """Finalize using an optional JARVIS-owned process return code."""
        with self._lock:
            if self._finalized:
                return
            if self._source != "structured_stdout" and self._provider is not None:
                from jarvis_cd.progress import ProcessExitProgressProvider

                if return_code is not None and isinstance(
                    self._provider,
                    ProcessExitProgressProvider,
                ):
                    observations = self._provider.finalize_progress_for_exit(
                        return_code
                    )
                else:
                    observations = self._provider.finalize_progress()
                if observations:
                    self._source = "provider"
                    self._persist(observations)
            if return_code is not None and return_code != 0:
                from jarvis_cd.progress import ProgressStore

                ProgressStore(self._progress_path).reconcile_process_exit(return_code)
            self._finalized = True

    def reconcile_process_exit(self, return_code: int) -> None:
        """Correct any terminal success after the effective process failure."""
        if return_code == 0:
            return
        from jarvis_cd.progress import ProgressStore

        with self._lock:
            ProgressStore(self._progress_path).reconcile_process_exit(return_code)

    def _persist(self, observations: list["ProgressObservation"]) -> None:
        """Persist typed observations with JARVIS-owned identity and sequence."""
        for observation in observations:
            self._reporter.emit(
                label=observation.label,
                state=observation.state,
                current=observation.current,
                total=observation.total,
                unit=observation.unit,
                message=observation.message,
                metadata=dict(observation.metadata),
            )


class _PackageArtifactLineCallback:
    """Persist structured or package-interpreted artifact observations."""

    def __init__(
        self,
        provider: Optional["PackageArtifactProvider"],
        reporter: "ArtifactReporter",
        *,
        artifact_path: Path,
        execution_id: str,
        package_name: str,
        package_id: str,
    ) -> None:
        self._provider = provider
        self._reporter = reporter
        self._artifact_path = artifact_path
        self._expected_identity = (execution_id, package_name, package_id)
        self._lock = threading.Lock()
        self._finalized = False
        self._source: Optional[str] = None

    def __call__(self, stream_name: str, line: str) -> None:
        """Interpret one stdout line and persist validated artifacts."""
        if stream_name != "stdout":
            return
        with self._lock:
            if self._finalized:
                raise RuntimeError("package artifact callback is already finalized")
            from jarvis_cd.artifacts import (
                ARTIFACT_LINE_PREFIX,
                ArtifactStore,
                event_from_artifact_line,
            )

            if line.strip().startswith(ARTIFACT_LINE_PREFIX):
                if self._source == "provider":
                    raise ValueError(
                        "package artifacts cannot mix provider and structured sources"
                    )
                event = event_from_artifact_line(line)
                if event is None:
                    raise ValueError("structured package artifact line was empty")
                identity = (
                    event.execution_id,
                    event.package_name,
                    event.package_id,
                )
                if identity != self._expected_identity:
                    raise ValueError("structured package artifact identity mismatch")
                ArtifactStore(self._artifact_path).append(event)
                self._source = "structured_stdout"
                return
            if self._source == "structured_stdout" or self._provider is None:
                return
            observations = self._provider.observe_artifacts(line)
            if observations:
                self._source = "provider"
                self._persist(observations)

    def finalize(self) -> None:
        """Flush a provider's final fragment once after output capture closes."""
        self.finalize_process(None)

    def finalize_process(self, return_code: int | None) -> None:
        """Finalize using an optional JARVIS-owned process return code."""
        with self._lock:
            if self._finalized:
                return
            if self._source != "structured_stdout" and self._provider is not None:
                from jarvis_cd.artifacts import ProcessExitArtifactProvider

                if return_code is not None and isinstance(
                    self._provider,
                    ProcessExitArtifactProvider,
                ):
                    observations = self._provider.finalize_artifacts_for_exit(
                        return_code
                    )
                else:
                    observations = self._provider.finalize_artifacts()
                if observations:
                    self._source = "provider"
                    self._persist(observations)
            if return_code is not None and return_code != 0:
                from jarvis_cd.artifacts import ArtifactStore

                ArtifactStore(self._artifact_path).reconcile_process_exit(return_code)
            self._finalized = True

    def reconcile_process_exit(self, return_code: int) -> None:
        """Correct any terminal success after the effective process failure."""
        if return_code == 0:
            return
        from jarvis_cd.artifacts import ArtifactStore

        with self._lock:
            ArtifactStore(self._artifact_path).reconcile_process_exit(return_code)

    def _persist(self, observations: list["ArtifactObservation"]) -> None:
        """Persist observations with JARVIS-owned execution identity."""
        for observation in observations:
            self._reporter.emit(
                logical_name=observation.logical_name,
                kind=observation.kind,
                role=observation.role,
                structure=observation.structure,
                ownership=observation.ownership,
                state=observation.state,
                artifact_id=observation.artifact_id,
                location=observation.location,
                media_type=observation.media_type,
                format=observation.format,
                size_bytes=observation.size_bytes,
                checksum=observation.checksum,
                message=observation.message,
                metadata=dict(observation.metadata),
            )


class _PackageRuntimeLineCallback:
    """Fan one application stream into progress and artifact semantics."""

    def __init__(self, *callbacks: Callable[[str, str], None]) -> None:
        self._callbacks = callbacks
        self._finalized = False
        self._lock = threading.Lock()

    def __call__(self, stream_name: str, line: str) -> None:
        """Deliver one output line to every configured semantic provider."""
        with self._lock:
            if self._finalized:
                raise RuntimeError("package runtime callback is already finalized")
            for callback in self._callbacks:
                callback(stream_name, line)

    def finalize(self) -> None:
        """Finalize every configured provider exactly once."""
        self.finalize_process(None)

    def finalize_process(self, return_code: int | None) -> None:
        """Finalize providers with an optional owned process return code."""
        with self._lock:
            if self._finalized:
                return
            failures: list[Exception] = []
            for callback in self._callbacks:
                process_finalizer = getattr(callback, "finalize_process", None)
                finalizer = getattr(callback, "finalize", None)
                if callable(process_finalizer) or callable(finalizer):
                    try:
                        if return_code is not None and callable(process_finalizer):
                            cast(Callable[[int], None], process_finalizer)(return_code)
                        elif callable(finalizer):
                            cast(Callable[[], None], finalizer)()
                    except Exception as exc:
                        failures.append(exc)
            self._finalized = True
            if failures:
                raise ExceptionGroup(
                    "package runtime callback finalization failed",
                    failures,
                )

    def reconcile_process_exit(self, return_code: int) -> None:
        """Correct every semantic stream using the effective process failure."""
        if return_code == 0:
            return
        with self._lock:
            failures: list[Exception] = []
            for callback in self._callbacks:
                reconciler = getattr(callback, "reconcile_process_exit", None)
                if not callable(reconciler):
                    continue
                try:
                    cast(Callable[[int], None], reconciler)(return_code)
                except Exception as exc:
                    failures.append(exc)
            if failures:
                raise ExceptionGroup(
                    "package runtime callback reconciliation failed",
                    failures,
                )


class Pkg:
    """
    Consolidated base class for all Jarvis packages.
    Provides common functionality and interface for services, applications, and interceptors.
    """

    @classmethod
    def load_standalone(cls, package_spec: str):
        """
        Load a package instance for standalone operations (not in a pipeline context).
        Creates a minimal standalone pipeline context for the package.

        :param package_spec: Package specification (repo.pkg or just pkg)
        :return: Package instance
        """
        from jarvis_cd.core.config import load_class, Jarvis

        jarvis = Jarvis.get_instance()

        # Parse package specification
        if "." in package_spec:
            # Full specification like "builtin.ior"
            import_parts = package_spec.split(".")
            repo_name = import_parts[0]
            pkg_name = import_parts[1]
        else:
            # Just package name, search in repos
            full_spec = jarvis.find_package(package_spec)
            if not full_spec:
                raise ValueError(f"Package not found: {package_spec}")
            import_parts = full_spec.split(".")
            repo_name = import_parts[0]
            pkg_name = import_parts[1]

        # Determine class name (convert snake_case to PascalCase)
        import re

        class_name = "".join(word.capitalize() for word in re.split(r"[_-]", pkg_name))

        # Load class
        if repo_name == "builtin":
            repo_path = str(jarvis.get_builtin_repo_path())
        else:
            # Find repo path in registered repos
            repo_path = None
            for registered_repo in jarvis.repos["repos"]:
                if Path(registered_repo).name == repo_name:
                    repo_path = registered_repo
                    break

            if not repo_path:
                raise ValueError(f"Repository not found: {repo_name}")

        import_str = f"{repo_name}.{pkg_name}.pkg"
        try:
            pkg_class = load_class(import_str, repo_path, class_name)
        except Exception as e:
            raise ValueError(
                f"Failed to load package '{package_spec}': Error loading class {class_name} from {import_str}: {e}"
            )

        if not pkg_class:
            raise ValueError(f"Package class not found: {class_name} in {import_str}")

        # Create a minimal standalone pipeline object
        class StandalonePipeline:
            def __init__(self):
                self.name = "standalone"

        standalone_pipeline = StandalonePipeline()

        # Create instance with standalone pipeline
        pkg_instance = pkg_class(pipeline=standalone_pipeline)

        # Set basic attributes for standalone use
        pkg_instance.pkg_id = pkg_name
        pkg_instance.global_id = f"standalone.{pkg_name}"

        # Initialize directories now that pkg_id is set
        pkg_instance._ensure_directories()

        return pkg_instance

    def __init__(self, pipeline):
        """
        Initialize package with default values.

        :param pipeline: Parent pipeline instance (REQUIRED)
        """
        self.jarvis = Jarvis.get_instance()
        self.pipeline = pipeline
        self.pkg_dir = None  # Directory containing the package source (pkg.py file)
        self.config_dir = None  # Directory for saving package configuration files
        self.shared_dir = None
        self.private_dir = None
        self.env = {}  # Base environment (everything except LD_PRELOAD)
        self.mod_env = {}  # Modified environment (exact replica of env + LD_PRELOAD)
        self.config = {"interceptors": {}}
        self.pkg_type = None
        self.global_id = None
        self.pkg_id = None

        # Note: Directories will be initialized by Pipeline._load_package_instance
        # after pkg_id is set, or by user code for standalone packages

        # Set pkg_dir to the directory containing this package's source
        self._detect_pkg_dir()

    @property
    def hostfile(self) -> Hostfile:
        """
        Get the effective hostfile for this package.
        Property wrapper around get_hostfile() for convenience.

        :return: Hostfile object
        """
        return self.get_hostfile()

    def get_hostfile(self) -> Hostfile:
        """
        Get the effective hostfile for this package.
        Falls back to pipeline hostfile if package hostfile is not set.

        :return: Hostfile object
        """
        # Check if package has a hostfile configured
        hostfile_path = self.config.get("hostfile", "")
        if hostfile_path:
            return Hostfile(path=hostfile_path)

        # Fall back to pipeline's hostfile
        if hasattr(self.pipeline, "get_hostfile"):
            return self.pipeline.get_hostfile()

        # Fall back to global jarvis hostfile
        return self.jarvis.hostfile

    def resolve_shared_path(
        self,
        value: object,
        *,
        field: str = "path",
        default: str = ".",
    ) -> Path:
        """Resolve a package-owned path against this package's shared root.

        Absolute paths remain operator-selected. Relative paths are scoped to
        ``shared_dir`` so package defaults stay portable across sites and, for
        scheduler runs, resolve inside the immutable execution snapshot rather
        than the process working directory.

        :param value: Configured string or path-like value.
        :param field: Configuration field name used in validation errors.
        :param default: Relative path used when ``value`` is unset or empty.
        :return: A normalized absolute path.
        :raises TypeError: If the configured value is not path-like.
        :raises ValueError: If a relative value escapes the package shared root.
        """
        configured = default if value is None or value == "" else value
        if not isinstance(configured, (str, os.PathLike)):
            raise TypeError(f"{field} must be a path string")
        raw = os.path.expanduser(os.path.expandvars(os.fspath(configured)))
        if not raw or any(ord(character) < 32 for character in raw):
            raise ValueError(f"{field} must be a non-empty printable path")

        path = Path(raw)
        if path.is_absolute():
            return path.resolve(strict=False)

        shared_dir = self.shared_dir
        if not isinstance(shared_dir, (str, os.PathLike)) or not os.fspath(shared_dir):
            raise RuntimeError(
                f"relative {field} requires a JARVIS package shared directory"
            )
        shared_root = Path(shared_dir).expanduser().resolve(strict=False)
        resolved = (shared_root / path).resolve(strict=False)
        if not resolved.is_relative_to(shared_root):
            raise ValueError(f"relative {field} cannot escape package shared directory")
        return resolved

    def get_progress_provider(self) -> Optional["PackageProgressProvider"]:
        """Load this package's optional sibling ``progress.py`` provider.

        The convention works for packages loaded from a registered filesystem
        repository as well as packages bundled in the JARVIS distribution.

        :return: A package progress provider, or ``None`` when not implemented.
        """
        from jarvis_cd.progress import provider_from_package

        return provider_from_package(self)

    def get_artifact_provider(self) -> Optional["PackageArtifactProvider"]:
        """Load this package's optional sibling ``artifacts.py`` provider.

        :return: A package artifact provider, or ``None`` when not implemented.
        """
        from jarvis_cd.artifacts import provider_from_package

        return provider_from_package(self)

    def bind_execution_progress(
        self,
        execution_id: str,
        path: str | os.PathLike[str],
    ) -> None:
        """Bind package reporters to a JARVIS-owned execution sidecar.

        Pipeline execution code must call this after assigning the execution ID
        and before invoking package lifecycle methods. Package configuration is
        deliberately not allowed to select the authoritative sidecar.

        :param execution_id: Stable JARVIS execution identifier.
        :param path: Absolute JSONL path inside the owned execution root.
        """
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise ValueError("execution progress requires a non-empty execution ID")
        progress_path = Path(path)
        if not progress_path.is_absolute():
            raise ValueError("execution progress path must be absolute")
        values = {
            "JARVIS_EXECUTION_ID": execution_id,
            "JARVIS_PROGRESS_PATH": str(progress_path),
            "JARVIS_PACKAGE_NAME": str(self.pkg_type),
            "JARVIS_PACKAGE_ID": str(self.pkg_id),
            "JARVIS_PROGRESS_TRANSPORT": "sidecar",
        }
        self.env.update(values)
        self.mod_env.update(values)

    def bind_execution_artifacts(
        self,
        execution_id: str,
        path: str | os.PathLike[str],
    ) -> None:
        """Bind package artifact reporting to a JARVIS-owned sidecar.

        :param execution_id: Stable JARVIS execution identifier.
        :param path: Absolute JSONL path inside the owned execution root.
        """
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise ValueError("execution artifacts require a non-empty execution ID")
        artifact_path = Path(path)
        if not artifact_path.is_absolute():
            raise ValueError("execution artifact path must be absolute")
        values = {
            "JARVIS_EXECUTION_ID": execution_id,
            "JARVIS_ARTIFACT_PATH": str(artifact_path),
            "JARVIS_PACKAGE_NAME": str(self.pkg_type),
            "JARVIS_PACKAGE_ID": str(self.pkg_id),
            "JARVIS_ARTIFACT_TRANSPORT": "sidecar",
        }
        self.env.update(values)
        self.mod_env.update(values)

    def bind_execution_service_runtime(
        self,
        execution_id: str,
        path: str | os.PathLike[str],
    ) -> None:
        """Bind service reporting to a JARVIS-owned runtime sidecar.

        :param execution_id: Stable JARVIS execution identifier.
        :param path: Absolute JSONL path inside the owned execution root.
        """
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise ValueError(
                "execution service runtime requires a non-empty execution ID"
            )
        runtime_path = Path(path)
        if not runtime_path.is_absolute():
            raise ValueError("execution service runtime path must be absolute")
        values = {
            "JARVIS_EXECUTION_ID": execution_id,
            "JARVIS_SERVICE_RUNTIME_PATH": str(runtime_path),
            "JARVIS_PACKAGE_NAME": str(self.pkg_type),
            "JARVIS_PACKAGE_ID": str(self.pkg_id),
        }
        self.env.update(values)
        self.mod_env.update(values)

    def _progress_line_callback(self) -> Optional[Callable[[str, str], None]]:
        """Build the progress half of the package runtime callback.

        Package-local providers interpret application output. Already
        structured ``JARVIS_PROGRESS`` lines are accepted without a provider.
        This helper owns event identity, sequencing, validation, persistence,
        and EOF finalization.

        :return: A line callback bound to this execution, or ``None``.
        """
        execution_id = self.mod_env.get("JARVIS_EXECUTION_ID")
        progress_path = self.mod_env.get("JARVIS_PROGRESS_PATH")
        package_name = self.mod_env.get("JARVIS_PACKAGE_NAME")
        package_id = self.mod_env.get("JARVIS_PACKAGE_ID")
        transport = self.mod_env.get("JARVIS_PROGRESS_TRANSPORT", "sidecar")
        if not all(
            isinstance(value, str) and value
            for value in (execution_id, progress_path, package_name, package_id)
        ):
            return None
        if transport not in {"sidecar", "stdout"}:
            raise ValueError("package progress transport must be sidecar or stdout")
        provider = self.get_progress_provider()
        if provider is None and transport != "stdout":
            return None

        from jarvis_cd.progress import ProgressReporter

        reporter = ProgressReporter(
            package_name=cast(str, package_name),
            package_id=cast(str, package_id),
            execution_id=cast(str, execution_id),
            path=cast(str, progress_path),
        )

        return _PackageProgressLineCallback(
            provider,
            reporter,
            progress_path=Path(cast(str, progress_path)),
            execution_id=cast(str, execution_id),
            package_name=cast(str, package_name),
            package_id=cast(str, package_id),
        )

    def _artifact_line_callback(self) -> Optional[Callable[[str, str], None]]:
        """Build the artifact half of the package runtime callback."""
        execution_id = self.mod_env.get("JARVIS_EXECUTION_ID")
        artifact_path = self.mod_env.get("JARVIS_ARTIFACT_PATH")
        package_name = self.mod_env.get("JARVIS_PACKAGE_NAME")
        package_id = self.mod_env.get("JARVIS_PACKAGE_ID")
        transport = self.mod_env.get("JARVIS_ARTIFACT_TRANSPORT", "sidecar")
        if not all(
            isinstance(value, str) and value
            for value in (execution_id, artifact_path, package_name, package_id)
        ):
            return None
        if transport not in {"sidecar", "stdout"}:
            raise ValueError("package artifact transport must be sidecar or stdout")
        provider = self.get_artifact_provider()
        if provider is None and transport != "stdout":
            return None

        from jarvis_cd.artifacts import ArtifactReporter

        reporter = ArtifactReporter(
            package_name=cast(str, package_name),
            package_id=cast(str, package_id),
            execution_id=cast(str, execution_id),
            path=cast(str, artifact_path),
        )
        return _PackageArtifactLineCallback(
            provider,
            reporter,
            artifact_path=Path(cast(str, artifact_path)),
            execution_id=cast(str, execution_id),
            package_name=cast(str, package_name),
            package_id=cast(str, package_id),
        )

    def runtime_line_callback(self) -> Optional[Callable[[str, str], None]]:
        """Build one callback for generic progress and artifact semantics."""
        callbacks = tuple(
            callback
            for callback in (
                self._progress_line_callback(),
                self._artifact_line_callback(),
            )
            if callback is not None
        )
        if not callbacks:
            return None
        return _PackageRuntimeLineCallback(*callbacks)

    def progress_line_callback(self) -> Optional[Callable[[str, str], None]]:
        """Return the combined runtime callback under the legacy method name."""
        return self.runtime_line_callback()

    def _init(self):
        """
        Override this method to initialize package-specific variables.
        Don't assume that self.config is initialized.
        This provides an overview of the parameters of this class.
        Default values should almost always be None.
        """
        pass

    def _configure_menu(self) -> List[Dict[str, Any]]:
        """
        Override this method to define configuration options.

        :return: List of configuration option dictionaries
        """
        return []

    def _deployment_environment(self) -> dict[str, str]:
        """Return the effective environment used by package readiness probes.

        The mapping is never included in the public deployment document.  It
        merely lets a package test the same activated PATH and provider state
        that its launch will inherit.
        """
        environment = dict(os.environ)
        for values in (self.env, self.mod_env):
            if not isinstance(values, Mapping):
                continue
            environment.update(
                {
                    key: value
                    for key, value in values.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
            )
        return environment

    def _deployment_contract(self) -> Optional["PackageDeploymentContract"]:
        """Return package-owned deployment metadata when implemented.

        Legacy packages deliberately return ``None``.  This keeps deployment
        semantics opt-in instead of inferring them from class names, source
        paths, or application-specific prompts.
        """
        return None

    def deployment_contract(self) -> Optional["PackageDeploymentContract"]:
        """Return and type-check this package's versioned deployment contract."""
        from jarvis_cd.deployment import PackageDeploymentContract

        contract = self._deployment_contract()
        if contract is not None and not isinstance(contract, PackageDeploymentContract):
            raise TypeError(
                "package deployment contract must be a PackageDeploymentContract"
            )
        return contract

    def describe_deployment(self) -> Optional[Dict[str, Any]]:
        """Serialize package deployment/readiness metadata for generic clients.

        :return: A ``jarvis.package-deployment.v1`` document, or ``None`` for a
            package that has not declared the contract.
        """
        contract = self.deployment_contract()
        return None if contract is None else contract.to_dict()

    def _configure(self, **kwargs):
        """
        Override this method to handle package configuration.
        Takes as input a dictionary with keys determined from _configure_menu.
        Updates self.config and generates application-specific configuration files.

        :param kwargs: Configuration parameters
        """
        self.update_config(kwargs, rebuild=False)

    def configure_menu(self):
        """
        Get the complete configuration menu including common parameters.
        Returns the menu in argument dictionary format so parameters can be set from command line.

        :return: List of configuration option dictionaries
        """
        # Get package-specific menu
        package_menu = self._configure_menu()

        # Add common parameters that all packages should have
        common_menu = [
            {
                "name": "install_method",
                "msg": "Installer to use for this package "
                "('pip', 'conda', 'spack', 'container'). Empty "
                "string defers to the pipeline's base_deploy_mode.",
                "type": str,
                "default": "",
            },
            {
                "name": "install_query",
                "msg": "Package spec consumed by the installer (e.g. spack "
                "spec, pip requirement, conda package name).",
                "type": str,
                "default": "",
            },
            {
                "name": "install",
                "msg": "Deprecated alias for install_query. Prefer install_query.",
                "type": str,
                "default": "",
            },
            {
                "name": "container_cache",
                "msg": "Use Docker layer cache when building containers (set false to force rebuild)",
                "type": bool,
                "default": True,
            },
            {
                "name": "interceptors",
                "msg": "List of interceptor package names to apply",
                "type": list,
                "default": [],
                "args": [
                    {
                        "name": "interceptor_name",
                        "msg": "Name of an interceptor package",
                        "type": str,
                    }
                ],
            },
            {
                "name": "sleep",
                "msg": "Sleep time in seconds",
                "type": int,
                "default": 0,
            },
            {
                "name": "do_dbg",
                "msg": "Enable debug mode",
                "type": bool,
                "default": False,
            },
            {
                "name": "dbg_port",
                "msg": "Debug port number",
                "type": int,
                "default": 1234,
            },
            {
                "name": "timeout",
                "msg": "Operation timeout in seconds",
                "type": int,
                "default": 300,
            },
            {
                "name": "retry_count",
                "msg": "Number of retry attempts",
                "type": int,
                "default": 3,
            },
            {
                "name": "hide_output",
                "msg": "Hide command output",
                "type": bool,
                "default": False,
            },
            {
                "name": "hostfile",
                "msg": "Path to hostfile (empty string means use pipeline hostfile)",
                "type": str,
                "default": "",
            },
        ]

        # These controls remain part of the CLI and persisted package config,
        # but generic agents should reason from the package-owned deployment
        # contract instead of choosing installers, paths, or debug machinery.
        for parameter in common_menu:
            parameter["agent_visible"] = False

        # Combine package-specific and common menus
        return package_menu + common_menu

    def configuration_input_materialization_matches(
        self,
        parameter: str,
        requested: object,
        materialized: object,
    ) -> bool:
        """Verify one package-declared durable configuration-input rewrite."""
        from jarvis_cd.configuration_input import (
            configuration_input_materialization_matches,
        )

        if self.shared_dir is None:
            return False
        return configuration_input_materialization_matches(
            menu=self.configure_menu(),
            parameter=parameter,
            requested=requested,
            materialized=materialized,
            shared_dir=self.shared_dir,
        )

    def get_argparse(self):
        """
        Get PkgArgParse instance for this package.
        Used to display configuration help.

        :return: PkgArgParse instance
        """
        from jarvis_cd.util import PkgArgParse

        pkg_name = getattr(self, "pkg_id", None) or self.__class__.__name__
        return PkgArgParse(pkg_name, self.configure_menu())

    def configure(self, **kwargs):
        """
        Public configuration method that calls internal _configure.

        :param kwargs: Configuration parameters
        :return: Configuration dictionary
        """
        # Ensure package directories are set
        self._ensure_directories()

        # Apply menu defaults first
        self._apply_menu_defaults()

        # Update configuration with provided parameters
        self.update_config(kwargs, rebuild=False)

        # Print hostfile being used
        hostfile = self.get_hostfile()
        if hostfile and hostfile.path:
            print(f"Package {self.pkg_id} using hostfile: {hostfile.path}")
        else:
            print(f"Package {self.pkg_id} using default hostfile (no path set)")

        # Call the internal configuration method
        self._configure(**kwargs)

        # Snapshot only inputs explicitly declared by the package. This runs
        # after package validation/configuration so persisted pipeline state no
        # longer depends on a caller- or transport-owned staging pathname.
        from jarvis_cd.configuration_input import materialize_configuration_inputs

        if self.shared_dir is None:
            raise RuntimeError("package configuration requires a shared directory")
        self.config = materialize_configuration_inputs(
            menu=self.configure_menu(),
            config=self.config,
            shared_dir=self.shared_dir,
        )

        return self.config.copy()

    def _get_delegate(self, deploy_mode: str):
        """
        Get or create the delegate implementation based on deploy mode.

        This method provides a unified delegation pattern for packages that have
        multiple implementations (e.g., default vs containerized).

        The method attempts to import a module relative to the package and instantiate
        a class named {BaseClassName}{DeployMode}. For example:
        - Ior class with deploy_mode='container' -> from .container import IorContainer
        - Ior class with deploy_mode='default' -> from .default import IorDefault

        :param deploy_mode: Deployment mode (e.g., 'default', 'container', 'docker')
        :return: Delegate instance
        """
        # Check if we already have a delegate for this mode
        delegate_key = f"_delegate_{deploy_mode}"
        if hasattr(self, delegate_key) and getattr(self, delegate_key) is not None:
            return getattr(self, delegate_key)

        # Get base class name (e.g., 'Ior' from 'Ior' class)
        base_class_name = self.__class__.__name__

        # Build delegate class name: {BaseClassName}{DeployModeCapitalized}
        # e.g., 'Ior' + 'Container' = 'IorContainer'
        deploy_mode_capitalized = "".join(
            word.capitalize() for word in deploy_mode.split("_")
        )
        delegate_class_name = f"{base_class_name}{deploy_mode_capitalized}"

        # Import the module dynamically relative to current package
        # e.g., if we're in builtin.ior.pkg, import builtin.ior.{deploy_mode}
        import importlib

        current_module = self.__class__.__module__  # e.g., 'builtin.ior.pkg'
        package_path = current_module.rsplit(".", 1)[0]  # e.g., 'builtin.ior'
        module_path = f"{package_path}.{deploy_mode}"

        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            raise ImportError(
                f"Failed to import deployment module '{module_path}' for deploy mode '{deploy_mode}'. "
                f"Expected file: {deploy_mode}.py in the same directory as {self.__class__.__name__}. "
                f"Error: {e}"
            )

        # Get the delegate class from the module
        try:
            delegate_class = getattr(module, delegate_class_name)
        except AttributeError:
            raise AttributeError(
                f"Module '{module_path}' does not contain class '{delegate_class_name}'. "
                f"Expected class name: {delegate_class_name}"
            )

        # Create delegate instance
        delegate = delegate_class.__new__(delegate_class)

        # Initialize the delegate with base class
        Pkg.__init__(delegate, pipeline=self.pipeline)

        # Copy our state to the delegate
        delegate.pkg_type = self.pkg_type
        delegate.pkg_id = self.pkg_id
        delegate.global_id = self.global_id
        delegate.config = self.config
        delegate.env = self.env
        delegate.mod_env = self.mod_env
        delegate._ensure_directories()

        # Cache the delegate
        setattr(self, delegate_key, delegate)

        return delegate

    def _ensure_directories(self):
        """
        Ensure package directories are set based on pipeline context.
        This method is called during __init__ so directories are always available.

        Directory structure:
        - config_dir: pipelines/pipeline_name/packages/pkg_id
        - shared_dir: pipeline_name/pkg_id
        - private_dir: pipeline_name/pkg_id
        """
        if not self.config_dir or not self.shared_dir or not self.private_dir:
            pkg_id = getattr(self, "pkg_id", None) or self.__class__.__name__.lower()

            # Get directories from pipeline
            getters = [
                getattr(self.pipeline, method, None)
                for method in (
                    "get_pipeline_config_dir",
                    "get_pipeline_shared_dir",
                    "get_pipeline_private_dir",
                )
            ]
            scoped_roots = (
                [getter() for getter in getters]
                if all(callable(getter) for getter in getters)
                else []
            )
            if len(scoped_roots) == 3 and all(
                isinstance(root, (str, os.PathLike)) for root in scoped_roots
            ):
                pipeline_config_dir, pipeline_shared_dir, pipeline_private_dir = (
                    Path(root) for root in scoped_roots
                )
            else:
                pipeline_config_dir = self.jarvis.get_pipeline_dir(self.pipeline.name)
                pipeline_shared_dir = self.jarvis.get_pipeline_shared_dir(
                    self.pipeline.name
                )
                pipeline_private_dir = self.jarvis.get_pipeline_private_dir(
                    self.pipeline.name
                )

            if not self.config_dir:
                self.config_dir = str(pipeline_config_dir / "packages" / pkg_id)
            if not self.shared_dir:
                self.shared_dir = str(pipeline_shared_dir / pkg_id)
            if not self.private_dir:
                self.private_dir = str(pipeline_private_dir / pkg_id)

            # Create directories if they don't exist
            for dir_path in [self.config_dir, self.shared_dir, self.private_dir]:
                if dir_path and not Path(dir_path).exists():
                    Path(dir_path).mkdir(parents=True, exist_ok=True)

            # Call user-defined initialization
            self._init()

    def _detect_pkg_dir(self):
        """
        Detect the directory containing this package's source code (where pkg.py is located).
        """
        try:
            # Get the file path of the class definition
            class_file = inspect.getfile(self.__class__)
            # Get the directory containing the package file
            self.pkg_dir = str(Path(class_file).parent)
        except Exception:
            # Fallback: leave pkg_dir as None if detection fails
            pass

    def _apply_menu_defaults(self):
        """
        Apply default values from the configuration menu to ensure all parameters have values.
        """
        menu = self.configure_menu()
        for item in menu:
            param_name = item.get("name")
            default_value = item.get("default")
            if (
                param_name
                and param_name not in self.config
                and default_value is not None
            ):
                self.config[param_name] = default_value

    def update_config(self, new_config: Dict[str, Any], rebuild: bool = True):
        """
        Update package configuration.

        :param new_config: New configuration values
        :param rebuild: Whether to rebuild configuration files
        """
        self.config.update(new_config)

        if rebuild and hasattr(self, "_configure"):
            self._configure(**self.config)

    def start(self):
        """
        Start the package.
        Called during pipeline run and start operations.
        Override this method in package implementations.
        """
        pass

    def stop(self):
        """
        Stop the package.
        Called during pipeline stop operations.
        Override this method in package implementations.
        """
        pass

    def kill(self):
        """
        Kill the package.
        Called during pipeline kill operations.
        Override this method in package implementations.
        """
        pass

    def clean(self):
        """
        Clean package data.
        Called during pipeline clean operations.
        Destroys all data for the package.
        Override this method in package implementations.
        """
        pass

    def status(self) -> str:
        """
        Override this method to return package status.
        Called during pipeline status operations.

        :return: Status string
        """
        return "unknown"

    def track_env(self, env_track_dict: Dict[str, str]):
        """
        Track environment variables.

        :param env_track_dict: Dictionary of environment variables to track
        """
        # Add to env (but not LD_PRELOAD)
        for key, value in env_track_dict.items():
            if key != "LD_PRELOAD":
                self.env[key] = value

        # mod_env is exact replica of env plus LD_PRELOAD
        self.mod_env = self.env.copy()
        if "LD_PRELOAD" in env_track_dict:
            self.mod_env["LD_PRELOAD"] = env_track_dict["LD_PRELOAD"]

    def prepend_env(self, env_name: str, val: str):
        """
        Prepend a value to an environment variable.

        :param env_name: Environment variable name
        :param val: Value to prepend
        """
        # For LD_PRELOAD, only update mod_env
        if env_name == "LD_PRELOAD":
            current_val = self.mod_env.get(env_name, "")
            if current_val:
                self.mod_env[env_name] = f"{val}:{current_val}"
            else:
                self.mod_env[env_name] = val
        else:
            # For other variables, update env
            current_val = self.env.get(env_name, "")
            if current_val:
                self.env[env_name] = f"{val}:{current_val}"
            else:
                self.env[env_name] = val

            # Keep mod_env in sync (exact replica of env + LD_PRELOAD)
            self.mod_env[env_name] = self.env[env_name]

    def setenv(self, env_name: str, val: str):
        """
        Set an environment variable.

        :param env_name: Environment variable name
        :param val: Value to set
        """
        # For LD_PRELOAD, only update mod_env
        if env_name == "LD_PRELOAD":
            self.mod_env[env_name] = val
        else:
            # For other variables, update env
            self.env[env_name] = val

            # Keep mod_env in sync (exact replica of env + LD_PRELOAD)
            self.mod_env[env_name] = val

    def find_library(self, library_name: str) -> Optional[str]:
        """
        Find a shared library by searching LD_LIBRARY_PATH and system paths.

        :param library_name: Name of the library to find
        :return: Path to library if found, None otherwise
        """
        import shutil

        # Generate possible library filenames
        lib_filenames = [
            f"lib{library_name}.so",  # Standard shared library
            f"{library_name}.so",  # Library name as-is with .so
            f"lib{library_name}.a",  # Static library
            library_name,  # Exact name as provided
        ]

        # Collect all library search paths in priority order
        search_paths = []

        # 1. Package-specific environment (mod_env takes precedence over env)
        mod_ld_path = self.mod_env.get("LD_LIBRARY_PATH")
        if mod_ld_path:
            search_paths.extend(mod_ld_path.split(os.pathsep))

        env_ld_path = self.env.get("LD_LIBRARY_PATH")
        if env_ld_path:
            search_paths.extend(env_ld_path.split(os.pathsep))

        # 2. System LD_LIBRARY_PATH
        system_ld_path = os.environ.get("LD_LIBRARY_PATH")
        if system_ld_path:
            search_paths.extend(system_ld_path.split(os.pathsep))

        # 3. Common system library directories
        search_paths.extend(
            [
                "/usr/lib",
                "/usr/local/lib",
                "/usr/lib64",
                "/usr/local/lib64",
                "/lib",
                "/lib64",
            ]
        )

        # Search for the library in all paths
        for search_path in search_paths:
            if not search_path:  # Skip empty paths
                continue

            search_dir = Path(search_path)
            if not search_dir.exists():
                continue

            for lib_filename in lib_filenames:
                lib_path = search_dir / lib_filename
                print(lib_path)
                if lib_path.exists():
                    return str(lib_path)

        # Fallback: try using shutil.which for executable-style lookup
        for lib_filename in lib_filenames:
            lib_path = shutil.which(lib_filename)
            if lib_path:
                return lib_path

        return None

    def log(self, message, color=None):
        """
        Log a message with package context and optional color.

        :param message: Message to log
        :param color: Color to use (from jarvis_cd.util.logger.Color enum), defaults to YELLOW for info messages
        """
        from jarvis_cd.util.logger import logger

        formatted_message = f"[{self.__class__.__name__}] {message}"

        if color is not None:
            logger.print(color, formatted_message)
        else:
            # Default to yellow for info messages
            logger.warning(formatted_message)

    def sleep(self, time_sec=None):
        """
        Sleep for a specified amount of time.

        :param time_sec: Time to sleep in seconds. If not provided, uses self.config['sleep']
        """
        if time_sec is None:
            time_sec = self.config.get("sleep", 0)

        self.log(f"Sleeping for {time_sec} seconds")
        if time_sec > 0:
            time.sleep(time_sec)

    def copy_template_file(self, source_path, dest_path, replacements=None):
        """
        Copy a template file from source to destination, replacing template constants.

        Template constants have the format ##CONSTANT_NAME## and are replaced with
        values from the replacements dictionary.

        :param source_path: Path to the source template file
        :param dest_path: Path where the processed file should be saved
        :param replacements: Dictionary of replacements {CONSTANT_NAME: value}

        Example:
            self.copy_template_file(f'{self.pkg_dir}/config/hermes.xml',
                                   self.adios2_xml_path,
                                   replacements={'PPN': 1})
        """
        try:
            if replacements is None:
                replacements = {}

            # Read the template file
            with open(source_path, "r") as f:
                content = f.read()

            # Replace template constants
            for key, value in replacements.items():
                template_token = f"##{key}##"
                content = content.replace(template_token, str(value))

            # Ensure destination directory exists
            dest_dir = Path(dest_path).parent
            dest_dir.mkdir(parents=True, exist_ok=True)

            # Write the processed content to destination
            with open(dest_path, "w") as f:
                f.write(content)

            self.log(
                f"Copied template file {source_path} -> {dest_path} with {len(replacements)} replacements"
            )

        except FileNotFoundError:
            self.log(f"Error: Template file not found: {source_path}")
            raise
        except Exception as e:
            self.log(f"Error copying template file {source_path} -> {dest_path}: {e}")
            raise

    # ------------------------------------------------------------------
    # Container support — properties
    # ------------------------------------------------------------------

    def build_image_name(self, suffix=None) -> str:
        """
        Stable name for the BUILD container image.

        :param suffix: Image suffix identifying this configuration.
                       When None, uses self._build_suffix (set by build_phase).
        :return: e.g. 'jarvis-build-lammps-kokkos-a100'
        """
        pkg_name = self.pkg_type.split(".")[-1].replace("_", "-")
        s = suffix if suffix is not None else getattr(self, "_build_suffix", "")
        if s:
            return f"jarvis-build-{pkg_name}-{s}"
        return f"jarvis-build-{pkg_name}"

    def deploy_image_name(self, suffix=None) -> str:
        """
        Name for the DEPLOY container image (pipeline-specific).

        :param suffix: Image suffix identifying this configuration.
                       When None, uses self._deploy_suffix (set by build_deploy_phase).
        """
        pipeline = getattr(self, "pipeline", None)
        base = (
            getattr(
                pipeline,
                "execution_container_name",
                getattr(pipeline, "name", "jarvis-deploy"),
            )
            if pipeline
            else "jarvis-deploy"
        )
        s = suffix if suffix is not None else getattr(self, "_deploy_suffix", "")
        if s:
            return f"{base}-{s}"
        return base

    @property
    def container_mounts(self) -> list:
        """
        Bind-mount list for container runs: shared_dir and private_dir mapped
        at the same path inside the container so config files are accessible.
        """
        mounts = []
        if self.shared_dir:
            mounts.append(f"{self.shared_dir}:{self.shared_dir}")
        if self.private_dir:
            mounts.append(f"{self.private_dir}:{self.private_dir}")
        return mounts

    @property
    def ssh_port(self) -> int:
        """SSH port for reaching container nodes.
        Returns the pipeline's container_ssh_port when the pipeline is
        containerized, otherwise the default SSH port (22)."""
        if hasattr(self, "pipeline") and self.pipeline:
            if self.pipeline._has_containerized_packages():
                return getattr(self.pipeline, "container_ssh_port", 22)
        return 22

    @property
    def _container_engine(self) -> str:
        """Return the pipeline's container engine when *this* package is
        running in container mode, so Exec/MpiExecInfo wraps commands in
        docker/podman/apptainer exec. Per-package: a pipeline can mix
        a containerized workload with host-side helpers (e.g. wrp_runtime,
        wrp_cte_libfuse running on the host while the workload runs in
        the SIF).
        """
        if hasattr(self, "pipeline") and self.pipeline:
            if self.config.get("deploy_mode") == "container":
                return getattr(self.pipeline, "container_engine", "none")
        return "none"

    @property
    def _build_engine(self) -> str:
        """Get the engine to use for building (apptainer needs docker/podman as intermediate)."""
        engine = self._container_engine
        if engine == "apptainer":
            import shutil

            if shutil.which("docker"):
                return "docker"
            elif shutil.which("podman"):
                return "podman"
            else:
                raise RuntimeError(
                    "Apptainer requires docker or podman for the build phase. "
                    "Neither was found in PATH."
                )
        return engine

    # ------------------------------------------------------------------
    # Container support — template helpers
    # ------------------------------------------------------------------

    def _read_template(self, filename, replacements=None):
        """
        Read a template file from self.pkg_dir and apply ##VAR##
        substitutions.  Used for both build.sh scripts and
        Dockerfile.deploy templates.

        :param filename: File name relative to self.pkg_dir
        :param replacements: dict of {VAR_NAME: value} replacements
        :return: File content with substitutions applied
        """
        import os

        path = os.path.join(self.pkg_dir, filename)
        with open(path, "r") as f:
            content = f.read()
        if replacements:
            for key, value in replacements.items():
                content = content.replace(f"##{key}##", str(value))
        return content

    # Keep old name as alias for backwards compatibility
    _read_dockerfile = _read_template
    _read_build_script = _read_template

    # ------------------------------------------------------------------
    # Container support — overrideable generators
    # ------------------------------------------------------------------

    def _build_phase(self):
        """
        Return the build script content and image suffix.

        Override in subclasses.  The returned script is executed inside
        a long-running build container (started from ``container_base``).
        It should install all build dependencies and compile the software.

        The image suffix identifies a specific build configuration so that
        different configurations (e.g., GPU vs CPU) produce distinct images.

        :return: (script_content, image_suffix) tuple, or None to skip.
                 Returning ('', suffix) also skips — normal for wrapper
                 packages that don't need their own build.
        """
        return None

    def _build_deploy_phase(self):
        """
        Return the Dockerfile content and image suffix for the DEPLOY container.

        Override in subclasses. Return None to skip.

        The deploy Dockerfile typically uses a multi-stage pattern:
        ``FROM ##BUILD_IMAGE## AS builder`` then ``FROM ##DEPLOY_BASE##``
        and copies compiled artifacts from the builder.

        :return: (dockerfile_content, image_suffix) tuple, or None to skip.
                 Returning ('', suffix) also skips the build.
        """
        return None

    # ------------------------------------------------------------------
    # Container support — image existence check
    # ------------------------------------------------------------------

    @staticmethod
    def _image_exists(engine, image_name, sif_dir=None, shared_dir=None):
        """
        Check whether a container image already exists locally.

        :param engine: 'docker', 'podman', or 'apptainer'
        :param image_name: Image name to check
        :param sif_dir: Directory holding apptainer .sif files. The
                        canonical location is ``$SHARED_DIR/containers``
                        (``Jarvis.get_containers_dir()``). Pass that in.
        :param shared_dir: Deprecated alias for ``sif_dir`` kept for
                           callers that still pass the per-pipeline
                           shared directory. Will be removed once
                           callers are migrated.
        :return: True if the image exists
        """
        if engine == "apptainer":
            from pathlib import Path

            sif_root = sif_dir or shared_dir
            if not sif_root:
                return False
            sif = Path(sif_root) / f"{image_name}.sif"
            return sif.exists()
        # docker / podman
        from jarvis_cd.shell import Exec, LocalExecInfo

        result = Exec(
            f"{engine} image inspect {image_name}", LocalExecInfo(hide_output=True)
        ).run()
        return result.exit_code.get("localhost", 1) == 0

    def show_build_script(self):
        """
        Print the build.sh content jarvis would feed into the build container
        for this package. Falls back to the raw build.sh on disk when
        _build_phase() returns None or empty (e.g., for inspection of
        packages that gate generation on deploy_mode).
        """
        # Most _build_phase implementations require deploy_mode='container'
        self.config.setdefault("deploy_mode", "container")

        try:
            result = self._build_phase()
        except Exception as e:
            result = None
            err = e
        else:
            err = None

        content = ""
        suffix = ""
        if result and isinstance(result, tuple):
            content, suffix = (result + ("",))[:2]

        if not content and self.pkg_dir:
            raw = Path(self.pkg_dir) / "build.sh"
            if raw.exists():
                print(f"=== build.sh for {self.__class__.__name__} (raw template) ===")
                print(f"Location: {raw}")
                if err:
                    print(f"Note: _build_phase() raised {type(err).__name__}: {err}")
                print()
                print(raw.read_text(encoding="utf-8"))
                return

        if content:
            label = f"build.sh for {self.__class__.__name__}"
            if suffix:
                label += f" (suffix: {suffix})"
            print(f"=== {label} ===")
            print()
            print(content)
            return

        print(f"No build.sh found for package {self.__class__.__name__}")
        if self.pkg_dir:
            print(f"Expected location: {Path(self.pkg_dir) / 'build.sh'}")

    def show_deploy_dockerfile(self):
        """
        Print the Dockerfile.deploy content jarvis would use to build the
        deploy image for this package. Falls back to the raw Dockerfile.deploy
        on disk when _build_deploy_phase() returns None or empty.
        """
        self.config.setdefault("deploy_mode", "container")

        try:
            result = self._build_deploy_phase()
        except Exception as e:
            result = None
            err = e
        else:
            err = None

        content = ""
        suffix = ""
        if result and isinstance(result, tuple):
            content, suffix = (result + ("",))[:2]

        if not content and self.pkg_dir:
            raw = Path(self.pkg_dir) / "Dockerfile.deploy"
            if raw.exists():
                print(
                    f"=== Dockerfile.deploy for {self.__class__.__name__} (raw template) ==="
                )
                print(f"Location: {raw}")
                if err:
                    print(
                        f"Note: _build_deploy_phase() raised {type(err).__name__}: {err}"
                    )
                print()
                print(raw.read_text(encoding="utf-8"))
                return

        if content:
            label = f"Dockerfile.deploy for {self.__class__.__name__}"
            if suffix:
                label += f" (suffix: {suffix})"
            print(f"=== {label} ===")
            print()
            print(content)
            return

        print(f"No Dockerfile.deploy found for package {self.__class__.__name__}")
        if self.pkg_dir:
            print(f"Expected location: {Path(self.pkg_dir) / 'Dockerfile.deploy'}")

    def show_readme(self):
        """
        Show README.md for this package.
        """
        if not self.pkg_dir:
            print("Package directory not set - cannot locate README")
            return

        readme_path = Path(self.pkg_dir) / "README.md"

        if readme_path.exists():
            print(f"=== README for {self.__class__.__name__} ===")
            print(f"Location: {readme_path}")
            print()
            try:
                with open(readme_path, "r", encoding="utf-8") as f:
                    content = f.read()
                print(content)
            except Exception as e:
                print(f"Error reading README: {e}")
        else:
            print(f"No README found for package {self.__class__.__name__}")
            print(f"Expected location: {readme_path}")

    def show_paths(self, path_flags: Dict[str, bool]):
        """
        Show directory paths based on flags.

        :param path_flags: Dictionary of path flags to show
        """
        try:
            # Ensure directories are set
            self._ensure_directories()

            paths_to_show = []

            # Check each flag and add corresponding paths
            if path_flags.get("conf"):
                if self.config_dir:
                    paths_to_show.append(f"{self.config_dir}/config.yaml")

            if path_flags.get("env"):
                if self.config_dir:
                    paths_to_show.append(f"{self.config_dir}/env.yaml")

            if path_flags.get("mod_env"):
                if self.config_dir:
                    paths_to_show.append(f"{self.config_dir}/mod_env.yaml")

            if path_flags.get("conf_dir"):
                if self.config_dir:
                    paths_to_show.append(self.config_dir)

            if path_flags.get("shared_dir"):
                if self.shared_dir:
                    paths_to_show.append(self.shared_dir)

            if path_flags.get("priv_dir"):
                if self.private_dir:
                    paths_to_show.append(self.private_dir)

            if path_flags.get("pkg_dir"):
                if self.pkg_dir:
                    paths_to_show.append(self.pkg_dir)

            # Print only the paths, one per line (for shell usage)
            for path in paths_to_show:
                if path:  # Only print non-None paths
                    print(path)

        except Exception as e:
            print(f"Error getting package paths: {e}", file=sys.stderr)


class Service(Pkg):
    """
    Base class for long-running services.
    Services typically need to be manually stopped.
    """

    def __init__(self, pipeline):
        super().__init__(pipeline=pipeline)

    def _init(self):
        """
        Initialize service-specific variables.
        Override in subclasses.
        """
        pass


class Application(Pkg):
    """
    Base class for applications that run and complete automatically.
    Applications typically don't need manual stopping.
    """

    def __init__(self, pipeline):
        super().__init__(pipeline=pipeline)

    def _init(self):
        """
        Initialize application-specific variables.
        Override in subclasses.
        """
        pass


class Library(Pkg):
    """
    Base class for library packages that only need to be built, not run.

    Libraries provide headers, shared objects, and binaries that other
    packages link against (e.g., HDF5, ADIOS2, compression libraries).
    They participate in the container build phase but have no runtime
    lifecycle — start(), stop(), and clean() are no-ops.

    In pipeline YAMLs, Library packages must appear BEFORE any packages
    that depend on them so the build container has the libraries installed
    when later packages compile.
    """

    def __init__(self, pipeline):
        super().__init__(pipeline=pipeline)

    def _init(self):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def clean(self):
        pass


class Interceptor(Pkg):
    """
    Base class for interceptors that modify environment variables.
    Interceptors route system and library calls to new functions.
    """

    def __init__(self, pipeline):
        super().__init__(pipeline=pipeline)

    def _init(self):
        """
        Initialize interceptor-specific variables.
        Override in subclasses.
        """
        pass

    def modify_env(self):
        """
        Override this method to modify the environment for interception.
        This is the main method interceptors should implement.
        """
        pass
