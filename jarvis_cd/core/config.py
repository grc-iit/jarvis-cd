import copy
import os
import tempfile
import yaml
from pathlib import Path
from typing import Dict, Any, Optional
from jarvis_cd.core.execution import validate_pipeline_id
from jarvis_cd.util.hostfile import Hostfile


_JARVIS_ROOT_ENVIRONMENT = "JARVIS_ROOT"
_STATE_ROOT_FIELDS = ("config_dir", "private_dir", "shared_dir")
MAX_BUILTIN_RESOURCE_GRAPH_PROFILES = 128


class BuiltinResourceGraphUnavailable(FileNotFoundError):
    """Raised only when an exact profile is absent from the builtin catalog."""


def _validate_builtin_resource_graph_profile(profile: str) -> str:
    """Return one safe exact builtin profile name or fail closed."""
    if (
        not profile
        or profile != profile.strip()
        or len(profile) > 256
        or profile in {".", ".."}
        or "/" in profile
        or "\\" in profile
        or any(ord(character) < 32 or ord(character) == 127 for character in profile)
    ):
        raise ValueError("builtin resource graph profile must be one safe exact name")
    return profile


def _fsync_directory(path: Path) -> None:
    """Durably publish a completed state-file replacement on POSIX."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonicalize_state_roots(config: Dict[str, Any]) -> Dict[str, Any]:
    """Map the OS logical-home prefix without resolving state descendants."""
    normalized = config.copy()
    logical_home = Path(os.path.abspath(Path.home()))
    canonical_home = logical_home.resolve()
    for field_name in _STATE_ROOT_FIELDS:
        value = normalized.get(field_name)
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            raise ValueError(f"JARVIS {field_name} must be a non-empty path string")
        absolute = Path(os.path.abspath(Path(value).expanduser()))
        try:
            relative = absolute.relative_to(logical_home)
        except ValueError:
            normalized[field_name] = str(absolute)
        else:
            normalized[field_name] = str(canonical_home / relative)
    return normalized


def load_class(import_str: str, path: str, class_name: str):
    """
    Loads a class from a python file.

    :param import_str: A python import string. E.g., for "myrepo.dir1.pkg"
    :param path: The absolute path to the directory which contains the
    beginning of the import statement.
    :param class_name: The name of the class in the file
    :return: The class data type
    """
    import sys

    fullpath = os.path.join(path, import_str.replace(".", "/") + ".py")

    # If the exact path doesn't exist, try replacing the last component
    if not os.path.exists(fullpath):
        # Handle legacy naming: if looking for "package.py", try "pkg.py"
        if import_str.endswith(".package"):
            legacy_import_str = import_str[:-8] + ".pkg"  # Replace .package with .pkg
            fullpath = os.path.join(path, legacy_import_str.replace(".", "/") + ".py")
            if os.path.exists(fullpath):
                import_str = legacy_import_str
            else:
                return None
        else:
            return None

    sys.path.insert(0, path)
    try:
        module = __import__(import_str, fromlist=[class_name])
        cls = getattr(module, class_name, None)
        if cls is None:
            raise AttributeError(
                f"Class '{class_name}' not found in module '{import_str}'"
            )
        return cls
    except ImportError as e:
        # Re-raise ImportError with more context instead of silently returning None
        raise ImportError(
            f"Failed to import module '{import_str}' from path '{path}': {e}"
        ) from e
    except AttributeError as e:
        # Re-raise AttributeError with more context instead of silently returning None
        raise AttributeError(
            f"Failed to get class '{class_name}' from module '{import_str}': {e}"
        ) from e
    finally:
        sys.path.pop(0)


class Jarvis:
    """
    Singleton class that manages Jarvis configuration and provides global access.
    Combines configuration management with singleton pattern.
    """

    _instance = None

    def __new__(cls, jarvis_root: Optional[str] = None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, jarvis_root: Optional[str] = None):
        """Initialize singleton (only happens once)"""
        if self._initialized:
            return

        # Set up jarvis root directory
        configured_root = jarvis_root
        if configured_root is None:
            configured_root = os.environ.get(_JARVIS_ROOT_ENVIRONMENT)
        if configured_root is None:
            self.jarvis_root = (Path.home() / ".ppi-jarvis").resolve()
        else:
            if not configured_root or any(
                ord(character) < 32 or ord(character) == 127
                for character in configured_root
            ):
                raise ValueError(
                    f"{_JARVIS_ROOT_ENVIRONMENT} must be a non-empty printable path"
                )
            self.jarvis_root = Path(configured_root).expanduser().resolve()

        self.config_file = self.jarvis_root / "jarvis_config.yaml"
        self.repos_file = self.jarvis_root / "repos.yaml"
        self.resource_graph_file = self.jarvis_root / "resource_graph.yaml"

        # Lazy-loaded properties
        self._config = None
        self._repos = None
        self._resource_graph = None
        self._hostfile = None

        # Directory paths
        self.config_dir: Optional[str] = None
        self.private_dir: Optional[str] = None
        self.shared_dir: Optional[str] = None

        # Try to automatically load configuration if it exists
        if self.is_initialized():
            config = self.config
            self.config_dir = config.get("config_dir", str(self.jarvis_root))
            self.private_dir = config.get(
                "private_dir", str(self.jarvis_root / "private")
            )
            self.shared_dir = config.get("shared_dir", str(self.jarvis_root / "shared"))

        self._initialized = True

    @classmethod
    def get_instance(cls, jarvis_root: Optional[str] = None):
        """Get the singleton instance"""
        if cls._instance is None:
            cls._instance = cls(jarvis_root)
        return cls._instance

    def initialize(
        self, config_dir: str, private_dir: str, shared_dir: str, force: bool = False
    ):
        """
        Initialize Jarvis configuration directories and files.

        :param config_dir: Directory for jarvis metadata
        :param private_dir: Machine-local data directory
        :param shared_dir: Shared data directory across all machines
        :param force: Force override of existing repos and resource_graph files
        """
        from jarvis_cd.util import logger

        # Create jarvis root directory
        self.jarvis_root.mkdir(parents=True, exist_ok=True)

        # Create required directories before canonicalizing them. This keeps
        # administrator-managed aliases such as /home -> /mnt/common outside
        # the private-state path boundary without relaxing its no-link checks.
        config_path = Path(config_dir).expanduser()
        private_path = Path(private_dir).expanduser()
        shared_path = Path(shared_dir).expanduser()
        config_path.mkdir(parents=True, exist_ok=True)
        private_path.mkdir(parents=True, exist_ok=True)
        shared_path.mkdir(parents=True, exist_ok=True)

        # Initialize default configuration
        default_config = {
            "config_dir": str(config_path.resolve()),
            "private_dir": str(private_path.resolve()),
            "shared_dir": str(shared_path.resolve()),
            "current_pipeline": None,
            "hostfile": None,
        }

        # Save configuration
        self.save_config(default_config)

        # Update instance attributes
        self.config_dir = default_config["config_dir"]
        self.private_dir = default_config["private_dir"]
        self.shared_dir = default_config["shared_dir"]

        # Handle repos.yaml
        repos_exists = self.repos_file.exists()
        if repos_exists and not force:
            logger.warning(
                "Existing repos.yaml detected - preserving (use +force to override)"
            )
        elif repos_exists and force:
            logger.warning("Existing repos.yaml detected - overriding due to +force")
            builtin_repo_path = self.get_builtin_repo_path()
            if builtin_repo_path.exists():
                builtin_repo_path_str = str(builtin_repo_path.absolute())
            else:
                builtin_repo_path_str = str((self.jarvis_root / "builtin").absolute())
            default_repos = {"repos": [builtin_repo_path_str]}
            self.save_repos(default_repos)
        else:
            # File doesn't exist, create it
            builtin_repo_path = self.get_builtin_repo_path()
            if builtin_repo_path.exists():
                builtin_repo_path_str = str(builtin_repo_path.absolute())
            else:
                builtin_repo_path_str = str((self.jarvis_root / "builtin").absolute())
            default_repos = {"repos": [builtin_repo_path_str]}
            self.save_repos(default_repos)

        # Handle resource_graph.yaml
        resource_graph_exists = self.resource_graph_file.exists()
        if resource_graph_exists and not force:
            logger.warning(
                "Existing resource_graph.yaml detected - preserving (use +force to override)"
            )
        elif resource_graph_exists and force:
            logger.warning(
                "Existing resource_graph.yaml detected - overriding due to +force"
            )
            default_resource_graph = {"storage": {}, "network": {}}
            self.save_resource_graph(default_resource_graph)
        else:
            # File doesn't exist, create it
            default_resource_graph = {"storage": {}, "network": {}}
            self.save_resource_graph(default_resource_graph)

        print(f"Jarvis initialized at {self.jarvis_root}")
        print(f"Config directory: {config_dir}")
        print(f"Private directory: {private_dir}")
        print(f"Shared directory: {shared_dir}")

    @property
    def config(self) -> Dict[str, Any]:
        """Get jarvis configuration, loading if necessary"""
        if self._config is None:
            self._config = self.load_config()
        return self._config

    @property
    def repos(self) -> Dict[str, Any]:
        """Get repos configuration, loading if necessary"""
        if self._repos is None:
            self._repos = self.load_repos()
        return self._repos

    @property
    def resource_graph(self) -> Dict[str, Any]:
        """Get resource graph, loading if necessary"""
        if self._resource_graph is None:
            self._resource_graph = self.load_resource_graph()
        return self._resource_graph

    @property
    def hostfile(self) -> Hostfile:
        """Get current hostfile"""
        if self._hostfile is None:
            hostfile_path = self.config.get("hostfile")
            if hostfile_path and os.path.exists(hostfile_path):
                self._hostfile = Hostfile(path=hostfile_path)
            else:
                # Default to localhost
                self._hostfile = Hostfile()
        return self._hostfile

    def load_config(self) -> Dict[str, Any]:
        """Load configuration and migrate legacy logical state roots in memory."""
        if not self.config_file.exists():
            raise FileNotFoundError("Jarvis not initialized. Run 'jarvis init' first.")

        with open(self.config_file, "r") as f:
            config = yaml.safe_load(f) or {}
        return _canonicalize_state_roots(config)

    def load_repos(self) -> Dict[str, Any]:
        """Load repositories and bind the legacy builtin slot to this install.

        Older installations copied the distribution's ``builtin`` repository
        into ``<JARVIS_ROOT>/builtin`` once and then kept using that mutable
        snapshot across package upgrades.  The exact legacy path is owned by
        JARVIS, so it is safe to rebind that one entry in memory to the builtin
        repository shipped beside the running ``jarvis_cd`` package.  Other
        repository entries, including operator-provided repositories named
        ``builtin``, remain untouched.
        """
        if not self.repos_file.exists():
            return {"repos": []}

        with open(self.repos_file, "r") as f:
            repositories = yaml.safe_load(f) or {"repos": []}
        if not isinstance(repositories, dict):
            return repositories
        return self._bind_distribution_builtin_repository(repositories)

    def load_resource_graph(self) -> Dict[str, Any]:
        """Load resource graph from file"""
        if not self.resource_graph_file.exists():
            return {"storage": {}, "network": {}}

        with open(self.resource_graph_file, "r") as f:
            return yaml.safe_load(f) or {"storage": {}, "network": {}}

    def save_config(self, config: Dict[str, Any]):
        """Save jarvis configuration with canonical state-root paths."""
        normalized = _canonicalize_state_roots(config)
        self.jarvis_root.mkdir(parents=True, exist_ok=True)
        with open(self.config_file, "w") as f:
            yaml.dump(normalized, f, default_flow_style=False)
        self._config = normalized

    def save_repos(self, repos: Dict[str, Any]):
        """Save repository config and bind its managed builtin view."""
        self.jarvis_root.mkdir(parents=True, exist_ok=True)
        with open(self.repos_file, "w") as f:
            yaml.dump(repos, f, default_flow_style=False)
        self._repos = self._bind_distribution_builtin_repository(repos)

    def save_resource_graph(self, resource_graph: Dict[str, Any]):
        """Atomically save the active graph under the configured JARVIS root."""
        self.jarvis_root.mkdir(parents=True, exist_ok=True)
        persisted_graph = copy.deepcopy(resource_graph)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.jarvis_root,
            prefix=".resource_graph.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                yaml.dump(persisted_graph, stream, default_flow_style=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.resource_graph_file)
            self._resource_graph = persisted_graph
            _fsync_directory(self.jarvis_root)
        finally:
            temporary_path.unlink(missing_ok=True)

    def add_repo(self, repo_path: str, force: bool = False):
        """Add a repository to the repos configuration"""
        repo_path = str(Path(repo_path).absolute())
        repos = self.repos.copy()

        # Check for existing repository with same name (not just same path)
        repo_name = Path(repo_path).name
        existing_repos_with_same_name = [
            existing_path
            for existing_path in repos["repos"]
            if Path(existing_path).name == repo_name
        ]

        if repo_path in repos["repos"]:
            if force:
                # Repository path already exists - remove and re-add to update order
                repos["repos"].remove(repo_path)
                repos["repos"].insert(0, repo_path)
                self.save_repos(repos)
                print(f"Repository already exists - updated position: {repo_path}")
            else:
                print(f"Repository already exists: {repo_path}")
                print("Use --force to override existing repository")
        elif existing_repos_with_same_name:
            if force:
                # Remove existing repositories with same name
                for existing_path in existing_repos_with_same_name:
                    repos["repos"].remove(existing_path)
                    print(f"Removed existing repository: {existing_path}")
                # Add new repository
                repos["repos"].insert(0, repo_path)
                self.save_repos(repos)
                print(f"Added repository (replacing existing): {repo_path}")
            else:
                print(f"Repository with name '{repo_name}' already exists:")
                for existing_path in existing_repos_with_same_name:
                    print(f"  {existing_path}")
                print("Use --force to replace existing repository")
        else:
            repos["repos"].insert(0, repo_path)  # Add to front for priority
            self.save_repos(repos)
            print(f"Added repository: {repo_path}")

    def remove_repo(self, repo_path: str):
        """Remove a repository from the repos configuration"""
        repo_path = str(Path(repo_path).absolute())
        repos = self.repos.copy()

        if repo_path in repos["repos"]:
            repos["repos"].remove(repo_path)
            self.save_repos(repos)
            print(f"Removed repository: {repo_path}")
        else:
            print(f"Repository not found: {repo_path}")

    def remove_repo_by_name(self, repo_name: str):
        """
        Remove all repositories with the given name from the repos configuration.

        :param repo_name: Name of repository to remove
        :return: Number of repositories removed
        """
        repos = self.repos.copy()
        removed_repos = []

        # Find all repositories with matching name
        remaining_repos = []
        for repo_path in repos["repos"]:
            if Path(repo_path).name == repo_name:
                removed_repos.append(repo_path)
            else:
                remaining_repos.append(repo_path)

        if removed_repos:
            repos["repos"] = remaining_repos
            self.save_repos(repos)

            print(f"Removed {len(removed_repos)} repository(ies) named '{repo_name}':")
            for repo_path in removed_repos:
                print(f"  - {repo_path}")
        else:
            print(f"No repositories found with name '{repo_name}'")

        return len(removed_repos)

    def cleanup_nonexistent_repos(self):
        """Remove any repositories from configuration that no longer exist on disk"""
        repos = self.repos.copy()
        removed_repos = []

        # Filter out non-existent repositories
        existing_repos = []
        for repo_path in repos["repos"]:
            if Path(repo_path).exists():
                existing_repos.append(repo_path)
            else:
                removed_repos.append(repo_path)

        # Update repos if any were removed
        if removed_repos:
            repos["repos"] = existing_repos
            self.save_repos(repos)

            print(
                f"Automatically removed {len(removed_repos)} non-existent repositories:"
            )
            for repo_path in removed_repos:
                print(f"  - {repo_path}")

        return len(removed_repos)

    def set_hostfile(self, hostfile_path: str):
        """Set the hostfile path in configuration"""
        hostfile_path = str(Path(hostfile_path).absolute())
        if not os.path.exists(hostfile_path):
            raise FileNotFoundError(f"Hostfile not found: {hostfile_path}")

        config = self.config.copy()
        config["hostfile"] = hostfile_path
        self.save_config(config)
        self._hostfile = None  # Reset cached hostfile
        print(f"Set hostfile: {hostfile_path}")

    def unset_hostfile(self):
        """Clear the hostfile, reverting to the default (localhost)"""
        config = self.config.copy()
        config["hostfile"] = None
        self.save_config(config)
        self._hostfile = None  # Reset cached hostfile
        print("Unset hostfile (reverted to default: localhost)")

    def get_pipeline_dir(self, pipeline_name: str) -> Path:
        """Get the config directory for a specific pipeline"""
        return (
            self._require_state_root(self.config_dir, "config_dir")
            / "pipelines"
            / validate_pipeline_id(pipeline_name)
        )

    def get_pipeline_shared_dir(self, pipeline_name: str) -> Path:
        """Get the shared directory for a specific pipeline"""
        return self._require_state_root(
            self.shared_dir, "shared_dir"
        ) / validate_pipeline_id(pipeline_name)

    def get_containers_dir(self) -> Path:
        """Centralized SIF cache shared by every pipeline.

        Apptainer SIFs are content-addressable by deploy image name, so
        storing them under ``<shared_dir>/containers`` lets multiple
        pipelines reuse the same image without rebuilding or
        re-pulling.
        """
        return self._require_state_root(self.shared_dir, "shared_dir") / "containers"

    def get_pipeline_private_dir(self, pipeline_name: str) -> Path:
        """Get the private directory for a specific pipeline"""
        return self._require_state_root(
            self.private_dir, "private_dir"
        ) / validate_pipeline_id(pipeline_name)

    @staticmethod
    def _require_state_root(value: Optional[str], field_name: str) -> Path:
        """Return one initialized state root or fail before using the CWD."""
        if value is None:
            raise RuntimeError(f"JARVIS {field_name} is not initialized")
        return Path(value)

    def get_current_pipeline_dir(self) -> Optional[Path]:
        """Get the config directory for the current pipeline"""
        current_pipeline = self.config.get("current_pipeline")
        if current_pipeline:
            return self.get_pipeline_dir(current_pipeline)
        return None

    def get_current_pipeline_shared_dir(self) -> Optional[Path]:
        """Get the shared directory for the current pipeline"""
        current_pipeline = self.config.get("current_pipeline")
        if current_pipeline:
            return self.get_pipeline_shared_dir(current_pipeline)
        return None

    def get_current_pipeline_private_dir(self) -> Optional[Path]:
        """Get the private directory for the current pipeline"""
        current_pipeline = self.config.get("current_pipeline")
        if current_pipeline:
            return self.get_pipeline_private_dir(current_pipeline)
        return None

    def set_current_pipeline(self, pipeline_name: str):
        """Set the current active pipeline"""
        config = self.config.copy()
        config["current_pipeline"] = validate_pipeline_id(pipeline_name)
        self.save_config(config)

    def get_current_pipeline(self) -> Optional[str]:
        """Get the name of the current active pipeline"""
        return self.config.get("current_pipeline")

    def set_current_module(self, module_name: Optional[str]):
        """Set the current active module"""
        config = self.config.copy()
        config["current_module"] = module_name
        self.save_config(config)

    def get_current_module(self) -> Optional[str]:
        """Get the name of the current active module"""
        return self.config.get("current_module")

    def get_pipelines_dir(self) -> Path:
        """Get the directory where all pipelines are stored"""
        config_dir = Path(self.config["config_dir"])
        return config_dir / "pipelines"

    def get_builtin_repo_path(self) -> Path:
        """Get the active builtin repository for this JARVIS installation."""
        # First check if builtin repo is registered in repos
        for repo_path_str in self.repos["repos"]:
            repo_path = Path(repo_path_str)
            if repo_path.name == "builtin" and repo_path.exists():
                return repo_path

        # Bind builtins to the running source tree or installed distribution.
        # This must precede the legacy copied tree so a wheel upgrade cannot
        # continue loading an older package contract from JARVIS_ROOT.
        distribution_builtin = self._distribution_builtin_repository()
        if distribution_builtin is not None:
            return distribution_builtin

        # Compatibility fallback for installations whose distribution did not
        # include builtins.  This path is never overwritten here.
        user_builtin = self.jarvis_root / "builtin"
        if user_builtin.exists():
            return user_builtin

        # Preserve compatibility with non-standard installations where the
        # builtin top-level package is not adjacent to jarvis_cd.
        try:
            import importlib.metadata
            import importlib.util
            import site

            # Method 1: Try to find builtin package directly
            try:
                import builtin

                builtin_module_path = Path(builtin.__file__).parent
                if builtin_module_path.exists():
                    return builtin_module_path
            except ImportError:
                pass

            # Method 2: Look for builtin in installed package files
            try:
                dist = importlib.metadata.distribution("jarvis_cd")
                if hasattr(dist, "files") and dist.files:
                    for file in dist.files:
                        if "builtin" in str(file) and str(file).endswith(
                            "builtin/__init__.py"
                        ):
                            # Get the actual installation path
                            for path in site.getsitepackages() + [
                                site.getusersitepackages()
                            ]:
                                if path:
                                    candidate = Path(path) / file.parent
                                    if candidate.exists():
                                        return candidate
            except Exception:
                pass

            # Method 3: Search in site-packages
            for path in site.getsitepackages() + [site.getusersitepackages()]:
                if path:
                    builtin_path = Path(path) / "builtin"
                    if (
                        builtin_path.exists()
                        and (builtin_path / "__init__.py").exists()
                    ):
                        return builtin_path

        except Exception:
            pass

        # Default fallback
        return user_builtin

    def list_builtin_resource_graphs(self) -> Dict[str, Path]:
        """Return the exact resource-graph profiles owned by the active builtin repo."""
        graph_root = self.get_builtin_repo_path() / "resource_graph"
        if not graph_root.is_dir():
            return {}
        profiles: Dict[str, Path] = {}
        for candidate in sorted(graph_root.iterdir()):
            if not candidate.is_file() or candidate.suffix not in {
                ".json",
                ".yaml",
                ".yml",
            }:
                continue
            profile = _validate_builtin_resource_graph_profile(candidate.stem)
            if profile in profiles:
                raise ValueError(f"duplicate builtin resource graph profile: {profile}")
            if len(profiles) >= MAX_BUILTIN_RESOURCE_GRAPH_PROFILES:
                raise ValueError(
                    "builtin resource graph catalog exceeds its profile limit"
                )
            profiles[profile] = candidate
        return profiles

    def get_builtin_resource_graph_path(self, profile: str) -> Path:
        """Resolve one exact builtin graph profile or report the owned catalog."""
        _validate_builtin_resource_graph_profile(profile)
        profiles = self.list_builtin_resource_graphs()
        selected = profiles.get(profile)
        if selected is None:
            available = ", ".join(profiles) if profiles else "none"
            raise BuiltinResourceGraphUnavailable(
                f"builtin resource graph profile {profile!r} is unavailable; "
                f"available profiles: {available}"
            )
        return selected

    def _bind_distribution_builtin_repository(
        self,
        repositories: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Return repository config with only managed legacy slots rebound.

        Ownership is path-based: JARVIS owns ``builtin`` directly beneath the
        active ``JARVIS_ROOT`` and beneath its historical default
        ``~/.ppi-jarvis`` root. A repository is never treated as managed merely
        because its basename is ``builtin``.
        """
        entries = repositories.get("repos")
        if not isinstance(entries, list):
            return repositories
        distribution_builtin = self._distribution_builtin_repository()
        if distribution_builtin is None:
            return repositories
        managed_builtin_slots = self._managed_builtin_repository_slots()
        rebound_path = str(distribution_builtin)
        changed = False
        rebound_entries: list[Any] = []
        for entry in entries:
            rebound: Any = entry
            if (
                isinstance(entry, str)
                and self._absolute_lexical_path(entry) in managed_builtin_slots
            ):
                rebound = rebound_path
                changed = rebound != entry
            if rebound not in rebound_entries:
                rebound_entries.append(rebound)
            elif rebound != entry:
                changed = True
        if not changed:
            return repositories
        return {**repositories, "repos": rebound_entries}

    def _managed_builtin_repository_slots(self) -> set[Path]:
        """Return exact legacy paths reserved for JARVIS-managed builtins."""
        historical_default = Path.home() / ".ppi-jarvis" / "builtin"
        return {
            self._absolute_lexical_path(self.jarvis_root / "builtin"),
            self._absolute_lexical_path(historical_default),
            self._absolute_lexical_path(historical_default.resolve(strict=False)),
        }

    @staticmethod
    def _absolute_lexical_path(path: str | os.PathLike[str]) -> Path:
        """Normalize a path without following operator-managed redirections."""
        return Path(os.path.abspath(Path(path).expanduser()))

    @staticmethod
    def _distribution_builtin_repository() -> Optional[Path]:
        """Locate the builtin repository shipped with the running package."""
        candidate = Path(__file__).resolve().parents[2] / "builtin"
        if (candidate / "__init__.py").is_file() and (
            candidate / "builtin" / "__init__.py"
        ).is_file():
            return candidate
        return None

    def find_package(self, pkg_name: str) -> Optional[str]:
        """
        Find a package in registered repositories.
        Returns the full import path if found.
        Searches repositories in order, respecting priority.
        """
        # Check all registered repos in order
        for repo_path in self.repos["repos"]:
            repo_name = Path(repo_path).name
            if self._check_package_exists(repo_path, repo_name, pkg_name):
                return f"{repo_name}.{pkg_name}"

        # Also check the builtin repo (may not be in repos list)
        builtin_path = self.get_builtin_repo_path()
        if builtin_path and self._check_package_exists(
            str(builtin_path), "builtin", pkg_name
        ):
            return f"builtin.{pkg_name}"

        return None

    def _check_package_exists(
        self, repo_path: str, repo_name: str, pkg_name: str
    ) -> bool:
        """Check if a package exists in a repository"""
        # Try both package.py and pkg.py (legacy naming)
        package_file = Path(repo_path) / repo_name / pkg_name / "package.py"
        if package_file.exists():
            return True

        # Check for legacy pkg.py naming
        legacy_package_file = Path(repo_path) / repo_name / pkg_name / "pkg.py"
        return legacy_package_file.exists()

    def is_initialized(self) -> bool:
        """Check if Jarvis has been initialized"""
        return self.config_file.exists()
