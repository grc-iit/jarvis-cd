import ast
import json
import keyword
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from jarvis_cd.core.config import BUILTIN_REPOSITORY_NAME, Jarvis

_URL_SCHEME = re.compile(r"^(https?|ssh|git|file|git\+ssh)://", re.IGNORECASE)
#: scp-like git address, e.g. ``git@github.com:iowarp/clio_jarvis.git``
_SCP_LIKE_URL = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s]+$")


def is_git_url(spec: str) -> bool:
    """Whether ``jarvis repo add`` should clone ``spec`` rather than register it."""
    if _URL_SCHEME.match(spec) or _SCP_LIKE_URL.match(spec):
        return True
    path = Path(spec).expanduser()
    # A local bare repository (``something.git`` holding HEAD and objects/)
    return (
        spec.rstrip("/\\").endswith(".git")
        and (path / "HEAD").is_file()
        and (path / "objects").is_dir()
    )


def url_basename(url: str) -> str:
    """A git URL's basename without ``.git`` (``clio-jarvis`` for ``.../clio-jarvis.git``)."""
    tail = re.split(r"[/:\\]", url.rstrip("/\\"))[-1]
    if tail.endswith(".git"):
        tail = tail[: -len(".git")]
    return tail


def normalize_repo_name(name: str) -> str:
    """The Python module name a repository is known by: ``-`` and ``.`` become ``_``."""
    return name.replace("-", "_").replace(".", "_")


def repo_name_from_url(url: str) -> str:
    """Repository name for a git URL: its normalized basename.

    ``https://github.com/iowarp/clio-jarvis`` clones as ``clio_jarvis``, which
    must also be the package directory inside the repository.
    """
    basename = url_basename(url)
    name = normalize_repo_name(basename)
    if not name.isidentifier() or keyword.iskeyword(name):
        raise ValueError(
            f"cannot derive a repository name from git URL {url}: its basename "
            f"'{basename}' normalizes to '{name}', which is not a valid Python "
            f"module name"
        )
    return name


def _name_candidates(repo_name: str) -> set[str]:
    """Names that ``repo update/rm`` match: as given, and normalized."""
    return {repo_name, normalize_repo_name(repo_name)}


def _validate_ref(ref: str) -> str:
    if not ref or ref.startswith("-") or any(c.isspace() for c in ref):
        raise ValueError(f"invalid git ref: {ref!r}")
    return ref


def _git(args: List[str], *, capture: bool = True) -> str:
    """Run one git command (argv list, no shell) and return its stdout."""
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required for git repositories but was not found on PATH")
    result = subprocess.run(
        [git, *args],
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip() if capture else ""
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {result.returncode})"
            + (f": {detail}" if detail else "")
        )
    return (result.stdout or "").strip() if capture else ""


def _git_ok(args: List[str]) -> bool:
    try:
        _git(args)
    except RuntimeError:
        return False
    return True


def _remove_tree(path: Path) -> None:
    """Delete a clone, including git's read-only object files on Windows."""
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    if not path.exists():
        return

    def _retry_writable(func, failed_path, _exc):
        os.chmod(failed_path, stat.S_IWRITE)
        func(failed_path)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_retry_writable)
    else:
        shutil.rmtree(path, onerror=_retry_writable)


REPOS_SCHEMA = "jarvis.repos.v1"


def static_menu(source: str) -> Optional[List[Dict[str, Any]]]:
    """The literal list a ``pkg.py``'s ``_configure_menu`` returns, read without importing it.

    ``type`` is its name (``int``, ``str``). An entry value built by a call is
    left out; a menu that is not one literal list is ``None``. Listing a
    repository so never runs its code.
    """
    try:
        tree = ast.parse(source.lstrip("\ufeff"))
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "_configure_menu":
            continue
        returns = [item for item in ast.walk(node) if isinstance(item, ast.Return)]
        if len(returns) != 1 or not isinstance(returns[0].value, ast.List):
            return None
        entries: List[Dict[str, Any]] = []
        for element in returns[0].value.elts:
            if not isinstance(element, ast.Dict):
                return None
            entry: Dict[str, Any] = {}
            for key_node, value_node in zip(element.keys, element.values):
                if key_node is None:
                    return None
                try:
                    key = ast.literal_eval(key_node)
                    entry[str(key)] = (
                        getattr(value_node, "id", None)
                        if key == "type"
                        else ast.literal_eval(value_node)
                    )
                except (ValueError, TypeError, SyntaxError):
                    continue
            if isinstance(entry.get("name"), str):
                entries.append(entry)
        return entries
    return None


def _git_source(repo_path: str) -> Optional[Dict[str, str]]:
    if not (Path(repo_path) / ".git").exists():
        return None
    try:
        commit = _git(["-C", repo_path, "rev-parse", "HEAD"])
    except RuntimeError:
        return None
    try:
        url = _git(["-C", repo_path, "remote", "get-url", "origin"])
    except RuntimeError:
        url = ""
    return {"commit": commit, "url": url}


def _contract(package_dir: Path) -> Dict[str, Any]:
    path = package_dir / "contract.yaml"
    if not path.is_file():
        return {"contract": None}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return {"contract": None, "contract_error": str(exc).splitlines()[0]}
    if not isinstance(loaded, dict):
        return {"contract": None, "contract_error": "contract.yaml is not a mapping"}
    return {"contract": json.loads(json.dumps(loaded, default=str))}


class RepositoryManager:
    """
    Manages Jarvis repositories - adding, removing, listing, and creating packages.
    """

    def __init__(self, jarvis_config: Jarvis):
        """
        Initialize repository manager.

        :param jarvis_config: Jarvis configuration singleton
        """
        self.jarvis_config = jarvis_config
        
    def add_repository(self, repo_path: str, force: bool = False, ref: Optional[str] = None):
        """
        Add a repository to Jarvis.
        
        :param repo_path: Path to repository directory, or a git URL to clone
        :param force: Force overwrite if repository already exists
        :param ref: Branch, tag or commit to check out (git URLs only)
        """
        if is_git_url(repo_path):
            self._add_git_repository(repo_path, force=force, ref=ref)
            return
        if ref is not None:
            raise ValueError("--ref applies only when adding a repository from a git URL")

        # Automatically clean up non-existent repositories first
        self.jarvis_config.cleanup_nonexistent_repos()
        
        path = Path(repo_path).absolute()
        
        if not path.exists():
            raise FileNotFoundError(f"Repository path does not exist: {path}")
            
        if not path.is_dir():
            raise ValueError(f"Repository path is not a directory: {path}")

        self._validate_repository_layout(path)
        self.jarvis_config.add_repo(str(path), force=force)

    def _validate_repository_layout(self, repo_path: Path) -> None:
        """Require the ``<name>/<name>/<pkg>/pkg.py`` repository layout."""
        # Check if it looks like a valid repository
        repo_name = repo_path.name
        expected_subdir = repo_path / repo_name

        if not expected_subdir.exists():
            raise ValueError(
                f"Invalid repository structure: {repo_path} does not contain subdirectory '{repo_name}'\n"
                f"Expected structure: {repo_name}/{repo_name}/package_name/pkg.py\n"
                f"Missing directory: {expected_subdir}\n\n"
                f"To fix this, ensure your repository follows the required structure:\n"
                f"  {repo_name}/\n"
                f"  â”œâ”€â”€ {repo_name}/           # Required subdirectory with same name\n"
                f"  â”‚   â”œâ”€â”€ package1/\n"
                f"  â”‚   â”‚   â””â”€â”€ pkg.py\n"
                f"  â”‚   â””â”€â”€ package2/\n"
                f"  â”‚       â””â”€â”€ pkg.py\n"
                f"  â””â”€â”€ pipelines/          # Optional pipeline index\n"
                f"      â””â”€â”€ example.yaml"
            )

        if not expected_subdir.is_dir():
            raise ValueError(f"Expected subdirectory exists but is not a directory: {expected_subdir}")

    def _add_git_repository(self, url: str, force: bool = False, ref: Optional[str] = None):
        """Clone ``url`` beneath the JARVIS git repos dir and register it."""
        if ref is not None:
            _validate_ref(ref)
        name = repo_name_from_url(url)
        repos_dir = self.jarvis_config.get_git_repos_dir()
        clone_dir = (repos_dir / name).absolute()

        self.jarvis_config.cleanup_nonexistent_repos()
        same_name = [
            repo_path
            for repo_path in self.jarvis_config.repos['repos']
            if Path(repo_path).name == name
            and Path(repo_path).absolute() != clone_dir
        ]
        if same_name and not force:
            raise FileExistsError(
                f"A repository named '{name}' is already registered: "
                f"{', '.join(same_name)}\n"
                f"Run 'jarvis repo rm {name}' first, or add with +force to replace it"
            )
        if clone_dir.exists() or clone_dir.is_symlink():
            if not force:
                raise FileExistsError(
                    f"Repository clone already exists: {clone_dir}\n"
                    f"Run 'jarvis repo update {name}' to refresh it, or "
                    f"'jarvis repo add {url} +force' to delete and re-clone it"
                )
            print(f"Removing existing clone (+force): {clone_dir}")
            _remove_tree(clone_dir)

        repos_dir.mkdir(parents=True, exist_ok=True)
        print(f"Cloning {url} into {clone_dir}")
        try:
            _git(["clone", "--", url, str(clone_dir)], capture=False)
            if ref is not None:
                _git(["-C", str(clone_dir), "checkout", ref, "--"])
            else:
                branch = _git(["-C", str(clone_dir), "rev-parse", "--abbrev-ref", "HEAD"])
                ref = branch if branch != "HEAD" else None
            try:
                self._validate_repository_layout(clone_dir)
            except ValueError as error:
                raise ValueError(
                    f"{url} is not a JARVIS repository named '{name}' (the URL "
                    f"basename '{url_basename(url)}' with '-' and '.' replaced by "
                    f"'_'): it must contain the package directory '{name}/'. "
                    f"Removed the clone {clone_dir}.\n\n{error}"
                ) from error
        except Exception:
            _remove_tree(clone_dir)
            raise

        commit = _git(["-C", str(clone_dir), "rev-parse", "HEAD"])
        if not self.jarvis_config.add_repo(str(clone_dir), force=force):
            _remove_tree(clone_dir)
            raise ValueError(
                f"Repository '{name}' was not added (see above); removed the clone "
                f"{clone_dir}. Use +force to replace the existing repository"
            )
        print(f"Checked out {ref or 'HEAD'} at {commit}")

    def update_repository(self, repo_name: str, ref: Optional[str] = None):
        """
        Refresh a repository added from a git URL.

        Fetches ``origin`` (with tags). A clone on a branch is reset to
        ``origin/<branch>``; a detached clone (a tag or commit) is pinned and
        stays where it is. ``ref`` moves the clone: a branch is checked out
        tracking ``origin/<branch>``, a tag or commit detached.

        :param repo_name: Name of the repository (not full path)
        :param ref: Branch, tag or commit to move to
        """
        if ref is not None:
            _validate_ref(ref)
        self.jarvis_config.cleanup_nonexistent_repos()
        names = _name_candidates(repo_name)
        matches = [
            repo_path
            for repo_path in self.jarvis_config.repos['repos']
            if Path(repo_path).name in names
        ]
        if not matches:
            raise ValueError(f"No repository named '{repo_name}' is registered")
        clones = [path for path in matches if self.jarvis_config.is_owned_git_clone(path)]
        if not clones:
            raise ValueError(
                f"Repository '{repo_name}' ({', '.join(matches)}) was not added from a "
                f"git URL; only repositories added with 'jarvis repo add <git url>' "
                f"can be updated"
            )
        for repo_path in clones:
            self._update_git_clone(repo_path, ref)

    def _update_git_clone(self, repo_path: str, ref: Optional[str]):
        clone = str(repo_path)
        url = _git(["-C", clone, "remote", "get-url", "origin"])
        print(f"Fetching {url} into {clone}")
        _git(["-C", clone, "fetch", "--tags", "origin"], capture=False)
        previous_commit = _git(["-C", clone, "rev-parse", "HEAD"])
        previous_branch = _git(["-C", clone, "rev-parse", "--abbrev-ref", "HEAD"])
        target = ref or (previous_branch if previous_branch != "HEAD" else None)
        if target is None:
            print(f"{Path(clone).name} is pinned at {previous_commit}; pass --ref to move it")
            return
        try:
            if _git_ok(["-C", clone, "show-ref", "--verify", "--quiet",
                        f"refs/remotes/origin/{target}"]):
                _git(["-C", clone, "checkout", "-B", target, f"origin/{target}", "--"])
            else:
                _git(["-C", clone, "checkout", "--detach", target, "--"])
            self._validate_repository_layout(Path(clone))
        except Exception:
            if previous_branch != "HEAD":
                _git_ok(["-C", clone, "checkout", "-B", previous_branch, previous_commit, "--"])
            else:
                _git_ok(["-C", clone, "checkout", "--detach", previous_commit, "--"])
            print(f"Update failed; {clone} stays at {previous_commit}")
            raise

        commit = _git(["-C", clone, "rev-parse", "HEAD"])
        print(f"Updated {Path(clone).name} ({clone}) to {target} at {commit}")
        
    def remove_repository(self, repo_path: str):
        """
        Remove a repository from Jarvis.
        
        :param repo_path: Path to repository directory
        """
        repo_path = Path(repo_path).absolute()
        self.jarvis_config.remove_repo(str(repo_path))
        
        # Also clean up any other non-existent repositories while we're at it
        self.jarvis_config.cleanup_nonexistent_repos()
        
    def remove_repository_by_name(self, repo_name: str):
        """
        Remove all repositories with the given name from Jarvis.

        Removing ``builtin`` also records ``no_builtin: true`` for this root
        so the distribution's builtin repository stays out. A repository
        cloned by ``jarvis repo add <git url>`` loses its clone directory.
        
        :param repo_name: Name of repository to remove (not full path)
        :return: Number of repositories removed
        """
        # Automatically clean up non-existent repositories first
        self.jarvis_config.cleanup_nonexistent_repos()

        names = _name_candidates(repo_name)
        matching = [
            repo_path
            for repo_path in self.jarvis_config.repos['repos']
            if Path(repo_path).name in names
        ]
        if repo_name == BUILTIN_REPOSITORY_NAME:
            removed = self._disable_builtin()
        else:
            # ``clio-jarvis`` also removes the clone registered as ``clio_jarvis``
            matched_names = sorted({Path(repo_path).name for repo_path in matching})
            removed = sum(
                self.jarvis_config.remove_repo_by_name(name)
                for name in (matched_names or [repo_name])
            )

        for repo_path in matching:
            if self.jarvis_config.is_owned_git_clone(repo_path):
                _remove_tree(Path(repo_path))
                print(f"Deleted clone: {repo_path}")
        
        # Clean up any other non-existent repositories while we're at it
        self.jarvis_config.cleanup_nonexistent_repos()
        
        return removed

    def _disable_builtin(self) -> int:
        """Back ``jarvis repo rm builtin``: drop and leave builtin out."""
        removed = self.jarvis_config.disable_builtin()
        if removed:
            print(f"Removed {len(removed)} repository(ies) named '{BUILTIN_REPOSITORY_NAME}':")
            for repo_path in removed:
                print(f"  - {repo_path}")
        print(
            f"Builtin package repository left out of this root "
            f"(no_builtin: true in {self.jarvis_config.config_file})"
        )
        return len(removed)
        
    def list_repositories(self):
        """List all registered repositories"""
        # Automatically clean up non-existent repositories
        removed_count = self.jarvis_config.cleanup_nonexistent_repos()
        if removed_count > 0:
            print()  # Add spacing after cleanup messages
        
        repos = self.jarvis_config.repos['repos']
        builtin_path = self.jarvis_config.get_builtin_repo_path()
        builtin_path_str = str(builtin_path) if builtin_path is not None else None
        
        print("Registered repositories:")
        if not repos:
            print("  No repositories registered")
        else:
            repo_count = 0
            for repo_path in repos:
                repo_name = Path(repo_path).name
                exists = "âœ“" if Path(repo_path).exists() else "âœ—"
                
                # Check if this is the builtin repository
                if repo_path == builtin_path_str:
                    print(f"  {repo_count+1}. {repo_name} ({repo_path}) {exists} [builtin]")
                else:
                    print(f"  {repo_count+1}. {repo_name} ({repo_path}) {exists}")
                if self.jarvis_config.is_owned_git_clone(repo_path):
                    try:
                        url = _git(["-C", repo_path, "remote", "get-url", "origin"])
                        branch = _git(["-C", repo_path, "rev-parse", "--abbrev-ref", "HEAD"])
                        commit = _git(["-C", repo_path, "rev-parse", "--short", "HEAD"])
                    except RuntimeError:
                        url, branch, commit = "unknown", "unknown", "unknown"
                    ref = "pinned" if branch == "HEAD" else branch
                    print(f"     git: {url} ref: {ref} commit: {commit}")
                repo_count += 1
                
        # Only show separate builtin entry if it's not in the registered repos
        if builtin_path is None:
            print("  Built-in: builtin left out (JARVIS_NO_BUILTIN / no_builtin)")
        elif builtin_path_str not in repos:
            builtin_exists = "âœ“" if builtin_path.exists() else "âœ—"
            print(f"  Built-in: builtin ({builtin_path}) {builtin_exists}")

    def describe_repositories(self) -> Dict[str, Any]:
        """The ``jarvis.repos.v1`` document ``jarvis repo list +json`` prints.

        Every registered repository with its path, git source and packages (a
        ``<repo>/<repo>/<package>/pkg.py``), each with its menu as
        :func:`static_menu` reads it and its ``contract.yaml``, if any, as data.
        No package is imported.
        """
        repos = []
        for repo_path in self.jarvis_config.repos["repos"]:
            path = Path(repo_path)
            name = path.name
            packages = []
            inner = path / name
            if inner.is_dir():
                for pkg_py in sorted(inner.glob("*/pkg.py")):
                    try:
                        menu = static_menu(pkg_py.read_text(encoding="utf-8"))
                    except (OSError, UnicodeDecodeError):
                        menu = None
                    packages.append(
                        {
                            "name": pkg_py.parent.name,
                            "dir": str(pkg_py.parent),
                            "menu": menu,
                            **_contract(pkg_py.parent),
                        }
                    )
            repos.append(
                {
                    "name": name,
                    "path": str(path),
                    "exists": path.is_dir(),
                    "git": _git_source(str(path)) if path.is_dir() else None,
                    "packages": packages,
                }
            )
        builtin = self.jarvis_config.get_builtin_repo_path()
        return {
            "schema_version": REPOS_SCHEMA,
            "builtin": str(builtin) if builtin is not None else None,
            "repos": repos,
        }
        
    def create_package(self, package_name: str, package_type: str):
        """
        Create a new package in the first available repository.
        
        :param package_name: Name of package to create
        :param package_type: Type of package (service, app, interceptor)
        """
        if package_type not in ['service', 'app', 'interceptor']:
            raise ValueError(f"Invalid package type: {package_type}. Must be service, app, or interceptor")
            
        repos = self.jarvis_config.repos['repos']
        if not repos:
            raise ValueError("No repositories registered. Add a repository first with 'jarvis repo add'")
            
        # Use the first repository
        repo_path = Path(repos[0])
        repo_name = repo_path.name
        
        if not repo_path.exists():
            raise FileNotFoundError(f"Repository path does not exist: {repo_path}")
            
        # Create package directory structure
        package_dir = repo_path / repo_name / package_name
        package_dir.mkdir(parents=True, exist_ok=True)
        
        # Create package.py file
        package_file = package_dir / 'package.py'
        
        # Generate package template based on type
        template_content = self._generate_package_template(package_name, package_type)
        
        with open(package_file, 'w') as f:
            f.write(template_content)
            
        print(f"Created {package_type} package: {package_name}")
        print(f"Location: {package_file}")
        
    def _generate_package_template(self, package_name: str, package_type: str) -> str:
        """
        Generate package template code based on package type.
        
        :param package_name: Name of the package
        :param package_type: Type of package (service, app, interceptor)
        :return: Template code as string
        """
        class_name = package_name.capitalize()
        
        if package_type == 'service':
            return self._generate_service_template(class_name, package_name)
        elif package_type == 'app':
            return self._generate_app_template(class_name, package_name)
        elif package_type == 'interceptor':
            return self._generate_interceptor_template(class_name, package_name)
        else:
            raise ValueError(f"Unknown package type: {package_type}")
            
    def _generate_service_template(self, class_name: str, package_name: str) -> str:
        """Generate service package template"""
        return f'''"""
{class_name} service package for Jarvis-CD.
This is a long-running service that needs to be manually stopped.
"""
from jarvis_cd.core.pkg import Service


class {class_name}(Service):
    """
    {class_name} service implementation.
    """
    
    def _init(self):
        """
        Initialize service-specific variables.
        Don't assume that self.config is initialized.
        """
        self.port = None
        self.daemon_process = None
        
    def _configure_menu(self):
        """
        Create a CLI menu for the configurator method.
        
        :return: List of argument dictionaries
        """
        return [
            {{
                'name': 'port',
                'msg': 'The port to listen on',
                'type': int,
                'default': 8080
            }},
            {{
                'name': 'config_file',
                'msg': 'Path to configuration file',
                'type': str,
                'default': None
            }}
        ]
        
    def configure(self, **kwargs):
        """
        Configure the service with given parameters.
        
        :param kwargs: Configuration parameters
        """
        self.update_config(kwargs, rebuild=False)
        
        # Generate service-specific configuration files
        config_data = {{
            'port': self.config['port'],
            'service_name': '{package_name}'
        }}
        
        # Save configuration to shared directory
        import yaml
        config_file = f'{{self.shared_dir}}/{package_name}_config.yaml'
        with open(config_file, 'w') as f:
            yaml.dump(config_data, f, default_flow_style=False)
            
        print(f"Generated {package_name} configuration: {{config_file}}")
        
    def start(self):
        """
        Start the {package_name} service.
        """
        print(f"Starting {package_name} service on port {{self.config['port']}}")
        
        # Example service startup - replace with actual service commands
        # self.daemon_process = subprocess.Popen([
        #     '{package_name}_daemon',
        #     '--port', str(self.config['port']),
        #     '--config', f'{{self.shared_dir}}/{package_name}_config.yaml'
        # ])
        
        # Sleep to ensure service starts up
        import time
        time.sleep(self.config.get('sleep', 2))
        print(f"{package_name} service started")
        
    def stop(self):
        """
        Stop the {package_name} service.
        """
        print(f"Stopping {package_name} service")
        
        # Example service shutdown - replace with actual service commands
        # if self.daemon_process:
        #     self.daemon_process.terminate()
        #     self.daemon_process.wait()
        #     self.daemon_process = None
        
        print(f"{package_name} service stopped")
        
    def kill(self):
        """
        Force kill the {package_name} service.
        """
        print(f"Force killing {package_name} service")
        
        # Example force kill - replace with actual kill commands
        # if self.daemon_process:
        #     self.daemon_process.kill()
        #     self.daemon_process = None
        
        print(f"{package_name} service killed")
        
    def clean(self):
        """
        Clean up all {package_name} data and configuration.
        """
        print(f"Cleaning {package_name} service data")
        
        # Remove configuration files
        import os
        config_file = f'{{self.shared_dir}}/{package_name}_config.yaml'
        if os.path.exists(config_file):
            os.remove(config_file)
            
        # Remove any data directories or files
        # os.system(f'rm -rf {{self.private_dir}}/{package_name}_data')
        
        print(f"{package_name} service cleaned")
        
    def status(self):
        """
        Check the status of the {package_name} service.
        
        :return: Service status string
        """
        # Example status check - replace with actual status logic
        # if self.daemon_process and self.daemon_process.poll() is None:
        #     return "running"
        # else:
        #     return "stopped"
        
        return "unknown"
'''
        
    def _generate_app_template(self, class_name: str, package_name: str) -> str:
        """Generate application package template"""
        return f'''"""
{class_name} application package for Jarvis-CD.
This is an application that runs and completes automatically.
"""
from jarvis_cd.core.pkg import Application


class {class_name}(Application):
    """
    {class_name} application implementation.
    """
    
    def _init(self):
        """
        Initialize application-specific variables.
        Don't assume that self.config is initialized.
        """
        self.input_file = None
        self.output_file = None
        self.nprocs = None
        
    def _configure_menu(self):
        """
        Create a CLI menu for the configurator method.
        
        :return: List of argument dictionaries
        """
        return [
            {{
                'name': 'input_file',
                'msg': 'Path to input file',
                'type': str,
                'default': '/tmp/{package_name}_input.dat'
            }},
            {{
                'name': 'output_file',
                'msg': 'Path to output file',
                'type': str,
                'default': '/tmp/{package_name}_output.dat'
            }},
            {{
                'name': 'nprocs',
                'msg': 'Number of processes',
                'type': int,
                'default': 1
            }},
            {{
                'name': 'ppn',
                'msg': 'Processes per node',
                'type': int,
                'default': 1
            }}
        ]
        
    def configure(self, **kwargs):
        """
        Configure the application with given parameters.
        
        :param kwargs: Configuration parameters
        """
        self.update_config(kwargs, rebuild=False)
        
        # Validate configuration
        if self.config['nprocs'] <= 0:
            raise ValueError("Number of processes must be positive")
            
        print(f"Configured {package_name} application:")
        print(f"  Input: {{self.config['input_file']}}")
        print(f"  Output: {{self.config['output_file']}}")
        print(f"  Processes: {{self.config['nprocs']}}")
        
    def start(self):
        """
        Run the {package_name} application.
        """
        print(f"Running {package_name} application")
        
        # Prepare input data if needed
        self._prepare_input()
        
        # Example application execution - replace with actual commands
        # cmd = [
        #     '{package_name}',
        #     '--input', self.config['input_file'],
        #     '--output', self.config['output_file']
        # ]
        
        # Example MPI execution:
        # from jarvis_util import MpiExecInfo, Exec
        # Exec(' '.join(cmd),
        #      MpiExecInfo(env=self.mod_env,
        #                  hostfile=self.jarvis.hostfile,
        #                  nprocs=self.config['nprocs'],
        #                  ppn=self.config['ppn']))
        
        print(f"{package_name} application completed")
        
    def stop(self):
        """
        Stop the application (usually not needed for apps).
        """
        print(f"Stopping {package_name} application")
        
    def clean(self):
        """
        Clean up application data and temporary files.
        """
        print(f"Cleaning {package_name} application data")
        
        # Remove output files
        import os
        if os.path.exists(self.config['output_file']):
            os.remove(self.config['output_file'])
            
        # Remove any temporary files
        # os.system(f'rm -f {{self.config["output_file"]}}*')
        
        print(f"{package_name} application cleaned")
        
    def _prepare_input(self):
        """
        Prepare input data for the application.
        """
        import os
        
        input_file = self.config['input_file']
        
        # Create input directory if needed
        os.makedirs(os.path.dirname(input_file), exist_ok=True)
        
        # Generate or copy input data
        if not os.path.exists(input_file):
            print(f"Generating input file: {{input_file}}")
            with open(input_file, 'w') as f:
                f.write(f"# {package_name} input data\\n")
                f.write(f"# Generated by Jarvis-CD\\n")
'''
        
    def _generate_interceptor_template(self, class_name: str, package_name: str) -> str:
        """Generate interceptor package template"""
        return f'''"""
{class_name} interceptor package for Jarvis-CD.
This modifies environment variables to intercept system/library calls.
"""
from jarvis_cd.core.pkg import Interceptor


class {class_name}(Interceptor):
    """
    {class_name} interceptor implementation.
    """
    
    def _init(self):
        """
        Initialize interceptor-specific variables.
        Don't assume that self.config is initialized.
        """
        self.library_path = None
        
    def _configure_menu(self):
        """
        Create a CLI menu for the configurator method.
        
        :return: List of argument dictionaries
        """
        return [
            {{
                'name': 'library_name',
                'msg': 'Name of the library to intercept',
                'type': str,
                'default': '{package_name}_intercept'
            }},
            {{
                'name': 'enable_logging',
                'msg': 'Enable interception logging',
                'type': bool,
                'default': False
            }}
        ]
        
    def configure(self, **kwargs):
        """
        Configure the interceptor with given parameters.
        
        :param kwargs: Configuration parameters
        """
        self.update_config(kwargs, rebuild=False)
        
        # Find the interception library
        library_name = self.config['library_name']
        self.config['library_path'] = self.find_library(library_name)
        
        if self.config['library_path'] is None:
            raise Exception(f'Could not find {{library_name}} library')
            
        print(f'Found {{library_name}} library at {{self.config["library_path"]}}')
        
        # Set up logging if enabled
        if self.config['enable_logging']:
            self.config['log_file'] = f'{{self.private_dir}}/{package_name}_intercept.log'
            print(f'Interception logging enabled: {{self.config["log_file"]}}')
        
    def modify_env(self):
        """
        Modify the environment to enable interception.
        """
        # Add library to LD_PRELOAD
        self.prepend_env('LD_PRELOAD', self.config['library_path'])
        
        # Set up logging environment if enabled
        if self.config['enable_logging']:
            self.setenv('{package_name.upper()}_LOG_FILE', self.config['log_file'])
            self.setenv('{package_name.upper()}_LOG_LEVEL', 'INFO')
            
        print(f"Environment modified for {package_name} interception")
        print(f"LD_PRELOAD: {{self.mod_env.get('LD_PRELOAD', '')}}")
'''

    def list_packages_in_repo(self, repo_path: str) -> List[str]:
        """
        List all packages in a repository.
        
        :param repo_path: Path to repository
        :return: List of package names
        """
        repo_path = Path(repo_path)
        repo_name = repo_path.name
        packages_dir = repo_path / repo_name
        
        if not packages_dir.exists():
            return []
            
        packages = []
        for item in packages_dir.iterdir():
            if item.is_dir() and (item / 'package.py').exists():
                packages.append(item.name)
                
        return sorted(packages)
        
    def find_all_packages(self) -> Dict[str, List[str]]:
        """
        Find all packages in all registered repositories.
        
        :return: Dictionary mapping repo names to package lists
        """
        all_packages = {}
        
        # Check builtin repository
        builtin_path = self.jarvis_config.get_builtin_repo_path()
        if builtin_path is not None and builtin_path.exists():
            packages = self.list_packages_in_repo(str(builtin_path))
            if packages:
                all_packages['builtin'] = packages
                
        # Check registered repositories
        for repo_path in self.jarvis_config.repos['repos']:
            repo_name = Path(repo_path).name
            if Path(repo_path).exists():
                packages = self.list_packages_in_repo(repo_path)
                if packages:
                    all_packages[repo_name] = packages
                    
        return all_packages